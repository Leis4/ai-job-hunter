"""Deterministic math operations used by the product.

Adapted from the upgrade pack (`modules/math_engine.py`) and extended with
calibration diagnostics, diversity selection and explainability. Standard
library only: no SymPy/NumPy requirement for core behaviour.

Rules:
* ``match.score`` is a ranking heuristic, never an interview probability.
* Sensitivity bounds are not credible intervals.
* Pending applications are right-censored, not failures.
* Calibration is only trained on confirmed, non-synthetic outcomes.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Callable, Iterable

METHODS = {
    "match.score": ("Weighted multi-criteria scoring with sensitivity bounds", "rank vacancies"),
    "score.explain": ("Contribution decomposition", "explain a match score"),
    "outcomes.response_rate": ("Bayesian Beta-binomial posterior", "historical reply/invite rates"),
    "retrieval.rrf": ("Reciprocal rank fusion (k=60)", "combine FTS and vector retrieval"),
    "queue.optimize": ("0/1 knapsack dynamic programming", "fit applications into a daily time budget"),
    "selection.mmr": ("Maximal marginal relevance", "balance repeated job archetypes"),
    "llm.estimate_cost": ("Token cost arithmetic", "estimate provider spend in RUB"),
    "calibration.evaluate": ("Brier / log loss / AUC / ECE with time split", "validate reply model"),
    "time.decay": ("Exponential time decay", "recency weighting"),
}


@dataclass(frozen=True)
class MathResult:
    operation: str
    method: str
    value: Any
    notes: str = ""


def _finite(value: float, name: str) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def matching(factors: dict[str, dict[str, float]],
             hard_constraints: dict[str, bool | None] | None = None) -> dict:
    """Weighted score with explicit feasibility.

    ``factors`` maps name -> {score: 0..1, weight: >0, low/high: optional 0..1}.
    Unknown hard constraints produce ``eligible=None`` (review), never a pass.
    """
    if not factors:
        raise ValueError("Need at least one factor")
    for name, factor in factors.items():
        score = _finite(factor["score"], f"{name}.score")
        weight = _finite(factor["weight"], f"{name}.weight")
        low = _finite(factor.get("low", score), f"{name}.low")
        high = _finite(factor.get("high", score), f"{name}.high")
        if not (0 <= low <= score <= high <= 1):
            raise ValueError(f"Bad sensitivity bounds for {name}: {low} <= {score} <= {high}")
        if weight <= 0:
            raise ValueError(f"Bad weight for {name}")
    total = sum(float(f["weight"]) for f in factors.values())

    def weighted(key: str) -> float:
        return 100 * sum(float(f["weight"]) * float(f.get(key, f["score"]))
                         for f in factors.values()) / total

    constraints = hard_constraints or {}
    failed = sorted(k for k, v in constraints.items() if v is False)
    unknown = sorted(k for k, v in constraints.items() if v is None)
    return {
        "score": round(weighted("score"), 1),
        "sensitivity_range": [round(weighted("low"), 1), round(weighted("high"), 1)],
        "eligible": False if failed else (None if unknown else True),
        "failed_constraints": failed,
        "needs_review": unknown,
        "weights_total": round(total, 4),
    }


def explain(factors: dict[str, dict[str, float]], limit: int = 3) -> list[dict]:
    """Top contributing factors: weight x score, with signed gap to 1.0."""
    if not factors:
        return []
    total = sum(float(f["weight"]) for f in factors.values()) or 1.0
    rows = []
    for name, factor in factors.items():
        score = float(factor["score"])
        weight = float(factor["weight"]) / total
        rows.append({
            "factor": name,
            "score": round(score, 3),
            "weight": round(weight, 3),
            "contribution": round(weight * score * 100, 1),
            "gap": round((1 - score) * weight * 100, 1),
        })
    return sorted(rows, key=lambda r: -r["contribution"])[:max(1, limit)]


def response_rate(successes: int, resolved_failures: int, *, pending: int = 0,
                  prior_a: float = 1, prior_b: float = 1, draws: int = 4000) -> dict:
    """Beta-binomial posterior for a *group* historical rate.

    Pending applications are excluded from the denominator. This is not a
    probability for a specific vacancy.
    """
    for name, value in (("successes", successes), ("resolved_failures", resolved_failures),
                        ("pending", pending)):
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if prior_a <= 0 or prior_b <= 0:
        raise ValueError("Priors must be positive")
    a, b = prior_a + successes, prior_b + resolved_failures
    rng = random.Random(42)
    samples = sorted(rng.betavariate(a, b) for _ in range(max(500, draws)))
    n = len(samples)

    def quantile(p: float) -> float:
        return round(samples[min(n - 1, max(0, int(n * p)))], 4)

    return {
        "mean": round(a / (a + b), 4),
        "interval_80": [quantile(0.10), quantile(0.90)],
        "interval_95": [quantile(0.025), quantile(0.975)],
        "resolved_n": successes + resolved_failures,
        "pending_n": pending,
        "prior": [prior_a, prior_b],
    }


def reciprocal_rank_fusion(rankings: list[list[int]], k: int = 60) -> list[dict]:
    if k <= 0:
        raise ValueError("k must be positive")
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(dict.fromkeys(int(x) for x in ranking), 1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1 / (k + rank)
    return [{"id": i, "score": round(score, 8)}
            for i, score in sorted(scores.items(), key=lambda x: (-x[1], x[0]))]


def choose_under_budget(items: list[dict], budget_minutes: int) -> dict:
    """0/1 knapsack maximising utility within a daily minute budget."""
    if not isinstance(budget_minutes, int) or not 0 <= budget_minutes <= 10080:
        raise ValueError("Budget must be an integer in [0,10080] minutes")
    dp: list[tuple[float, tuple[str, ...]]] = [(0.0, ()) for _ in range(budget_minutes + 1)]
    seen: set[str] = set()
    for item in items:
        ident = str(item.get("id", "")).strip()
        if not ident:
            raise ValueError("Item id is required")
        if ident in seen:
            raise ValueError(f"Duplicate item id: {ident}")
        seen.add(ident)
        minutes = int(item.get("minutes", 0))
        utility = _finite(item.get("utility", 0), "utility")
        if minutes <= 0:
            raise ValueError("Item minutes must be positive")
        if minutes > budget_minutes:
            continue
        for b in range(budget_minutes, minutes - 1, -1):
            candidate = dp[b - minutes][0] + utility
            if candidate > dp[b][0]:
                dp[b] = (candidate, dp[b - minutes][1] + (ident,))
    used, (utility, selected) = max(enumerate(dp), key=lambda pair: (pair[1][0], -pair[0]))
    return {"minutes_used": used, "utility": round(utility, 4), "selected_ids": list(selected)}


def mmr(items: list[dict], *, limit: int, lambda_: float = 0.72,
        similarity: Callable[[dict, dict], float] | None = None) -> list[dict]:
    """Diversity-aware selection. Items need id/utility and optionally 'group'."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if not 0 <= lambda_ <= 1:
        raise ValueError("lambda_ must be in [0,1]")
    if similarity is None:
        def similarity(a: dict, b: dict) -> float:
            return 1.0 if a.get("group") and a.get("group") == b.get("group") else 0.0
    remaining = list(items)
    chosen: list[dict] = []
    while remaining and len(chosen) < limit:
        best = None
        best_value = -1e9
        for candidate in remaining:
            relevance = _finite(candidate.get("utility", 0), "utility")
            redundancy = max((similarity(candidate, other) for other in chosen), default=0.0)
            value = lambda_ * relevance - (1 - lambda_) * redundancy
            if value > best_value:
                best, best_value = candidate, value
        if best is None:
            break
        chosen.append(best)
        remaining = [x for x in remaining if x is not best]
    return chosen


