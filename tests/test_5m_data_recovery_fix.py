"""MEXC_GOREV_04: regression tests for the 5m kline lookback-seconds fix (root cause of zero
Momentum/Entry-Timing-Lab candle audits) and the candle-before-live-chase ordering fix.
"""
import asyncio
import json
from datetime import datetime, timedelta

import pytest


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    @property
    def text(self):
        return json.dumps(self._payload)

    def json(self):
        return self._payload


class RecordingKlineClient:
    """Returns a fixed set of rows for every call and records the params it was called with."""

    def __init__(self, rows, status_code=200, malformed=False):
        self.rows = rows
        self.status_code = status_code
        self.malformed = malformed
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.malformed:
            return FakeResponse(200, {"success": True, "code": 0, "data": {"time": []}})
        data = {
            "time": [r[0] for r in self.rows], "open": [r[1] for r in self.rows],
            "high": [r[2] for r in self.rows], "low": [r[3] for r in self.rows],
            "close": [r[4] for r in self.rows], "vol": [r[5] for r in self.rows],
        }
        return FakeResponse(self.status_code, {"success": True, "code": 0, "data": data})


def flat_rows(end_ts, count, close=100.0, vol=100.0):
    start_ts = end_ts - (count - 1) * 300
    return [[start_ts + i * 300, close - 0.01, close + 0.01, close - 0.01, close, vol] for i in range(count)]


def _configure(monkeypatch, module):
    monkeypatch.setattr(module, "MEXC_MIN_REQUEST_INTERVAL", 0)
    monkeypatch.setattr(module.random, "uniform", lambda low, high: 0.0)
    # _RESEARCH_ERROR_LOG_DEDUP is a process-global dict (by design, so it survives across scan
    # cycles in production); conftest's isolated_app fixture resets it per test.


# ---------------------------------------------------------------------------
# Root cause: TF_SECONDS / klines() 5m mapping
# ---------------------------------------------------------------------------

def test_tf_seconds_has_an_entry_for_every_supported_timeframe(isolated_app):
    module, _ = isolated_app
    assert set(module.TF_SECONDS) == set(module.TF)
    assert module.TF_SECONDS["5m"] == 300


def test_klines_5m_no_longer_raises_keyerror(isolated_app, monkeypatch):
    module, _ = isolated_app
    _configure(monkeypatch, module)
    rows = flat_rows(datetime.now().timestamp() - 310, 40)
    client = RecordingKlineClient(rows)
    result = asyncio.run(module.klines(client, "ALT_USDT", "5m", 40))
    assert len(result) == 40
    assert client.calls[0]["params"]["interval"] == module.TF["5m"]


@pytest.mark.parametrize("tf", ["15m", "1h", "4h"])
def test_klines_15m_1h_4h_regression_unchanged(isolated_app, monkeypatch, tf):
    module, _ = isolated_app
    _configure(monkeypatch, module)
    rows = flat_rows(datetime.now().timestamp() - module.TF_SECONDS[tf], 40)
    client = RecordingKlineClient(rows)
    result = asyncio.run(module.klines(client, "ALT_USDT", tf, 40))
    assert len(result) == 40
    called_params = client.calls[0]["params"]
    assert called_params["interval"] == module.TF[tf]
    expected_start = int(datetime.now().timestamp()) - module.TF_SECONDS[tf] * (40 + 5)
    assert called_params["start"] == pytest.approx(expected_start, abs=2)


def test_klines_unsupported_timeframe_raises_clear_error(isolated_app, monkeypatch):
    module, _ = isolated_app
    _configure(monkeypatch, module)
    client = RecordingKlineClient([])
    with pytest.raises(ValueError, match="3m"):
        asyncio.run(module.klines(client, "ALT_USDT", "3m", 40))
    assert client.calls == []  # unsupported timeframe must fail before any network call


def test_klines_malformed_data_raises_runtime_error_not_silent(isolated_app, monkeypatch):
    module, _ = isolated_app
    _configure(monkeypatch, module)
    client = RecordingKlineClient([], malformed=True)
    with pytest.raises(RuntimeError):
        asyncio.run(module.klines(client, "ALT_USDT", "5m", 40))


