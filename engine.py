"""Backward-compatible facade over the modular core.

The original MVP API (``import_jobs``, ``rank_all``, ``prepare_letters``,
``stats``, ``factual_letter``) is preserved so existing scripts and
``jobhunter.py`` keep working.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core import db as dbm
from core.letters import ensure_letters, factual_letter as _factual_letter
from core.paths import INBOX, PROCESSED, ROOT, ensure_dirs
from core.pipeline import ingest_inbox as _ingest_inbox
from core.resume import load_resume, sync_from_profile
from core.scoring import ScoreContext, build_context, rank_pending, score_row
from core.settings import load_profile, load_settings
from core.sources.importer import normalize, read_file

DB = dbm.DB_PATH
__all__ = [
    "ROOT", "DB", "connect", "load_json", "import_jobs", "score_job", "rank_all",
    "factual_letter", "llm_letter", "prepare_letters", "ingest_inbox", "stats",
]


def now() -> str:
    return dbm.now()


def connect() -> sqlite3.Connection:
    return dbm.init_db()


def load_json(filename: str) -> dict:
    with (ROOT / filename).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def import_jobs(path) -> int:
    items = read_file(Path(path))
    added = 0
    with connect() as db:
        for item in items:
            job = normalize(item)
            if not job:
                continue
            try:
                _, created = dbm.upsert_job(db, job)
                added += int(created)
            except ValueError:
                continue
    return added


def score_job(job, profile: dict | None = None, settings: dict | None = None):
    """Score one row/dict. Kept for compatibility with the original MVP."""
    profile = profile or load_profile()
    settings = settings or load_settings()
    resume = sync_from_profile(load_resume())
    if isinstance(job, dict):
        row = {
            "id": job.get("id") or "adhoc", "source": job.get("source") or "manual",
            "title": job.get("title") or "", "company": job.get("company") or "",
            "description": job.get("description") or "",
            "skills": json.dumps(job.get("skills") or [], ensure_ascii=False),
            "experience_years": job.get("experience_years"),
            "experience_text": job.get("experience_text") or "",
            "salary_min": job.get("salary_min"), "salary_max": job.get("salary_max"),
            "currency": job.get("currency") or "RUB", "location": job.get("location") or "",
            "work_mode": job.get("work_mode") or "", "published_at": job.get("published_at") or "",
            "fetched_at": job.get("fetched_at") or "", "created_at": job.get("created_at") or dbm.now(),
            "duplicate_of": None, "letter": job.get("letter") or "", "threshold": None,
            "score": None, "detail": job.get("detail") or "{}",
        }
    else:
        row = job
    ctx = build_context([row], settings, profile, resume)
    result = score_row(row, ctx, 0)
    return result["score"], result["eligible"], result["reason"]


def rank_all() -> int:
    settings, profile = load_settings(), load_profile()
    resume = sync_from_profile(load_resume())
    with connect() as db:
        return rank_pending(db, settings, profile, resume)["scored"]


def factual_letter(job, profile: dict | None = None) -> str:
    profile = profile or load_profile()
    resume = sync_from_profile(load_resume())
    return _factual_letter(job, profile, resume)


def llm_letter(job, profile: dict | None = None) -> str:
    """Deprecated single-shot helper; returns a checked letter or the factual one."""
    from core.letters import generate_letter
    settings = load_settings()
    profile = profile or load_profile()
    resume = sync_from_profile(load_resume())
    with connect() as db:
        row = dbm.get_job(db, job["id"]) if job.get("id") else None
        if not row:
            return _factual_letter(job, profile, resume)
        return generate_letter(db, settings, profile, resume, row, force=True)["letter"]


def prepare_letters(max_new: int = 15) -> int:
    settings = load_settings()
    result = ensure_letters(connect(), settings, load_profile(), sync_from_profile(load_resume()),
                            limit=max_new)
    return result["drafted"]


def ingest_inbox() -> int:
    with connect() as db:
        return _ingest_inbox(db)["added"]


def stats() -> dict:
    from core.analytics import summary
    with connect() as db:
        data = summary(db)
    return {"total": data["jobs"], "matched": data["eligible"], "drafts": data["drafted"],
            "submitted": data["sent"]}
