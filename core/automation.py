"""Autonomous search mode shared by CLI and Windows Task Scheduler."""
from __future__ import annotations

import socket
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db as dbm
from .pipeline import run_cycle
from .settings import load_settings
from .sources.telegram_discovery import (discover_catalogs, enable_recommended,
                                         validate_candidates)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_state(db: sqlite3.Connection) -> sqlite3.Row:
    row = db.execute("SELECT * FROM automation_state WHERE id=1").fetchone()
    if not row:
        db.execute("""INSERT INTO automation_state(id,updated_at) VALUES (1,?)""", (dbm.now(),))
        db.commit()
        row = db.execute("SELECT * FROM automation_state WHERE id=1").fetchone()
    return row


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _online() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=4).close()
        return True
    except OSError:
        return False


def status(db: sqlite3.Connection, settings: dict | None = None) -> dict:
    settings = settings or load_settings()
    cfg = settings.get("automation") or {}
    state = _get_state(db)
    next_run = state["next_run_at"] or ""
    if not next_run:
        next_run = (_now() + timedelta(minutes=int(cfg.get("light_minutes", 30)))).isoformat(timespec="seconds")
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "light_minutes": int(cfg.get("light_minutes", 30)),
        "full_hours": float(cfg.get("full_hours", 6)),
        "discovery_hours": float(cfg.get("discovery_hours", 24)),
        "auto_enable_channels": bool(cfg.get("auto_enable_channels", False)),
        "auto_enable_limit": int(cfg.get("auto_enable_limit", 100)),
        "prepare_best": bool(cfg.get("prepare_best", True)),
        "last_run_at": state["last_run_at"] or "",
        "last_full_at": state["last_full_at"] or "",
        "last_discovery_at": state["last_discovery_at"] or "",
        "last_startups_at": state["last_startups_at"] or "",
        "startups_hours": float((settings.get("startups") or {}).get("hours", 24)),
        "last_status": state["last_status"] or "",
        "last_error": state["last_error"] or "",
        "next_run_at": next_run,
        "task_hint": "schedule_task.ps1 -IntervalHours 0.5",
    }


def run_auto(*, force: bool = False, settings: dict | None = None) -> dict:
    settings = settings or load_settings()
    cfg = settings.get("automation") or {}
    if not cfg.get("enabled", True) and not force:
        return {"ok": False, "skipped": "automation disabled"}
    db = dbm.init_db()
    state = _get_state(db)
    now = _now()
    light_due = force or not _parse(state["last_run_at"]) or \
        now - _parse(state["last_run_at"]) >= timedelta(minutes=int(cfg.get("light_minutes", 30)))
    full_due = force or not _parse(state["last_full_at"]) or \
        now - _parse(state["last_full_at"]) >= timedelta(hours=float(cfg.get("full_hours", 6)))
    discovery_due = force or not _parse(state["last_discovery_at"]) or \
        now - _parse(state["last_discovery_at"]) >= timedelta(hours=float(cfg.get("discovery_hours", 24)))
    startup_cfg = settings.get("startups") or {}
    startups_due = bool(startup_cfg.get("enabled", True)) and (
        force or not _parse(state["last_startups_at"]) or
        now - _parse(state["last_startups_at"]) >= timedelta(hours=float(startup_cfg.get("hours", 24))))
    next_run = (now + timedelta(minutes=int(cfg.get("light_minutes", 30)))).isoformat(timespec="seconds")
    if not light_due:
        db.execute("UPDATE automation_state SET next_run_at=?, updated_at=? WHERE id=1",
                   (next_run, dbm.now()))
        db.commit()
        db.close()
        return {"ok": True, "skipped": "not due", "next_run_at": next_run}
    if not _online():
        db.execute("""UPDATE automation_state SET last_status='offline', last_error=?,
                      next_run_at=?, updated_at=? WHERE id=1""",
                   ("интернет недоступен", (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
                    dbm.now()))
        db.commit()
        db.close()
        return {"ok": False, "status": "offline", "reason": "интернет недоступен"}
    report: dict = {"started_at": dbm.now(), "mode": "full" if full_due else "light", "steps": {}}
    error = ""
    try:
        if full_due:
            report["steps"]["cycle"] = run_cycle(settings=settings, force_sources=True)
        else:
            report["steps"]["cycle"] = run_cycle(settings=settings, sources=["telegram"],
                                                 force_sources=True, draft=False, do_queue=False)
        from .readiness import compute_all as compute_readiness
        report["steps"]["readiness"] = compute_readiness(db, settings)
        try:
            from .probability_engine import maybe_retrain
            report["steps"]["probability"] = maybe_retrain(db)
        except Exception as exc:
            report["steps"]["probability"] = {"trained": False,
                                              "reason": f"{type(exc).__name__}: {exc}"}
        try:
            from . import autopilot
            mode = (settings.get("autopilot") or {}).get("mode", "observer")
            if mode in ("assisted", "autopilot"):
                report["steps"]["autopilot"] = autopilot.tick(db, settings)
        except Exception as exc:
            report["steps"]["autopilot"] = {"ok": False,
                                            "reason": f"{type(exc).__name__}: {exc}"}
        if discovery_due:
            catalog = discover_catalogs(db)
            validation = validate_candidates(db, limit=max(1, min(80, int(cfg.get("validate_limit", 40)))))
            report["steps"]["discovery"] = {**catalog, "validation": validation}
            if cfg.get("auto_enable_channels"):
                report["steps"]["auto_enable"] = enable_recommended(
                    db, settings, limit=int(cfg.get("auto_enable_limit", 100)))
        if startups_due:
            from . import startups
            try:
                report["steps"]["startups"] = startups.refresh(db, settings)
            except Exception as exc:
                report["steps"]["startups"] = {
                    "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        report["ok"] = True
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        report["ok"] = False
        report["error"] = error
    db.execute("""UPDATE automation_state SET last_run_at=?, last_status=?, last_error=?,
                  next_run_at=?, updated_at=? WHERE id=1""",
               (now.isoformat(timespec="seconds"), "ok" if report.get("ok") else "error",
                error[:500], next_run, dbm.now()))
    if full_due:
        db.execute("UPDATE automation_state SET last_full_at=? WHERE id=1",
                   (now.isoformat(timespec="seconds"),))
    if discovery_due:
        db.execute("UPDATE automation_state SET last_discovery_at=? WHERE id=1",
                   (now.isoformat(timespec="seconds"),))
    if startups_due:
        db.execute("UPDATE automation_state SET last_startups_at=? WHERE id=1",
                   (now.isoformat(timespec="seconds"),))
    db.commit()
    db.close()
    report["next_run_at"] = next_run
    return report
