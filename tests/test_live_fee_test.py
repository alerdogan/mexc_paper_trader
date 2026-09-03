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


class FakePublicResponse:
    status_code = 200

    def json(self):
        return {
            "success": True,
            "data": [
                {"symbol": "AAA_USDT", "apiAllowed": False, "state": 0},
                {
                    "symbol": "ZZZ_USDT",
                    "apiAllowed": True,
                    "state": 0,
                    "settleCoin": "USDT",
                    "contractSize": 0.01,
                    "minVol": 1,
                    "volUnit": 1,
                    "minLeverage": 1,
                    "takerFeeRate": 0.0004,
                },
            ],
        }


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


def test_live_fee_audit_is_local_and_read_only(isolated_app, monkeypatch):
    module, _ = isolated_app
    prepared_candidate(module)
    calls = []

    async def private(client, method, path, params=None, body=None):
        calls.append((method, path, body))
        if path.endswith("open_positions"):
            return [{"positionId": "other", "symbol": "USELESS_USDT", "positionType": 2, "holdVol": 1}]
        if "open_orders/BTC_USDT" in path:
            return []
        raise AssertionError(path)

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(module, "mexc_private_request", private)

    result = asyncio.run(module.live_fee_test_audit(LocalRequest()))

    assert result["symbol"] == "BTC_USDT"
    assert result["open_position_count"] == 0
    assert result["open_order_count"] == 0
    assert all(method == "GET" and body is None for method, _, body in calls)


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


def test_prepare_uses_first_liquid_eligible_symbol_without_score_gate(isolated_app, monkeypatch):
    module, _ = isolated_app
    module.arm_live_fee_test(LocalRequest())

    async def public(*args, **kwargs):
        assert "params" not in kwargs
        return FakePublicResponse()

    async def private(*args, **kwargs):
        return []

    monkeypatch.setattr(module, "mexc_get", public)
    monkeypatch.setattr(module, "mexc_private_request", private)
    markets = {
        "AAA_USDT": {"15m": {"price": 5, "trend": 1}, "long_score": 99},
        "ZZZ_USDT": {"15m": {"price": 10, "trend": -1}, "short_score": 42},
    }

    asyncio.run(module.maybe_prepare_live_fee_test(None, markets, {}, ["AAA_USDT", "ZZZ_USDT"]))

    candidate = module._live_fee_row(("PREPARED",))
    assert candidate["symbol"] == "ZZZ_USDT"
    assert candidate["side"] == "SHORT"
    assert candidate["score"] == 42
    assert candidate["contracts"] == 1
    assert candidate["reference_price"] == 10


def test_prepare_skips_btc_with_real_position_and_reports_long_short_check(
    isolated_app, monkeypatch
):
    module, _ = isolated_app
    module.arm_live_fee_test(LocalRequest())

    class ContractsResponse:
        status_code = 200

        def json(self):
            return {
                "success": True,
                "data": [
                    {
                        "symbol": symbol,
                        "apiAllowed": True,
                        "state": 0,
                        "settleCoin": "USDT",
                        "contractSize": 0.001,
                        "minVol": 1,
                        "volUnit": 1,
                        "minLeverage": 1,
                        "takerFeeRate": 0.0004,
                    }
                    for symbol in ("BTC_USDT", "ZZZ_USDT")
                ],
            }

    async def public(*args, **kwargs):
        return ContractsResponse()

    async def private(*args, **kwargs):
        return [
            {"positionId": "btc-long", "symbol": "BTC_USDT", "positionType": 1, "holdVol": 2},
            {"positionId": "eth-short", "symbol": "ETH_USDT", "positionType": 2, "holdVol": 3},
        ]

    monkeypatch.setattr(module, "mexc_get", public)
    monkeypatch.setattr(module, "mexc_private_request", private)
    markets = {
        "BTC_USDT": {"15m": {"price": 100, "trend": 1}},
        "ZZZ_USDT": {"15m": {"price": 10, "trend": -1}},
    }

    asyncio.run(module.maybe_prepare_live_fee_test(None, markets, {}, ["ZZZ_USDT"]))

    candidate = module._live_fee_row(("PREPARED",))
    assert candidate["symbol"] == "ZZZ_USDT"
    status = module.live_fee_test_status()
    assert status["position_check"]["long_symbols"] == ["BTC_USDT"]
    assert status["position_check"]["short_symbols"] == ["ETH_USDT"]
    assert status["position_check"]["open_position_count"] == 2


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


