"""Deterministic math operation tests. Run: py -3 tests/math_smoke.py"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.mathx import (  # noqa: E402
    ROUTER, calibration_metrics, choose_under_budget, expected_api_cost, explain,
    fit_logistic, matching, mmr, predict_logistic, reciprocal_rank_fusion,
    response_rate, time_decay,
)

PASS = []


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAIL: {name} {detail}")
    PASS.append(name)
    print(f"  ok  {name}{(' — ' + detail) if detail else ''}")


def raises(func):
    try:
        func()
        return False
    except (ValueError, KeyError):
        return True


def main():
    result = matching(
        {"skills": {"score": 1.0, "weight": 7, "low": 0.8, "high": 1.0},
         "schedule": {"score": 0.5, "weight": 3}},
        {"experience": True})
    check("match: weighted score", abs(result["score"] - 85.0) < 0.01, str(result["score"]))
    check("match: sensitivity", result["sensitivity_range"] == [71.0, 85.0], str(result["sensitivity_range"]))
    check("match: eligible", result["eligible"] is True)
    unknown = matching({"skills": {"score": 1, "weight": 1}}, {"visa": None})
    check("match: unknown => review", unknown["eligible"] is None and unknown["needs_review"] == ["visa"])
    failed = matching({"skills": {"score": 1, "weight": 1}}, {"experience": False})
    check("match: failed constraint", failed["eligible"] is False)
    check("match: invalid weight rejected",
          raises(lambda: matching({"x": {"score": .5, "weight": 0}})))
    check("match: invalid bounds rejected",
          raises(lambda: matching({"x": {"score": .5, "weight": 1, "low": .8}})))

    factors = {
        "skills": {"score": 0.9, "weight": 3},
        "role": {"score": 0.7, "weight": 2},
        "salary": {"score": 0.2, "weight": 1},
    }
    top = explain(factors, limit=3)
    check("explain: top factor", top[0]["factor"] == "skills", top[0]["factor"])
    check("explain: contributions sum", abs(sum(x["contribution"] for x in top) - 71.7) < 0.2,
          str(sum(x["contribution"] for x in top)))

    rate = response_rate(successes=3, resolved_failures=17, pending=40)
    check("beta: excludes pending", rate["resolved_n"] == 20 and rate["pending_n"] == 40)
    check("beta: mean sane", 0.05 < rate["mean"] < 0.4, str(rate["mean"]))
    check("beta: 95 interval", rate["interval_95"][0] <= rate["mean"] <= rate["interval_95"][1])
    check("beta: invalid counts rejected", raises(lambda: response_rate(-1, 2)))

    fused = reciprocal_rank_fusion([[1, 2], [2, 1]], k=60)
    check("rrf: symmetric", fused[0]["id"] in (1, 2) and len(fused) == 2)
    check("rrf: k validation", raises(lambda: reciprocal_rank_fusion([[1]], k=0)))

    basket = choose_under_budget([
        {"id": "A", "minutes": 5, "utility": 4},
        {"id": "B", "minutes": 7, "utility": 7},
        {"id": "C", "minutes": 2, "utility": 2},
    ], 7)
    check("knapsack: optimum", basket["selected_ids"] == ["B"], str(basket))
    check("knapsack: zero budget", choose_under_budget([{"id": "A", "minutes": 1, "utility": 1}], 0)["selected_ids"] == [])
    check("knapsack: duplicate rejected",
          raises(lambda: choose_under_budget([{"id": "A", "minutes": 1, "utility": 1}] * 2, 5)))

    diverse = mmr([
        {"id": "a1", "utility": 1.0, "group": "python"},
        {"id": "a2", "utility": 0.98, "group": "python"},
        {"id": "b1", "utility": 0.9, "group": "data"},
    ], limit=2, lambda_=0.5)
    check("mmr: diversity", {x["id"] for x in diverse} == {"a1", "b1"}, str([x["id"] for x in diverse]))

    check("cost: exact", expected_api_cost(2000, 400, 30, 100) == 0.1,
          str(expected_api_cost(2000, 400, 30, 100)))
    check("cost: negatives rejected", raises(lambda: expected_api_cost(-1, 0, 1, 1)))
    check("decay: half-life", abs(time_decay(21, 21) - 0.5) < 1e-6)
    check("decay: floor", time_decay(999, 21, floor=0.3) == 0.3)

    perfect = calibration_metrics([0, 0, 1, 1], [0.01, 0.02, 0.98, 0.99])
    check("calibration: perfect Brier", perfect["brier"] < 0.001, str(perfect["brier"]))
    check("calibration: perfect AUC", perfect["auc"] == 1.0)
    check("calibration: reliability bins", len(perfect["reliability"]) >= 2)
    check("calibration: invalid labels", raises(lambda: calibration_metrics([2], [0.5])))

    X = [[1.0, 0.0], [1.0, 0.1], [1.0, 0.2], [1.0, 0.9], [1.0, 1.0], [1.0, 0.8]]
    y = [0, 0, 0, 1, 1, 1]
    weights = fit_logistic(X, y, epochs=500)
    p_low = predict_logistic(weights, [1.0, 0.05])
    p_high = predict_logistic(weights, [1.0, 0.95])
    check("logistic: learns direction", p_high > p_low + 0.3, f"{p_low} -> {p_high}")
    check("logistic: length validation", raises(lambda: predict_logistic([1.0], [1.0, 2.0])))

    operations = ROUTER.operations()
    check("router: operations registered", len(operations) >= 9, str(len(operations)))
    check("router: method provenance", "method" in operations["match.score"])
    check("router: unknown op", raises(lambda: ROUTER.run("nope")))

    print(f"\nMATH SMOKE: {len(PASS)} проверок пройдено")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\n{exc}")
        sys.exit(1)
