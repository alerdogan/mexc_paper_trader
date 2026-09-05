import json

import pytest

from test_entry_quality_filter import market, universe


def _score(**overrides):
    data = {
        "signal_side": "LONG", "rsi_value": 55, "volume_ratio": 1.2,
        "resistance_distance_pct": 2.0, "support_distance_pct": 3.0,
        "trend_15m": 1, "trend_1h": 1, "trend_4h": 1,
    }
    data.update(overrides)
    return data


def _regime(**overrides):
    data = {"bullish_pct": 60, "bearish_pct": 20,
            "btc_15m_volume_ratio": 1.2, "eth_15m_volume_ratio": 1.2}
    data.update(overrides)
    return data


@pytest.mark.parametrize(("score", "regime", "rule"), [
    (_score(), _regime(btc_15m_volume_ratio=.99), "RULE_A"),
    (_score(resistance_distance_pct=1.49), _regime(), "RULE_B"),
    (_score(), _regime(eth_15m_volume_ratio=.99), "RULE_C"),
])
def test_rule_matching(score, regime, rule, isolated_app):
    module, _ = isolated_app
    assert module._entry_quality_v2_rules(score, regime) == [rule]


def test_multi_rule_single_record_and_long_only(isolated_app):
    module, _ = isolated_app
    score = _score(resistance_distance_pct=1.0)
    regime = _regime(btc_15m_volume_ratio=.8, eth_15m_volume_ratio=.7)
    assert module._entry_quality_v2_rules(score, regime) == ["RULE_A", "RULE_B", "RULE_C"]
    c = module.db()
    module._insert_entry_quality_v2_research(c, 1, "ALT_USDT", "LONG", "2026-01-01T00:00:00", 90, score, regime)
    module._insert_entry_quality_v2_research(c, 1, "ALT_USDT", "LONG", "2026-01-01T00:00:00", 90, score, regime)
    module._insert_entry_quality_v2_research(c, 2, "ALT_USDT", "SHORT", "2026-01-01T00:00:00", 90, score, regime)
    c.commit()
    rows = c.execute("SELECT * FROM entry_quality_filter_v2_research").fetchall(); c.close()
    assert len(rows) == 1
    assert json.loads(rows[0]["matched_rules_json"]) == ["RULE_A", "RULE_B", "RULE_C"]


def test_v1_reject_unchanged_and_current_position_creation_unchanged(isolated_app):
    module, _ = isolated_app
    rejected = market(rsi=64)
    current = universe(rejected)
    assert module.process_paper_signal("ALT_USDT", rejected, current, {}, list(current)) == "ENTRY_FILTER_REJECTED"
    c = module.db()
    assert c.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM entry_quality_filter_v2_research").fetchone()[0] == 0
    c.close()

    accepted = market(rsi=55)
    accepted["15m"]["resistance"] = 101.0
    current = universe(accepted, btc_ratio=.8, eth_ratio=.7)
    module.process_paper_signal("ALT_USDT", accepted, current, {}, list(current))
    c = module.db()
    position = c.execute("SELECT * FROM positions").fetchone()
    v2 = c.execute("SELECT * FROM entry_quality_filter_v2_research").fetchone(); c.close()
    assert position["side"] == "LONG" and position["status"] == "OPEN"
    assert v2["position_id"] == position["id"]


def test_metrics_math_counts_multi_rule_once_in_any(isolated_app):
    module, _ = isolated_app
    rows = [
        {"status": "CLOSED", "final_net_pnl": -10, "close_reason": "STOP", "tp1_hit": 0, "tp2_hit": 0},
        {"status": "CLOSED", "final_net_pnl": 6, "close_reason": "STOP", "tp1_hit": 1, "tp2_hit": 1},
        {"status": "OPEN", "final_net_pnl": None, "close_reason": None, "tp1_hit": 0, "tp2_hit": 0},
    ]
    result = module._v2_research_metrics(rows)
    assert result["total"] == 3 and result["closed"] == 2
    assert result["wins"] == 1 and result["losses"] == 1
    assert result["initial_stop_count"] == 1 and result["initial_stop_rate"] == 50
    assert result["tp1_rate"] == 50 and result["tp2_rate"] == 50
    assert result["total_pnl"] == -4 and result["expectancy"] == -2
    assert result["hypothetical_filter_impact"] == 4


def test_restart_recovery_preserves_and_syncs_record(isolated_app):
    module, _ = isolated_app
    accepted = market(rsi=55)
    current = universe(accepted)
    module.process_paper_signal("ALT_USDT", accepted, current, {}, list(current))
    module.init_db()
    c = module.db()
    position_id = c.execute("SELECT id FROM positions").fetchone()[0]
    c.execute("UPDATE positions SET status='CLOSED',pnl=-12,tp1_done=1,tp2_done=0,mae_r=1.2,mfe_r=1.1,close_reason='STOP',closed_at='2026-01-01T01:00:00' WHERE id=?", (position_id,))
    c.commit(); c.close()
    module._sqlite_write_with_retry(module._sync_entry_quality_v2_research_once)
    c = module.db(); row = c.execute("SELECT * FROM entry_quality_filter_v2_research").fetchone(); c.close()
    assert row["status"] == "CLOSED" and row["tp1_hit"] == 1
    assert row["final_net_pnl"] == -12 and row["close_reason"] == "STOP"