# ---------------------------------------------------------------------------
# Momentum ordering fix: completed candles evaluated before the live-price chase/timeout fallback
# ---------------------------------------------------------------------------

def _insert_momentum_candidate(module, position_id, created_minutes_ago=10):
    created = (datetime.now() - timedelta(minutes=created_minutes_ago)).isoformat(timespec="seconds")
    c = module.db()
    m = {"15m": {"price": 100.0, "atr": 1.0}}  # no '5m' key -> original_5m_rsi baseline is None
    module._insert_momentum_5m_candidate(c, position_id, "ALT_USDT", "LONG", created, 90, m)
    c.commit()
    row = dict(c.execute("SELECT * FROM momentum_5m_entry_candidates WHERE current_position_id=?", (position_id,)).fetchone())
    c.close()
    return row


def test_momentum_manage_confirms_on_completed_candle_despite_deep_live_chase_price(isolated_app, insert_position, monkeypatch):
    """The core ordering-bug regression: before MEXC_GOREV_04, a live price already past +0.75R would
    terminal-chase the candidate BEFORE candles were ever fetched/evaluated, so a perfectly good
    completed candle that already confirmed would never be looked at. Confirming here (using the
    candle's own close, not the stale-in-spirit live price) proves the fix."""
    module, _ = isolated_app
    position_id = insert_position(symbol="ALT_USDT", entry=100.0)
    row = _insert_momentum_candidate(module, position_id)
    now = datetime.now()
    rows = flat_rows(now.timestamp() - 610, 39, close=99.5)  # 39 "old" candles, all before candidate_created_at
    last_open = now.timestamp() - 310  # opens after candidate_created_at (10 min ago) AND already completed
    rows.append([last_open, 100.2, 100.35, 100.15, 100.30, 100.0])  # bullish, > prev_close(99.5), +0.2R move
    client = RecordingKlineClient(rows)
    module.state["live_prices"] = {"ALT_USDT": 108.0}  # deep chase price - must NOT win if a candle confirms
    _configure(monkeypatch, module)
    asyncio.run(module.manage_momentum_5m_candidates(client))
    c = module.db()
    updated = dict(c.execute("SELECT * FROM momentum_5m_entry_candidates WHERE id=?", (row["id"],)).fetchone())
    evals = c.execute("SELECT COUNT(*) FROM momentum_5m_entry_candle_evaluations WHERE candidate_id=?", (row["id"],)).fetchone()[0]
    c.close()
    assert updated["confirmation_status"] == "CONFIRMED"
    assert updated["confirmation_price"] == pytest.approx(100.30)  # from the candle, not the live 108
    assert evals == 1
    assert module.state.get("momentum_5m_entry_error") is None


def test_momentum_manage_falls_back_to_live_chase_when_no_completed_candle_available(isolated_app, insert_position, monkeypatch):
    """With too few candles to evaluate (still a successful, non-erroring klines() call - the root
    cause fix means this no longer raises), the candidate correctly falls through to the live-price
    chase fallback - proving the fallback still works after the reorder."""
    module, _ = isolated_app
    position_id = insert_position(symbol="ALT_USDT", entry=100.0)
    row = _insert_momentum_candidate(module, position_id)
    rows = flat_rows(datetime.now().timestamp() - 310, 10)  # <26 candles: _momentum_5m_completed_candles returns []
    client = RecordingKlineClient(rows)
    module.state["live_prices"] = {"ALT_USDT": 108.0}
    _configure(monkeypatch, module)
    asyncio.run(module.manage_momentum_5m_candidates(client))
    status = module.db().execute("SELECT confirmation_status FROM momentum_5m_entry_candidates WHERE id=?", (row["id"],)).fetchone()[0]
    assert status == "MOMENTUM_CHASED_EXPIRED"
    assert module.state.get("momentum_5m_entry_error") is None


