import json

import pytest

from test_entry_quality_filter import market, universe


def score(**overrides):
    data = {
        "signal_side": "LONG", "rsi_value": 54, "volume_ratio": 1.2,
        "resistance_distance_pct": 2.0, "support_distance_pct": 1.0,
        "trend_15m": 1, "trend_1h": 1, "trend_4h": 1,
    }
    data.update(overrides)
    return data


def regime(**overrides):
    data = {"bullish_pct": 40, "bearish_pct": 20,
            "btc_15m_volume_ratio": 1.2, "eth_15m_volume_ratio": 1.2}
    data.update(overrides)
    return data


@pytest.mark.parametrize(("snapshot", "market_regime", "rule"), [
    (score(support_distance_pct=3.0), regime(bearish_pct=0), "RULE_A"),
    (score(rsi_value=55), regime(bearish_pct=20), "RULE_B"),
    (score(support_distance_pct=1.5), regime(bearish_pct=20), "RULE_C"),
])
def test_v3_rule_matching(snapshot, market_regime, rule, isolated_app):
    module, _ = isolated_app
    assert module._entry_quality_v3_rules(snapshot, market_regime) == [rule]


def test_v3_multi_rule_duplicate_protection_and_long_only(isolated_app):
    module, _ = isolated_app
    snapshot = score(rsi_value=55, support_distance_pct=3.0)
    market_regime = regime()
    assert module._entry_quality_v3_rules(snapshot, market_regime) == ["RULE_A", "RULE_B", "RULE_C"]
    c = module.db()
    module._insert_entry_quality_v3_research(c, 1, "ALT_USDT", "LONG", "2026-01-01T00:00:00", 90, snapshot, market_regime)
    module._insert_entry_quality_v3_research(c, 1, "ALT_USDT", "LONG", "2026-01-01T00:00:00", 90, snapshot, market_regime)
    module._insert_entry_quality_v3_research(c, 2, "ALT_USDT", "SHORT", "2026-01-01T00:00:00", 90, snapshot, market_regime)
    c.commit()
    rows = c.execute("SELECT * FROM entry_quality_filter_v3_research").fetchall(); c.close()
    assert len(rows) == 1
    assert json.loads(rows[0]["matched_rules_json"]) == ["RULE_A", "RULE_B", "RULE_C"]


def test_v3_only_records_no_v2_opportunities(isolated_app):
    module, _ = isolated_app
    c = module.db()
    no_v2 = score(rsi_value=55, support_distance_pct=3.0)
    module._insert_entry_quality_v3_research(c, 1, "A_USDT", "LONG", "2026-01-01T00:00:00", 90, no_v2, regime())
    v2_match = regime(bullish_pct=60, btc_15m_volume_ratio=.9)
    assert module._entry_quality_v2_rules(no_v2, v2_match) == ["RULE_A"]
    module._insert_entry_quality_v3_research(c, 2, "B_USDT", "LONG", "2026-01-01T00:01:00", 90, no_v2, v2_match)
    c.commit()
    assert c.execute("SELECT COUNT(*) FROM entry_quality_filter_v3_research").fetchone()[0] == 1
    c.close()


def test_v1_rejection_and_current_creation_paths_remain_unchanged(isolated_app):
    module, _ = isolated_app
    rejected = market(rsi=64)
    current = universe(rejected)
    assert module.process_paper_signal("ALT_USDT", rejected, current, {}, list(current)) == "ENTRY_FILTER_REJECTED"
    c = module.db()
    assert c.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM entry_quality_filter_v3_research").fetchone()[0] == 0
    c.close()

    accepted = market(rsi=54)
    current = universe(accepted)
    module.process_paper_signal("ALT_USDT", accepted, current, {}, list(current))
    c = module.db()
    position = c.execute("SELECT * FROM positions").fetchone()
    assert position["side"] == "LONG" and position["status"] == "OPEN"
    assert c.execute("SELECT COUNT(*) FROM entry_quality_filter_v2_research").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM entry_quality_filter_v3_research").fetchone()[0] == 1
    c.close()


