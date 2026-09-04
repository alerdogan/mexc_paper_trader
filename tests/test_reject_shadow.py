import pytest

from test_entry_quality_filter import market, universe


def _reject(module, symbol="ALT_USDT"):
    coin = market(rsi=64)
    current = universe(coin)
    module.process_paper_signal(symbol, coin, current, {}, list(current))


def test_one_shadow_per_continuous_opportunity_and_no_trading_side_effects(isolated_app):
    module, _ = isolated_app
    _reject(module)
    _reject(module)
    connection = module.db()
    assert connection.execute("SELECT COUNT(*) FROM reject_shadow_trades").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM strategy_lab_experiments").fetchone()[0] == 0
    connection.close()


def test_restart_recovery_preserves_open_shadow(isolated_app):
    module, _ = isolated_app
    _reject(module)
    module.init_db()
    module.state["live_prices"]["ALT_USDT"] = 101.0
    module.manage()
    connection = module.db()
    row = connection.execute("SELECT * FROM reject_shadow_trades").fetchone()
    connection.close()
    assert row["status"] == "OPEN"
    assert row["mfe_r"] == pytest.approx(1.0 / 1.5)


@pytest.mark.parametrize(("side", "target"), [("LONG", 110.0), ("SHORT", 90.0)])
def test_current_transition_long_short_tp_math(isolated_app, side, target):
    module, _ = isolated_app
    stop = 90.0 if side == "LONG" else 110.0
    result = module._current_position_transition({
        "side": side, "entry": 100.0, "initial_stop": stop, "stop": stop,
        "qty": 10.0, "remaining_qty": 10.0, "gross_pnl": 0.0,
        "fee_paid": 0.8, "mae": 0.0, "mfe": 0.0, "mae_r": 0.0,
        "mfe_r": 0.0, "tp1_done": 0, "tp2_done": 0,
    }, target)
    assert result["events"] == ["TP1"]
    assert result["remaining_qty"] == pytest.approx(7.0)
    assert result["stop"] == 100.0
    assert result["gross_pnl"] == pytest.approx(30.0)
    assert result["fee_paid"] == pytest.approx(0.8 + target * 3 * 0.0008)


def test_tp1_tp2_then_stop_fee_and_filter_impact(isolated_app):
    module, _ = isolated_app
    _reject(module)
    module.state["live_prices"]["ALT_USDT"] = 103.0
    module.manage()
    connection = module.db()
    row = connection.execute("SELECT * FROM reject_shadow_trades").fetchone()
    assert row["tp1_hit"] == 1 and row["tp2_hit"] == 1
    assert row["remaining_qty"] == pytest.approx(row["qty"] * 0.4)
    assert row["current_stop"] == pytest.approx(101.5)
    module.state["live_prices"]["ALT_USDT"] = 101.5
    module.manage()
    row = connection.execute("SELECT * FROM reject_shadow_trades").fetchone()
    connection.close()
    assert row["status"] == "CLOSED" and row["final_exit_reason"] == "STOP"
    assert row["tp1_hit"] == 1 and row["tp2_hit"] == 1
    assert row["simulated_fee"] > 0
    assert row["net_simulated_pnl"] == pytest.approx(row["gross_simulated_pnl"] - row["simulated_fee"])
    summary = module.entry_filter_rejections()["shadow_summary"]
    assert summary["net_filter_impact"] == pytest.approx(-row["net_simulated_pnl"])


def test_avoided_loss_classification(isolated_app):
    module, _ = isolated_app
    _reject(module)
    module.state["live_prices"]["ALT_USDT"] = 98.5
    module.manage()
    connection = module.db()
    row = connection.execute("SELECT * FROM reject_shadow_trades").fetchone()
    connection.close()
    assert row["filter_result"] == "AVOIDED_LOSS"
    assert row["avoided_loss"] == pytest.approx(-row["net_simulated_pnl"])


def test_missed_profit_classification(isolated_app):
    module, _ = isolated_app
    _reject(module)
    module.state["live_prices"]["ALT_USDT"] = 103.0
    module.manage()
    module.state["live_prices"]["ALT_USDT"] = 101.5
    module.manage()
    connection = module.db()
    row = connection.execute("SELECT * FROM reject_shadow_trades").fetchone()
    connection.close()
    assert row["filter_result"] == "MISSED_PROFIT"
    assert row["missed_profit"] == pytest.approx(row["net_simulated_pnl"])
