from datetime import datetime, timedelta

import pytest

from test_entry_quality_filter import market, universe


def candidate(module):
    c = module.db()
    row = dict(c.execute("SELECT * FROM momentum_5m_entry_candidates").fetchone())
    c.close()
    return row


def create_candidate(module, side="LONG", rsi=54):
    signal = market(side=side, rsi=rsi)
    signal["5m"] = {"rsi": 50}
    current = universe(signal)
    assert module.process_paper_signal("ALT_USDT", signal, current, {}, list(current)) is None
    return current


def candle(now=None, **overrides):
    now = now or datetime.now()
    opened = now - timedelta(minutes=5)
    data = {"opened_at": opened.timestamp(), "closed_at": now.timestamp(),
            "open": 100.1, "high": 100.6, "low": 100.0, "close": 100.5,
            "previous_close": 100.0, "rsi": 51.0, "volume_ratio": 0.9}
    data.update(overrides)
    return data


def test_current_unchanged_and_candidate_matches_long_only(isolated_app):
    module, _ = isolated_app
    create_candidate(module)
    row = candidate(module)
    c = module.db()
    position = dict(c.execute("SELECT * FROM positions WHERE id=?", (row["current_position_id"],)).fetchone())
    c.close()
    assert position["status"] == "OPEN" and position["entry"] == pytest.approx(100)
    assert row["confirmation_status"] == "PENDING"
    assert row["model"] == "MOMENTUM_5M_ENTRY" and row["version"] == module.MOMENTUM_5M_ENTRY_VERSION
    assert row["original_current_entry_price"] == pytest.approx(100) and row["original_atr"] == pytest.approx(1.0)
    assert module.current_paper_balance() == pytest.approx(4000 - position["fee_paid"])
    # CONFIRMED_5M_ENTRY_V1 candidate for the same position must also exist, untouched by this task.
    c = module.db()
    timing_row = dict(c.execute("SELECT * FROM entry_timing_lab_candidates").fetchone())
    c.close()
    assert timing_row["current_position_id"] == row["current_position_id"]


def test_short_creates_no_momentum_candidate(isolated_app):
    module, _ = isolated_app
    signal = market(side="SHORT", rsi=40)
    signal["5m"] = {"rsi": 50}
    current = universe(signal)
    assert module.process_paper_signal("ALT_USDT", signal, current, {}, list(current)) is None
    c = module.db()
    count = c.execute("SELECT COUNT(*) FROM momentum_5m_entry_candidates").fetchone()[0]
    c.close()
    assert count == 0


def test_duplicate_protection_and_no_historical_backfill(isolated_app, insert_position):
    module, _ = isolated_app
    old = insert_position(status="CLOSED", closed_at="2025-01-01T01:00:00")
    module.init_db()
    c = module.db()
    assert c.execute("SELECT COUNT(*) FROM momentum_5m_entry_candidates").fetchone()[0] == 0
    m = {"15m": {"price": 100, "atr": 10}, "5m": {"rsi": 50}}
    module._insert_momentum_5m_candidate(c, old, "ALT_USDT", "LONG", "2026-01-01T00:00:00", 90, m)
    module._insert_momentum_5m_candidate(c, old, "ALT_USDT", "LONG", "2026-01-01T00:00:00", 90, m)
    c.commit()
    assert c.execute("SELECT COUNT(*) FROM momentum_5m_entry_candidates").fetchone()[0] == 1
    c.close()


def test_all_conditions_confirm_with_min_move_r_and_confirmation_price(isolated_app):
    module, _ = isolated_app
    create_candidate(module)
    row = candidate(module)
    created = datetime.fromisoformat(row["candidate_created_at"])
    now = created + timedelta(minutes=6)
    bar = candle(now=now, open=100.1, high=100.6, low=100.0, close=100.15, previous_close=100.0, rsi=51, volume_ratio=0.9)
    c = module.db()
    action = module._update_momentum_5m_candidate(c, row, [bar], now)
    c.commit()
    confirmed = dict(c.execute("SELECT * FROM momentum_5m_entry_candidates").fetchone())
    evals = [dict(x) for x in c.execute("SELECT * FROM momentum_5m_entry_candle_evaluations").fetchall()]
    c.close()
    assert action == "CONFIRMED" and confirmed["confirmation_status"] == "CONFIRMED"
    assert confirmed["confirmation_price"] == pytest.approx(100.15)
    assert confirmed["confirmation_delay_seconds"] == 360
    assert confirmed["price_move_from_original_r"] == pytest.approx(0.10, abs=1e-6)
    assert len(evals) == 1 and evals[0]["confirmed_this_candle"] == 1
    plan = module._current_entry_plan("ALT_USDT", "LONG", {"15m": {"price": 100.15, "atr": row["original_atr"]}})
    assert confirmed["shadow_entry_price"] == pytest.approx(100.15)
    assert confirmed["shadow_initial_stop"] == pytest.approx(plan["initial_stop"])
    assert confirmed["shadow_qty"] == pytest.approx(plan["qty"])
    assert confirmed["fee"] == pytest.approx(100.15 * plan["qty"] * .0008)


