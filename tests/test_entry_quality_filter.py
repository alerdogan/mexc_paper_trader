import json

import pytest


def market(side="LONG", rsi=60.0, volume_score=15.0, trend_15m=1):
    return {
        "signal": side,
        "long_score": 85,
        "short_score": 85 if side == "SHORT" else 0,
        "15m": {
            "price": 100.0,
            "atr": 1.0,
            "trend": trend_15m,
            "rsi": rsi,
            "volume_ratio": 1.0,
            "support": 98.0,
            "resistance": 103.0,
        },
        "1h": {"trend": 1},
        "4h": {"trend": 1},
        "score_details": [
            {"component": "volume", "long": volume_score, "short": volume_score},
            {"component": "trend", "timeframe": "15m", "long": 10, "short": 0},
        ],
    }


def universe(coin, btc_ratio=1.0, eth_ratio=1.0):
    return {
        "ALT_USDT": coin,
        "BTC_USDT": {"15m": {"volume_ratio": btc_ratio, "trend": 1}},
        "ETH_USDT": {"15m": {"volume_ratio": eth_ratio, "trend": 1}},
    }


@pytest.mark.parametrize(
    ("coin", "btc_ratio", "eth_ratio", "reason"),
    [
        (market(rsi=64), 1.0, 1.0, "COIN_RSI_GTE_64"),
        (market(), 0.54, 1.0, "BTC_15M_VOLUME_RATIO_LT_0_55"),
        (market(), 1.0, 0.54, "ETH_15M_VOLUME_RATIO_LT_0_55"),
        (market(volume_score=0), 1.0, 1.0, "COIN_VOLUME_SCORE_EQ_0"),
    ],
)
def test_long_filter_rejects_and_persists_research_snapshots(
    isolated_app, coin, btc_ratio, eth_ratio, reason
):
    module, _ = isolated_app
    current = universe(coin, btc_ratio, eth_ratio)

    result = module.process_paper_signal(
        "ALT_USDT", coin, current, {}, list(current)
    )

    connection = module.db()
    rejection = connection.execute(
        "SELECT * FROM entry_filter_rejections"
    ).fetchone()
    position_count = connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    lab_count = connection.execute(
        "SELECT COUNT(*) FROM strategy_lab_experiments"
    ).fetchone()[0]
    connection.close()

    assert result == "ENTRY_FILTER_REJECTED"
    assert position_count == 0
    assert lab_count == 0
    assert rejection["filter_version"] == "ENTRY_QUALITY_FILTER_V1"
    assert rejection["status"] == "ENTRY_FILTER_REJECTED"
    assert reason in json.loads(rejection["reasons_json"])
    assert json.loads(rejection["score_snapshot_json"])["trend_15m"] == coin["15m"]["trend"]
    assert "regime_classification" in json.loads(rejection["market_regime_snapshot_json"])


def test_bearish_15m_alone_does_not_reject_long(isolated_app):
    module, _ = isolated_app
    coin = market(trend_15m=-1)
    current = universe(coin)

    result = module.process_paper_signal("ALT_USDT", coin, current, {}, list(current))

    connection = module.db()
    assert connection.execute("SELECT COUNT(*) FROM entry_filter_rejections").fetchone()[0] == 0
    position = connection.execute("SELECT side FROM positions").fetchone()
    connection.close()
    assert result is None
    assert position["side"] == "LONG"


def test_short_entry_bypasses_long_quality_filter(isolated_app):
    module, _ = isolated_app
    coin = market(side="SHORT", rsi=70, volume_score=0)
    current = universe(coin, btc_ratio=0.1, eth_ratio=0.1)

    module.process_paper_signal("ALT_USDT", coin, current, {}, list(current))

    connection = module.db()
    assert connection.execute("SELECT COUNT(*) FROM entry_filter_rejections").fetchone()[0] == 0
    position = connection.execute("SELECT side FROM positions").fetchone()
    connection.close()
    assert position["side"] == "SHORT"


def test_rejection_summary_counts_each_reason(isolated_app):
    module, _ = isolated_app
    coin = market(rsi=70, volume_score=0)
    current = universe(coin, btc_ratio=0.1, eth_ratio=0.1)
    module.process_paper_signal("ALT_USDT", coin, current, {}, list(current))

    summary = module.entry_filter_rejections()

    assert summary["total"] == 1
    assert summary["reason_counts"] == {
        "COIN_RSI_GTE_64": 1,
        "BTC_15M_VOLUME_RATIO_LT_0_55": 1,
        "ETH_15M_VOLUME_RATIO_LT_0_55": 1,
        "COIN_VOLUME_SCORE_EQ_0": 1,
    }


def test_continuous_rejected_setup_is_one_opportunity_and_rearms_after_signal_break(
    isolated_app,
):
    module, _ = isolated_app
    coin = market(rsi=64)
    current = universe(coin)

    module.process_paper_signal("ALT_USDT", coin, current, {}, list(current))
    module.process_paper_signal("ALT_USDT", coin, current, current, list(current))
    coin["score_details"][0]["long"] = 0
    module.process_paper_signal("ALT_USDT", coin, current, current, list(current))

    connection = module.db()
    rows = connection.execute(
        "SELECT reasons_json FROM entry_filter_rejections ORDER BY id"
    ).fetchall()
    connection.close()
    assert len(rows) == 1
    assert json.loads(rows[0]["reasons_json"]) == [
        "COIN_RSI_GTE_64",
        "COIN_VOLUME_SCORE_EQ_0",
    ]

    coin["signal"] = "BEKLE"
    module.process_paper_signal("ALT_USDT", coin, current, current, list(current))
    coin["signal"] = "LONG"
    module.process_paper_signal("ALT_USDT", coin, current, current, list(current))

    connection = module.db()
    assert connection.execute("SELECT COUNT(*) FROM entry_filter_rejections").fetchone()[0] == 2
    connection.close()