def test_momentum_manage_data_error_leaves_candidate_pending_and_records_error(isolated_app, insert_position, monkeypatch):
    module, _ = isolated_app
    position_id = insert_position(symbol="ALT_USDT", entry=100.0)
    row = _insert_momentum_candidate(module, position_id)
    client = RecordingKlineClient([], malformed=True)
    module.state["live_prices"] = {"ALT_USDT": 108.0}  # would chase if the ordering bug reappeared
    _configure(monkeypatch, module)
    asyncio.run(module.manage_momentum_5m_candidates(client))
    status = module.db().execute("SELECT confirmation_status FROM momentum_5m_entry_candidates WHERE id=?", (row["id"],)).fetchone()[0]
    assert status == "PENDING"
    assert "RuntimeError" in (module.state.get("momentum_5m_entry_error") or "")
    assert module.state.get("momentum_5m_entry_last_error_at") is not None


def test_momentum_error_log_is_deduped_not_spammed(isolated_app, insert_position, monkeypatch):
    module, _ = isolated_app
    position_id = insert_position(symbol="ALT_USDT", entry=100.0)
    _insert_momentum_candidate(module, position_id)
    client = RecordingKlineClient([], malformed=True)
    _configure(monkeypatch, module)
    logged = []
    monkeypatch.setattr(module, "log", lambda msg, level="INFO": logged.append((msg, level)))
    asyncio.run(module.manage_momentum_5m_candidates(client))
    asyncio.run(module.manage_momentum_5m_candidates(client))
    warn_logs = [x for x in logged if x[1] == "WARN"]
    assert len(warn_logs) == 1  # second failure within the dedup window must not log again


def test_momentum_success_clears_a_previous_error(isolated_app, insert_position, monkeypatch):
    module, _ = isolated_app
    position_id = insert_position(symbol="ALT_USDT", entry=100.0)
    _insert_momentum_candidate(module, position_id)
    _configure(monkeypatch, module)
    bad_client = RecordingKlineClient([], malformed=True)
    asyncio.run(module.manage_momentum_5m_candidates(bad_client))
    assert module.state.get("momentum_5m_entry_error") is not None
    good_client = RecordingKlineClient(flat_rows(datetime.now().timestamp() - 310, 10))
    asyncio.run(module.manage_momentum_5m_candidates(good_client))
    assert module.state.get("momentum_5m_entry_error") is None
    assert module.state.get("momentum_5m_entry_last_success_at") is not None


def test_momentum_completed_candles_excludes_candles_at_or_after_expiry(isolated_app):
    module, _ = isolated_app
    created = datetime.now() - timedelta(minutes=10)
    expires = created + timedelta(minutes=30)
    rows = flat_rows((created + timedelta(minutes=5)).timestamp(), 30)
    # Force the last candle to open exactly at/after expiry.
    rows[-1][0] = expires.timestamp()
    candles = module._momentum_5m_completed_candles(
        rows, created.isoformat(timespec="seconds"), now=expires + timedelta(minutes=1),
        until_iso=expires.isoformat(timespec="seconds"),
    )
    assert all(c["opened_at"] < expires.timestamp() for c in candles)


# ---------------------------------------------------------------------------
# Entry Timing Lab (CONFIRMED_5M_ENTRY_V1): same ordering fix, same 5m fetch fix
# ---------------------------------------------------------------------------

def _insert_timing_candidate(module, position_id, created_minutes_ago=10):
    created = (datetime.now() - timedelta(minutes=created_minutes_ago)).isoformat(timespec="seconds")
    score = {"rsi_value": 54, "support_distance_pct": 1, "resistance_distance_pct": 2,
              "trend_15m": 1, "trend_1h": 1, "trend_4h": 1}
    regime = {"bullish_pct": 60, "bearish_pct": 10, "btc_15m_volume_ratio": 1, "eth_15m_volume_ratio": 1}
    m = {"15m": {"price": 100.0, "atr": 1.0}}  # no '5m' -> baseline RSI None
    c = module.db()
    module._insert_entry_timing_candidate(c, position_id, "ALT_USDT", "LONG", created, 90, score, regime, m)
    c.commit()
    row = dict(c.execute("SELECT * FROM entry_timing_lab_candidates WHERE current_position_id=?", (position_id,)).fetchone())
    c.close()
    return row


