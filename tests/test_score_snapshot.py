import json

import pytest


def signal_market():
    return {
        "15m": {
            "price": 100.0,
            "atr": 1.0,
            "trend": 1,
            "rsi": 60.0,
            "volume_ratio": 1.5,
            "support": 98.5,
            "resistance": 101.0,
            "fib_near": True,
            "nearest_fib": 99.8,
            "fib_distance": 0.2,
            "fib_distance_pct": 0.2,
        },
        "1h": {"trend": 1},
        "4h": {"trend": 1},
    }


@pytest.mark.parametrize(
    ("trends", "rsi", "volume", "room", "fib_near", "expected"),
    [
        ((1, 1, 1), 60.0, 1.5, 5.0, True, (100, 15)),
        ((-1, -1, -1), 40.0, 1.5, 5.0, True, (15, 100)),
        ((0, 0, 0), 50.0, 1.0, 1.0, False, (0, 0)),
        ((1, -1, 0), 60.0, 1.5, 5.0, True, (65, 55)),
    ],
)
def test_score_totals_remain_unchanged(
    isolated_app, trends, rsi, volume, room, fib_near, expected
):
    module, _ = isolated_app
    trend_4h, trend_1h, trend_15m = trends
    market = {
        "15m": {
            "price": 100.0,
            "atr": 1.0,
            "trend": trend_15m,
            "rsi": rsi,
            "volume_ratio": volume,
            "support": 100.0 - room,
            "resistance": 100.0 + room,
            "fib_near": fib_near,
        },
        "1h": {"trend": trend_1h},
        "4h": {"trend": trend_4h},
    }

    long_score, short_score, _ = module.scores(market)

    assert (long_score, short_score) == expected


def test_score_breakdown_snapshot_is_complete_and_frozen(isolated_app):
    module, _ = isolated_app
    market = signal_market()
    long_score, short_score, details = module.scores(market)

    assert (long_score, short_score) == (85, 0)
    market["score_details"] = details
    module.paper_open("BTC_USDT", "LONG", market, long_score)

    connection = module.db()
    position = connection.execute(
        "SELECT id,score FROM positions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    snapshot = connection.execute(
        "SELECT * FROM position_score_snapshots WHERE position_id=?",
        (position["id"],),
    ).fetchone()
    lab_link = connection.execute(
        """SELECT s.position_id FROM strategy_lab_experiments e
        JOIN position_score_snapshots s ON s.position_id=e.source_position_id
        WHERE e.source_position_id=?""",
        (position["id"],),
    ).fetchone()
    connection.close()

    saved = dict(snapshot)
    assert saved["score_version"] == "1.0"
    assert position["score"] == 85
    assert saved["trend_score"] == 45
    assert saved["trend_4h_score"] == 20
    assert saved["trend_1h_score"] == 15
    assert saved["trend_15m_score"] == 10
    assert saved["rsi_score"] == 15
    assert saved["volume_score"] == 15
    assert saved["support_resistance_score"] == 0
    assert saved["fibonacci_score"] == 10
    assert saved["total_score"] == 85
    assert saved["rsi_value"] == 60.0
    assert saved["volume_ratio"] == 1.5
    assert saved["support_distance"] == 1.5
    assert saved["resistance_distance"] == 1.0
    assert saved["fibonacci_level"] == 99.8
    assert saved["fibonacci_distance"] == 0.2
    assert (saved["trend_15m"], saved["trend_1h"], saved["trend_4h"]) == (1, 1, 1)
    assert {item["component"] for item in json.loads(saved["score_details_json"])} == {
        "trend",
        "rsi",
        "volume",
        "support_resistance",
        "fibonacci",
    }
    assert lab_link["position_id"] == position["id"]

    market["15m"]["rsi"] = 1.0
    market["score_details"].clear()
    connection = module.db()
    unchanged = dict(
        connection.execute(
            "SELECT * FROM position_score_snapshots WHERE position_id=?",
            (position["id"],),
        ).fetchone()
    )
    connection.close()
    assert unchanged == saved


def test_existing_position_may_have_no_score_snapshot(isolated_app, insert_position):
    module, _ = isolated_app
    position_id = insert_position(status="CLOSED", closed_at="2026-01-02T00:00:00")
    connection = module.db()
    row = connection.execute(
        """SELECT s.position_id FROM positions p
        LEFT JOIN position_score_snapshots s ON s.position_id=p.id WHERE p.id=?""",
        (position_id,),
    ).fetchone()
    connection.close()

    assert row["position_id"] is None
