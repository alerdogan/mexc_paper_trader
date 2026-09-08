import pytest

from test_entry_quality_filter import market, universe


def test_epoch_starts_at_4000_and_excludes_pre_epoch_history(isolated_app, insert_position):
    module, _ = isolated_app
    epoch = module.active_paper_epoch()
    insert_position(status="CLOSED", pnl=250, fee_paid=4, closed_at="2026-01-02T00:00:00")
    insert_position(symbol="ETH_USDT", pnl=20, fee_paid=1)

    metrics = module.paper_epoch_metrics()

    assert epoch["starting_balance"] == 4000
    assert metrics["current_balance"] == 4000
    assert metrics["realized_pnl"] == 0
    assert metrics["fees"] == 0
    assert module.all_time_realized_pnl() == 270


def test_new_trade_is_linked_and_partial_tp_cash_is_counted_once(isolated_app):
    module, _ = isolated_app
    coin = market(rsi=55)
    current = universe(coin)
    module.process_paper_signal("ALT_USDT", coin, current, {}, list(current))
    connection = module.db()
    position = dict(connection.execute("SELECT * FROM positions").fetchone())
    connection.close()
    assert position["paper_epoch_id"] == module.active_paper_epoch()["id"]

    entry_fee = position["fee_paid"]
    assert module.paper_epoch_metrics()["current_balance"] == pytest.approx(4000 - entry_fee)
    module.state["live_prices"] = {"ALT_USDT": 102.0}
    module.manage()
    updated = module.paper_epoch_metrics()
    stored = module.db().execute("SELECT pnl FROM positions WHERE id=?", (position["id"],)).fetchone()[0]
    assert updated["realized_pnl"] == pytest.approx(stored)
    assert updated["current_balance"] == pytest.approx(4000 + stored)


def test_epoch_survives_restart_without_creating_another(isolated_app):
    module, _ = isolated_app
    before = module.active_paper_epoch()
    module.init_db()
    after = module.active_paper_epoch()
    connection = module.db()
    count = connection.execute("SELECT COUNT(*) FROM paper_epochs").fetchone()[0]
    connection.close()
    assert after["id"] == before["id"]
    assert after["started_at"] == before["started_at"]
    assert count == 1


def test_v1_reject_shadow_and_pass_research_stay_on_expected_sides(isolated_app):
    module, _ = isolated_app
    rejected = market(rsi=64)
    current = universe(rejected)
    assert module.process_paper_signal("ALT_USDT", rejected, current, {}, list(current)) == "ENTRY_FILTER_REJECTED"
    connection = module.db()
    assert connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM reject_shadow_trades").fetchone()[0] == 1
    connection.close()

    passed = market(rsi=55)
    passed["15m"]["resistance"] = 101
    current = universe(passed, btc_ratio=.8, eth_ratio=.7)
    module.process_paper_signal("PASS_USDT", passed, current, {}, list(current))
    connection = module.db()
    position = connection.execute("SELECT * FROM positions").fetchone()
    assert position["side"] == "LONG"
    assert connection.execute("SELECT COUNT(*) FROM entry_quality_filter_v2_research").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM entry_quality_filter_v3_research").fetchone()[0] == 0
    connection.close()


def test_short_behavior_is_unchanged_and_assigned_to_epoch(isolated_app):
    module, _ = isolated_app
    short = market(side="SHORT", rsi=70, volume_score=0)
    current = universe(short, btc_ratio=.1, eth_ratio=.1)
    module.process_paper_signal("ALT_USDT", short, current, {}, list(current))
    connection = module.db()
    position = connection.execute("SELECT * FROM positions").fetchone()
    assert position["side"] == "SHORT"
    assert position["paper_epoch_id"] == module.active_paper_epoch()["id"]
    assert connection.execute("SELECT COUNT(*) FROM entry_filter_rejections").fetchone()[0] == 0
    connection.close()
