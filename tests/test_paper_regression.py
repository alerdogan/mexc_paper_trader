import asyncio
import copy
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

import app
from conftest import INITIAL_STATE, PRODUCTION_DB


def row(module, position_id):
    connection = module.db()
    result = connection.execute(
        "SELECT * FROM positions WHERE id=?", (position_id,)
    ).fetchone()
    connection.close()
    return dict(result)


def trade_logs(module):
    connection = module.db()
    count = connection.execute(
        "SELECT COUNT(*) FROM logs WHERE level='TRADE'"
    ).fetchone()[0]
    connection.close()
    return count


def market(price, atr=10.0):
    return {"15m": {"price": price, "atr": atr}}


def test_restart_preserves_positions_balance_and_realized_pnl(
    isolated_app, insert_position, monkeypatch
):
    module, test_db = isolated_app
    open_id = insert_position(pnl=12.5, fee_paid=1.5)
    closed_id = insert_position(
        symbol="ETH_USDT",
        status="CLOSED",
        remaining_qty=0.0,
        pnl=75.25,
        closed_at="2026-01-02T00:00:00",
        close_price=110.0,
    )
    balance_before = module.current_paper_balance()
    realized_before = module.all_time_realized_pnl()

    # Simulate fresh process memory while retaining only the test database.
    module.settings.clear()
    module.settings.update(copy.deepcopy(module.DEFAULTS))
    module.state.clear()
    module.state.update(copy.deepcopy(INITIAL_STATE))
    monkeypatch.setattr(module, "DB", test_db)
    module.init_db()

    assert row(module, open_id)["status"] == "OPEN"
    assert row(module, closed_id)["status"] == "CLOSED"
    assert module.all_time_realized_pnl() == pytest.approx(realized_before)
    assert module.current_paper_balance() == pytest.approx(balance_before)


def test_tp1_tp2_and_runner_are_applied_once(isolated_app, insert_position):
    module, _ = isolated_app
    position_id = insert_position()
    module.state["live_prices"] = {"BTC_USDT": 120.0}

    module.manage()
    first = row(module, position_id)
    module.manage()
    second = row(module, position_id)

    expected_fee = (120.0 * 3.0 * module.settings["paper_fee_rate"]) * 2
    assert first["tp1_done"] == 1
    assert first["tp2_done"] == 1
    assert first["remaining_qty"] == pytest.approx(4.0)
    assert first["remaining_qty"] / first["qty"] * 100 == pytest.approx(
        module.settings["runner_pct"]
    )
    assert first["pnl"] == pytest.approx(120.0 - expected_fee)
    assert second["remaining_qty"] == pytest.approx(first["remaining_qty"])
    assert second["pnl"] == pytest.approx(first["pnl"])
    assert second["fee_paid"] == pytest.approx(first["fee_paid"])


def test_stop_loss_and_fee_are_applied_once(isolated_app, insert_position):
    module, _ = isolated_app
    position_id = insert_position()
    module.state["live_prices"] = {"BTC_USDT": 90.0}

    module.manage()
    first = row(module, position_id)
    logs_after_first = trade_logs(module)
    module.manage()
    second = row(module, position_id)

    exit_fee = 90.0 * 10.0 * module.settings["paper_fee_rate"]
    assert first["status"] == "CLOSED"
    assert first["remaining_qty"] == 0
    assert first["close_reason"] == "STOP"
    assert first["pnl"] == pytest.approx(-100.0 - exit_fee)
    assert first["fee_paid"] == pytest.approx(exit_fee)
    assert second["pnl"] == pytest.approx(first["pnl"])
    assert trade_logs(module) == logs_after_first


def test_missing_or_zero_price_does_not_close_or_crash(isolated_app, insert_position):
    module, _ = isolated_app
    position_id = insert_position()

    module.state["live_prices"] = {}
    module.state["market"] = {}
    module.manage()
    module.state["live_prices"] = {"BTC_USDT": 0}
    module.manage()

    unchanged = row(module, position_id)
    assert unchanged["status"] == "OPEN"
    assert unchanged["remaining_qty"] == pytest.approx(10.0)
    assert unchanged["pnl"] == pytest.approx(0.0)


@pytest.mark.xfail(
    strict=True,
    reason="manage() does not track price age and accepts stale live_prices",
)
def test_stale_price_must_not_close_position(isolated_app, insert_position):
    module, _ = isolated_app
    position_id = insert_position()
    module.state["live_prices"] = {"BTC_USDT": 89.0}
    module.state["last_price_update"] = (
        datetime.now() - timedelta(hours=1)
    ).isoformat(timespec="seconds")
    module.manage()
    assert row(module, position_id)["status"] == "OPEN"


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class SequenceClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def get(self, url, **kwargs):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def configure_fast_retry(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(app.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(app.random, "uniform", lambda low, high: 0.05)
    monkeypatch.setattr(app, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "MEXC_MIN_REQUEST_INTERVAL", 0)
    return sleeps


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout("timeout"),
        httpx.ConnectError("connection error"),
    ],
)
def test_transient_network_errors_are_retried_without_real_network(
    error, monkeypatch
):
    sleeps = configure_fast_retry(monkeypatch)
    client = SequenceClient([error, FakeResponse(200, {"success": True})])
    response = asyncio.run(app.mexc_get(client, "https://mock.invalid/test"))
    assert response.status_code == 200
    assert client.calls == 2
    assert sleeps == pytest.approx([0.55])