def expected_api_cost(input_tokens: int, output_tokens: int,
                      input_rub_per_m: float, output_rub_per_m: float) -> float:
    for name, value in (("input_tokens", input_tokens), ("output_tokens", output_tokens),
                        ("input_rub_per_m", input_rub_per_m), ("output_rub_per_m", output_rub_per_m)):
        if _finite(value, name) < 0:
            raise ValueError(f"{name} cannot be negative")
    return round((input_tokens * input_rub_per_m + output_tokens * output_rub_per_m) / 1_000_000, 6)


def time_decay(age_days: float, half_life_days: float = 21.0, floor: float = 0.35) -> float:
    if age_days < 0 or half_life_days <= 0:
        raise ValueError("age_days >= 0 and half_life_days > 0 required")
    if not 0 <= floor <= 1:
        raise ValueError("floor must be in [0,1]")
    return round(max(floor, 0.5 ** (age_days / half_life_days)), 4)


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _auc(y: list[int], p: list[float]) -> float:
    pairs = sorted(zip(p, y))
    ranks = [0.0] * len(pairs)
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    pos = sum(y)
    neg = len(y) - pos
    if not pos or not neg:
        return 0.5
    rank_sum = sum(r for r, (_, yi) in zip(ranks, pairs) if yi == 1)
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def calibration_metrics(y: list[int], p: list[float], bins: int = 5) -> dict:
    if len(y) != len(p) or not y:
        raise ValueError("Need aligned, non-empty labels/probabilities")
    if any(v not in (0, 1) for v in y):
        raise ValueError("Labels must be 0/1")
    if any(not 0 <= v <= 1 for v in p):
        raise ValueError("Probabilities must be in [0,1]")
    n = len(y)
    brier = sum((pi - yi) ** 2 for pi, yi in zip(p, y)) / n
    eps = 1e-9
    logloss = -sum(yi * math.log(max(pi, eps)) + (1 - yi) * math.log(max(1 - pi, eps))
                   for pi, yi in zip(p, y)) / n
    reliability = []
    ece = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        members = [(pi, yi) for pi, yi in zip(p, y) if lo <= pi < hi or (b == bins - 1 and pi == 1.0)]
        if members:
            predicted = sum(m[0] for m in members) / len(members)
            actual = sum(m[1] for m in members) / len(members)
            ece += abs(predicted - actual) * len(members) / n
            reliability.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(members),
                                "predicted": round(predicted, 3), "actual": round(actual, 3)})
    return {
        "n": n, "positives": sum(y), "negatives": n - sum(y),
        "brier": round(brier, 4), "log_loss": round(logloss, 4),
        "auc": round(_auc(y, p), 4), "ece": round(ece, 4),
        "base_rate": round(sum(y) / n, 4), "reliability": reliability,
    }