def test_live_fee_external_oid_is_unique_bounded_and_identifies_test_and_leg(isolated_app):
    module, _ = isolated_app

    entry_ids = {module._live_fee_external_oid(123, "e") for _ in range(100)}
    exit_id = module._live_fee_external_oid(123, "x")

    assert len(entry_ids) == 100
    assert all(len(value) <= 32 for value in entry_ids)
    assert all(value.startswith("lft123e") for value in entry_ids)
    assert len(exit_id) <= 32
    assert exit_id.startswith("lft123x")
    assert exit_id not in entry_ids


def test_order_details_accepts_cancelled_partial_fill(isolated_app, monkeypatch):
    module, _ = isolated_app

    async def private(*args, **kwargs):
        return {"state": 4, "dealVol": 0.4}

    monkeypatch.setattr(module, "mexc_private_request", private)

    result = asyncio.run(module._order_details(None, "partial-order"))

    assert result["dealVol"] == 0.4


def test_execute_uses_isolated_minimum_and_reduce_only_then_disarms(
    isolated_app, monkeypatch
):
    module, _ = isolated_app
    test_id = prepared_candidate(module)
    calls = []
    position_checks = iter(
        [[], [{"positionId": "position-1", "symbol": "BTC_USDT", "positionType": 1, "holdVol": 1}], []]
    )

    async def private(client, method, path, params=None, body=None):
        calls.append((method, path, body))
        if path.endswith("position_mode"):
            return 2
        if path.endswith("open_positions"):
            return next(position_checks)
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


def test_execute_blocks_any_existing_position_on_candidate_symbol(
    isolated_app, monkeypatch
):
    module, _ = isolated_app
    test_id = prepared_candidate(module)
    posts = []

    async def private(client, method, path, params=None, body=None):
        if path.endswith("position_mode"):
            return 1
        if path.endswith("open_positions"):
            return [
                {"positionId": "btc-short", "symbol": "BTC_USDT", "positionType": 2, "holdVol": 1},
                {"positionId": "other-long", "symbol": "USELESS_USDT", "positionType": 1, "holdVol": 2},
            ]
        if method == "POST":
            posts.append(body)
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

    assert "BTC_USDT üzerinde açık gerçek LONG/SHORT" in exc.value.detail
    assert posts == []
    assert module._live_fee_row(("PREFLIGHT_FAILED",))["id"] == test_id


