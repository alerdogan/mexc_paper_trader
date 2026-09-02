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


def test_strategy_lab_creates_four_isolated_shadow_models(
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
        "TRAILING_RUNNER",
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


@pytest.mark.parametrize("model", ["NO_STOP_MINI", "SMART_EXIT", "TRAILING_RUNNER"])
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


def model_run(module, position_id, model="TRAILING_RUNNER"):
    return next(x for x in lab_runs(module, position_id) if x["model"] == model)


def set_price_and_manage_lab(module, symbol, price):
    module.state["live_prices"][symbol] = price
    module.manage_strategy_lab()


def test_trailing_runner_long_starts_after_tp2_and_never_loosens(
    isolated_app, insert_position
):
    module, _ = isolated_app
    position_id = insert_position()
    module.strategy_lab_create(position_id)

    set_price_and_manage_lab(module, "BTC_USDT", 119.0)
    before_tp2 = model_run(module, position_id)
    assert before_tp2["tp1_done"] == 1
    assert before_tp2["tp2_done"] == 0
    assert before_tp2["stop"] == 100.0

    set_price_and_manage_lab(module, "BTC_USDT", 124.0)
    at_tp2 = model_run(module, position_id)
    assert at_tp2["tp2_done"] == 1
    assert at_tp2["remaining_qty"] == pytest.approx(4.0)
    assert at_tp2["mfe_r"] == pytest.approx(2.4)
    assert at_tp2["stop"] == pytest.approx(114.0)

    set_price_and_manage_lab(module, "BTC_USDT", 130.0)
    peak = model_run(module, position_id)
    assert peak["mfe_r"] == pytest.approx(3.0)
    assert peak["stop"] == pytest.approx(120.0)

    set_price_and_manage_lab(module, "BTC_USDT", 125.0)
    retrace = model_run(module, position_id)
    assert retrace["mfe_r"] == pytest.approx(3.0)
    assert retrace["stop"] == pytest.approx(120.0)
    assert retrace["status"] == "OPEN"

    set_price_and_manage_lab(module, "BTC_USDT", 119.0)
    closed = model_run(module, position_id)
    assert closed["status"] == "CLOSED"
    assert closed["remaining_qty"] == 0
    assert closed["close_reason"] == "RUNNER_TRAILING_STOP"


def test_trailing_runner_short_is_directionally_symmetric(
    isolated_app, insert_position
):
    module, _ = isolated_app
    position_id = insert_position(side="SHORT", stop=110.0, initial_stop=110.0)
    module.strategy_lab_create(position_id)

    set_price_and_manage_lab(module, "BTC_USDT", 76.0)
    at_tp2 = model_run(module, position_id)
    assert at_tp2["tp1_done"] == 1
    assert at_tp2["tp2_done"] == 1
    assert at_tp2["remaining_qty"] == pytest.approx(4.0)
    assert at_tp2["mfe_r"] == pytest.approx(2.4)
    assert at_tp2["stop"] == pytest.approx(86.0)

    set_price_and_manage_lab(module, "BTC_USDT", 70.0)
    peak = model_run(module, position_id)
    assert peak["stop"] == pytest.approx(80.0)
    set_price_and_manage_lab(module, "BTC_USDT", 75.0)
    assert model_run(module, position_id)["stop"] == pytest.approx(80.0)

    set_price_and_manage_lab(module, "BTC_USDT", 81.0)
    closed = model_run(module, position_id)
    assert closed["status"] == "CLOSED"
    assert closed["close_reason"] == "RUNNER_TRAILING_STOP"


def test_trailing_runner_exact_two_r_keeps_one_r_distance(
    isolated_app, insert_position
):
    module, _ = isolated_app
    position_id = insert_position()
    module.strategy_lab_create(position_id)

    set_price_and_manage_lab(module, "BTC_USDT", 120.0)
    run = model_run(module, position_id)
    api_run = module.strategy_lab()["experiments"][0]["models"]["TRAILING_RUNNER"]

    assert run["stop"] == pytest.approx(110.0)
    assert api_run["current_r"] == pytest.approx(2.0)
    assert api_run["max_favorable_r"] == pytest.approx(2.0)
    assert api_run["runner_trailing_stop_price"] == pytest.approx(110.0)
    assert api_run["runner_trailing_stop_r"] == pytest.approx(1.0)


def test_existing_models_keep_same_tp_behavior_with_trailing_runner_present(
    isolated_app, insert_position
):
    module, _ = isolated_app
    position_id = insert_position()
    module.strategy_lab_create(position_id)
    module.state["live_prices"]["BTC_USDT"] = 120.0
    module.manage()
    module.manage_strategy_lab()

    runs = {x["model"]: x for x in lab_runs(module, position_id)}
    for model in ("CURRENT", "NO_STOP_MINI", "SMART_EXIT"):
        assert runs[model]["tp1_done"] == 1
        assert runs[model]["tp2_done"] == 1
        assert runs[model]["remaining_qty"] == pytest.approx(4.0)
        assert runs[model]["stop"] == pytest.approx(110.0)


def test_init_db_preserves_old_three_model_experiment(
    isolated_app, insert_position, monkeypatch
):
    module, _ = isolated_app
    position_id = insert_position()
    monkeypatch.setattr(
        module, "STRATEGY_LAB_MODELS", ("CURRENT", "NO_STOP_MINI", "SMART_EXIT")
    )
    module.strategy_lab_create(position_id)
    before = lab_runs(module, position_id)

    module.init_db()
    after = lab_runs(module, position_id)

    assert after == before
    assert {x["model"] for x in after} == {"CURRENT", "NO_STOP_MINI", "SMART_EXIT"}