def fit_logistic(X: list[list[float]], y: list[int], *, l2: float = 0.02,
                 epochs: int = 900, lr: float = 0.35) -> list[float]:
    if not X or len(X) != len(y):
        raise ValueError("X and y must be aligned and non-empty")
    if any(v not in (0, 1) for v in y):
        raise ValueError("Labels must be 0/1")
    dim = len(X[0])
    if any(len(row) != dim for row in X):
        raise ValueError("Inconsistent feature dimensions")
    w = [0.0] * dim
    n = len(X)
    for _ in range(epochs):
        grad = [0.0] * dim
        for xi, yi in zip(X, y):
            p = _sigmoid(sum(wj * xj for wj, xj in zip(w, xi)))
            err = p - yi
            for k in range(dim):
                grad[k] += err * xi[k]
        for k in range(1, dim):
            grad[k] += l2 * w[k] * n
        for k in range(dim):
            w[k] -= lr * grad[k] / n
    return [round(x, 8) for x in w]


def predict_logistic(weights: list[float], x: list[float]) -> float:
    if len(weights) != len(x):
        raise ValueError("Weight/feature length mismatch")
    return round(_sigmoid(sum(a * b for a, b in zip(weights, x))), 4)


class MathRouter:
    """Typed operation registry with method provenance."""

    def __init__(self):
        self._operations: dict[str, tuple[Callable, str, str]] = {}
        self.register("match.score", matching, *METHODS["match.score"])
        self.register("score.explain", explain, *METHODS["score.explain"])
        self.register("outcomes.response_rate", response_rate, *METHODS["outcomes.response_rate"])
        self.register("retrieval.rrf", reciprocal_rank_fusion, *METHODS["retrieval.rrf"])
        self.register("queue.optimize", choose_under_budget, *METHODS["queue.optimize"])
        self.register("selection.mmr", mmr, *METHODS["selection.mmr"])
        self.register("llm.estimate_cost", expected_api_cost, *METHODS["llm.estimate_cost"])
        self.register("calibration.evaluate", calibration_metrics, *METHODS["calibration.evaluate"])
        self.register("time.decay", time_decay, *METHODS["time.decay"])

    def register(self, name: str, func: Callable, method: str, purpose: str) -> None:
        if name in self._operations:
            raise ValueError(f"Duplicate operation: {name}")
        self._operations[name] = (func, method, purpose)

    def run(self, name: str, **kwargs) -> MathResult:
        if name not in self._operations:
            raise KeyError(f"Unknown math operation: {name}")
        func, method, purpose = self._operations[name]
        return MathResult(name, method, func(**kwargs), purpose)

    def operations(self) -> dict[str, dict]:
        return {name: {"method": method, "purpose": purpose}
                for name, (_, method, purpose) in self._operations.items()}


ROUTER = MathRouter()
