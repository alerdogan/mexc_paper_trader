import asyncio
import json
import sqlite3

import app


class RunningTask:
    def done(self):
        return False


def mark_all_tasks_running(module):
    for name in module.TASK_NAMES:
        module.task_status[name].update(
            alive=True,
            running=True,
            last_success="2026-09-01T12:00:00",
        )
        module.background_tasks[name] = RunningTask()


def test_health_reports_safe_task_and_database_status(isolated_app):
    module, _ = isolated_app
    mark_all_tasks_running(module)
    module.state["last_scan"] = "2026-09-01T12:00:00"
    module.state["last_price_update"] = "2026-09-01T12:00:03"
    module.state["public_api"] = "BAĞLI"

    result = module.health()

    assert result["service_status"] == "healthy"
    assert result["mode"] == "PAPER"
    assert result["database_connectivity"] is True
    assert result["mexc_api_connection"] == "BAĞLI"
    assert isinstance(result["uptime_seconds"], int)
    assert result["uptime_seconds"] >= 0
    assert result["last_successful_market_data_timestamp"] == "2026-09-01T12:00:03"
    for name in module.TASK_NAMES:
        assert result[name]["alive"] is True
        assert result[name]["running"] is True
        assert result[name]["last_success"] == "2026-09-01T12:00:00"

    payload = json.dumps(result).lower()
    for forbidden in ("api_key", "api_secret", "password", "credential"):
        assert forbidden not in payload


def test_health_degrades_without_exposing_database_error(isolated_app, monkeypatch):
    module, _ = isolated_app
    mark_all_tasks_running(module)

    def broken_db():
        raise sqlite3.OperationalError("sensitive internal database path")

    monkeypatch.setattr(module, "db", broken_db)
    result = module.health()

    assert result["service_status"] == "degraded"
    assert result["database_connectivity"] is False
    assert "sensitive" not in json.dumps(result).lower()


def test_supervisor_restarts_failed_worker_with_safe_error(isolated_app, monkeypatch):
    module, _ = isolated_app
    monkeypatch.setattr(module, "SUPERVISOR_BACKOFF", (0,))
    monkeypatch.setattr(module, "log", lambda *args, **kwargs: None)

    async def scenario():
        ready = asyncio.Event()
        calls = 0

        async def worker():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("secret-like detail must not enter health")
            module._task_success("scanner")
            ready.set()
            await asyncio.Event().wait()

        supervisor = asyncio.create_task(
            module.supervise_task("scanner", worker)
        )
        await asyncio.wait_for(ready.wait(), timeout=1)
        info = module.task_status["scanner"]
        assert calls == 2
        assert info["alive"] is True
        assert info["running"] is True
        assert info["restart_count"] == 1
        assert info["consecutive_failures"] == 0
        assert info["last_error"]["type"] == "RuntimeError"
        assert "message" not in info["last_error"]
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)
        assert info["alive"] is False
        assert info["running"] is False

    asyncio.run(scenario())


def test_start_background_tasks_registers_all_supervisors(
    isolated_app, monkeypatch
):
    module, _ = isolated_app

    async def idle_worker():
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "engine", idle_worker)
    monkeypatch.setattr(module, "position_engine", idle_worker)
    monkeypatch.setattr(module, "ghost_analysis_engine", idle_worker)

    async def scenario():
        module.start_background_tasks()
        await asyncio.sleep(0)
        assert set(module.background_tasks) == set(module.TASK_NAMES)
        assert all(not task.done() for task in module.background_tasks.values())
        tasks = list(module.background_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_supervisor_backoff_is_bounded_and_non_decreasing():
    assert app.SUPERVISOR_BACKOFF
    assert tuple(sorted(app.SUPERVISOR_BACKOFF)) == app.SUPERVISOR_BACKOFF
    assert app.SUPERVISOR_BACKOFF[-1] <= 30


def test_dashboard_contains_compact_health_panel():
    template = (app.BASE / "templates" / "index.html").read_text()

    assert "Sistem Sağlığı" in template
    assert "fetch('/api/health')" in template
    for element_id in (
        "healthMexc",
        "healthScanner",
        "healthPosition",
        "healthGhost",
        "healthDatabase",
        "healthMarketData",
        "healthUptime",
    ):
        assert f'id="{element_id}"' in template


def test_dashboard_does_not_expose_live_fee_test_controls():
    template = (app.BASE / "templates" / "index.html").read_text()

    assert 'id="liveFeeTestPanel"' not in template
    assert "refreshLiveFeeTest();" not in template
