from datetime import datetime, timedelta

import pytest

from test_entry_quality_filter import market, universe


def candidate(module):
    c = module.db()
    row = dict(c.execute("SELECT * FROM entry_timing_lab_candidates").fetchone())
    c.close()
    return row


def create_candidate(module):
    signal = market(rsi=54)
    signal["5m"] = {"rsi": 50}
    current = universe(signal)
    assert module.process_paper_signal("ALT_USDT", signal, current, {}, list(current)) is None
    row = candidate(module)
    return row["current_position_id"], row


def candle(**overrides):
    now = datetime.now()
    data = {"opened_at": (now - timedelta(minutes=5)).isoformat(timespec="seconds"),
            "closed_at": now.isoformat(timespec="seconds"), "open": 100, "high": 102,
            "low": 99, "close": 101, "previous_close": 100, "rsi": 51,
            "volume_ratio": .9}
    data.update(overrides)
    return data


def test_current_entry_unchanged_and_candidate_is_matched_long_only(isolated_app):
    module, _ = isolated_app
    position_id, row = create_candidate(module)
    c = module.db(); position = dict(c.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()); c.close()
    assert position["status"] == "OPEN" and position["entry"] == pytest.approx(100)
    assert row["current_position_id"] == position_id and row["confirmation_status"] == "PENDING"
    assert module.current_paper_balance() == pytest.approx(4000 - position["fee_paid"])


def test_duplicate_protection_and_no_historical_backfill(isolated_app, insert_position):
    module, _ = isolated_app
    old = insert_position(status="CLOSED", closed_at="2025-01-01T01:00:00")
    module.init_db()
    c = module.db()
    assert c.execute("SELECT COUNT(*) FROM entry_timing_lab_candidates").fetchone()[0] == 0
    score = {"rsi_value": 54, "support_distance_pct": 1, "resistance_distance_pct": 2,
             "trend_15m": 1, "trend_1h": 1, "trend_4h": 1}
    regime = {"bullish_pct": 60, "bearish_pct": 10, "btc_15m_volume_ratio": 1,
              "eth_15m_volume_ratio": 1}
    m = {"15m": {"price": 100, "atr": 10}, "5m": {"rsi": 50}}
    module._insert_entry_timing_candidate(c, old, "ALT_USDT", "LONG", "2026-01-01T00:00:00", 90, score, regime, m)
    module._insert_entry_timing_candidate(c, old, "ALT_USDT", "LONG", "2026-01-01T00:00:00", 90, score, regime, m)
    c.commit()
    assert c.execute("SELECT COUNT(*) FROM entry_timing_lab_candidates").fetchone()[0] == 1
    c.close()


def test_bullish_confirmation_delay_entry_price_and_risk_reuse(isolated_app):
    module, _ = isolated_app
    _, row = create_candidate(module)
    now = datetime.fromisoformat(row["candidate_created_at"]) + timedelta(minutes=6)
    bar = candle(closed_at=(now - timedelta(seconds=1)).isoformat(timespec="seconds"))
    c = module.db(); action = module._update_entry_timing_candidate(c, row, bar, 101, now); c.commit()
    confirmed = dict(c.execute("SELECT * FROM entry_timing_lab_candidates").fetchone()); c.close()
    assert action == "CONFIRMED" and confirmed["confirmation_status"] == "OPEN"
    assert confirmed["confirmation_delay_seconds"] == 360
    assert confirmed["shadow_entry_price"] == 101
    plan = module._current_entry_plan("ALT_USDT", "LONG", {"15m": {"price": 101, "atr": row["original_atr"]}})
    assert confirmed["shadow_initial_stop"] == pytest.approx(plan["initial_stop"])
    assert confirmed["shadow_qty"] == pytest.approx(plan["qty"])
    assert confirmed["fee"] == pytest.approx(101 * plan["qty"] * .0008)


def test_confirmation_rules_timeout_and_chased_rejection(isolated_app):
    module, _ = isolated_app
    _, row = create_candidate(module)
    created = datetime.fromisoformat(row["candidate_created_at"])
    valid = candle(closed_at=(created + timedelta(minutes=5)).isoformat(timespec="seconds"))
    assert module._entry_timing_evaluate(row, valid, 101, created + timedelta(minutes=6))["action"] == "CONFIRMED"
    assert module._entry_timing_evaluate(row, candle(close=99), 101, created + timedelta(minutes=6))["action"] == "PENDING"
    assert module._entry_timing_evaluate(row, valid, 108, created + timedelta(minutes=6))["action"] == "CHASED_EXPIRED"
    assert module._entry_timing_evaluate(row, valid, 101, created + timedelta(minutes=31))["action"] == "NO_CONFIRMATION_TIMEOUT"


def test_shadow_tp_fee_stop_and_no_paper_or_live_impact(isolated_app, monkeypatch):
    module, _ = isolated_app
    position_id, row = create_candidate(module)
    created = datetime.fromisoformat(row["candidate_created_at"])
    c = module.db(); module._update_entry_timing_candidate(c, row, candle(closed_at=(created + timedelta(minutes=5)).isoformat(timespec="seconds")), 100, created + timedelta(minutes=6)); c.commit(); c.close()
    balance_before = module.current_paper_balance()
    module.state["live_prices"] = {"ALT_USDT": 120}
    module._sqlite_write_with_retry(module._manage_entry_timing_shadows_once)
    c = module.db(); shadow = dict(c.execute("SELECT * FROM entry_timing_lab_candidates").fetchone()); position = dict(c.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()); c.close()
    assert shadow["tp1_reached"] == 1 and shadow["tp2_reached"] == 1
    assert shadow["shadow_remaining_qty"] / shadow["shadow_qty"] * 100 == pytest.approx(module.settings["runner_pct"])
    assert position["tp1_done"] == 0 and module.current_paper_balance() == pytest.approx(balance_before)
    assert not hasattr(module, "entry_timing_live_order")


def test_restart_persistence_and_matched_metrics(isolated_app):
    module, _ = isolated_app
    position_id, before = create_candidate(module)
    module.init_db()
    assert candidate(module)["id"] == before["id"]
    c = module.db()
    c.execute("UPDATE entry_timing_lab_candidates SET confirmation_status='CLOSED',tp1_reached=1,tp2_reached=1,mae_r=.5,mfe_r=2,gross_pnl=12,fee=2,net_pnl=10,exit_reason='STOP',closed_at='2026-01-01T01:00:00' WHERE id=?", (before["id"],))
    c.execute("UPDATE positions SET status='CLOSED',tp1_done=1,tp2_done=1,mae_r=.7,mfe_r=2.2,pnl=8,fee_paid=2,close_reason='STOP',closed_at='2026-01-01T01:00:00' WHERE id=?", (position_id,))
    c.commit(); c.close()
    result = module.entry_timing_lab()
    assert result["closed_matched_count"] == 1
    assert result["current"]["total_net_pnl"] == 8
    assert result["confirmed"]["total_net_pnl"] == 10
    assert result["delta"]["total_net_pnl"] == 2
