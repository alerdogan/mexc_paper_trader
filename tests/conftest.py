import copy
import socket
import sqlite3
from pathlib import Path

import pytest

import app


PRODUCTION_DB = (Path(__file__).parents[1] / "trader.db").resolve()
INITIAL_STATE = copy.deepcopy(app.state)
REAL_CONNECT = sqlite3.connect


def _database_path(database):
    raw = str(database)
    if raw.startswith("file:"):
        raw = raw[5:].split("?", 1)[0]
    if raw == ":memory:":
        return None
    return Path(raw).resolve()


@pytest.fixture(autouse=True)
def forbid_production_database(monkeypatch):
    """Fail before any test can open the production SQLite database."""

    def guarded_connect(database, *args, **kwargs):
        assert _database_path(database) != PRODUCTION_DB, (
            "Tests must never open the production trader.db"
        )
        return REAL_CONNECT(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    """Force every network scenario to use an injected fake client."""

    def blocked_network(*args, **kwargs):
        raise AssertionError("Regression tests must not access the real network")

    monkeypatch.setattr(socket.socket, "connect", blocked_network)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_network)


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    test_db = tmp_path / "test-trader.db"
    monkeypatch.setattr(app, "DB", test_db)
    monkeypatch.setattr(app, "keychain_get", lambda account: "")
    app.settings.clear()
    app.settings.update(copy.deepcopy(app.DEFAULTS))
    app.state.clear()
    app.state.update(copy.deepcopy(INITIAL_STATE))
    app.background_tasks.clear()
    for name in app.TASK_NAMES:
        app.task_status[name] = app._new_task_status()
    app.init_db()
    yield app, test_db


@pytest.fixture
def insert_position(isolated_app):
    module, _ = isolated_app

    def insert(**overrides):
        values = {
            "symbol": "BTC_USDT",
            "side": "LONG",
            "status": "OPEN",
            "entry": 100.0,
            "stop": 90.0,
            "initial_stop": 90.0,
            "qty": 10.0,
            "remaining_qty": 10.0,
            "risk_usd": 100.0,
            "score": 90.0,
            "opened_at": "2026-01-01T00:00:00",
            "closed_at": None,
            "pnl": 0.0,
            "tp1_done": 0,
            "tp2_done": 0,
            "mode": "PAPER",
            "close_price": None,
            "fee_paid": 0.0,
            "tracking_started_at": "2026-01-01T00:00:00",
        }
        values.update(overrides)
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        connection = module.db()
        cursor = connection.execute(
            f"INSERT INTO positions ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        connection.commit()
        position_id = cursor.lastrowid
        connection.close()
        return position_id

    return insert