@pytest.mark.parametrize(
    ("field_overrides", "failing_flag"),
    [
        ({"open": 101.0, "close": 100.5}, "bullish_pass"),
        ({"previous_close": 101.0, "close": 100.5}, "previous_close_pass"),
        ({"close": 99.5, "open": 99.0, "previous_close": 98.5}, "breakout_pass"),
        ({"rsi": 40.0}, "rsi_pass"),
        ({"volume_ratio": 0.5}, "volume_pass"),
        ({"close": 100.05, "open": 100.0}, "min_move_pass"),
        ({"close": 108.0, "open": 107.5, "high": 108.5}, "chase_pass"),
    ],
)
def test_each_condition_can_independently_fail(isolated_app, field_overrides, failing_flag):
    module, _ = isolated_app
    create_candidate(module)
    row = candidate(module)
    created = datetime.fromisoformat(row["candidate_created_at"])
    now = created + timedelta(minutes=6)
    bar = candle(now=now, **field_overrides)
    c = module.db()
    action = module._update_momentum_5m_candidate(c, row, [bar], now)
    c.commit()
    ev = dict(c.execute("SELECT * FROM momentum_5m_entry_candle_evaluations").fetchone())
    c.close()
    assert ev[failing_flag] == 0
    assert ev["confirmed_this_candle"] == 0
    # A chase-level move (>=0.75R) inside the candle-processing loop is itself terminal.
    if failing_flag == "chase_pass":
        assert action == "MOMENTUM_CHASED_EXPIRED"
    else:
        assert action == "PENDING"


def test_completed_candles_only_no_lookahead(isolated_app):
    module, _ = isolated_app
    create_candidate(module)
    row = candidate(module)
    created = datetime.fromisoformat(row["candidate_created_at"])
    now = created + timedelta(seconds=90)  # under 5 minutes: nothing has completed yet
    rows = []
    t = created.timestamp() - 300
    for i in range(40):
        rows.append([t + i * 300, 100 + i * 0.01, 100.2 + i * 0.01, 99.8 + i * 0.01, 100.05 + i * 0.01, 100])
    candles = module._momentum_5m_completed_candles(rows, row["candidate_created_at"], now)
    assert candles == []


def test_breakout_reference_expands_and_first_candle_uses_no_future_info(isolated_app):
    module, _ = isolated_app
    create_candidate(module)
    row = candidate(module)
    created = datetime.fromisoformat(row["candidate_created_at"])
    # First candle: high 100.4 but close 100.05 is below the +0.10R min-move floor, so it cannot
    # confirm; it should NOT use its own high as its own breakout reference (it is compared to the
    # original entry price, 100 - no future/self information).
    c1 = candle(now=created + timedelta(minutes=5), open=100.02, high=100.4, low=100.0, close=100.05,
                previous_close=100.0, rsi=51, volume_ratio=0.9)
    c = module.db()
    action = module._update_momentum_5m_candidate(c, row, [c1], created + timedelta(minutes=6))
    c.commit()
    ev1 = dict(c.execute("SELECT * FROM momentum_5m_entry_candle_evaluations ORDER BY id").fetchall()[0])
    c.close()
    assert action == "PENDING"
    assert ev1["breakout_reference"] == pytest.approx(100.0)  # anchored at original entry, not future info
    assert ev1["breakout_pass"] == 1  # 100.35 > 100.0

    row2 = candidate(module)
    assert row2["breakout_reference_high"] == pytest.approx(100.4)  # running max now includes candle 1 high

    # Second candle must beat the EXPANDED reference (100.4), not the original entry price.
    c2 = candle(now=created + timedelta(minutes=10), open=100.35, high=100.55, low=100.2, close=100.39,
                previous_close=100.35, rsi=51, volume_ratio=0.9)
    c = module.db()
    module._update_momentum_5m_candidate(c, row2, [c2], created + timedelta(minutes=11))
    c.commit()
    ev2 = dict(c.execute("SELECT * FROM momentum_5m_entry_candle_evaluations ORDER BY id").fetchall()[1])
    c.close()
    assert ev2["breakout_reference"] == pytest.approx(100.4)
    assert ev2["breakout_pass"] == 0  # 100.39 < 100.4: does not beat the expanded high


