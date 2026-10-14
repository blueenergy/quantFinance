from services.model_training_promotion import (
    build_promotion_context,
    evaluate_single_pool_promotion,
    recipe_fingerprint,
)


def _fold_reports(model_ic: float, baseline_ic: float = 0.01):
    return [
        {"fold_id": i, "model": {"mean_ic": model_ic}, "baseline": {"mean_ic": baseline_ic}}
        for i in range(4)
    ]


def test_single_pool_passes_without_universe_count():
    result = {
        "fold_reports": _fold_reports(0.05),
        "baseline_ic": {"mean_ic": 0.03},
        "permutation_p": 0.02,
        "promotion": {"passed": False, "reason": "insufficient_universes"},
    }
    single = evaluate_single_pool_promotion(result)
    assert single["passed"] is True
    assert single["model_mean_ic"] == 0.05


def test_group_promotion_two_indices():
    params = {"model_kind": "ridge", "train_days": 60, "test_days": 20, "purge_days": 60}
    fp = recipe_fingerprint(params, code_version="0.1.0")
    assert fp == recipe_fingerprint(params, code_version="0.1.0")

    def job(jid, index_code):
        return {
            "job_id": jid,
            "status": "completed",
            "params": {**params, "index_code": index_code},
        }

    def result(model_ic):
        return {
            "code_version": "0.1.0",
            "fold_reports": _fold_reports(model_ic),
            "baseline_ic": {"mean_ic": 0.03},
            "permutation_p": 0.02,
        }

    job_a = job("a", "hs300")
    job_b = job("b", "a500")
    ctx = build_promotion_context(
        job_a,
        result(0.05),
        [job_a, job_b],
        {"a": result(0.05), "b": result(0.06)},
    )
    assert ctx["single_pool"]["passed"] is True
    assert ctx["group"]["passed"] is True
    assert set(ctx["group"]["passed_index_codes"]) == {"a500", "hs300"}


def test_group_fails_when_one_index_below_baseline():
    params = {"model_kind": "ridge", "train_days": 60, "test_days": 20, "purge_days": 60}
    job_a = {"job_id": "a", "params": {**params, "index_code": "hs300"}}
    job_b = {"job_id": "b", "params": {**params, "index_code": "csi1000"}}
    good = {
        "code_version": "0.1.0",
        "fold_reports": _fold_reports(0.05),
        "baseline_ic": {"mean_ic": 0.03},
        "permutation_p": 0.02,
    }
    bad = {
        "code_version": "0.1.0",
        "fold_reports": _fold_reports(-0.01),
        "baseline_ic": {"mean_ic": 0.03},
        "permutation_p": 0.02,
    }
    ctx = build_promotion_context(job_a, good, [job_a, job_b], {"a": good, "b": bad})
    assert ctx["group"]["passed"] is False
    assert ctx["group"]["passed_pool_count"] == 1
