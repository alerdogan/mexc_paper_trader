import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


class LocalRequest:
    client = SimpleNamespace(host="127.0.0.1")


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def prepared_candidate(module):
    now = module.datetime.now()
    connection = module.db()
    cursor = connection.execute(
        """INSERT INTO live_fee_tests(
        status,armed_at,candidate_at,expires_at,symbol,side,score,reference_price,
        contract_size,contracts,leverage,estimated_taker_rate,estimated_entry_fee,
        estimated_exit_fee,max_estimated_loss)
        VALUES('PREPARED',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            now.isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
            (now + module.timedelta(minutes=5)).isoformat(timespec="seconds"),
            "BTC_USDT",
            "LONG",
            100,
            100.0,
            0.001,
            1,
            1,
            0.0008,
            0.00008,
            0.00008,
            0.00066,
        ),
    )
    connection.commit()
    test_id = cursor.lastrowid
    connection.close()
    module.state["market"] = {
        "BTC_USDT": {"price": 100.0, "signal": "LONG", "long_score": 100, "short_score": 0}
    }
    return test_id


def test_arm_is_local_and_does_not_enable_orders(isolated_app):
    module, _ = isolated_app

    result = module.arm_live_fee_test(LocalRequest())

    assert result["status"] == "ARMED"
    assert result["orders_enabled"] is False
    assert module._live_fee_row(("ARMED",))["id"] == result["id"]


def test_cancel_only_disarms_non_executing_test(isolated_app):
    module, _ = isolated_app
    module.arm_live_fee_test(LocalRequest())

    result = module.cancel_live_fee_test(LocalRequest())

    assert result == {"status": "CANCELLED", "orders_enabled": False}
    assert module._live_fee_row(("ARMED", "PREPARED")) is None

    connection = module.db()
    connection.execute(
        "INSERT INTO live_fee_tests(status,armed_at) VALUES('EXECUTING','2026-01-01T00:00:00')"
    )
    connection.commit()
    connection.close()
    with pytest.raises(HTTPException) as exc:
        module.cancel_live_fee_test(LocalRequest())
    assert exc.value.status_code == 409


def test_execute_requires_exact_explicit_confirmation(isolated_app, monkeypatch):
    module, _ = isolated_app
    prepared_candidate(module)
    called = False

    async def private(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(module, "mexc_private_request", private)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            module.execute_live_fee_test(
                module.LiveFeeExecute(confirmation="NO"), LocalRequest()
            )
        )

    assert exc.value.status_code == 403
    assert called is False


def test_execute_uses_isolated_minimum_and_reduce_only_then_disarms(
    isolated_app, monkeypatch
):
    module, _ = isolated_app
    test_id = prepared_candidate(module)
    calls = []

    async def private(client, method, path, params=None, body=None):
        calls.append((method, path, body))
        if path.endswith("position_mode"):
            return 2
        if path.endswith("open_positions"):
            return []
        if path.endswith("funding_records"):
            return {"resultList": []}
        if method == "POST":
            return "entry-order" if sum(x[0] == "POST" for x in calls) == 1 else "exit-order"
        raise AssertionError(path)

    async def order_details(client, order_id):
        return {"state": 3, "dealVol": 1, "positionId": "position-1"}

    async def order_fills(client, order_id, contract_size):
        price = 100.1 if order_id == "entry-order" else 100.0
        return {
            "executed_contracts": 1,
            "executed_qty": contract_size,
            "average_fill_price": price,
            "notional": price * contract_size,
            "actual_fee": price * contract_size * 0.0008,
            "fee_currency": "USDT",
            "trade_ids": [order_id + "-trade"],
            "timestamps": [1],
            "is_taker": True,
        }

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(module, "mexc_private_request", private)
    monkeypatch.setattr(module, "_order_details", order_details)
    monkeypatch.setattr(module, "_order_fills", order_fills)

    result = asyncio.run(
        module.execute_live_fee_test(
            module.LiveFeeExecute(
                confirmation=f"ONAY LIVE_FEE_TEST {test_id} BTC_USDT LONG"
            ),
            LocalRequest(),
        )
    )

    posts = [body for method, _, body in calls if method == "POST"]
    assert posts[0]["openType"] == 1
    assert posts[0]["type"] == 5
    assert posts[0]["leverage"] == 1
    assert posts[1]["reduceOnly"] is True
    assert posts[1]["positionId"] == "position-1"
    assert posts[1]["vol"] == posts[0]["vol"] == 1
    assert result["status"] == "COMPLETED"
    assert module._live_fee_row(("ARMED", "PREPARED", "EXECUTING", "OPEN_ALARM")) is None
    connection = module.db()
    assert connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    connection.close()


def test_entry_submission_uncertainty_sets_open_position_alarm(isolated_app, monkeypatch):
    module, _ = isolated_app
    test_id = prepared_candidate(module)

    async def private(client, method, path, params=None, body=None):
        if path.endswith("position_mode"):
            return 2
        if path.endswith("open_positions"):
            return []
        if method == "POST":
            raise RuntimeError("timeout")
        raise AssertionError(path)

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(module, "mexc_private_request", private)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            module.execute_live_fee_test(
                module.LiveFeeExecute(
                    confirmation=f"ONAY LIVE_FEE_TEST {test_id} BTC_USDT LONG"
                ),
                LocalRequest(),
            )
        )

    assert "AÇIK LIVE POZİSYON ALARMI" in exc.value.detail
    assert module._live_fee_row(("OPEN_ALARM",))["id"] == test_id
