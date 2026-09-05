import pytest


def _position(module, **overrides):
    values = {
        "symbol": "BTC_USDT", "side": "LONG", "status": "CLOSED",
        "entry": 100.0, "stop": 90.0, "initial_stop": 90.0, "qty": 1.0,
        "remaining_qty": 0.0, "risk_usd": 10.0, "score": 85.0,
        "opened_at": "2026-09-04T08:00:00", "closed_at": "2026-09-04T09:00:00",
        "pnl": 10.0, "fee_paid": 1.0, "tp1_done": 0, "tp2_done": 0,
        "mode": "PAPER", "mae_r": 0.5, "mfe_r": 1.2, "close_reason": "STOP",
    }
    values.update(overrides)
    c = module.db()
    c.execute(f"INSERT INTO positions ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
    c.commit(); c.close()


def test_deploy_boundary_is_strict_and_open_closed_handling(isolated_app):
    module, _ = isolated_app
    boundary = module.ENTRY_QUALITY_FILTER_V1_DEPLOYED_AT
    _position(module, opened_at="2026-09-04T07:45:27", symbol="PRE_USDT")
    _position(module, opened_at=boundary, symbol="EDGE_USDT")
    _position(module, opened_at="2026-09-04T07:45:29", symbol="POST_USDT")
    _position(module, opened_at="2026-09-04T08:00:00", symbol="OPEN_USDT", status="OPEN", closed_at=None, pnl=-1.0)
    result = module.post_filter_analytics()
    assert result["deploy_timestamp"] == boundary + "Z"
    assert result["pre"]["count"] == 2
    assert result["post"]["count"] == 2
    assert result["post"]["open"] == 1 and result["post"]["closed"] == 1


def test_expectancy_profit_factor_drawdown_and_quality_metrics(isolated_app):
    module, _ = isolated_app
    _position(module, pnl=10, fee_paid=1, tp1_done=1, close_reason="STOP", mae_r=.4, mfe_r=1.2)
    _position(module, symbol="ETH_USDT", side="SHORT", pnl=-5, fee_paid=2, tp1_done=1, tp2_done=1,
              close_reason="STOP", opened_at="2026-09-04T08:01:00", mae_r=.8, mfe_r=2.2)
    _position(module, symbol="SOL_USDT", pnl=-10, fee_paid=1, close_reason="STOP",
              opened_at="2026-09-04T08:02:00", mae_r=1, mfe_r=.1)
    p = module.post_filter_analytics()["post"]
    assert p["expectancy"] == pytest.approx(-5 / 3)
    assert p["profit_factor"] == pytest.approx(10 / 15)
    assert p["max_drawdown"] == pytest.approx(15)
    assert p["gross_pnl"] == pytest.approx(-1)
    assert p["fees"] == pytest.approx(4)
    assert p["initial_stop_count"] == 1
    assert p["tp1_reached_count"] == 2 and p["tp2_reached_count"] == 1
    assert p["tp1_then_stop_count"] == 1 and p["tp2_protective_stop_count"] == 1
    assert p["average_mae_r"] == pytest.approx((.4 + .8 + 1) / 3)
    assert p["average_mfe_r"] == pytest.approx((1.2 + 2.2 + .1) / 3)


def test_long_short_and_symbol_splits(isolated_app):
    module, _ = isolated_app
    _position(module, symbol="BTC_USDT", side="LONG", pnl=8)
    _position(module, symbol="BTC_USDT", side="SHORT", pnl=-4, opened_at="2026-09-04T08:01:00")
    result = module.post_filter_analytics()
    assert result["by_side"]["LONG"]["win_rate"] == 100
    assert result["by_side"]["SHORT"]["profit_factor"] == 0
    assert result["by_symbol"]["BTC_USDT"]["count"] == 2
    assert result["by_symbol"]["BTC_USDT"]["expectancy"] == 2


def test_aster_excluded_shadow_metrics(isolated_app):
    module, _ = isolated_app
    rows = [
        {"status": "CLOSED", "symbol": "ASTER_USDT", "net_simulated_pnl": -10,
         "filter_result": "AVOIDED_LOSS", "avoided_loss": 10, "missed_profit": 0},
        {"status": "CLOSED", "symbol": "BTC_USDT", "net_simulated_pnl": 6,
         "filter_result": "MISSED_PROFIT", "avoided_loss": 0, "missed_profit": 6},
        {"status": "OPEN", "symbol": "ETH_USDT", "net_simulated_pnl": -2,
         "filter_result": "OPEN", "avoided_loss": 0, "missed_profit": 0},
    ]
    all_metrics = module._reject_shadow_metrics(rows)
    excluded = module._reject_shadow_metrics([x for x in rows if x["symbol"] != "ASTER_USDT"])
    assert all_metrics == {"closed": 2, "avoided_loss_count": 1, "avoided_loss_usd": 10,
                           "missed_profit_count": 1, "missed_profit_usd": 6,
                           "net_filter_impact": 4, "expectancy": -2}
    assert excluded["closed"] == 1
    assert excluded["net_filter_impact"] == -6
    assert excluded["expectancy"] == 6
