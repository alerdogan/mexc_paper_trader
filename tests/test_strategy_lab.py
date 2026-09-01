import pytest


def lab_runs(module, position_id):
    connection = module.db()
    rows = connection.execute(
        """SELECT r.* FROM strategy_lab_runs r
        JOIN strategy_lab_experiments e ON e.id=r.experiment_id
        WHERE e.source_position_id=? ORDER BY r.model""",
        (position_id,),
    ).fetchall()
    connection.close()
    return [dict(item) for item in rows]


def test_strategy_lab_creates_three_isolated_shadow_models(
    isolated_app, insert_position
):
    module, _ = isolated_app
    position_id = insert_position()
    balance_before = module.current_paper_balance()

    module.strategy_lab_create(position_id)
    runs = lab_runs(module, position_id)

    assert {item["model"] for item in runs} == {
        "CURRENT",
        "NO_STOP_MINI",
        "SMART_EXIT",
    }
    assert all(item["status"] == "OPEN" for item in runs)
    assert module.current_paper_balance() == balance_before


def test_strategy_lab_counts_recovery_after_current_stop(
    isolated_app, insert_position
):
    module, _ = isolated_app
    position_id = insert_position()
    module.strategy_lab_create(position_id)

    module.state["live_prices"]["BTC_USDT"] = 89.0
    module.manage()
    module.manage_strategy_lab()

    connection = module.db()
    source = connection.execute(
        "SELECT status,close_reason FROM positions WHERE id=?", (position_id,)
    ).fetchone()
    connection.close()
    assert dict(source) == {"status": "CLOSED", "close_reason": "STOP"}

    module.state["live_prices"]["BTC_USDT"] = 170.0
    module.manage_strategy_lab()
    result = module.strategy_lab()

    assert result["current_stop_total"] == 1
    assert result["models"]["NO_STOP_MINI"]["recovered_after_current_stop"] == 1
    assert result["models"]["SMART_EXIT"]["recovered_after_current_stop"] == 1
    assert result["models"]["NO_STOP_MINI"]["profitable_close_after_current_stop"] == 1
    assert result["models"]["SMART_EXIT"]["profitable_close_after_current_stop"] == 1


@pytest.mark.parametrize("model", ["NO_STOP_MINI", "SMART_EXIT"])
def test_strategy_lab_never_writes_to_paper_position(
    isolated_app, insert_position, model
):
    module, _ = isolated_app
    position_id = insert_position()
    module.strategy_lab_create(position_id)
    connection = module.db()
    before = dict(
        connection.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
    )
    connection.execute(
        "UPDATE strategy_lab_runs SET realized_pnl=999,status='CLOSED' WHERE model=?",
        (model,),
    )
    connection.commit()
    after = dict(
        connection.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
    )
    connection.close()

    assert after == before