def test_execute_hedge_mode_closes_only_fetched_position_and_partial_fill(
    isolated_app, monkeypatch
):
    module, _ = isolated_app
    test_id = prepared_candidate(module)
    calls = []
    unrelated = {"positionId": "other-short", "symbol": "USELESS_USDT", "positionType": 2, "holdVol": 3}
    position_checks = iter(
        [
            [unrelated],
            [unrelated, {"positionId": "hedge-long-1", "symbol": "BTC_USDT", "positionType": 1, "holdVol": 0.4}],
            [unrelated],
        ]
    )

    async def private(client, method, path, params=None, body=None):
        calls.append((method, path, body))
        if path.endswith("position_mode"):
            return 1
        if path.endswith("open_positions"):
            return next(position_checks)
        if path.endswith("funding_records"):
            assert params["position_id"] == "hedge-long-1"
            return {"resultList": []}
        if method == "POST":
            return "entry-order" if sum(x[0] == "POST" for x in calls) == 1 else "exit-order"
        raise AssertionError(path)

    async def order_details(client, order_id):
        return {"state": 3, "dealVol": 0.4}

    async def order_fills(client, order_id, contract_size):
        contracts = 0.4
        price = 100.1 if order_id == "entry-order" else 100.0
        return {
            "executed_contracts": contracts,
            "executed_qty": contracts * contract_size,
            "average_fill_price": price,
            "notional": price * contracts * contract_size,
            "actual_fee": price * contracts * contract_size * 0.0008,
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
    assert posts[0]["positionMode"] == 1
    assert posts[0]["side"] == 1
    assert len(posts[0]["externalOid"]) <= 32
    assert posts[0]["externalOid"].startswith(f"lft{test_id}e")
    assert posts[1]["positionMode"] == 1
    assert posts[1]["side"] == 4
    assert len(posts[1]["externalOid"]) <= 32
    assert posts[1]["externalOid"].startswith(f"lft{test_id}x")
    assert posts[1]["positionId"] == "hedge-long-1"
    assert posts[1]["vol"] == 0.4
    assert "reduceOnly" not in posts[1]
    assert result["status"] == "COMPLETED"


def test_hedge_mode_opposite_leg_after_entry_raises_open_alarm(
    isolated_app, monkeypatch
):
    module, _ = isolated_app
    test_id = prepared_candidate(module)
    posts = []
    position_checks = iter(
        [
            [],
            [{"positionId": "wrong-short", "symbol": "BTC_USDT", "positionType": 2, "holdVol": 1}],
        ]
    )

    async def private(client, method, path, params=None, body=None):
        if path.endswith("position_mode"):
            return 1
        if path.endswith("open_positions"):
            return next(position_checks)
        if method == "POST":
            posts.append(body)
            return "entry-order"
        raise AssertionError(path)

    async def order_details(client, order_id):
        return {"state": 3, "dealVol": 1}

    async def order_fills(client, order_id, contract_size):
        return {
            "executed_contracts": 1,
            "executed_qty": contract_size,
            "average_fill_price": 100,
            "notional": 100 * contract_size,
            "actual_fee": 0.00008,
            "fee_currency": "USDT",
            "trade_ids": ["entry-trade"],
            "timestamps": [1],
            "is_taker": True,
        }

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(module, "mexc_private_request", private)
    monkeypatch.setattr(module, "_order_details", order_details)
    monkeypatch.setattr(module, "_order_fills", order_fills)

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
    assert len(posts) == 1
    assert posts[0]["side"] == 1
    assert module._live_fee_row(("OPEN_ALARM",))["id"] == test_id


def test_hedge_mode_remaining_position_after_exit_is_open_alarm(
    isolated_app, monkeypatch
):
    module, _ = isolated_app
    test_id = prepared_candidate(module)
    position_checks = iter(
        [
            [],
            [{"positionId": "hedge-long-1", "symbol": "BTC_USDT", "positionType": 1, "holdVol": 1}],
            *[
                [{"positionId": "hedge-long-1", "symbol": "BTC_USDT", "positionType": 1, "holdVol": 0.2}]
                for _ in range(20)
            ],
        ]
    )
    post_count = 0

    async def private(client, method, path, params=None, body=None):
        nonlocal post_count
        if path.endswith("position_mode"):
            return 1
        if path.endswith("open_positions"):
            return next(position_checks)
        if path.endswith("funding_records"):
            return {"resultList": []}
        if method == "POST":
            post_count += 1
            return "entry-order" if post_count == 1 else "exit-order"
        raise AssertionError(path)

    async def order_details(client, order_id):
        return {"state": 3, "dealVol": 1}

    async def order_fills(client, order_id, contract_size):
        return {
            "executed_contracts": 1,
            "executed_qty": contract_size,
            "average_fill_price": 100,
            "notional": 100 * contract_size,
            "actual_fee": 0.00008,
            "fee_currency": "USDT",
            "trade_ids": [order_id + "-trade"],
            "timestamps": [1],
            "is_taker": True,
        }

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(module, "mexc_private_request", private)
    monkeypatch.setattr(module, "_order_details", order_details)
    monkeypatch.setattr(module, "_order_fills", order_fills)

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
    assert "0.2 contract kaldı" in exc.value.detail
    assert module._live_fee_row(("OPEN_ALARM",))["id"] == test_id


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
