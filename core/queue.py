"""Application queue with fail-closed sending.

Nothing is sent unless *all* of these are true:
  queue.enabled, settings.send_applications, the per-channel switch,
  an approved queue item (when require_manual_approval), a fresh eligible score,
  a non-empty checked letter, and the daily limit is not exceeded.
CAPTCHA/403/429 stops the batch immediately. Manual export always works.
"""
from __future__ import annotations

import csv
import json
import random
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import db as dbm
from .browser import BrowserBlocked, BrowserUnavailable, apply_hh
from .mathx import choose_under_budget, mmr
from .paths import EXPORTS, ensure_dirs
from .text import normalize

STATES = ("queued", "manual_review", "ready_to_review", "opening_application", "form_filling",
          "requires_user_action", "approved", "sending", "submitted",
          "submission_unverified", "exported", "failed", "blocked", "cancelled",
          "viewed", "replied", "interview", "offer", "rejected", "withdrawn", "unknown")


def _channel_for(job, settings: dict) -> str:
    channels = settings["queue"].get("channels") or {}
    if job["source"] == "hh_api" and (channels.get("hh_ui") or {}).get("enabled"):
        return "hh_ui"
    return "manual_export"


def daily_sent(db: sqlite3.Connection) -> int:
    today = date.today().isoformat()
    row = db.execute(
        "SELECT COUNT(*) FROM applications WHERE sent_at IS NOT NULL AND substr(sent_at,1,10)=?", (today,)
    ).fetchone()
    return int(row[0] or 0)


def _archetype(job) -> str:
    words = [w for w in normalize(str(job["title"] or "")).replace("-", " ").split()
             if len(w) > 3][:3]
    return " ".join(words) or str(job["source"] or "other")


def prune_queue(db: sqlite3.Connection) -> dict:
    """Reconcile queue after rescoring without losing prepared letters."""
    rows = db.execute(
        """SELECT a.id, a.job_id, a.state, a.letter_hash, a.resume_variant,
                  j.eligible, j.duplicate_of, j.letter, j.letter_hash AS current_hash,
                  j.reason, j.title
           FROM applications a JOIN jobs j ON j.id=a.job_id
           WHERE a.state IN ('queued','approved','manual_review','requires_user_action','form_filling')"""
    ).fetchall()
    cancelled = manual = 0
    for row in rows:
        reason = ""
        if row["duplicate_of"]:
            reason = "дубль вакансии: объединено с основной публикацией"
        elif not row["eligible"]:
            reason = "после пересчёта не проходит фильтры: " + (row["reason"] or "")[:180]
            if row["state"] != "manual_review":
                db.execute("""UPDATE applications SET state='manual_review', last_error=?,
                              updated_at=? WHERE id=?""",
                           (reason[:400], dbm.now(), row["id"]))
                dbm.log_event(db, row["job_id"], "application_moved_to_manual_review",
                              payload={"reason": reason[:400]}, application_id=row["id"])
                manual += 1
            continue
        elif not (row["letter"] or "").strip():
            reason = "письмо не подготовлено"
        elif (row["letter_hash"] or "") != (row["current_hash"] or ""):
            reason = "письмо изменилось после постановки в очередь — проверьте заново"
        else:
            continue
        db.execute("UPDATE applications SET state='cancelled', last_error=?, updated_at=? WHERE id=?",
                   (reason[:400], dbm.now(), row["id"]))
        dbm.log_event(db, row["job_id"], "application_cancelled_stale",
                      payload={"reason": reason[:400]}, application_id=row["id"])
        cancelled += 1
    db.commit()
    return {"checked": len(rows), "cancelled": cancelled, "manual_review": manual}