def test_preliminary_chase_and_timeout(isolated_app):
    module, _ = isolated_app
    create_candidate(module)
    row = candidate(module)
    created = datetime.fromisoformat(row["candidate_created_at"])
    assert module._momentum_5m_preliminary(row, 100.15, created + timedelta(minutes=1))["action"] == "PENDING"
    assert module._momentum_5m_preliminary(row, 101.2, created + timedelta(minutes=1))["action"] == "MOMENTUM_CHASED_EXPIRED"
    assert module._momentum_5m_preliminary(row, 100.15, created + timedelta(minutes=31))["action"] == "NO_MOMENTUM_TIMEOUT"


def test_cancelled_invalid_on_bad_risk_data(isolated_app):
    module, _ = isolated_app
    create_candidate(module)
    row = candidate(module)
    row["original_atr"] = 0
    row["original_current_entry_price"] = 0
    c = module.db()
    action = module._update_momentum_5m_candidate(c, row, [], datetime.now())
    c.commit()
    status = c.execute("SELECT confirmation_status FROM momentum_5m_entry_candidates").fetchone()[0]
    c.close()
    assert action == "CANCELLED_INVALID" and status == "CANCELLED_INVALID"


def test_terminal_transition_is_atomic_and_idempotent(isolated_app):
    module, _ = isolated_app
    create_candidate(module)
    row = candidate(module)
    created = datetime.fromisoformat(row["candidate_created_at"])
    now = created + timedelta(minutes=6)
    bar = candle(now=now)
    c = module.db()
    first = module._update_momentum_5m_candidate(c, row, [bar], now)
    c.commit()
    # Re-running with the SAME stale (pre-transition) candidate dict must be a no-op: the guarded
    # WHERE confirmation_status='PENDING' blocks the second write, and INSERT OR IGNORE on the
    # audit table prevents a duplicate row for the same candle.
    second = module._update_momentum_5m_candidate(c, row, [bar], now)
    c.commit()
    evals = c.execute("SELECT COUNT(*) FROM momentum_5m_entry_candle_evaluations").fetchone()[0]
    status = c.execute("SELECT confirmation_status FROM momentum_5m_entry_candidates").fetchone()[0]
    c.close()
    assert first == "CONFIRMED"
    assert evals == 1
    assert status == "CONFIRMED"


def test_original_r_denominator_unaffected_by_current_stop_move(isolated_app):
    module, _ = isolated_app
    create_candidate(module)
    row = candidate(module)
    c = module.db()
    position_id = row["current_position_id"]
    # Move CURRENT's own stop to break-even (simulating TP1), as _manage_once would.
    c.execute("UPDATE positions SET stop=entry, tp1_done=1 WHERE id=?", (position_id,))
    c.commit(); c.close()
    original, risk = module._momentum_5m_original_risk(row)
    assert original == pytest.approx(100.0) and risk == pytest.approx(1.5)