def test_any_v3_dedup_no_v3_metrics_and_impact_math(isolated_app):
    module, _ = isolated_app
    rows = [
        {"status": "CLOSED", "final_net_pnl": -10, "close_reason": "STOP", "tp1_hit": 0, "tp2_hit": 0},
        {"status": "CLOSED", "final_net_pnl": 6, "close_reason": "STOP", "tp1_hit": 1, "tp2_hit": 1},
        {"status": "OPEN", "final_net_pnl": None, "close_reason": None, "tp1_hit": 0, "tp2_hit": 0},
    ]
    any_metrics = module._v3_research_metrics(rows)
    no_metrics = module._v3_research_metrics([rows[1]])
    assert any_metrics == module._v3_research_metrics(list({id(x): x for x in rows}.values()))
    assert any_metrics["total"] == 3 and any_metrics["closed"] == 2
    assert any_metrics["total_pnl"] == -4 and any_metrics["expectancy"] == -2
    assert any_metrics["hypothetical_filter_impact"] == 4
    assert no_metrics["total"] == 1 and no_metrics["total_pnl"] == 6


def test_api_any_v3_counts_multi_rule_position_once_and_separates_no_v3(isolated_app):
    module, _ = isolated_app
    c = module.db()
    multi = score(rsi_value=55, support_distance_pct=3.0)
    none = score(rsi_value=54, support_distance_pct=1.0)
    module._insert_entry_quality_v3_research(c, 1, "A_USDT", "LONG", "2026-01-01T00:00:00", 90, multi, regime())
    module._insert_entry_quality_v3_research(c, 2, "B_USDT", "LONG", "2026-01-01T00:01:00", 90, none, regime())
    c.commit(); c.close()
    result = module.entry_quality_filter_v3_research()
    assert result["rules"]["RULE_A"]["total"] == 1
    assert result["rules"]["RULE_B"]["total"] == 1
    assert result["rules"]["RULE_C"]["total"] == 1
    assert result["any_v3_rule"]["total"] == 1
    assert result["no_v3_rule"]["total"] == 1


def test_restart_recovery_syncs_full_result_and_never_backfills_history(isolated_app, insert_position):
    module, _ = isolated_app
    historical_id = insert_position(status="CLOSED", closed_at="2025-01-01T01:00:00", pnl=5)
    module.init_db()
    c = module.db()
    assert c.execute("SELECT COUNT(*) FROM entry_quality_filter_v3_research").fetchone()[0] == 0
    snapshot = score(rsi_value=55, support_distance_pct=3.0)
    module._insert_entry_quality_v3_research(c, historical_id, "ALT_USDT", "LONG", "2026-01-01T00:00:00", 90, snapshot, regime())
    c.execute("UPDATE positions SET pnl=-12,fee_paid=2,tp1_done=1,mae_r=1.2,mfe_r=1.1,close_reason='STOP' WHERE id=?", (historical_id,))
    c.commit(); c.close()
    module.init_db()
    module._sqlite_write_with_retry(module._sync_entry_quality_v3_research_once)
    c = module.db(); row = c.execute("SELECT * FROM entry_quality_filter_v3_research").fetchone(); c.close()
    assert row["status"] == "CLOSED" and row["tp1_hit"] == 1
    assert row["gross_pnl"] == -10 and row["fee"] == 2 and row["final_net_pnl"] == -12


def test_current_position_management_still_uses_existing_transition(isolated_app, insert_position):
    module, _ = isolated_app
    position_id = insert_position()
    module.state["live_prices"] = {"BTC_USDT": 120.0}
    module.manage()
    c = module.db(); row = c.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone(); c.close()
    assert row["tp1_done"] == 1 and row["tp2_done"] == 1
    assert row["remaining_qty"] == pytest.approx(4.0)
