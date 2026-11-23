"""Probability engine checks on an isolated temporary database."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbm
from core import probability_engine as engine


def make_db(tmp: Path):
    return dbm.init_db(dbm.connect(tmp / "probability.sqlite3"))


def row(i: int, status: str, *, seniority="junior", direction="analytics",
        skill=60.0, at_submission=True, captured="2025-01-01T10:00:00+00:00") -> dict:
    return {
        "title": f"Junior Data Analyst {i}", "employer": "Test Co",
        "url": f"https://example.test/vacancy/{i}", "sent_at": f"2025-01-{i%27+1:02d}T10:00:00+00:00",
        "status": status, "seniority": seniority, "direction": direction,
        "source": "manual", "match_score": 72,
        "features": {"skill_match": skill, "career_fit": 60.0, "experience_gap_years": 0.0,
                     "required_experience": 0.0, "seniority": seniority, "direction": direction,
                     "work_mode": "remote", "location": "Москва", "education_required": False,
                     "company_type": "", "vacancy_type": "job", "freshness_days": 3,
                     "at_submission": at_submission, "captured_at": captured},
    }


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = make_db(tmp)
        old_model = engine.MODEL_PATH
        engine.MODEL_PATH = tmp / "model.json"
        try:
            rows = [row(i, "rejected") for i in range(20)]
            rows += [row(100 + i, "invited") for i in range(8)]
            rows += [row(200, "no_response_yet"), row(201, "unknown")]
            result = engine.commit(db, "manual", rows)
            assert result["added"] == 30, result

            summary = engine.summary(db)
            assert summary["failures"] == 20, summary
            assert summary["successes"] == 8, summary
            assert summary["pending"] == 2, summary
            estimate = engine.estimate(db, {"title": "Junior Data Analyst"})
            assert estimate["available"] is True, estimate
            assert estimate["n"] == 28 and estimate["successes"] == 8
            assert 0.2 < estimate["personal_probability"] < 0.5, estimate
            assert estimate["interval_90"][0] < estimate["personal_probability"] < estimate["interval_90"][1]
            assert engine.estimate(db, {"title": "Senior ML Engineer"})["available"] is False

            # Re-import same values must not double-count.
            again = engine.commit(db, "manual", rows[:3])
            assert again["updated"] == 3 and again["added"] == 0, again
            assert engine.summary(db)["total"] == 30

            # A pending application is not a rejection.
            assert engine.summary(db)["failures"] == 20

            # ML stays off below the transparent minimum.
            low = engine.train(db)
            assert low["trained"] is False, low

            # Add enough labelled observations for a chronological ML candidate.
            more = []
            for i in range(80):
                label = "invited" if i < 45 else "rejected"
                more.append(row(300 + i, label, skill=85.0 if label == "invited" else 30.0,
                                captured=f"2025-03-{i%27+1:02d}T10:00:00+00:00"))
                more[-1]["sent_at"] = f"2025-03-{i%27+1:02d}T09:00:00+00:00"
            # One leaky row must be excluded because its snapshot was captured later.
            leak = row(999, "invited", skill=99.0, captured="2025-04-01T10:00:00+00:00")
            leak["sent_at"] = "2025-01-01T09:00:00+00:00"
            more.append(leak)
            engine.commit(db, "manual", more)
            trained = engine.train(db)
            assert trained["trained"] is True, trained
            assert "metrics" in trained and "baseline" in trained
            assert trained["n"] >= 60

            quality = engine.quality(db)
            assert quality["last"] is not None
            print("PROBABILITY SMOKE: ok")
        finally:
            engine.MODEL_PATH = old_model
            db.close()


if __name__ == "__main__":
    main()
