"""End-to-end cycle: inbox -> sources -> rank -> details -> letters -> queue -> monitor."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import db as dbm
from .analytics import summary
from .letters import ensure_letters
from .memory import Memory
from .paths import DATA, INBOX, PROCESSED, ensure_dirs
from .queue import build_queue
from .resume import facts_hash, facts_pack, load_resume, sync_from_profile
from .scoring import rank_pending, train_calibration
from .settings import load_profile, load_settings
from .sources import collect_all, fetch_details
from .sources.importer import normalize, read_file


def ingest_inbox(db: sqlite3.Connection, *, source: str = "user-import") -> dict:
    ensure_dirs()
    INBOX.mkdir(parents=True, exist_ok=True)
    PROCESSED.mkdir(parents=True, exist_ok=True)
    added = files = errors = 0
    for path in sorted(INBOX.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".json", ".jsonl", ".csv"):
            continue
        try:
            items = read_file(path)
        except Exception as exc:
            errors += 1
            dbm.log_event(db, None, "inbox_error", payload={"file": path.name, "error": str(exc)[:300]})
            continue
        for item in items:
            job = normalize(item, source=source)
            if not job:
                continue
            try:
                _, created = dbm.upsert_job(db, job)
                added += int(created)
            except ValueError:
                continue
        files += 1
        target = PROCESSED / path.name
        if target.exists():
            target = PROCESSED / f"{path.stem}-{datetime.now():%Y%m%d%H%M%S%f}{path.suffix}"
        shutil.move(str(path), str(target))
    return {"files": files, "added": added, "errors": errors}


def run_cycle(*, sources: list[str] | None = None, force_sources: bool = False,
              draft: bool = True, do_queue: bool = True, monitor: bool = False,
              do_train: bool = False, settings: dict | None = None,
              progress=None, stop_check=None) -> dict:
    started = time.time()
    ensure_dirs()
    lock = DATA / "cycle.lock"
    if lock.exists():
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            age = 0
        if age < 3 * 3600:
            return {"ok": False, "skipped": "previous cycle still running",
                    "started_at": dbm.now(), "steps": {}, "cancelled": False}
    try:
        lock.write_text(f"pid={os.getpid()} started={dbm.now()}", encoding="utf-8")
    except OSError:
        pass
    settings = settings or load_settings()
    profile = load_profile()
    resume = sync_from_profile(load_resume())
    ensure_dirs()
    db = dbm.init_db()
    memory = Memory(db, settings) if (settings.get("memory") or {}).get("enabled", True) else None
    report: dict = {"started_at": dbm.now(), "steps": {}, "cancelled": False}

    def stopped() -> bool:
        return bool(stop_check and stop_check())

    def note(step: str, data: dict) -> None:
        report["steps"][step] = data
        if progress:
            progress(step, data)

    try:
        note("inbox", ingest_inbox(db))
        if stopped():
            report["cancelled"] = True
            return report
        note("sources", collect_all(db, settings, force=force_sources, only=sources))
        if stopped():
            report["cancelled"] = True
            return report
        note("rank_cheap", rank_pending(db, settings, profile, resume))
        note("details", fetch_details(db, settings))
        from .dedup import deduplicate_jobs
        note("dedup", deduplicate_jobs(db))
        enrich_limit = int(((settings.get("sources") or {}).get("telegram") or {}).get("enrich_limit", 8))
        if enrich_limit > 0:
            from .sources.job_enrichment import enrich_pending
            note("enrich", enrich_pending(db, settings, limit=enrich_limit))
        note("rank_deep", rank_pending(db, settings, profile, resume))
        if stopped():
            report["cancelled"] = True
            return report
        if memory is not None:
            facts = facts_pack(resume, profile)
            memory_added = memory.sync_facts(resume, profile, facts_hash(facts))
            memory.sync_aliases(profile.get("skill_aliases") or {})
            threshold = float(settings.get("threshold", 70))
            index_rows = db.execute(
                """SELECT * FROM jobs WHERE duplicate_of IS NULL AND (eligible=1 OR score>=?)
                   ORDER BY score DESC LIMIT ?""",
                (threshold - 12, int((settings.get("memory") or {}).get("max_docs_per_cycle", 40)))
            ).fetchall()
            indexed_jobs = memory.index_jobs([dict(row) for row in index_rows])
            embedded = memory.embed_pending(limit=int((settings.get("memory") or {}).get("max_docs_per_cycle", 40)))
            note("memory", {"facts": memory_added, "job_chunks": indexed_jobs,
                            "embedded": embedded, "stats": memory.stats()})
        if draft:
            note("letters", ensure_letters(db, settings, profile, resume, memory=memory))
        else:
            note("letters", {"skipped": True})
        if do_queue:
            note("queue", build_queue(db, settings))
        if monitor:
            note("monitor", _monitor(db, settings))
        if do_train:
            note("calibration", train_calibration(db))
        retention = int((settings.get("privacy") or {}).get("retention_days", 365))
        note("retention", {"removed": dbm.vacuum_old(db, retention)})
        report["summary"] = summary(db)
        report["llm_facts_hash"] = __import__("hashlib").sha256(
            json.dumps(facts_pack(resume, profile), ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:12]
        report["ok"] = True
        try:
            from .notifier import enabled, send
            if enabled(settings):
                s = report["summary"]
                send(settings, f"AI Job Hunter: найдено {s['jobs']}, прошли {s['eligible']}, "
                               f"писем {s['drafted']}, откликов {s['sent']}, ответов {s['replied']}.")
        except Exception:
            pass
    except Exception as exc:
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        dbm.log_event(db, None, "cycle_error", payload={"error": str(exc)[:500]})
    finally:
        if memory is not None:
            memory.unload_embedder()
        report["duration_sec"] = round(time.time() - started, 2)
        db.close()
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass
    return report


def _monitor(db: sqlite3.Connection, settings: dict) -> dict:
    result: dict = {}
    if not settings.get("monitor", {}).get("enabled", True):
        return {"skipped": True}
    from .messages import sync_from_browser
    if (settings.get("monitor") or {}).get("use_browser"):
        result["messages"] = sync_from_browser(db, settings, limit=20)
    else:
        result["messages"] = {"skipped": "monitor.use_browser=false (ручной импорт сообщений)"}
    return result
