def market(trend, price=100.0):
    return {
        "15m": {
            "price": price,
            "ema20": price + 1 if trend < 0 else price - 1,
            "ema50": price + 2 if trend < 0 else price - 2,
            "trend": trend,
            "rsi": 40.0 if trend < 0 else 60.0,
            "volume_ratio": 1.4,
            "atr": 1.0,
            "support": price - 3,
            "resistance": price + 3,
            "fib_near": False,
        },
        "1h": {"trend": trend},
        "4h": {"trend": trend},
        "score_details": [],
    }


def test_bearish_regime_snapshot_is_saved_with_new_position(isolated_app):
    module, _ = isolated_app
    current = {
        "BTC_USDT": market(-1, 100.0),
        "ETH_USDT": market(-1, 50.0),
        "ALT_USDT": market(-1, 25.0),
    }
    previous = {symbol: market(-1, data["15m"]["price"]) for symbol, data in current.items()}
    snapshot = module.market_regime_snapshot(
        current, previous, list(current), "SHORT", "2026-01-01T00:00:00"
    )

    module.paper_open("ALT_USDT", "SHORT", current["ALT_USDT"], 90, snapshot)

    connection = module.db()
    position = connection.execute("SELECT id,opened_at FROM positions").fetchone()
    saved = dict(
        connection.execute(
            "SELECT * FROM market_regime_snapshots WHERE source_position_id=?",
            (position["id"],),
        ).fetchone()
    )
    connection.close()

    assert saved["captured_at"] == position["opened_at"]
    assert saved["regime_classification"] == "BEARISH"
    assert saved["market_alignment"] == "ALIGNED"
    assert (saved["bullish_count"], saved["bearish_count"], saved["neutral_count"]) == (0, 3, 0)
    assert saved["bearish_pct"] == 100.0
    assert saved["trend_changed_15m_pct"] == 0.0
    assert (saved["btc_trend_15m"], saved["btc_trend_1h"], saved["btc_trend_4h"]) == (-1, -1, -1)
    assert (saved["eth_trend_15m"], saved["eth_trend_1h"], saved["eth_trend_4h"]) == (-1, -1, -1)
    assert saved["btc_15m_momentum"] == "BEARISH_STACK"

    current["BTC_USDT"]["15m"]["trend"] = 1
    connection = module.db()
    unchanged = dict(
        connection.execute(
            "SELECT * FROM market_regime_snapshots WHERE source_position_id=?",
            (position["id"],),
        ).fetchone()
    )
    connection.close()
    assert unchanged == saved


def test_direction_change_marks_reversal_risk(isolated_app):
    module, _ = isolated_app
    symbols = ["BTC_USDT", "ETH_USDT", "A_USDT", "B_USDT"]
    previous = {symbol: market(-1) for symbol in symbols}
    current = {symbol: market(1) for symbol in symbols}

    snapshot = module.market_regime_snapshot(
        current, previous, symbols, "SHORT", "2026-01-01T00:00:00"
    )

    assert snapshot["trend_changed_15m_count"] == 4
    assert snapshot["trend_changed_15m_denominator"] == 4
    assert snapshot["trend_changed_15m_pct"] == 100.0
    assert snapshot["regime_classification"] == "REVERSAL_RISK"
    assert snapshot["market_alignment"] == "UNCERTAIN"


def test_existing_positions_are_not_backfilled(isolated_app, insert_position):
    module, _ = isolated_app
    position_id = insert_position(status="CLOSED", closed_at="2026-01-02T00:00:00")

    connection = module.db()
    snapshot = connection.execute(
        "SELECT 1 FROM market_regime_snapshots WHERE source_position_id=?", (position_id,)
    ).fetchone()
    connection.close()

    assert snapshot is None