def build_queue(db: sqlite3.Connection, settings: dict, *, limit: int | None = None,
                min_score: float | None = None) -> dict:
    if not settings["queue"].get("enabled"):
        return {"queued": 0, "disabled": True,
                "reason": "queue.enabled=false — очередь не создаётся"}
    pruned = prune_queue(db)
    daily_limit = int(settings["queue"].get("daily_limit", 8))
    limit = limit or daily_limit * 2
    min_score = float(min_score if min_score is not None else settings["queue"].get("min_score", 80))
    window = int(settings["queue"].get("duplicate_window_days", 180))
    rows = db.execute(
        """SELECT j.* FROM jobs j
           WHERE j.eligible=1 AND j.letter<>'' AND j.duplicate_of IS NULL
             AND j.status IN ('new','draft','approved')
             AND j.score >= ?
             AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=j.id)
             AND NOT EXISTS (SELECT 1 FROM applications a2 JOIN jobs j2 ON j2.id=a2.job_id
                             WHERE j2.fingerprint=j.fingerprint AND a2.created_at > strftime('%Y-%m-%dT%H:%M:%S','now',?))
           ORDER BY j.score DESC LIMIT ?""",
        (min_score, f"-{window} days", max(limit * 3, 30)),
    ).fetchall()
    if not rows:
        return {"queued": 0, "candidates": 0, "budget_minutes": 0, "pruned": pruned["cancelled"]}

    minutes = int(settings["queue"].get("minutes_per_application", 12))
    daily_minutes = int(settings["queue"].get("daily_minutes", 90))
    remaining_minutes = max(0, daily_minutes - daily_sent(db) * minutes)
    candidates = []
    for job in rows:
        detail = dbm.job_detail(job)
        utility = float(job["score"] or 0) / 100.0
        if detail.get("needs_review"):
            utility *= 0.85
        candidates.append({
            "id": job["id"], "minutes": minutes, "utility": round(utility, 4),
            "group": _archetype(job), "job": job,
        })
    # Diversity first (avoid eight identical roles), then the daily time budget.
    diversified = mmr(candidates, limit=limit,
                      lambda_=float(settings["queue"].get("diversity_lambda", 0.72)))
    budget = choose_under_budget(
        [{"id": c["id"], "minutes": c["minutes"], "utility": c["utility"]} for c in diversified],
        remaining_minutes)
    selected_ids = set(budget["selected_ids"])
    selected = [c for c in diversified if c["id"] in selected_ids]
    added = 0
    for candidate in selected:
        job = candidate["job"]
        channel = _channel_for(job, settings)
        cur = db.execute(
            """INSERT OR IGNORE INTO applications(job_id, channel, state, letter_hash, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (job["id"], channel, "queued", job["letter_hash"], dbm.now(), dbm.now()),
        )
        if cur.rowcount:
            added += 1
            dbm.set_job_fields(db, job["id"], status="queued")
            dbm.log_event(db, job["id"], "queued",
                          payload={"channel": channel, "utility": candidate["utility"],
                                   "archetype": candidate["group"]})
    db.commit()
    return {"queued": added, "candidates": len(rows), "selected": len(selected),
            "budget_minutes": remaining_minutes, "minutes_used": budget["minutes_used"],
            "skipped_by_time": max(0, len(diversified) - len(selected)),
            "skipped_by_diversity": max(0, len(candidates) - len(diversified)),
            "pruned": pruned["cancelled"],
            "method": "MMR diversity + 0/1 knapsack"}


def list_applications(db: sqlite3.Connection, state: str | None = None, limit: int = 300) -> list[sqlite3.Row]:
    clause, params = "", []
    if state:
        clause, params = "WHERE a.state=?", [state]
    return db.execute(
        f"""SELECT a.*, j.title, j.company, j.source, j.score, j.url, j.status AS job_status,
                   j.letter, j.letter_source, j.eligible, j.reason
            FROM applications a JOIN jobs j ON j.id=a.job_id {clause}
            ORDER BY CASE a.state WHEN 'approved' THEN 0 WHEN 'queued' THEN 1 WHEN 'manual_review' THEN 2 ELSE 3 END,
                     j.score DESC LIMIT ?""",
        (*params, limit),
    ).fetchall()


def approve(db: sqlite3.Connection, app_id: int, *, actor: str = "user") -> dict:
    row = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "Отклик не найден"}
    if row["state"] not in ("queued", "failed", "requires_user_action", "form_filling",
                            "ready_to_review"):
        return {"ok": False, "reason": f"Нельзя одобрить из состояния {row['state']}"}
    job = dbm.get_job(db, row["job_id"])
    if not job or not job["letter"].strip():
        return {"ok": False, "reason": "Нет письма"}
    if not job["eligible"]:
        return {"ok": False, "reason": "Вакансия не проходит фильтры"}
    db.execute("UPDATE applications SET state='approved', approved_at=?, updated_at=? WHERE id=?",
               (dbm.now(), dbm.now(), app_id))
    dbm.set_job_fields(db, job["id"], status="approved")
    dbm.log_event(db, job["id"], "application_approved", actor=actor, application_id=app_id)
    db.commit()
    return {"ok": True, "job_id": job["id"]}


def mark_manual_submission(db: sqlite3.Connection, app_id: int, *, confirm: bool = False) -> dict:
    """Record an application the user *actually* sent outside this program.

    Never claims to submit anything; the caller must explicitly attest the
    external action. Only an exported manual application may be marked.
    """
    if not confirm:
        return {"ok": False, "reason": "Требуется подтверждение фактической отправки"}
    app = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not app or app["channel"] != "manual_export" or app["state"] != "exported":
        return {"ok": False, "reason": "Сначала выгрузите ручной отклик"}
    job = dbm.get_job(db, app["job_id"])
    if not job:
        return {"ok": False, "reason": "Вакансия не найдена"}
    return confirm_submission(db, app_id, method="manual_export",
                              source="user_attestation", confirm=True)


def confirm_submission(db: sqlite3.Connection, app_id: int, *, method: str,
                       source: str = "", confirm: bool = False,
                       external_id: str = "") -> dict:
    """Explicit user/portal confirmation; never inferred from a page visit."""
    if not confirm:
        return {"ok": False, "reason": "Требуется явное подтверждение отправки"}
    if method not in ("manual", "manual_export", "portal", "api", "email", "telegram"):
        return {"ok": False, "reason": "Неизвестный способ подтверждения"}
    app = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not app:
        return {"ok": False, "reason": "Отклик не найден"}
    if app["state"] in ("submitted", "viewed", "replied", "interview", "offer"):
        return {"ok": True, "status": app["state"], "already": True}
    job = dbm.get_job(db, app["job_id"])
    if not job:
        return {"ok": False, "reason": "Вакансия не найдена"}
    ts = dbm.now()
    followup = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(timespec="seconds")
    db.execute("""UPDATE applications SET state='submitted', sent_at=?, verified_at=?,
                  verification_method=?, verification_source=?, external_id=?, outcome='unknown',
                  followup_at=?, updated_at=? WHERE id=?""",
               (ts, ts, method, (source or "user_attestation")[:500],
                (external_id or "").strip()[:200], followup, ts, app_id))
    dbm.set_job_fields(db, job["id"], status="submitted")
    dbm.save_outcome(db, job, "applied", app_id)
    try:
        from .probability_engine import record_submitted
        fresh = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if fresh:
            record_submitted(db, fresh, job)
    except Exception:
        pass
    dbm.log_event(db, job["id"], "application_submitted_verified", actor="user",
                  payload={"method": method, "source": source,
                           "external_id": external_id}, application_id=app_id)
    db.commit()
    return {"ok": True, "status": "submitted", "verified_at": ts,
            "method": method, "reason": "Отправка подтверждена пользователем"}


def mark_unverified(db: sqlite3.Connection, app_id: int, *, confirm: bool = False,
                    note: str = "") -> dict:
    if not confirm:
        return {"ok": False, "reason": "Требуется подтверждение"}
    app = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not app:
        return {"ok": False, "reason": "Отклик не найден"}
    ts = dbm.now()
    followup = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
    db.execute("""UPDATE applications SET state='submission_unverified', last_error=?,
                  followup_at=?, updated_at=? WHERE id=?""",
               ((note or "Результат отправки не подтверждён площадкой")[:400], followup, ts, app_id))
    try:
        from .probability_engine import record_status
        record_status(db, app_id, "unknown", source="user_unverified", confidence=0.4, note=note)
    except Exception:
        pass
    dbm.log_event(db, app["job_id"], "application_submission_unverified", actor="user",
                  payload={"note": note[:300]}, application_id=app_id)
    db.commit()
    return {"ok": True, "status": "submission_unverified"}


def set_outcome(db: sqlite3.Connection, app_id: int, outcome: str, *, note: str = "") -> dict:
    """Track a real reply/status without ever treating silence as rejection."""
    allowed = ("viewed", "replied", "interview", "offer", "rejected", "withdrawn", "unknown")
    if outcome not in allowed:
        return {"ok": False, "reason": "Неизвестный статус"}
    app = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not app:
        return {"ok": False, "reason": "Отклик не найден"}
    ts = dbm.now()
    db.execute("""UPDATE applications SET state=?, outcome=?, last_error=?, updated_at=?
                  WHERE id=?""", (outcome, outcome, note[:400] or None, ts, app_id))
    try:
        from .probability_engine import record_status
        record_status(db, app_id, outcome, source="user_outcome", confidence=0.9, note=note)
    except Exception:
        pass
    dbm.log_event(db, app["job_id"], "application_outcome", actor="user",
                  payload={"outcome": outcome, "note": note[:300]}, application_id=app_id)
    db.commit()
    return {"ok": True, "status": outcome}


def due_followups(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("""SELECT a.id, a.followup_at, a.state, j.title, j.company
                         FROM applications a JOIN jobs j ON j.id=a.job_id
                         WHERE a.followup_at IS NOT NULL AND a.followup_at<>''
                           AND a.state IN ('submitted','submission_unverified','viewed')
                         ORDER BY a.followup_at LIMIT 50""").fetchall()
    return [dict(r) for r in rows]


def cancel(db: sqlite3.Connection, app_id: int, *, actor: str = "user") -> dict:
    row = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "Отклик не найден"}
    db.execute("UPDATE applications SET state='cancelled', updated_at=? WHERE id=?", (dbm.now(), app_id))
    dbm.log_event(db, row["job_id"], "application_cancelled", actor=actor, application_id=app_id)
    db.commit()
    return {"ok": True}


def _preconditions(db: sqlite3.Connection, settings: dict, app: sqlite3.Row) -> tuple[bool, str]:
    if not settings["queue"].get("enabled"):
        return False, "queue.enabled=false"
    channel = app["channel"]
    channel_cfg = (settings["queue"].get("channels") or {}).get(channel) or {}
    if not channel_cfg.get("enabled"):
        return False, f"канал {channel} выключен"
    # Manual export only writes a local file; it is not an application and must
    # stay available while automatic sending is disabled.
    if channel != "manual_export" and not settings.get("send_applications"):
        return False, "send_applications=false (безопасный режим)"
    # A manual-review item may ONLY be exported locally; it is never approved
    # for automated submission by this exception.
    if settings["queue"].get("require_manual_approval", True) and app["state"] != "approved":
        if not (channel == "manual_export" and app["state"] in ("manual_review", "requires_user_action")):
            return False, "нужно ручное одобрение"
    if channel != "manual_export" and daily_sent(db) >= int(settings["queue"].get("daily_limit", 8)):
        return False, "дневной лимит откликов исчерпан"
    return True, ""


def _last_sent_ts(db: sqlite3.Connection) -> float:
    row = db.execute("SELECT MAX(sent_at) FROM applications WHERE sent_at IS NOT NULL").fetchone()
    if not row or not row[0]:
        return 0.0
    try:
        return datetime.fromisoformat(row[0]).timestamp()
    except ValueError:
        return 0.0


def send_application(db: sqlite3.Connection, settings: dict, app_id: int, *,
                     confirm: bool = False, headless: bool | None = None) -> dict:
    app = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not app:
        return {"ok": False, "status": "missing", "reason": "Отклик не найден"}
    job = dbm.get_job(db, app["job_id"])
    if not job:
        return {"ok": False, "status": "missing", "reason": "Вакансия не найдена"}
    ok, why = _preconditions(db, settings, app)
    if not ok:
        return {"ok": False, "status": "blocked", "reason": why}
    if job["duplicate_of"]:
        return {"ok": False, "status": "blocked", "reason": "Дубликат вакансии"}
    if (job["letter_hash"] or "") != (app["letter_hash"] or ""):
        return {"ok": False, "status": "blocked", "reason": "Письмо изменилось после постановки в очередь; переодобрите"}
    if not job["letter"].strip():
        return {"ok": False, "status": "blocked", "reason": "Пустое письмо"}

    if app["channel"] == "manual_export":
        path = export_application(db, settings, app_id)
        db.execute("UPDATE applications SET state='exported', updated_at=? WHERE id=?", (dbm.now(), app_id))
        dbm.log_event(db, job["id"], "application_exported", payload={"path": str(path)}, application_id=app_id)
        db.commit()
        return {"ok": True, "status": "exported", "path": str(path),
                "reason": "Отклик выгружен для ручной отправки; автоматически не отправлялся"}

    dry = not confirm
    try:
        result = apply_hh(settings, job, job["letter"], dry_run=dry, headless=headless)
    except BrowserBlocked as exc:
        db.execute("UPDATE applications SET state='blocked', last_error=?, updated_at=? WHERE id=?",
                   (str(exc)[:400], dbm.now(), app_id))
        dbm.log_event(db, job["id"], "application_blocked", payload={"reason": str(exc)}, application_id=app_id)
        db.commit()
        return {"ok": False, "status": "blocked", "reason": f"Остановлено: {exc}. Требуется ручное действие."}
    except BrowserUnavailable as exc:
        return {"ok": False, "status": "unavailable", "reason": str(exc)}

    status = result.get("status")
    if status == "submitted":
        ts = dbm.now()
        db.execute("""UPDATE applications SET state='submitted', sent_at=?, attempts=attempts+1,
                      last_error=NULL, external_id=?, verified_at=?, verification_method='portal',
                      verification_source=?, outcome='unknown', followup_at=?, updated_at=?
                      WHERE id=?""",
                   (ts, job["source_id"], ts, "hh_ui:page_confirmation", ts, ts, app_id))
        dbm.set_job_fields(db, job["id"], status="submitted")
        dbm.save_outcome(db, job, "applied", app_id)
        dbm.log_event(db, job["id"], "application_submitted", application_id=app_id,
                      payload={"letter_attached": result.get("letter_attached"),
                               "verification": "portal"})
        db.commit()
        return {"ok": True, "status": "submitted", "reason": "Отклик отправлен"}
    if status in ("dry_run", "already", "unsupported", "needs_manual", "auth_required"):
        db.execute("UPDATE applications SET last_error=?, updated_at=? WHERE id=?",
                   (f"{status}: {result.get('reason','')}"[:400], dbm.now(), app_id))
        db.commit()
        return {"ok": False, "status": status, "reason": result.get("reason", ""),
                "screenshot": result.get("screenshot", "")}
    db.execute("UPDATE applications SET state='failed', attempts=attempts+1, last_error=?, updated_at=? WHERE id=?",
               (str(result.get("reason", ""))[:400], dbm.now(), app_id))
    dbm.log_event(db, job["id"], "application_failed", payload=result, application_id=app_id)
    db.commit()
    return {"ok": False, "status": status or "failed", "reason": result.get("reason", "")}


def send_batch(db: sqlite3.Connection, settings: dict, *, confirm: bool = False,
               limit: int | None = None, headless: bool | None = None) -> dict:
    limit = limit or int(settings["queue"].get("daily_limit", 8))
    rows = db.execute(
        "SELECT id FROM applications WHERE state='approved' ORDER BY updated_at LIMIT ?", (limit,)
    ).fetchall()
    results = []
    delays = settings["queue"].get("delay_seconds") or [25, 90]
    for index, row in enumerate(rows):
        outcome = send_application(db, settings, row["id"], confirm=confirm, headless=headless)
        results.append(outcome)
        if outcome.get("status") in ("blocked", "unavailable", "auth_required"):
            break
        if index < len(rows) - 1 and confirm:
            time.sleep(random.uniform(float(delays[0]), float(delays[1])))
    return {"attempted": len(results), "results": results,
            "sent_today": daily_sent(db)}


def export_application(db: sqlite3.Connection, settings: dict, app_id: int) -> Path:
    ensure_dirs()
    row = db.execute(
        """SELECT a.id, a.channel, j.title, j.company, j.url, j.source, j.score,
                  j.letter, j.reason, j.location
           FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.id=?""", (app_id,)
    ).fetchone()
    if not row:
        raise ValueError("Отклик не найден")
    EXPORTS.mkdir(parents=True, exist_ok=True)
    path = EXPORTS / f"application-{app_id}-{date.today().isoformat()}.txt"
    text = (f"Вакансия: {row['title']}\nКомпания: {row['company']}\nИсточник: {row['source']}\n"
            f"Ссылка: {row['url']}\nMatch: {row['score']}\n\n--- Сопроводительное письмо ---\n{row['letter']}\n")
    path.write_text(text, encoding="utf-8")
    return path


def export_queue_csv(db: sqlite3.Connection, path: Path | None = None) -> Path:
    ensure_dirs()
    path = path or (EXPORTS / f"queue-{date.today().isoformat()}.csv")
    rows = db.execute(
        """SELECT j.id, j.title, j.company, j.source, j.score, j.url, j.status,
                  a.state, a.channel, j.letter
           FROM applications a JOIN jobs j ON j.id=a.job_id
           ORDER BY j.score DESC"""
    ).fetchall()
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "title", "company", "source", "score", "url", "job_status",
                         "queue_state", "channel", "letter"])
        for row in rows:
            writer.writerow([row[k] for k in row.keys()])
    return path


def queue_stats(db: sqlite3.Connection) -> dict:
    counts = {state: 0 for state in STATES}
    for row in db.execute("SELECT state, COUNT(*) AS n FROM applications GROUP BY state"):
        counts[row["state"]] = row["n"]
    readiness = {r["readiness"] or "": r["n"] for r in db.execute(
        "SELECT COALESCE(readiness,'') readiness, COUNT(*) n FROM applications GROUP BY readiness")}
    return {"counts": counts, "sent_today": daily_sent(db),
            "approved": counts.get("approved", 0), "queued": counts.get("queued", 0),
            "readiness": readiness,
            "ready_to_submit": readiness.get("ready_to_submit", 0),
            "ready_to_review": readiness.get("ready_to_review", 0),
            "needs_action": sum(readiness.get(k, 0) for k in
                                ("needs_resume", "needs_letter", "needs_contact_info",
                                 "needs_verification", "requires_user_action"))}
