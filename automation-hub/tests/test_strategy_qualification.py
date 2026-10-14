from services.strategy_qualification import (
    QualificationPolicy, chronological_partition, concentration_analysis,
    evaluate_qualification, out_of_sample_report, seeded_monte_carlo, stress_costs,
    walk_forward_report, walk_forward_windows,
)


def test_qualification_missing_evidence_is_not_qualified_and_explains_blockers():
    result = evaluate_qualification(
        {"strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0"},
        {"completed_episode_count": 120, "net_profit_factor": "1.8", "expectancy_net_r": "0.2",
         "cost_coverage": {"status": "PARTIAL"}}, evidence_quality="PARTIAL")
    assert result["status"] == "NOT_QUALIFIED"
    assert "cost_coverage" in result["failed_requirements"]
    assert "out_of_sample" in result["unknown_requirements"]
    assert result["policy"]["fingerprint"] == QualificationPolicy().fingerprint


def test_chronological_partition_and_warmup_never_mix_future_rows():
    rows = [{"timestamp": f"2026-01-{index:02d}"} for index in range(1, 11)]
    result = chronological_partition(rows, train=.6, validation=.2, holdout=.2, warmup=3)
    assert [row["timestamp"] for row in result["train"]] == [f"2026-01-{i:02d}" for i in range(1, 7)]
    assert result["validation"][0]["timestamp"] == "2026-01-07"
    assert result["holdout"][0]["timestamp"] == "2026-01-09"
    assert result["warmup"] == 3


def test_walk_forward_windows_are_disjoint_and_deterministic():
    rows = [{"timestamp": f"{i:02d}"} for i in range(12)]
    anchored = walk_forward_windows(rows, development_size=4, evaluation_size=2, anchored=True)
    rolling = walk_forward_windows(rows, development_size=4, evaluation_size=2, anchored=False)
    assert anchored[0]["evaluation"] == rows[4:6]
    assert anchored[1]["development"] == rows[:6]
    assert rolling[1]["development"] == rows[2:6]
    assert set(row["timestamp"] for row in rolling[0]["development"]).isdisjoint(
        row["timestamp"] for row in rolling[0]["evaluation"])


def test_monte_carlo_is_seed_reproducible_and_concentration_is_explicit():
    first = seeded_monte_carlo(["1", "-0.5", "0.25"], runs=100, seed=7)
    second = seeded_monte_carlo(["1", "-0.5", "0.25"], runs=100, seed=7)
    assert first == second
    concentrated = concentration_analysis(["8", "1", "-0.5"])
    assert concentrated["concentration_status"] == "FAIL"
    assert concentrated["profit_excluding_largest"] == "0.5"


def test_unknown_funding_is_not_treated_as_zero_in_cost_stress():
    result = stress_costs(["1", "-0.25"], fee="0.1", slippage="0.1")
    assert result["status"] == "UNKNOWN"
    assert result["funding"] == "UNKNOWN"


def test_oos_and_walk_forward_reports_use_independent_evaluation_callbacks():
    partition = {"train": [{"t": "1"}], "validation": [{"t": "2"}], "holdout": [{"t": "3"}]}
    def evaluate(rows, **kwargs):
        return {"net_r": "1" if kwargs["phase"] == "development" else "0.25", "trades": len(rows)}
    oos = out_of_sample_report(partition, evaluate)
    assert oos["status"] == "PASS"
    windows = walk_forward_windows([{ "timestamp": str(i)} for i in range(8)], development_size=2, evaluation_size=2)
    wf = walk_forward_report(windows, lambda rows, **kwargs: {"expectancy_r": "0.1", "trades": len(rows)})
    assert wf["status"] == "PASS"
    assert wf["total_windows"] == 3


def test_validation_runs_are_immutable_and_holdout_reuse_is_counted(tmp_path):
    from test_strategy_evidence_runtime import runtime
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    base = {"run_id": "validation-1", "strategy_id": "adaptive_trend_pullback",
            "strategy_version": "1.0.0", "config_fingerprint": "f" * 64,
            "dataset_identity": "dataset-a", "execution_mode": "backtest",
            "method": "out_of_sample", "status": "COMPLETE", "report": {"status": "PASS"}}
    assert store.context.record_validation_run(base) is True
    assert store.context.record_validation_run(base) is False
    second = {**base, "run_id": "validation-2"}
    assert store.context.record_validation_run(second) is True
    rows = store.context.validation_runs(filters={"strategy_id": "adaptive_trend_pullback"})
    assert rows[0]["holdout_access_count"] == 2
    conflict = {**base, "report": {"status": "FAIL"}}
    try:
        store.context.record_validation_run(conflict)
    except ValueError as error:
        assert "immutable" in str(error)
    else:
        raise AssertionError("conflicting validation result was accepted")