def test_transient_network_retry_is_bounded(monkeypatch):
    sleeps = configure_fast_retry(monkeypatch)
    client = SequenceClient([httpx.ConnectTimeout("timeout")])
    with pytest.raises(httpx.ConnectTimeout):
        asyncio.run(app.mexc_get(client, "https://mock.invalid/test"))
    assert client.calls == 4
    assert sleeps == pytest.approx([0.55, 1.05, 2.05])


def test_http_5xx_uses_exponential_backoff_and_jitter(monkeypatch):
    sleeps = configure_fast_retry(monkeypatch)
    client = SequenceClient(
        [FakeResponse(503), FakeResponse(503), FakeResponse(200, {"success": True})]
    )
    response = asyncio.run(app.mexc_get(client, "https://mock.invalid/test"))
    assert response.status_code == 200
    assert client.calls == 3
    assert sleeps == pytest.approx([0.55, 1.05])


def test_http_4xx_is_not_retried(monkeypatch):
    sleeps = configure_fast_retry(monkeypatch)
    client = SequenceClient([FakeResponse(400)])
    response = asyncio.run(app.mexc_get(client, "https://mock.invalid/test"))
    assert response.status_code == 400
    assert client.calls == 1
    assert sleeps == []


def test_database_locked_does_not_lose_or_duplicate_trade(
    isolated_app, monkeypatch
):
    module, test_db = isolated_app
    real_db = module.db

    def fast_db():
        connection = sqlite3.connect(test_db, timeout=0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=1")
        return connection

    blocker = sqlite3.connect(test_db, timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")
    monkeypatch.setattr(module, "db", fast_db)

    released = False

    def release_lock_on_retry(delay):
        nonlocal released
        if not released:
            blocker.rollback()
            blocker.close()
            released = True

    monkeypatch.setattr(module.time, "sleep", release_lock_on_retry)
    try:
        module.paper_open("BTC_USDT", "LONG", market(100.0), 90)
    finally:
        if not released:
            blocker.rollback()
            blocker.close()
        monkeypatch.setattr(module, "db", real_db)

    connection = real_db()
    count = connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    connection.close()
    assert count == 1


def test_non_lock_operational_error_is_not_retried(isolated_app):
    module, _ = isolated_app
    calls = 0

    def broken_write(connection):
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        module._sqlite_write_with_retry(broken_write)
    assert calls == 1


def test_manage_retries_lock_without_duplicate_stop(
    isolated_app, insert_position, monkeypatch
):
    module, test_db = isolated_app
    position_id = insert_position()
    module.state["live_prices"] = {"BTC_USDT": 89.0}
    real_db = module.db

    def fast_db():
        connection = sqlite3.connect(test_db, timeout=0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=1")
        return connection

    blocker = sqlite3.connect(test_db, timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")
    released = False

    def release_lock_on_retry(delay):
        nonlocal released
        if not released:
            blocker.rollback()
            blocker.close()
            released = True

    monkeypatch.setattr(module, "db", fast_db)
    monkeypatch.setattr(module.time, "sleep", release_lock_on_retry)
    try:
        module.manage()
    finally:
        if not released:
            blocker.rollback()
            blocker.close()
        monkeypatch.setattr(module, "db", real_db)

    final = row(module, position_id)
    assert final["status"] == "CLOSED"
    assert final["remaining_qty"] == 0
    assert trade_logs(module) == 1


def test_duplicate_stop_event_is_idempotent(isolated_app, insert_position):
    module, _ = isolated_app
    position_id = insert_position()
    module.state["live_prices"] = {"BTC_USDT": 89.0}

    for _ in range(3):
        module.manage()

    final = row(module, position_id)
    assert final["status"] == "CLOSED"
    assert trade_logs(module) == 1
    assert final["remaining_qty"] == 0


def test_paper_open_creates_only_paper_record_and_uses_no_network(
    isolated_app, monkeypatch
):
    module, _ = isolated_app

    def forbidden_network(*args, **kwargs):
        raise AssertionError("paper_open must not use network or send an order")

    monkeypatch.setattr(httpx, "get", forbidden_network, raising=False)
    monkeypatch.setattr(httpx, "post", forbidden_network, raising=False)
    module.paper_open("BTC_USDT", "LONG", market(100.0), 90)

    connection = module.db()
    modes = connection.execute("SELECT DISTINCT mode FROM positions").fetchall()
    connection.close()
    assert [item[0] for item in modes] == ["PAPER"]


def test_production_database_guard_blocks_open_attempt():
    with pytest.raises(AssertionError, match="production trader.db"):
        sqlite3.connect(PRODUCTION_DB)


def test_test_database_is_not_production(isolated_app):
    module, test_db = isolated_app
    assert Path(module.DB).resolve() == test_db.resolve()
    assert Path(module.DB).resolve() != PRODUCTION_DB