def test_shadow_tp1_tp2_fee_runner_and_zero_paper_or_live_impact(isolated_app):
    module, _ = isolated_app
    create_candidate(module)
    row = candidate(module)
    created = datetime.fromisoformat(row["candidate_created_at"])
    now = created + timedelta(minutes=6)
    bar = candle(now=now, open=100.1, high=100.6, low=100.0, close=100.15, previous_close=100.0, rsi=51, volume_ratio=0.9)
    c = module.db()
    module._update_momentum_5m_candidate(c, row, [bar], now)
    c.commit(); c.close()
    balance_before = module.current_paper_balance()
    live_fee_tests_before = module.db().execute("SELECT COUNT(*) FROM live_fee_tests").fetchone()[0]
    module.state["live_prices"] = {"ALT_USDT": 130}
    module._sqlite_write_with_retry(module._manage_momentum_5m_shadows_once)
    c = module.db()
    shadow = dict(c.execute("SELECT * FROM momentum_5m_entry_candidates").fetchone())
    position = dict(c.execute("SELECT * FROM positions WHERE id=?", (row["current_position_id"],)).fetchone())
    live_fee_tests_after = c.execute("SELECT COUNT(*) FROM live_fee_tests").fetchone()[0]
    c.close()
    assert shadow["tp1_reached"] == 1 and shadow["tp2_reached"] == 1
    assert shadow["shadow_remaining_qty"] / shadow["shadow_qty"] * 100 == pytest.approx(module.settings["runner_pct"])
    assert shadow["fee"] > 0  # 0.08% taker fee accrued on the shadow entry
    # CURRENT position and real PAPER balance/state are fully untouched by the shadow trade.
    assert position["tp1_done"] == 0 and position["stop"] == pytest.approx(row["original_current_entry_price"] - 1.5)
    assert module.current_paper_balance() == pytest.approx(balance_before)
    assert live_fee_tests_after == live_fee_tests_before
    assert not hasattr(module, "momentum_5m_live_order")


def test_restart_recovery_migration_idempotency_and_existing_positions_unaffected(isolated_app, insert_position):
    module, _ = isolated_app
    open_id = insert_position(status="OPEN", risk_usd=0.0)
    create_candidate(module)
    before = candidate(module)
    module.init_db()
    module.init_db()  # idempotent: calling twice must not duplicate meta rows or alter data
    after = candidate(module)
    c = module.db()
    meta_count = c.execute("SELECT COUNT(*) FROM momentum_5m_entry_meta").fetchone()[0]
    candidate_count = c.execute("SELECT COUNT(*) FROM momentum_5m_entry_candidates").fetchone()[0]
    pre_existing_position = dict(c.execute("SELECT * FROM positions WHERE id=?", (open_id,)).fetchone())
    c.close()
    assert after["id"] == before["id"] and after["confirmation_status"] == "PENDING"
    assert meta_count == 1 and candidate_count == 1
    assert pre_existing_position["status"] == "OPEN"  # untouched by the migration or the new model


def test_matched_cohort_and_failure_analytics(isolated_app):
    module, _ = isolated_app
    create_candidate(module)
    row = candidate(module)
    c = module.db()
    c.execute("""UPDATE momentum_5m_entry_candidates SET confirmation_status='CLOSED',tp1_reached=1,tp2_reached=1,
     mae_r=.5,mfe_r=2,gross_pnl=12,fee=2,net_pnl=10,exit_reason='STOP',closed_at='2026-01-01T01:00:00' WHERE id=?""",
              (row["id"],))
    c.execute("""UPDATE positions SET status='CLOSED',tp1_done=1,tp2_done=1,mae_r=.7,mfe_r=2.2,pnl=8,fee_paid=2,
     close_reason='STOP',closed_at='2026-01-01T01:00:00' WHERE id=?""", (row["current_position_id"],))
    c.execute("""INSERT INTO momentum_5m_entry_candle_evaluations(
     candidate_id,candle_open_ts,candle_closed_at,open,high,low,close,previous_close,breakout_reference,
     rsi,volume_ratio,move_r,bullish_pass,previous_close_pass,breakout_pass,rsi_pass,volume_pass,
     min_move_pass,chase_pass,confirmed_this_candle,created_at
     ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (row["id"], 1.0, "2026-01-01T00:05:00", 100, 101, 99, 100.5, 100, 100, 51, 0.9, 0.33,
               1, 1, 1, 0, 1, 1, 1, 0, "2026-01-01T00:05:00"))
    c.commit(); c.close()
    result = module.momentum_5m_entry()
    assert result["closed_matched_count"] == 1
    assert result["current_matched"]["total_net_pnl"] == 8
    assert result["momentum"]["total_net_pnl"] == 10
    assert result["delta"]["total_net_pnl"] == 2
    assert result["failure_analytics"]["rsi_ever_seen_rate"] == 0.0  # rsi_pass=0 on the only recorded candle
    assert result["failure_analytics"]["bullish_ever_seen_rate"] == 100.0
    assert result["params"]["min_move_R"] == 0.10 and result["params"]["max_chase_R"] == 0.75
