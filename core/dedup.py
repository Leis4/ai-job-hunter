"""Near-duplicate clustering for vacancies.

The same vacancy is often published several times: by an employer on its career
site, by an aggregator, and by a regional job board, sometimes with slightly
different titles. Exact fingerprints only catch byte-identical copies from
different sources, so this module adds a semantic-ish pass:

* description cosine + 4-word shingles,
* title/company/skills similarity,
* salary and location agreement.

Every copy keeps its own row and URL. The cluster points to one canonical job,
and ``job_publications`` stores links to all original publications.
"""
from __future__ import annotations

import json
import sqlite3

from . import db as dbm
from .text import cosine_counter, jaccard, normalize

LEGAL_FORMS = ("ооо", "оао", "зао", "пао", "ао", "ип", "llc", "ltd", "inc", "corp", "gmbh", "company")


def company_key(name: str) -> str:
    text = normalize(name or "")
    text = text.replace("«", " ").replace("»", " ").replace('"', " ").replace("'", " ")
    words = []
    for word in text.replace(",", " ").split():
        clean = "".join(ch for ch in word if ch.isalnum() or ch in "+#.")
        if clean and clean not in LEGAL_FORMS:
            words.append(clean)
    return " ".join(words)


def _location_key(value: str) -> str:
    text = normalize(value or "")
    for prefix in ("город ", "г. ", "г ", "city "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip()


def _salary_equal(a, b) -> bool:
    return bool(a["salary_min"] and b["salary_min"] and a["salary_min"] == b["salary_min"]
                and (a["salary_max"] or 0) == (b["salary_max"] or 0))


def _skills(row) -> list[str]:
    try:
        value = json.loads(row["skills"] or "[]")
        return [str(x) for x in value] if isinstance(value, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def similarity(a, b) -> tuple[float, dict]:
    company_match = company_key(a["company"]) == company_key(b["company"]) and bool(company_key(a["company"]))
    title_sim = jaccard(a["title"] or "", b["title"] or "", n=1)
    desc_a, desc_b = (a["description"] or "")[:4000], (b["description"] or "")[:4000]
    desc_sim = cosine_counter(desc_a, desc_b) if desc_a and desc_b else 0.0
    shingle = jaccard(desc_a, desc_b, n=4) if desc_a and desc_b else 0.0
    skills_sim = jaccard(" ".join(_skills(a)), " ".join(_skills(b)), n=1)
    salary_match = _salary_equal(a, b)
    location_match = bool(_location_key(a["location"])) and \
        _location_key(a["location"]) == _location_key(b["location"])
    score = (0.40 * desc_sim + 0.15 * shingle + 0.15 * title_sim + 0.12 * float(company_match)
             + 0.06 * skills_sim + 0.06 * float(salary_match) + 0.06 * float(location_match))
    parts = {"description": round(desc_sim, 3), "shingles": round(shingle, 3),
             "title": round(title_sim, 3), "company": company_match,
             "skills": round(skills_sim, 3), "salary": salary_match, "location": location_match}
    return round(score, 4), parts


def is_duplicate(a, b, score: float, parts: dict) -> bool:
    company_match = bool(parts["company"])
    if company_match and parts["description"] >= 0.86 and parts["title"] >= 0.55:
        return True
    if company_match and parts["title"] >= 0.85 and parts["description"] >= 0.55:
        return True
    if company_match and parts["description"] >= 0.78 and parts["title"] >= 0.55 \
            and parts["location"]:
        return True
    if company_match and parts["title"] >= 0.9 and parts["salary"] and parts["location"] \
            and parts["description"] >= 0.3:
        return True
    # Aggregators sometimes rewrite the company name but not the vacancy text.
    if parts["description"] >= 0.93 and parts["title"] >= 0.6 and parts["shingles"] >= 0.6:
        return True
    return False


def _ensure_publication(db: sqlite3.Connection, row, canonical_id: str, score: float,
                        timestamp: str) -> None:
    db.execute(
        """INSERT INTO job_publications(copy_id, canonical_id, source, source_id, url, title,
                                        company, similarity, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(copy_id) DO UPDATE SET canonical_id=excluded.canonical_id,
             source=excluded.source, source_id=excluded.source_id, url=excluded.url,
             title=excluded.title, company=excluded.company, similarity=excluded.similarity""",
        (row["id"], canonical_id, row["source"], row["source_id"] or "", row["url"] or "",
         row["title"] or "", row["company"] or "", score, timestamp),
    )


def _merge_applications(db: sqlite3.Connection, canonical_id: str, copy_id: str,
                        timestamp: str) -> int:
    """Move queued work to the canonical job; cancel a real channel clash."""
    moved = 0
    for app in db.execute("SELECT * FROM applications WHERE job_id=?", (copy_id,)).fetchall():
        clash = db.execute("SELECT id FROM applications WHERE job_id=? AND channel=?",
                           (canonical_id, app["channel"])).fetchone()
        if clash:
            db.execute("""UPDATE applications SET state='cancelled', last_error=?, updated_at=?
                          WHERE id=?""",
                       (f"Дубль вакансии: объединено с {canonical_id}", timestamp, app["id"]))
            dbm.log_event(db, canonical_id, "application_cancelled_duplicate",
                          payload={"copy_job": copy_id, "application_id": app["id"]},
                          application_id=app["id"])
        else:
            db.execute("UPDATE applications SET job_id=?, updated_at=? WHERE id=?",
                       (canonical_id, timestamp, app["id"]))
            moved += 1
    return moved


def deduplicate_jobs(db: sqlite3.Connection, *, min_similarity: float = 0.62) -> dict:
    """Group near-duplicates; keep every original row and URL."""
    timestamp = dbm.now()
    # Backfill links for copies marked by the exact-fingerprint pass.
    for copy in db.execute("SELECT * FROM jobs WHERE duplicate_of IS NOT NULL").fetchall():
        _ensure_publication(db, copy, copy["duplicate_of"], 1.0, timestamp)
        canonical = db.execute("SELECT * FROM jobs WHERE id=?", (copy["duplicate_of"],)).fetchone()
        if canonical:
            _ensure_publication(db, canonical, canonical["id"], 1.0, timestamp)
    rows = db.execute(
        """SELECT j.*, (SELECT COUNT(*) FROM applications a WHERE a.job_id=j.id) AS apps
           FROM jobs j
           WHERE j.duplicate_of IS NULL
           ORDER BY apps DESC, length(j.description) DESC, j.fetched_at ASC, j.id"""
    ).fetchall()
    canonicals: dict[str, list] = {}
    merged = 0
    clusters: set[str] = set()
    for row in rows:
        key = company_key(row["company"]) or " ".join(normalize(row["title"] or "").split()[:2])
        representatives = canonicals.get(key, [])
        target = None
        best_score = 0.0
        best_parts: dict = {}
        for candidate in representatives:
            score, parts = similarity(candidate, row)
            if score < min_similarity or score <= best_score:
                continue
            if is_duplicate(candidate, row, score, parts):
                target, best_score, best_parts = candidate, score, parts
        if target is None:
            canonicals.setdefault(key, []).append(row)
            _ensure_publication(db, row, row["id"], 1.0, timestamp)
            continue
        moved = _merge_applications(db, target["id"], row["id"], timestamp)
        db.execute("UPDATE jobs SET duplicate_of=?, updated_at=? WHERE id=?",
                   (target["id"], timestamp, row["id"]))
        _ensure_publication(db, row, target["id"], best_score, timestamp)
        _ensure_publication(db, target, target["id"], 1.0, timestamp)
        dbm.log_event(db, target["id"], "job_duplicate_linked",
                      payload={"copy_id": row["id"], "source": row["source"],
                               "url": row["url"], "similarity": best_score,
                               "applications_moved": moved,
                               "signals": best_parts})
        merged += 1
        clusters.add(target["id"])
    db.commit()
    return {"checked": len(rows), "merged": merged, "clusters": len(clusters)}


def publications(db: sqlite3.Connection, job_id: str) -> list[sqlite3.Row]:
    canonical = job_id
    row = db.execute("SELECT duplicate_of FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row and row["duplicate_of"]:
        canonical = row["duplicate_of"]
    return db.execute(
        """SELECT * FROM job_publications WHERE canonical_id=?
           ORDER BY similarity DESC, copy_id""", (canonical,)
    ).fetchall()
