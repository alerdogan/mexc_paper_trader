import asyncio


def position_rows(module):
    connection = module.db()
    rows = [dict(x) for x in connection.execute("SELECT * FROM positions ORDER BY id")]
    connection.close()
    return rows


def lab_rows(module):
    connection = module.db()
    rows = [dict(x) for x in connection.execute("SELECT * FROM strategy_lab_runs ORDER BY id")]
    connection.close()
    return rows


def simple_market(price=100.0):
    return {"15m": {"price": price, "atr": 10.0}}


def test_bot_stop_blocks_only_new_entries(isolated_app, insert_position):
    module, _ = isolated_app
    existing_id = insert_position(symbol="BTC_USDT")
    module.state["running"] = True

    result = module.stop()
    module.paper_open("ETH_USDT", "LONG", simple_market(), 90)

    rows = position_rows(module)
    assert result["ok"] is True
    assert module.state["running"] is False
    assert module.state["entry_paused"] is True
    assert [(x["id"], x["status"]) for x in rows] == [(existing_id, "OPEN")]

    module.state["live_prices"]["BTC_USDT"] = 89.0
    module.manage()
    assert position_rows(module)[0]["status"] == "CLOSED"


def test_close_all_is_idempotent_preserves_bot_and_strategy_lab(
    isolated_app, insert_position, monkeypatch
):
    module, _ = isolated_app
    btc_id = insert_position(symbol="BTC_USDT", side="LONG")
    eth_id = insert_position(symbol="ETH_USDT", side="SHORT")
    module.strategy_lab_create(btc_id)
    module.strategy_lab_create(eth_id)
    lab_before = lab_rows(module)
    module.state["running"] = True

    async def prices(symbols):
        return {"BTC_USDT": 110.0, "ETH_USDT": 90.0}

    monkeypatch.setattr(module, "bulk_market_prices", prices)
    request = module.BulkCloseRequest(position_ids=[btc_id, eth_id])
    first = asyncio.run(module.close_all_positions(request))
    after_first = position_rows(module)
    second = asyncio.run(module.close_all_positions(request))
    after_second = position_rows(module)

    assert first["ok"] is True
    assert len(first["closed"]) == 2
    assert module.state["running"] is True
    assert all(x["status"] == "CLOSED" for x in after_first)
    assert all(x["close_reason"] == "EMERGENCY_CLOSE" for x in after_first)
    assert after_first[0]["pnl"] == 100.0 - (110.0 * 10.0 * 0.0008)
    assert after_first[1]["pnl"] == 100.0 - (90.0 * 10.0 * 0.0008)
    assert after_first[0]["fee_paid"] == 110.0 * 10.0 * 0.0008
    assert after_first[1]["fee_paid"] == 90.0 * 10.0 * 0.0008
    assert second["closed"] == []
    assert len(second["already_closed"]) == 2
    assert after_second == after_first
    assert lab_rows(module) == lab_before

    module.state["live_prices"].update(BTC_USDT=100.0, ETH_USDT=100.0)
    module.manage_strategy_lab()
    assert all(x["status"] == "OPEN" for x in lab_rows(module))


def test_close_all_reports_missing_price_and_leaves_position_open(
    isolated_app, insert_position, monkeypatch
):
    module, _ = isolated_app
    btc_id = insert_position(symbol="BTC_USDT")
    eth_id = insert_position(symbol="ETH_USDT")

    async def prices(symbols):
        return {"BTC_USDT": 105.0}

    monkeypatch.setattr(module, "bulk_market_prices", prices)
    result = asyncio.run(
        module.close_all_positions(
            module.BulkCloseRequest(position_ids=[btc_id, eth_id])
        )
    )
    rows = {x["id"]: x for x in position_rows(module)}

    assert result["ok"] is False
    assert [x["id"] for x in result["closed"]] == [btc_id]
    assert result["failed"] == [
        {
            "id": eth_id,
            "symbol": "ETH_USDT",
            "reason": "Güncel public piyasa fiyatı bulunamadı",
        }
    ]
    assert rows[btc_id]["status"] == "CLOSED"
    assert rows[eth_id]["status"] == "OPEN"


def test_close_all_and_stop_pauses_entries_before_price_request(
    isolated_app, insert_position, monkeypatch
):
    module, _ = isolated_app
    position_id = insert_position()
    module.state.update(running=True, entry_paused=False)

    async def prices(symbols):
        assert module.state["running"] is False
        assert module.state["entry_paused"] is True
        return {"BTC_USDT": 101.0}

    monkeypatch.setattr(module, "bulk_market_prices", prices)
    result = asyncio.run(
        module.close_all_positions_and_stop(
            module.BulkCloseRequest(position_ids=[position_id])
        )
    )

    assert result["bot_stopped"] is True
    assert len(result["closed"]) == 1
    assert module.state["running"] is False
    assert module.state["entry_paused"] is True


def test_open_summary_contains_confirmation_count(isolated_app, insert_position):
    module, _ = isolated_app
    insert_position(symbol="BTC_USDT")
    insert_position(symbol="ETH_USDT")

    preview = module.open_positions_summary()

    assert preview["count"] == 2
    assert [x["symbol"] for x in preview["positions"]] == ["BTC_USDT", "ETH_USDT"]