def test_entry_timing_manage_confirms_on_completed_candle_despite_deep_live_chase_price(isolated_app, insert_position, monkeypatch):
    module, _ = isolated_app
    position_id = insert_position(symbol="ALT_USDT", entry=100.0)
    row = _insert_timing_candidate(module, position_id)
    now = datetime.now()
    rows = flat_rows(now.timestamp() - 610, 39, close=99.5)
    rows.append([now.timestamp() - 310, 100.2, 100.35, 100.15, 100.30, 100.0])
    client = RecordingKlineClient(rows)
    module.state["live_prices"] = {"ALT_USDT": 108.0}
    _configure(monkeypatch, module)
    asyncio.run(module.manage_entry_timing_candidates(client))
    status = module.db().execute("SELECT confirmation_status FROM entry_timing_lab_candidates WHERE id=?", (row["id"],)).fetchone()[0]
    assert status == "OPEN"  # entry_timing_lab's CONFIRMED action moves status straight to OPEN
    assert module.state.get("entry_timing_lab_error") is None


def test_entry_timing_manage_data_error_leaves_candidate_pending(isolated_app, insert_position, monkeypatch):
    module, _ = isolated_app
    position_id = insert_position(symbol="ALT_USDT", entry=100.0)
    row = _insert_timing_candidate(module, position_id)
    client = RecordingKlineClient([], malformed=True)
    module.state["live_prices"] = {"ALT_USDT": 108.0}
    _configure(monkeypatch, module)
    asyncio.run(module.manage_entry_timing_candidates(client))
    status = module.db().execute("SELECT confirmation_status FROM entry_timing_lab_candidates WHERE id=?", (row["id"],)).fetchone()[0]
    assert status == "PENDING"
    assert "RuntimeError" in (module.state.get("entry_timing_lab_error") or "")


# ---------------------------------------------------------------------------
# Pre-fix / post-fix research cohort separation
# ---------------------------------------------------------------------------

def test_momentum_version_breakdown_separates_pre_and_post_fix_cohorts_without_touching_old_rows(isolated_app, insert_position):
    module, _ = isolated_app
    pre_fix_id = insert_position(symbol="ALT_USDT", entry=100.0)
    post_fix_id = insert_position(symbol="BTC_USDT", entry=200.0)
    c = module.db()
    c.execute("""INSERT INTO momentum_5m_entry_candidates(
      model,version,current_position_id,symbol,side,candidate_created_at,expires_at,
      original_current_entry_price,original_atr,confirmation_status,created_at,updated_at
     ) VALUES('MOMENTUM_5M_ENTRY','MOMENTUM_5M_ENTRY_V1',?,'ALT_USDT','LONG',
      '2026-09-10T00:00:00','2026-09-10T00:30:00',100,1,'NO_MOMENTUM_TIMEOUT',
      '2026-09-10T00:00:00','2026-09-10T00:30:00')""", (pre_fix_id,))
    c.execute("""INSERT INTO momentum_5m_entry_candidates(
      model,version,current_position_id,symbol,side,candidate_created_at,expires_at,
      original_current_entry_price,original_atr,confirmation_status,created_at,updated_at
     ) VALUES('MOMENTUM_5M_ENTRY',?,?,'BTC_USDT','LONG',
      '2026-09-19T00:00:00','2026-09-19T00:30:00',200,1,'PENDING',
      '2026-09-19T00:00:00','2026-09-19T00:00:00')""", (module.MOMENTUM_5M_ENTRY_VERSION, post_fix_id))
    c.commit(); c.close()
    result = module.momentum_5m_entry()
    breakdown = result["version_breakdown"]
    assert breakdown["MOMENTUM_5M_ENTRY_V1"] == {"total": 1, "confirmed": 0, "pending": 0, "terminal_unconfirmed": 1}
    assert breakdown[module.MOMENTUM_5M_ENTRY_VERSION] == {"total": 1, "confirmed": 0, "pending": 1, "terminal_unconfirmed": 0}
    assert result["total_candidates"] == 2  # the pre-fix row is preserved, not dropped or rewritten
