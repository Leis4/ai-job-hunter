"""JSON API for the React frontend. Reuses the existing Python core; no second backend.

Authentication is local-only: the SPA obtains the CSRF token from `/api/session`
and sends it in `X-JobHunter-Token` for writes. All data stays on this machine.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

from core import db as dbm
from core import onboarding as onboarding_io
from core import probability_engine
from core.analytics import (calibration_status, dashboard_data, export_all, funnel,
                            outcome_posterior, summary as analytics_summary)
from core.llm import LLM
from core.memory import Memory
from core.paths import DATA, EXPORTS, LOGS, ROOT
from core.queue import (approve, build_queue, cancel, confirm_submission, export_queue_csv,
                        due_followups, list_applications, mark_manual_submission,
                        mark_unverified, queue_stats, send_application, set_outcome)
from core import readiness
from core.resume import (VARIANTS, confirm_items, diff_variants, export_variant, facts_pack,
                         import_resume, list_resume_versions, load_resume, restore_version,
                         save_resume, select_variant, sync_from_profile, variant_summary,
                         variant_view, verified_skills)
from core.scoring import calibrated_probability, rank_pending, train_calibration
from core.settings import load_profile, load_settings, save_profile, save_settings
from core.sources import SOURCES, collect_all, source_overview
from core.startups import (KIND_LABELS as STARTUP_KIND_LABELS, STATE_LABELS as STARTUP_STATES,
                           get as get_lead, list_leads as list_startup_leads,
                           payload as startup_payload, prepare_draft as prepare_startup_draft,
                           refresh as refresh_startups, rescore as rescore_startups,
                           save_note as save_startup_note, set_state as set_startup_state,
                           stats as startup_stats)
from core.text import clean_text, scrub_contacts


def _row(value) -> dict:
    return dict(value) if value is not None else {}


def get(path: str, query: dict) -> tuple[int, dict | bytes, str]:
    """Return (status, payload, content_type)."""
    from .server import token
    if path == "/api/session":
        return 200, {"token": token(), "version": "4.0"}, "application/json"
    db = dbm.init_db()
    try:
        if path == "/api/dashboard":
            return 200, dashboard_payload(db, load_settings()), "application/json"
        if path == "/api/jobs":
            return 200, jobs_payload(db, query), "application/json"
        if path.startswith("/api/jobs/"):
            ident = path.rsplit("/", 1)[1]
            data = job_detail_payload(db, ident)
            return (200, data, "application/json") if data else (404, {"error": "not found"}, "application/json")
        if path == "/api/resume":
            return 200, resume_payload(), "application/json"
        if path == "/api/onboarding":
            return 200, onboarding_payload(), "application/json"
        if path == "/api/applications":
            readiness.compute_all(db, load_settings())
            state = (query.get("state", [""])[0] or "") or None
            rows = list_applications(db, state=state, limit=300)
            items = []
            for row in rows:
                item = _row(row)
                item["readiness_label"] = readiness.LABELS.get(item.get("readiness") or "", "")
                job = dbm.get_job(db, item["job_id"])
                if job:
                    detail = dbm.job_detail(job)
                    item["apply_url"] = detail.get("apply_url") or job["url"]
                item["history"] = [dict(e) for e in db.execute(
                    """SELECT type, actor, payload, created_at FROM events
                       WHERE application_id=? OR job_id=? ORDER BY id DESC LIMIT 6""",
                    (item["id"], item["job_id"]))]
                items.append(item)
            return 200, {"items": items, "stats": queue_stats(db),
                         "readiness": readiness.summary(db),
                         "followups": due_followups(db)}, "application/json"
        if path == "/api/readiness":
            return 200, {"summary": readiness.summary(db), "stats": queue_stats(db)}, "application/json"
        if path == "/api/messages":
            from core.messages import list_messages, message_stats
            return 200, {"items": [_row(r) for r in list_messages(db, limit=200)],
                         "stats": message_stats(db)}, "application/json"
        if path == "/api/settings":
            return 200, settings_payload(db), "application/json"
        if path == "/api/memory/status":
            from core.candidate_memory import CandidateMemory
            return 200, CandidateMemory(db, load_settings()).status(), "application/json"
        if path == "/api/memory/facts":
            from core.candidate_memory import CandidateMemory
            limit = int((query.get("limit", ["100"])[0] or 100))
            memory_store = CandidateMemory(db, load_settings())
            return 200, {"items": memory_store.facts(limit), "status": memory_store.status()}, "application/json"
        if path == "/api/memory/learning":
            from core.resume_memory import ResumeMemory
            return 200, {"items": ResumeMemory(db, load_settings()).history()}, "application/json"
        if path == "/api/telegram/channels":
            return 200, {"items": _telegram_channels(db, load_settings())}, "application/json"
        if path == "/api/telegram/candidates":
            return 200, {"items": _telegram_candidates(db)}, "application/json"
        if path == "/api/telegram/summary":
            return 200, _telegram_summary(db, load_settings()), "application/json"
        if path == "/api/automation/status":
            from core.automation import status as automation_status
            return 200, automation_status(db, load_settings()), "application/json"
        if path == "/api/hh/browser/status":
            from core.browser import hh_session_status
            return 200, hh_session_status(load_settings()), "application/json"
        if path == "/api/hh/oauth/status":
            from core import hh_oauth
            return 200, hh_oauth.status(), "application/json"
        if path == "/api/hh/oauth/callback":
            from core import hh_oauth
            code = (query.get("code", [""])[0] or "").strip()
            state = (query.get("state", [""])[0] or "").strip()
            error = (query.get("error", [""])[0] or "").strip()
            if error or not code:
                html = hh_oauth.callback_html(False, f"Авторизация не завершена: {error or 'нет code'}")
            else:
                try:
                    payload = hh_oauth.exchange(code, state)
                    name = ((payload.get("me") or {}).get("first_name") or "пользователь")
                    html = hh_oauth.callback_html(True, f"hh.ru подключён: {name}")
                except Exception as exc:
                    html = hh_oauth.callback_html(False, f"Ошибка OAuth: {exc}")
            return 200, html.encode("utf-8"), "text/html; charset=utf-8"
        if path == "/api/hh/status":
            from core.hh_client import status as hh_status
            return 200, hh_status(db), "application/json"
        if path == "/api/autopilot":
            from core import autopilot
            return 200, autopilot.status(db, load_settings()), "application/json"
        if path == "/api/autopilot/decisions":
            from core import autopilot
            state = (query.get("status", ["pending"])[0] or "pending")
            return 200, {"items": autopilot.decisions(db, state)}, "application/json"
        if path == "/api/autopilot/actions":
            rows = db.execute("""SELECT * FROM autopilot_actions ORDER BY id DESC LIMIT 300""").fetchall()
            return 200, {"items": [_row(r) for r in rows]}, "application/json"
        if path == "/api/analytics":
            return 200, dashboard_data(db), "application/json"
        if path == "/api/probability":
            return 200, {"summary": probability_engine.summary(db),
                         "quality": probability_engine.quality(db),
                         "estimate": probability_engine.estimate(db),
                         "dataset": probability_engine.dataset_status(db)}, "application/json"
        if path == "/api/outcomes":
            rows = db.execute("""SELECT o.*, COUNT(e.id) events
                                 FROM application_outcomes o
                                 LEFT JOIN application_outcome_events e ON e.outcome_id=o.id
                                 GROUP BY o.id ORDER BY COALESCE(o.sent_at,o.updated_at) DESC
                                 LIMIT 500""").fetchall()
            return 200, {"items": [_row(r) for r in rows],
                         "summary": probability_engine.summary(db),
                         "dataset": probability_engine.dataset_status(db),
                         "queue": probability_engine.queue_status(db)}, "application/json"
        if path.startswith("/api/outcomes/"):
            ident = path.rsplit("/", 1)[1]
            if ident.isdigit():
                row = db.execute("SELECT * FROM application_outcomes WHERE id=?", (int(ident),)).fetchone()
                if not row:
                    return 404, {"error": "not found"}, "application/json"
                events = [_row(e) for e in db.execute(
                    "SELECT * FROM application_outcome_events WHERE outcome_id=? ORDER BY id",
                    (int(ident),))]
                details = json.loads(row["details_json"] or "{}")
                features = json.loads(row["feature_json"] or "{}")
                used = bool(features.get("at_submission")) and row["status"] in (
                    probability_engine.RESOLVED_POSITIVE + probability_engine.RESOLVED_NEGATIVE)
                reason = ("Используется: подтверждённый исход и признаки на момент отклика."
                          if used else
                          ("Не используется: исход не подтверждён." if row["status"] not in
                           (probability_engine.RESOLVED_POSITIVE + probability_engine.RESOLVED_NEGATIVE)
                           else "Не используется: признаки не были зафиксированы на момент отклика."))
                return 200, {**_row(row), "events": events, "details": details,
                             "features": features, "ml": {"used": used, "reason": reason}}, "application/json"
        if path == "/api/startups":
            readiness.compute_all(db, load_settings())
            kind = (query.get("kind", [""])[0] or "").strip()
            kinds = [kind] if kind else None
            pay = (query.get("pay", [""])[0] or "").strip()
            search = (query.get("q", [""])[0] or "").strip()
            rows = list_startup_leads(db, kinds=kinds, paid_only=pay == "paid", query=search)
            return 200, {"items": [startup_payload(r) for r in rows],
                         "stats": startup_stats(db),
                         "kinds": STARTUP_KIND_LABELS, "states": STARTUP_STATES}, "application/json"
        if path.startswith("/api/startups/"):
            ident = path.rsplit("/", 1)[1]
            row = get_lead(db, ident)
            if not row:
                return 404, {"error": "not found"}, "application/json"
            data = startup_payload(row)
            data["related"] = [startup_payload(r) for r in list_startup_leads(db, limit=300)
                               if r["company"].casefold() == row["company"].casefold()
                               and r["id"] != row["id"]][:6]
            return 200, data, "application/json"
        if path == "/api/export/jobs.csv":
            return 200, (export_all(db, "csv").read_bytes(), "text/csv; charset=utf-8"), "csv"
        if path == "/api/export/analytics.json":
            return 200, (export_all(db, "json").read_bytes(), "application/json"), "json"
        if path == "/api/export/queue.csv":
            return 200, (export_queue_csv(db).read_bytes(), "text/csv; charset=utf-8"), "csv"
        if path.startswith("/api/drafts/"):
            name = Path(path.rsplit("/", 1)[1]).name
            target = LOGS / name
            if target.suffix.lower() == ".png" and target.is_file():
                return 200, target.read_bytes(), "image/png"
            return 404, {"error": "not found"}, "application/json"
        if path.startswith("/api/resume/job/"):
            ident = path.rsplit("/", 1)[1]
            job = dbm.get_job(db, ident)
            if not job:
                return 404, {"error": "not found"}, "application/json"
            from core import resume as resume_io
            resume = sync_from_profile(load_resume())
            detail = dbm.job_detail(job)
            variant = select_variant(resume, dict(job), detail)
            view = variant_view(resume, variant, job=dict(job), details=detail)
            markup = resume_io.render_html(view, resume.get("contact") or {})
            return 200, markup.encode("utf-8"), "text/html; charset=utf-8"
        return 404, {"error": "not found"}, "application/json"
    finally:
        db.close()


def _scores(detail: dict) -> dict:
    return detail.get("scores") or {}


def job_payload(row, detail: dict | None = None) -> dict:
    detail = detail if detail is not None else dbm.job_detail(row)
    scores = _scores(detail)
    skills = detail.get("skills") or {}
    return {
        "id": row["id"], "title": row["title"], "company": row["company"], "source": row["source"],
        "url": row["url"], "location": row["location"] or "", "work_mode": row["work_mode"] or "",
        "salary_min": row["salary_min"], "salary_max": row["salary_max"], "currency": row["currency"] or "RUB",
        "description": row["description"] or "", "published_at": row["published_at"] or "",
        "fetched_at": row["fetched_at"] or "", "score": row["score"] or 0,
        "score_low": row["score_low"] or 0, "score_high": row["score_high"] or 0,
        "eligible": bool(row["eligible"]), "needs_review": bool(row["needs_review"]),
        "favorite": bool(row["favorite"]) if "favorite" in row.keys() else False,
        "status": row["status"], "reason": row["reason"] or "", "letter": row["letter"] or "",
        "letter_source": row["letter_source"] or "", "eligibility": detail.get("eligibility", "review"),
        "category": detail.get("category", ""), "category_reasons": detail.get("category_reasons") or [],
        "failed_constraints": detail.get("failed_constraints") or [],
        "seniority": detail.get("seniority", "middle"), "scores": scores,
        "skills": {"matched": skills.get("matched") or [], "missing": skills.get("missing") or [],
                   "required_missing": skills.get("required_missing") or []},
        "experience": detail.get("experience") or {},
        "education": detail.get("education") or {},
        "license": detail.get("license") or {},
        "factors": detail.get("factors") or {},
        "interval": detail.get("interval") or {},
        "penalty": detail.get("penalty"),
        "hard_fail": bool(detail.get("hard_fail")),
        "realism": detail.get("realism") or {},
        "commercial_experience": detail.get("commercial_experience") or {},
        "schedule": detail.get("schedule") or {},
        "location_mismatch": bool(detail.get("location_mismatch")),
        "enrichment": detail.get("enrichment") or {},
        "apply_url": detail.get("apply_url") or "",
        "source_post_url": detail.get("source_post_url") or "",
        "enriched_at": detail.get("enriched_at") or "",
        "publication_count": 0,
        "red_flags": detail.get("red_flags") or [],
        "explanation": detail.get("explanation") or [],
    }


def _is_prospect(row) -> bool:
    """Conservative discovery lens, not an auto-apply eligibility override.

    Finds entry-level or unspecified-level jobs worth reading manually even
    when they miss an automatic skill/score threshold. Explicit hard failures
    never enter this lane.
    """
    detail = dbm.job_detail(row)
    if detail.get("category") == "opportunity":
        return True
    eligibility = detail.get("eligibility")
    if eligibility not in ("eligible", "weak", "review"):
        return False
    if bool(row["eligible"]):
        return True
    if float(row["score"] or 0) < 38:
        return False
    if detail.get("seniority") not in ("intern", "junior", "unspecified"):
        return False
    role_fit = float((detail.get("factors", {}).get("role") or {}).get("value", 0))
    skills = float((detail.get("scores") or {}).get("skill_match") or 0)
    return role_fit >= 0.30 and (skills >= 20 or detail.get("seniority") == "intern")


def _counts(db: sqlite3.Connection) -> dict:
    return analytics_summary(db)


def _events(db: sqlite3.Connection, limit: int = 12) -> list[dict]:
    rows = db.execute(
        """SELECT e.id,e.type,e.created_at,e.payload,j.title AS job_title
           FROM events e LEFT JOIN jobs j ON j.id=e.job_id
           ORDER BY e.id DESC LIMIT ?""", (limit,)).fetchall()
    return [_row(r) for r in rows]


def _skip_reasons(db: sqlite3.Connection) -> list[dict]:
    counts: dict[str, int] = {}
    for row in db.execute("SELECT reason FROM jobs WHERE eligible=0 AND duplicate_of IS NULL"):
        reason = row["reason"] or ""
        if "Исключённая" in reason:
            key = "Исключённая категория"
        elif "жёсткий фильтр опыта" in reason:
            key = "Недостаточный опыт"
        elif "senior/lead" in reason or "уровень" in reason:
            key = "Уровень senior/lead"
        elif "нужна проверка" in reason:
            key = "Неизвестные требования"
        else:
            key = "Ниже порога"
        counts[key] = counts.get(key, 0) + 1
    return [{"reason": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]


def dashboard_payload(db: sqlite3.Connection, settings: dict) -> dict:
    data = dashboard_data(db)
    llm = LLM(db, settings)
    runner = _runner_status()
    top = dbm.list_jobs(db, eligible_only=True, min_score=float(settings.get("threshold", 65)), limit=6)
    # Do not fill the dashboard with unrelated or senior vacancies merely
    # because too few roles clear the strict automatic threshold.
    all_candidates = dbm.list_jobs(db, limit=5000)
    prospects = [r for r in all_candidates if _is_prospect(r) and not r["eligible"]]
    if len(top) < 6:
        top = list(top) + prospects[:6-len(top)]
    last_update = db.execute("SELECT MAX(updated_at) FROM jobs").fetchone()[0]
    profile = load_profile()
    draft = onboarding_io.load_draft()
    try:
        from core.automation import status as automation_status
        automation = automation_status(db, settings)
    except Exception as exc:
        automation = {"enabled": False, "last_error": f"{type(exc).__name__}: {exc}"}
    metrics = {
        "source_types": db.execute(
            "SELECT COUNT(DISTINCT source) FROM jobs WHERE duplicate_of IS NULL").fetchone()[0],
        "telegram_active": db.execute(
            "SELECT COUNT(*) FROM telegram_channels WHERE enabled=1").fetchone()[0],
        "incomplete": db.execute(
            """SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NULL AND
               (company='' OR work_mode='' OR location='')""").fetchone()[0],
        "awaiting": db.execute(
            """SELECT COUNT(*) FROM applications WHERE state IN
               ('queued','manual_review','requires_user_action','opened','form_filling')""").fetchone()[0],
        "submitted": db.execute(
            "SELECT COUNT(*) FROM applications WHERE sent_at IS NOT NULL").fetchone()[0],
    }
    startups_top = [startup_payload(r) for r in list_startup_leads(db, limit=6)]
    readiness.compute_all(db, settings)
    actions = readiness.summary(db)
    qstats = queue_stats(db)
    actions["replies"] = sum(qstats["counts"].get(k, 0) for k in
                             ("viewed", "replied", "interview", "offer"))
    actions["needs_action"] = qstats.get("needs_action", actions["needs_action"])
    return {
        "summary": data["summary"],
        "funnel": data["funnel"],
        "series": data["series"],
        "sources": data["by_source"],
        "score_bands": data["score_bands"],
        "skills": data["skills"],
        "posterior": data["posterior"],
        "calibration": data["calibration"],
        "events": _events(db, 8),
        "skip_reasons": _skip_reasons(db),
        "top_jobs": [job_payload(r) for r in top],
        "prospects_count": len(prospects),
        "runner": runner,
        "last_update": last_update,
        "llm": llm.status(),
        "profile": {"verified_skills": len(profile.get("verified_skills") or []),
                    "has_resume": bool(load_resume().get("skills")),
                    "draft_status": (draft or {}).get("status")},
        "queue": queue_stats(db),
        "threshold": float(settings.get("threshold", 65)),
        "automation": automation,
        "startups": {"stats": startup_stats(db), "top": startups_top},
        "actions": actions,
        "source_health": source_overview(db, settings),
        "metrics": metrics,
    }


def jobs_payload(db: sqlite3.Connection, query: dict) -> dict:
    def val(name: str, default: str = "") -> str:
        raw = query.get(name, [default])
        return (raw[0] if isinstance(raw, list) else raw or default).strip()

    try:
        min_score = float(val("min_score", "0") or 0)
    except ValueError:
        min_score = 0.0
    try:
        max_exp = float(val("max_experience")) if val("max_experience") else None
    except ValueError:
        max_exp = None
    try:
        min_salary = int(val("min_salary")) if val("min_salary") else None
    except ValueError:
        min_salary = None
    page = max(0, int(val("page", "0") or 0))
    size = min(100, max(10, int(val("size", "30") or 30)))
    view = val("view", "all")
    rows = dbm.list_jobs(
        db, status=val("status") or None, source=val("source") or None,
        eligible_only=val("eligible") == "1" or view == "matched", min_score=min_score or None,
        search=val("q"), limit=5000, offset=0, role=val("role"),
        max_experience=max_exp, min_salary=min_salary, work_mode=val("mode"),
        favorite_only=val("favorite") == "1", exclude_company=val("exclude_company"),
        exclude_words=val("exclude_words"), all_required=val("all_required") == "1",
        skill=val("skill"), sort=val("sort", "score"))
    if view == "prospects":
        rows = [r for r in rows if _is_prospect(r)]
    elif view == "interns":
        rows = [r for r in rows if dbm.job_detail(r).get("seniority") in ("intern", "junior")
                or any(w in (r["title"] or "").casefold()
                       for w in ("стаж", "intern", "junior", "практик"))]
    total = len(rows)
    rows = rows[page * size:(page + 1) * size]
    facets = {r["source"]: r["n"] for r in db.execute(
        "SELECT source, COUNT(*) n FROM jobs WHERE duplicate_of IS NULL GROUP BY source")}
    counts = {r["canonical_id"]: r["n"] for r in db.execute(
        """SELECT canonical_id, COUNT(*) n FROM job_publications
           WHERE copy_id != canonical_id GROUP BY canonical_id""")}
    items = []
    for row in rows:
        payload = job_payload(row)
        payload["publication_count"] = int(counts.get(row["id"], 0))
        items.append(payload)
    return {"items": items, "total": total, "page": page, "size": size,
            "facets": {"sources": facets}}


def job_detail_payload(db: sqlite3.Connection, ident: str) -> dict | None:
    row = dbm.get_job(db, ident)
    if not row:
        return None
    detail = dbm.job_detail(row)
    profile, resume = load_profile(), sync_from_profile(load_resume())
    variant = select_variant(resume, dict(row), detail)
    apps = [_row(a) for a in db.execute(
        "SELECT * FROM applications WHERE job_id=? ORDER BY id DESC", (ident,)).fetchall()]
    messages = [_row(m) for m in db.execute(
        "SELECT * FROM messages WHERE job_id=? ORDER BY id DESC", (ident,)).fetchall()]
    events = [_row(e) for e in db.execute(
        "SELECT * FROM events WHERE job_id=? ORDER BY id DESC LIMIT 30", (ident,)).fetchall()]
    prob = calibrated_probability(
        row["score"] or 0,
        float((_scores(detail).get("skill_match") or 0) / 100),
        float(((detail.get("factors") or {}).get("role") or {}).get("value", 0)),
        float(((detail.get("factors") or {}).get("experience") or {}).get("value", 0)),
        bool(row["letter"]))
    payload = job_payload(row, detail)
    from core.dedup import publications
    pubs = publications(db, ident)
    letter_event = db.execute(
        "SELECT payload FROM events WHERE job_id=? AND type='letter_drafted' ORDER BY id DESC LIMIT 1",
        (ident,)).fetchone()
    letter_checks = {}
    if letter_event:
        try:
            letter_checks = (json.loads(letter_event["payload"] or "{}").get("checks") or {})
        except json.JSONDecodeError:
            letter_checks = {}
    payload.update({
        "applications": apps, "messages": messages, "events": events,
        "variant": variant, "variant_label": VARIANTS.get(variant, {}).get("label", variant),
        "calibrated_probability": prob,
        "personal_probability": probability_engine.estimate(
            db, {"title": payload.get("title", ""), "seniority": payload.get("seniority", ""),
                 "direction": payload.get("title", ""),
                 "features": probability_engine.snapshot_features(row, detail)}),
        "resume_preview_url": f"/api/resume/job/{ident}",
        "analysis": detail.get("analysis") or {},
        "threshold": float(load_settings().get("threshold", 65)),
        "publications": [dict(p) for p in pubs],
        "publication_count": max(0, len(pubs) - 1),
        "letter_checks": letter_checks,
    })
    return payload


def resume_payload() -> dict:
    profile = load_profile()
    resume = sync_from_profile(load_resume())
    completeness = {
        "skills": len(verified_skills(resume)),
        "projects": len([p for p in resume.get("projects") or [] if p.get("verified")]),
        "education": len([e for e in resume.get("education") or [] if e.get("verified")]),
        "contact": bool((resume.get("contact") or {}).get("email") or (resume.get("contact") or {}).get("phone")),
    }
    return {"resume": resume, "profile": profile, "variants": variant_summary(resume),
            "versions": list_resume_versions(), "completeness": completeness,
            "facts": facts_pack(resume, profile)}


def onboarding_payload() -> dict:
    draft = onboarding_io.load_draft()
    files = []
    if onboarding_io.UPLOADS.exists():
        files = [p.name for p in sorted(onboarding_io.UPLOADS.iterdir(),
                                       key=lambda p: p.stat().st_mtime, reverse=True)
                 if p.suffix.lower() in (".pdf", ".docx", ".txt", ".md")]
    settings = load_settings()
    router_ready = bool((settings.get("llm") or {}).get("aitunnel"))
    from core.qwen import QwenParser
    qwen_available, qwen_reason = QwenParser(settings).available()
    return {"draft": draft, "files": files, "runner": _parse_status(),
            "qwen": {"available": qwen_available, "reason": qwen_reason,
                     "model": settings.get("qwen", {}).get("model")},
            "aitunnel": {"available": router_ready}}


def settings_payload(db: sqlite3.Connection) -> dict:
    settings = load_settings()
    profile = load_profile()
    resume = sync_from_profile(load_resume())
    llm = LLM(db, settings)
    memory = Memory(db, settings)
    from core.qwen_engine import QwenEngine
    from core.submission import capabilities as submission_capabilities
    from core.candidate_memory import CandidateMemory
    qwen = QwenEngine(db, settings).status()
    submission = submission_capabilities(settings)
    candidate_memory = CandidateMemory(db, settings, memory=memory).status()
    math_ops = __import__("core.mathx", fromlist=["ROUTER"]).ROUTER.operations()
    data = dashboard_payload(db, settings)
    return {"settings": settings, "profile": profile, "resume_stats": {
        "skills": len(verified_skills(resume))}, "llm": llm.status(),
        "qwen": qwen, "submission": submission, "candidate_memory": candidate_memory,
        "memory": memory.stats(), "calibration": calibration_status(),
        "dashboard": data, "math_operations": math_ops,
        "source_status": source_overview(db, settings)}


def _runner_status() -> dict:
    from .server import RUNNER
    return RUNNER.status()


def _parse_status() -> dict:
    from .server import PARSE_RUNNER
    return PARSE_RUNNER.status()


# --------------------------------------------------------------------------- actions
def _sync_letter_hash(db: sqlite3.Connection, job_id: str, letter_hash: str | None) -> None:
    if not letter_hash:
        return
    db.execute(
        """UPDATE applications SET letter_hash=?, updated_at=?
           WHERE job_id=? AND state IN ('queued','manual_review','requires_user_action',
                                        'form_filling','approved','opening_application')""",
        (letter_hash, dbm.now(), job_id))
    db.commit()


def _prepare_job(db: sqlite3.Connection, settings: dict, profile: dict, resume: dict,
                 ident: str, *, manual: bool = False, variant: str = "") -> tuple[dict, int]:
    job = dbm.get_job(db, ident)
    if not job:
        return {"error": "Вакансия не найдена"}, 404
    detail = dbm.job_detail(job)
    is_strict = bool(job["eligible"])
    if not is_strict and not (manual and detail.get("eligibility") in ("weak", "review", "eligible")
                              and not detail.get("hard_fail") and not job["duplicate_of"]):
        return {"error": "Автоматический отбор не пройден; ручная подготовка возможна только после явного выбора пользователя и без жёстких ограничений"}, 409
    from core.letters import generate_letter
    memory = Memory(db, settings)
    if not job["letter"]:
        generate_letter(db, settings, profile, resume, job, force=False, memory=memory)
        job = dbm.get_job(db, ident)
    chosen = variant or select_variant(resume, dict(job), dbm.job_detail(job))
    if chosen not in VARIANTS:
        return {"error": "Неизвестная версия резюме"}, 400
    state = "queued" if is_strict else "manual_review"
    existing = db.execute("SELECT id, state FROM applications WHERE job_id=? AND channel='manual_export'",
                          (ident,)).fetchone()
    if existing and existing["state"] in ("submitted", "approved"):
        return {"error": "Отклик уже одобрен или отправлен"}, 409
    if existing and existing["state"] in ("opening_application", "form_filling",
                                         "requires_user_action", "blocked"):
        return {"error": f"Отклик уже в работе ({existing['state']})"}, 409
    if not existing:
        db.execute("""INSERT INTO applications(job_id,channel,state,letter_hash,resume_variant,
                      created_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
                   (ident, "manual_export", state, job["letter_hash"], chosen,
                    dbm.now(), dbm.now()))
    elif existing["state"] in ("cancelled", "failed", "exported", "manual_review", "queued"):
        db.execute("""UPDATE applications SET state=?, letter_hash=?, resume_variant=?,
                      last_error=NULL, updated_at=? WHERE id=?""",
                   (state, job["letter_hash"], chosen, dbm.now(), existing["id"]))
    dbm.log_event(db, ident, "manual_review_prepared" if not is_strict else "queued",
                  payload={"channel": "manual_export", "actor": "user", "manual": not is_strict})
    dbm.set_job_fields(db, ident, status="draft" if not is_strict else "queued")
    db.commit()
    return {"ok": True, "job": job_detail_payload(db, ident)}, 200


def _telegram_channels(db: sqlite3.Connection, settings: dict) -> list[dict]:
    configured = {str(x.get("channel") if isinstance(x, dict) else x).lstrip("@"): x
                  for x in ((settings.get("sources", {}).get("telegram") or {}).get("channels") or [])}
    rows = db.execute("SELECT * FROM telegram_channels ORDER BY channel").fetchall()
    out = []
    for row in rows:
        prefix = row["channel"] + ":%"
        jobs = db.execute(
            """SELECT COUNT(*) n, SUM(eligible) el FROM jobs
               WHERE source='telegram' AND duplicate_of IS NULL AND source_id LIKE ?""",
            (prefix,)).fetchone()
        cfg = configured.get(row["channel"]) or {}
        out.append({**dict(row), "configured": bool(cfg),
                    "jobs": int(jobs["n"] or 0), "eligible": int(jobs["el"] or 0)})
    return out


def _telegram_candidates(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        """SELECT * FROM telegram_candidates ORDER BY
           CASE status WHEN 'qualified' THEN 0 WHEN 'discovered' THEN 1 WHEN 'validating' THEN 2
                       WHEN 'enabled' THEN 3 ELSE 4 END, quality DESC, channel"""
    ).fetchall()
    return [dict(row) for row in rows]


def _telegram_summary(db: sqlite3.Connection, settings: dict) -> dict:
    candidates = db.execute(
        """SELECT COUNT(*) total,
                  SUM(CASE WHEN status IN ('discovered') THEN 1 ELSE 0 END) discovered,
                  SUM(CASE WHEN status IN ('validating') THEN 1 ELSE 0 END) validating,
                  SUM(CASE WHEN status IN ('qualified') THEN 1 ELSE 0 END) qualified,
                  SUM(CASE WHEN status IN ('enabled') THEN 1 ELSE 0 END) enabled,
                  SUM(CASE WHEN status IN ('inactive') THEN 1 ELSE 0 END) inactive,
                  SUM(CASE WHEN status IN ('unavailable') THEN 1 ELSE 0 END) unavailable,
                  SUM(CASE WHEN status IN ('irrelevant') THEN 1 ELSE 0 END) irrelevant
           FROM telegram_candidates""").fetchone()
    channels = db.execute(
        """SELECT COUNT(*) total, SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) active,
                  COALESCE(SUM(vacancies),0) extracted, COALESCE(SUM(posts),0) posts,
                  MAX(last_run_at) last_run
           FROM telegram_channels""").fetchone()
    jobs = db.execute(
        """SELECT COUNT(*) unique_jobs, SUM(CASE WHEN eligible=1 THEN 1 ELSE 0 END) eligible,
                  SUM(CASE WHEN json_extract(detail,'$.enrichment') IS NOT NULL THEN 1 ELSE 0 END) enriched
           FROM jobs WHERE source='telegram' AND duplicate_of IS NULL""").fetchone()
    return {
        "candidates": {key: int(candidates[key] or 0) for key in candidates.keys()},
        "channels": {key: (int(channels[key] or 0) if key != "last_run" else channels[key] or "")
                     for key in channels.keys()},
        "jobs": {key: int(jobs[key] or 0) for key in jobs.keys()},
        "last_run_at": channels["last_run"] or "",
    }


def action(db: sqlite3.Connection, path: str, body: dict, files=None) -> tuple[dict, int]:
    settings = load_settings()
    profile, resume = load_profile(), sync_from_profile(load_resume())
    from .server import RUNNER, PARSE_RUNNER
    if path == "/api/hh/browser/login":
        from core.browser import hh_login
        return hh_login(settings, timeout_minutes=max(1, min(20, int(body.get("timeout_minutes") or 8)))), 200
    if path == "/api/hh/browser/check":
        from core.browser import hh_session_status
        return hh_session_status(settings), 200
    if path == "/api/hh/browser/capture-history":
        from core.browser import capture_history
        return capture_history(settings, pages=max(1, min(20, int(body.get("pages") or 5)))), 200
    if path == "/api/hh/browser/clear":
        from core.browser import clear_hh_session
        return clear_hh_session(), 200
    if path == "/api/hh/oauth/start":
        from core import hh_oauth
        return hh_oauth.start(), 200
    if path == "/api/hh/oauth/credentials":
        from core import hh_oauth
        return hh_oauth.save_credentials(str(body.get("client_id") or ""),
                                         str(body.get("client_secret") or "")), 200
    if path == "/api/hh/oauth/diagnostics":
        from core import hh_oauth
        from core.hh_api_client import HHClient, HHAPIError
        status = hh_oauth.status()
        if not status.get("connected"):
            return {"ok": False, "oauth": status, "steps": [],
                    "reason": "OAuth не подключён"}, 200
        try:
            result = HHClient().diagnostics()
            return {"ok": result.get("ok"), "oauth": status, "steps": result.get("steps")}, 200
        except HHAPIError as exc:
            return {"ok": False, "oauth": status,
                    "steps": [{"step": "client", "ok": False, "kind": exc.kind,
                               "reason": exc.reason}]}, 200
    if path == "/api/hh/oauth/disconnect":
        from core import hh_oauth
        hh_oauth.clear_tokens()
        return {"ok": True}, 200
    if path == "/api/autopilot/policy":
        from core import autopilot
        return autopilot.set_policy(db, settings, body.get("policy") or {}), 200
    if path == "/api/autopilot/sync-profile":
        from core import autopilot
        return autopilot.sync_profile(db), 200
    if path == "/api/autopilot/sync-history":
        from core import autopilot
        return autopilot.sync_history(db, settings,
                                      pages=max(1, min(500, int(body.get("pages") or 500)))), 200
    if path == "/api/autopilot/search":
        from core import autopilot
        return autopilot.search(db, settings, pages=max(1, min(10, int(body.get("pages") or 3)))), 200
    if path == "/api/autopilot/run":
        from core import autopilot
        return autopilot.run_once(db, settings), 200
    if path == "/api/autopilot/stop":
        from core import autopilot
        return autopilot.stop(db, settings), 200
    if path == "/api/autopilot/decision":
        from core import autopilot
        return autopilot.decide(db, int(body.get("id") or 0), str(body.get("status") or ""),
                                str(body.get("answer") or "")), 200
    if path == "/api/cycle/start":
        return {"ok": RUNNER.start(settings)}, 200
    if path == "/api/cycle/stop":
        RUNNER.cancel()
        return {"ok": True}, 200
    if path == "/api/onboarding/qwen":
        draft = onboarding_io.load_draft()
        source = Path(str((draft or {}).get("source_text_path") or (draft or {}).get("source_file") or ""))
        if not source.exists():
            return {"error": "Исходный файл не найден"}, 400
        started = PARSE_RUNNER.start("qwen", source, settings)
        return {"ok": started}, 200 if started else 409
    if path == "/api/onboarding/aitunnel":
        draft = onboarding_io.load_draft()
        source = Path(str((draft or {}).get("source_text_path") or (draft or {}).get("source_file") or ""))
        if not source.exists():
            return {"error": "Исходный файл не найден"}, 400
        started = PARSE_RUNNER.start("aitunnel", source, settings)
        return {"ok": started}, 200 if started else 409
    if path == "/api/onboarding/cancel":
        PARSE_RUNNER.cancel()
        return {"ok": True}, 200
    if path == "/api/onboarding/discard":
        onboarding_io.discard()
        return {"ok": True}, 200
    if path == "/api/onboarding/confirm":
        draft = onboarding_io.load_draft()
        if not draft:
            return {"error": "Черновик не найден"}, 400
        form = dict(body or {})
        for key in ("mode_remote", "mode_hybrid", "mode_office"):
            form[key] = "1" if body.get(key) else ""  # JSON booleans -> form flags
        onboarding_io.apply_confirmation(draft, form)
        if body.get("start"):
            RUNNER.start(settings)
        return {"ok": True, "draft": onboarding_io.load_draft()}, 200
    if path == "/api/onboarding/upload":
        if not files:
            return {"error": "Файл не получен"}, 400
        _, filename, data = files[0]
        suffix = Path(filename).suffix.lower()
        if suffix not in (".pdf", ".docx", ".txt", ".md"):
            return {"error": "Поддерживаются PDF, DOCX, TXT, MD"}, 400
        if len(data) > 20_000_000:
            return {"error": "Файл больше 20 МБ"}, 400
        onboarding_io.UPLOADS.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(filename).stem)[:60] or "resume"
        target = onboarding_io.UPLOADS / f"{date.today():%Y%m%d}-{safe}{suffix}"
        target.write_bytes(data)
        try:
            draft = onboarding_io.extract_file(target, db=db, settings=settings)
        except Exception as exc:
            return {"error": f"Не удалось разобрать: {exc}"}, 400
        return {"ok": True, "draft": draft}, 200
    if path == "/api/onboarding/import":
        filename = Path(str(body.get("filename") or "")).name
        target = onboarding_io.UPLOADS / filename
        if not filename or not target.exists():
            return {"error": "Файл не найден"}, 400
        draft = onboarding_io.extract_file(target, db=db, settings=settings)
        return {"ok": True, "draft": draft}, 200
    if path == "/api/telegram/channels":
        action_name = str(body.get("action") or "list")
        raw = str(body.get("channel") or body.get("url") or "").strip()
        channel = raw.rstrip("/").split("/")[-1].lstrip("@")
        tg = settings.setdefault("sources", {}).setdefault("telegram", {})
        channels = tg.setdefault("channels", [])
        if action_name == "add":
            if not re.match(r"^[A-Za-z0-9_]{4,32}$", channel):
                return {"error": "Некорректный канал: нужен @name или https://t.me/name"}, 400
            if any(str(x.get("channel") if isinstance(x, dict) else x).lstrip("@") == channel
                   for x in channels):
                return {"error": "Канал уже подключён"}, 409
            channels.append({"channel": channel, "category": str(body.get("category") or "custom"),
                             "enabled": True})
            save_settings(settings)
            db.execute("""INSERT OR IGNORE INTO telegram_channels(channel,url,category,enabled,updated_at)
                          VALUES (?,?,?,?,?)""",
                       (channel, f"https://t.me/{channel}", str(body.get("category") or "custom"),
                        1, dbm.now()))
            db.commit()
        elif action_name == "toggle":
            enabled = bool(body.get("enabled"))
            db.execute("UPDATE telegram_channels SET enabled=?, updated_at=? WHERE channel=?",
                       (int(enabled), dbm.now(), channel))
            for item in channels:
                if isinstance(item, dict) and item.get("channel") == channel:
                    item["enabled"] = enabled
            save_settings(settings)
            db.commit()
        elif action_name == "remove":
            tg["channels"] = [x for x in channels
                              if str(x.get("channel") if isinstance(x, dict) else x).lstrip("@") != channel]
            db.execute("DELETE FROM telegram_channels WHERE channel=?", (channel,))
            save_settings(settings)
            db.commit()
        else:
            return {"error": "Неизвестное действие"}, 400
        settings = load_settings()
        return {"ok": True, "items": _telegram_channels(db, settings)}, 200
    if path == "/api/telegram/discover":
        from core.sources.telegram_discovery import discover_catalogs
        catalogs = body.get("catalogs")
        result = discover_catalogs(db, catalogs=catalogs if isinstance(catalogs, list) else None,
                                   csv_text=str(body.get("csv") or ""))
        return {"ok": True, **result, "items": _telegram_candidates(db)}, 200
    if path == "/api/telegram/validate":
        from core.sources.telegram_discovery import validate_candidates
        channels = body.get("channels")
        result = validate_candidates(db, limit=max(1, min(50, int(body.get("limit") or 20))),
                                     channels=[str(x) for x in channels] if isinstance(channels, list) else None)
        return {"ok": True, **result, "items": _telegram_candidates(db)}, 200
    if path == "/api/telegram/enable":
        from core.sources.telegram_discovery import enable_candidates
        channels = [str(x) for x in (body.get("channels") or []) if str(x).strip()]
        if not channels:
            return {"error": "Выберите хотя бы один канал"}, 400
        result = enable_candidates(db, settings, channels)
        return {**result, "channels": _telegram_channels(db, load_settings())}, 200
    if path == "/api/telegram/pause-inactive":
        from core.sources.telegram_discovery import pause_inactive
        result = pause_inactive(db, settings, days=max(7, min(365, int(body.get("days") or 45))))
        return {"ok": True, **result, "channels": _telegram_channels(db, load_settings())}, 200
    if path == "/api/startups/refresh":
        report = refresh_startups(db, settings, force=bool(body.get("force")))
        return {"ok": True, **report}, 200
    if path == "/api/startups/rescore":
        return {"ok": True, **rescore_startups(db), "stats": startup_stats(db)}, 200
    if path.startswith("/api/startups/"):
        parts = path.strip("/").split("/")
        ident = parts[2] if len(parts) > 2 else ""
        action_name = parts[3] if len(parts) > 3 else ""
        if action_name == "draft":
            result = prepare_startup_draft(db, ident)
            if result.get("error"):
                return result, 409 if result.get("warning") else 404
            return result, 200
        if action_name == "state":
            state = str(body.get("state") or "").strip()
            if state == "contacted" and body.get("confirm") is not True:
                return {"error": "Требуется явное подтверждение отправки"}, 400
            result = set_startup_state(db, ident, state, note=str(body.get("note") or ""))
            if result.get("error"):
                return result, 400
            return {**result, "stats": startup_stats(db)}, 200
        if action_name == "note":
            result = save_startup_note(db, ident, str(body.get("note") or ""))
            return (result, 200) if result.get("ok") else (result, 404)
        return {"error": "Неизвестное действие"}, 404
    if path == "/api/telegram/reprocess":
        from core.sources.job_enrichment import reprocess_telegram
        limit = max(1, min(500, int(body.get("limit") or 200)))
        rows = db.execute("""SELECT * FROM jobs WHERE source='telegram' AND duplicate_of IS NULL
                             ORDER BY fetched_at DESC LIMIT ?""", (limit,)).fetchall()
        changed = 0
        for row in rows:
            res = reprocess_telegram(db, row)
            changed += int(bool(res.get("updated")))
        rank_pending(db, settings, profile, resume)
        return {"ok": True, "checked": len(rows), "changed": changed}, 200
    if path == "/api/telegram/enable-recommended":
        from core.sources.telegram_discovery import enable_recommended
        result = enable_recommended(db, settings,
                                    limit=max(1, min(150, int(body.get("limit") or 100))),
                                    min_quality=float(body.get("min_quality") or 45))
        return {"ok": True, **result, "channels": _telegram_channels(db, load_settings())}, 200
    if path == "/api/telegram/disable-irrelevant":
        from core.sources.telegram_discovery import disable_irrelevant
        result = disable_irrelevant(db, settings)
        return {"ok": True, **result, "channels": _telegram_channels(db, load_settings())}, 200
    if path == "/api/telegram/add-bulk":
        from core.sources.telegram_discovery import discover_catalogs
        text = str(body.get("text") or body.get("csv") or "")
        if not text.strip():
            return {"error": "Нет каналов для добавления"}, 400
        result = discover_catalogs(db, csv_text=text)
        return {"ok": True, **result, "items": _telegram_candidates(db)}, 200
    if path == "/api/automation/enable":
        settings.setdefault("automation", {})["enabled"] = True
        save_settings(settings)
        from core.automation import status as automation_status
        return {"ok": True, **automation_status(db, load_settings())}, 200
    if path == "/api/automation/pause":
        settings.setdefault("automation", {})["enabled"] = False
        save_settings(settings)
        from core.automation import status as automation_status
        return {"ok": True, **automation_status(db, load_settings())}, 200
    if path == "/api/automation/run":
        from .server import RUNNER
        started = RUNNER.start(settings)
        return {"ok": started, "started": started, "reason": "" if started else "цикл уже выполняется"}, 200
    if path == "/api/jobs/reprocess":
        ident = str(body.get("id") or "")
        job = dbm.get_job(db, ident)
        if not job:
            return {"error": "Вакансия не найдена"}, 404
        from core.sources.job_enrichment import reprocess_telegram
        result = reprocess_telegram(db, job)
        rank_pending(db, settings, profile, resume)
        return {**result, "job": job_detail_payload(db, ident)}, 200
    if path == "/api/jobs/enrich":
        ident = str(body.get("id") or "")
        job = dbm.get_job(db, ident)
        if not job:
            return {"error": "Вакансия не найдена"}, 404
        from core.sources.job_enrichment import enrich_job
        result = enrich_job(db, job)
        if result.get("ok"):
            rank_pending(db, settings, profile, resume)
        return result, (200 if result.get("ok") else 409)
    if path == "/api/jobs/favorite":
        ident = str(body.get("id") or "")
        job = dbm.get_job(db, ident)
        if not job:
            return {"error": "Вакансия не найдена"}, 404
        dbm.set_job_fields(db, ident, favorite=0 if job["favorite"] else 1)
        return {"ok": True, "favorite": not bool(job["favorite"])}, 200
    if path == "/api/jobs/prepare":
        ident = str(body.get("id") or "")
        return _prepare_job(db, settings, profile, resume, ident,
                            manual=bool(body.get("manual_review")),
                            variant=str(body.get("variant") or ""))
    if path == "/api/prepare/best":
        limit = max(1, min(5, int(body.get("limit") or 3)))
        rows = db.execute(
            """SELECT j.id FROM jobs j
               WHERE j.eligible=1 AND j.duplicate_of IS NULL AND j.status IN ('new','draft','queued')
                 AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=j.id
                                 AND a.state IN ('queued','manual_review','approved','submitted',
                                                 'form_filling','requires_user_action','opening_application'))
               ORDER BY j.score DESC LIMIT ?""", (limit * 3,)).fetchall()
        prepared = []
        for row in rows:
            result, status = _prepare_job(db, settings, profile, resume, row["id"])
            if status == 200:
                job = result.get("job") or {}
                prepared.append({"id": job.get("id"), "title": job.get("title"),
                                 "company": job.get("company"), "score": job.get("score"),
                                 "variant": job.get("variant_label")})
            if len(prepared) >= limit:
                break
        return {"ok": True, "prepared": prepared, "count": len(prepared), "requested": limit,
                "cost_note": "Письма генерируются только для выбранных вакансий и кешируются."}, 200
    if path == "/api/jobs/letter":
        ident = str(body.get("id") or "")
        job = dbm.get_job(db, ident)
        if not job:
            return {"error": "Вакансия не найдена"}, 404
        text = clean_text(str(body.get("text") or ""))
        if not text:
            return {"error": "Пустое письмо"}, 400
        import hashlib
        dbm.set_job_fields(db, ident, letter=text,
                           letter_hash=hashlib.sha256(text.encode()).hexdigest()[:20],
                           letter_source="manual",
                           status="draft" if job["status"] == "new" else job["status"])
        _sync_letter_hash(db, ident, hashlib.sha256(text.encode()).hexdigest()[:20])
        return {"ok": True, "job": job_detail_payload(db, ident)}, 200
    if path == "/api/jobs/generate-letter":
        ident = str(body.get("id") or "")
        job = dbm.get_job(db, ident)
        if not job:
            return {"error": "Вакансия не найдена"}, 404
        from core.letters import generate_letter
        result = generate_letter(db, settings, profile, resume, job, force=True, memory=Memory(db, settings))
        fresh = dbm.get_job(db, ident)
        _sync_letter_hash(db, ident, fresh["letter_hash"] if fresh else None)
        return {"ok": True, "source": result["source"], "job": job_detail_payload(db, ident)}, 200
    if path == "/api/resume/contact":
        from core.candidate_memory import CandidateMemory
        memory_store = CandidateMemory(db, settings)
        contact = memory_store.update_contact({key: body.get(key) for key in
            ("first_name", "last_name", "middle_name", "email", "phone", "city",
             "telegram", "citizenship") if key in body})
        for app in db.execute("SELECT id, note FROM applications WHERE note IS NOT NULL AND note<>''"):
            try:
                note = json.loads(app["note"])
            except (TypeError, ValueError):
                continue
            required = note.get("empty_required") or []
            if not required:
                continue
            note["empty_required"] = [label for label in required
                                      if readiness._label_gap(label, contact)]
            db.execute("UPDATE applications SET note=?, updated_at=? WHERE id=?",
                       (json.dumps(note, ensure_ascii=False), dbm.now(), app["id"]))
        db.commit()
        readiness.compute_all(db, settings)
        return {"ok": True, "contact": contact, "readiness": readiness.summary(db)}, 200
    if path == "/api/readiness/recompute":
        result = readiness.compute_all(db, settings)
        return {"ok": True, **result, "summary": readiness.summary(db)}, 200
    if path == "/api/queue/batch-approve":
        ids = [int(x) for x in (body.get("ids") or []) if str(x).isdigit()]
        approved, rejected = [], []
        for app_id in ids:
            app = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
            if not app:
                rejected.append({"id": app_id, "reason": "не найден"})
                continue
            state = readiness.application(db, app)
            if state["readiness"] not in ("ready_to_review", "ready_to_submit"):
                rejected.append({"id": app_id, "reason": state["reason"]})
                continue
            result = approve(db, app_id, actor="user")
            (approved if result.get("ok") else rejected).append(
                app_id if result.get("ok") else {"id": app_id, "reason": result.get("reason")})
        readiness.compute_all(db, settings)
        return {"ok": True, "approved": approved, "rejected": rejected,
                "summary": readiness.summary(db)}, 200
    if path == "/api/outcomes/status":
        return probability_engine.set_status(
            db, int(body.get("id") or 0), str(body.get("status") or ""),
            note=str(body.get("note") or "")), 200
    if path == "/api/outcomes/upload":
        if not files:
            return {"error": "Файлы не получены"}, 400
        payload = []
        for name, filename, data in files:
            if len(data) > 20_000_000:
                return {"error": f"{filename}: файл больше 20 МБ"}, 400
            payload.append((filename, data))
        result = probability_engine.parse_history_files(db, payload)
        result["dataset"] = probability_engine.dataset_status(db)
        return result, 200
    if path == "/api/outcomes/enrich":
        return probability_engine.enrich_batch(
            db, limit=max(1, min(50, int(body.get("limit") or 10))),
            use_api=body.get("use_api") is not False), 200
    if path == "/api/outcomes/enqueue":
        result = probability_engine.enqueue_all(db)
        return {**result, "dataset": probability_engine.dataset_status(db)}, 200
    if path == "/api/outcomes/process":
        result = probability_engine.process_queue(
            db, limit=max(1, min(50, int(body.get("limit") or 20))),
            use_api=body.get("use_api") is not False)
        return result, 200
    if path == "/api/outcomes/dataset":
        return probability_engine.dataset_status(db), 200
    if path == "/api/outcomes/preview":
        return probability_engine.preview(db, str(body.get("source") or "json"),
                                          str(body.get("text") or ""),
                                          body.get("data")), 200
    if path == "/api/outcomes/import":
        rows = body.get("rows") or []
        if not isinstance(rows, list):
            return {"error": "rows должен быть массивом"}, 400
        return probability_engine.commit(db, str(body.get("source") or "json"), rows), 200
    if path == "/api/outcomes/hh-preview":
        return probability_engine.preview_hh_api(
            db, None, pages=max(1, min(500, int(body.get("pages") or 500)))), 200
    if path == "/api/outcomes/manual":
        row = dict(body)
        if not str(row.get("title") or "").strip():
            return {"error": "Укажите должность"}, 400
        status = probability_engine.canonical_status(str(row.get("status") or "unknown"))
        if status not in probability_engine.STATUSES:
            return {"error": "Неизвестный статус"}, 400
        if not str(row.get("sent_at") or "").strip():
            return {"error": "Укажите дату отклика"}, 400
        result = probability_engine.commit(db, "manual", [row])
        return {**result, "summary": probability_engine.summary(db)}, 200
    if path == "/api/probability/retrain":
        result = probability_engine.train(db, force=bool(body.get("force")))
        return {**result, "quality": probability_engine.quality(db),
                "summary": probability_engine.summary(db)}, 200
    if path == "/api/queue/build":
        return build_queue(db, settings), 200
    if path.startswith("/api/queue/"):
        parts = path.strip("/").split("/")
        if len(parts) != 4:
            return {"error": "Неверный путь действия с откликом"}, 404
        try:
            app_id = int(parts[2])
        except ValueError:
            return {"error": "Неверный идентификатор отклика"}, 400
        verb = parts[3]
        if verb == "approve":
            result = approve(db, app_id)
        elif verb == "cancel":
            result = cancel(db, app_id)
        elif verb == "send":
            result = send_application(db, settings, app_id, confirm=bool(body.get("confirm")))
        elif verb == "mark-submitted":
            result = mark_manual_submission(db, app_id, confirm=body.get("confirm") is True)
        elif verb == "confirm-submitted":
            result = confirm_submission(
                db, app_id, method=str(body.get("method") or "manual"),
                source=str(body.get("source") or "user_attestation"),
                confirm=body.get("confirm") is True,
                external_id=str(body.get("external_id") or ""))
        elif verb == "unverified":
            result = mark_unverified(db, app_id, confirm=body.get("confirm") is True,
                                     note=str(body.get("note") or ""))
        elif verb == "resume":
            variant = str(body.get("variant") or "")
            if variant not in VARIANTS:
                return {"error": "Неизвестный вариант резюме"}, 400
            db.execute("UPDATE applications SET resume_variant=?, updated_at=? WHERE id=?",
                       (variant, dbm.now(), app_id))
            db.commit()
            readiness.compute_all(db, settings)
            return {"ok": True, "variant": variant, "readiness": readiness.summary(db)}, 200
        elif verb == "outcome":
            result = set_outcome(db, app_id, str(body.get("outcome") or ""),
                                 note=str(body.get("note") or ""))
        elif verb in ("browser-draft", "form"):
            from core.apply_browser import prepare_form
            result = prepare_form(db, settings, app_id, headed=body.get("headed") is True)
            readiness.compute_all(db, settings)
            return result, (200 if result.get("ok") else 409)
        elif verb == "draft":
            app = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
            if not app:
                return {"error": "Отклик не найден"}, 404
            result = prepare_form(db, settings, app_id, headed=body.get("headed") is True)
            readiness.compute_all(db, settings)
            return result, (200 if result.get("ok") else 409)
        else:
            return {"error": "Неизвестное действие"}, 404
        return result, 200 if result.get("ok") else 409
    if path == "/api/messages/import":
        from core.messages import save_incoming
        text = clean_text(str(body.get("body") or ""))
        if not text:
            return {"error": "Пустое сообщение"}, 400
        result = save_incoming(db, str(body.get("job_id") or "") or None, text,
                              sender=str(body.get("sender") or ""))
        return {"ok": True, "result": result}, 200
    if path == "/api/messages/reply":
        from core.messages import draft_reply
        result = draft_reply(db, settings, int(body.get("id") or 0), resume, profile)
        return result, 200 if result.get("ok") else 400
    if path == "/api/messages/sync":
        from core.messages import sync_from_browser
        result = sync_from_browser(db, settings, limit=20)
        return result, 200 if result.get("ok") else 409
    if path == "/api/resume/save":
        if not isinstance(body.get("resume"), dict):
            return {"error": "Нет resume"}, 400
        save_resume(body["resume"])
        return {"ok": True, "resume": resume_payload()}, 200
    if path == "/api/resume/confirm":
        indexes = body.get("indexes") or []
        confirm_items(resume, "skills", [int(x) for x in indexes])
        save_resume(resume)
        return {"ok": True, "resume": resume_payload()}, 200
    if path == "/api/resume/project":
        index = int(body.get("index") or 0)
        projects = resume.get("projects") or []
        if 0 <= index < len(projects):
            projects[index]["verified"] = not projects[index].get("verified")
            save_resume(resume)
        return {"ok": True, "resume": resume_payload()}, 200
    if path == "/api/resume/import":
        text = str(body.get("text") or "")
        filename = str(body.get("path") or "").strip()
        imported = import_resume(Path(filename)) if filename else onboarding_io.extract_facts(text, source_name="import")
        save_resume(imported)
        return {"ok": True, "resume": resume_payload()}, 200
    if path == "/api/resume/restore":
        restore_version(str(body.get("name") or ""))
        return {"ok": True, "resume": resume_payload()}, 200
    if path == "/api/submission/email/test":
        from core.submission import test_email_connection
        return test_email_connection(settings), 200
    if path == "/api/submission/email":
        from core.secure_store import delete_secret, store_secret
        from core.submission import EMAIL_SECRET, capabilities as submission_capabilities
        channels = dict((settings.get("submission") or {}).get("channels") or {})
        email_cfg = dict(channels.get("email") or {})
        for key in ("smtp_host", "smtp_port", "smtp_security", "smtp_username",
                    "from_email", "from_name"):
            if key in body:
                email_cfg[key] = body.get(key)
        email_cfg["smtp_configured"] = bool(email_cfg.get("smtp_host"))
        channels["email"] = email_cfg
        settings["submission"] = {**(settings.get("submission") or {}), "channels": channels}
        save_settings(settings)
        password = str(body.get("password") or "")
        if body.get("clear_password"):
            delete_secret(EMAIL_SECRET)
        elif password:
            try:
                store_secret(EMAIL_SECRET, password)
            except (ValueError, OSError) as exc:
                return {"ok": False, "reason": str(exc)}, 400
        return {"ok": True, "email": submission_capabilities(settings)["email"]}, 200
    if path in ("/api/submission/prepare", "/api/submission/submit"):
        from core.submission import submit_application, submit_job
        app_id = int(body.get("id") or 0)
        job_id = str(body.get("job_id") or "")
        confirm = path.endswith("/submit") and bool(body.get("confirm"))
        dry_run = path.endswith("/prepare")
        if app_id:
            result = submit_application(db, settings, app_id, confirm=confirm,
                                        dry_run=dry_run, headless=True)
        elif job_id:
            job = dbm.get_job(db, job_id)
            if not job:
                return {"ok": False, "reason": "Вакансия не найдена"}, 404
            result = submit_job(db, settings, job, confirm=confirm, dry_run=dry_run,
                                headless=True)
        else:
            return {"ok": False, "reason": "Нужен id отклика или job_id"}, 400
        return result, 200
    if path == "/api/resume/export":
        outputs = export_variant(resume, str(body.get("variant") or "python"), job=dbm.get_job(db, str(body.get("job") or "")) if body.get("job") else None)
        return {"ok": True, "outputs": outputs}, 200
    if path == "/api/qwen/test":
        from core.qwen_engine import QwenEngine
        from core.resume import facts_pack
        engine = QwenEngine(db, settings)
        result = engine.selftest(facts_pack(resume, profile))
        return result, 200
    if path == "/api/qwen/key":
        from core.secure_store import AITUNNEL_SECRET, aitunnel_key_source, delete_secret, store_secret
        key = str(body.get("key") or "").strip()
        if key:
            try:
                store_secret(AITUNNEL_SECRET, key)
            except (ValueError, OSError) as exc:
                return {"ok": False, "reason": str(exc)}, 400
            return {"ok": True, "source": aitunnel_key_source(settings)}, 200
        delete_secret(AITUNNEL_SECRET)
        return {"ok": True, "source": aitunnel_key_source(settings)}, 200
    if path == "/api/settings":
        payload = body.get("settings")
        if not isinstance(payload, dict):
            return {"error": "Нет settings"}, 400
        save_settings(payload)
        return {"ok": True}, 200
    if path == "/api/profile":
        payload = body.get("profile")
        if not isinstance(payload, dict):
            return {"error": "Нет profile"}, 400
        save_profile(payload)
        return {"ok": True}, 200
    if path == "/api/sources/run":
        name = str(body.get("source") or "")
        report = collect_all(db, settings, force=True, only=[name] if name in SOURCES else None)
        rank_pending(db, settings, profile, resume)
        return {"ok": True, "report": report}, 200
    if path == "/api/model/refresh":
        from core.aitunnel import AITunnelRouter
        result = AITunnelRouter(db, settings).refresh_catalog(force=True)
        return {"ok": bool(result.get("refreshed")), "result": result}, 200
    if path == "/api/calibrate":
        return train_calibration(db, settings), 200
    if path == "/api/memory/resume/import":
        if not files:
            return {"ok": False, "reason": "Нужен PDF, DOCX, TXT или JSON"}, 400
        _, filename, data = files[0]
        suffix = Path(filename).suffix.lower()
        if suffix not in (".pdf", ".docx", ".txt", ".md", ".json"):
            return {"ok": False, "reason": "Поддерживаются PDF, DOCX, TXT, MD и JSON"}, 400
        onboarding_io.UPLOADS.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(filename).stem)[:60] or "resume"
        target = onboarding_io.UPLOADS / f"memory-{date.today():%Y%m%d}-{safe}{suffix}"
        target.write_bytes(data)
        from core.resume_memory import ResumeMemory
        try:
            result = ResumeMemory(db, settings).import_file(
                target, use_qwen=bool(body.get("use_qwen", True)))
        except Exception as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:300]}, 400
        return result, 200
    if path == "/api/memory/resume/confirm":
        from core.resume_memory import ResumeMemory
        try:
            result = ResumeMemory(db, settings).confirm_import(
                str(body.get("import_id") or ""), body.get("selected") or [])
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}, 400
        return result, 200
    if path == "/api/memory/skill":
        from core.resume_memory import ResumeMemory
        service = ResumeMemory(db, settings)
        if not body.get("confirm"):
            return service.propose_skill(str(body.get("text") or "")), 200
        try:
            result = service.apply_skill(str(body.get("skill") or ""),
                                         str(body.get("status") or "unknown"),
                                         evidence=str(body.get("evidence") or ""))
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}, 400
        return result, 200
    if path == "/api/memory/project":
        from core.resume_memory import ResumeMemory
        try:
            result = ResumeMemory(db, settings).add_project({
                "name": str(body.get("name") or "").strip(),
                "stack": [str(x) for x in body.get("stack") or []],
                "description": str(body.get("description") or "").strip(),
                "role": str(body.get("role") or "").strip(),
            })
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}, 400
        return result, 200
    if path == "/api/memory/learning":
        from core.resume_memory import ResumeMemory
        service = ResumeMemory(db, settings)
        text = str(body.get("description") or "").strip()
        if not text:
            return {"ok": False, "reason": "Нужно описание"}, 400
        entry = service.add_learning(text, source="user",
                                     skills=[str(x) for x in body.get("skills") or []],
                                     status=str(body.get("status") or "confirmed"))
        return {"ok": True, "id": entry}, 200
    if path == "/api/memory/resume/preview":
        from core.resume_memory import ResumeMemory
        return ResumeMemory(db, settings).preview_resume_update(), 200
    if path == "/api/memory/resume/update":
        from core.resume_memory import ResumeMemory
        if not body.get("confirm"):
            return ResumeMemory(db, settings).preview_resume_update(), 200
        result = ResumeMemory(db, settings).apply_resume_update(
            skips_skills=body.get("skip_skills") or [],
            skips_projects=body.get("skip_projects") or [],
            variant=str(body.get("variant") or "data"))
        return result, 200
    if path == "/api/memory/sync":
        from core.candidate_memory import CandidateMemory
        memory_store = CandidateMemory(db, settings)
        result = memory_store.sync(embed=bool(body.get("embed", True)))
        return {"ok": True, **result}, 200
    if path == "/api/memory/fact":
        from core.candidate_memory import CandidateMemory
        memory_store = CandidateMemory(db, settings)
        key = str(body.get("key") or "").strip()
        if not key:
            return {"ok": False, "reason": "Нужен key"}, 400
        if body.get("delete"):
            return {"ok": memory_store.forget_fact(key), "key": key}, 200
        try:
            fact = memory_store.set_fact(
                key, body.get("value"), source=str(body.get("source") or "user"),
                confirmed=True, kind=str(body.get("kind") or "fact"),
                index=bool(body.get("index", True)))
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}, 400
        return {"ok": True, "fact": fact}, 200
    if path == "/api/memory/secret/forget":
        from core.candidate_memory import CandidateMemory
        name = str(body.get("name") or "")
        try:
            removed = CandidateMemory(db, settings).forget_secret(name)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}, 400
        return {"ok": removed, "name": name}, 200
    if path == "/api/memory/rebuild":
        memory = Memory(db, settings)
        memory.sync_facts(resume, profile, "spa")
        rows = db.execute("SELECT * FROM jobs WHERE duplicate_of IS NULL ORDER BY score DESC LIMIT 60").fetchall()
        memory.index_jobs([dict(r) for r in rows])
        embedded = memory.embed_pending(limit=60)
        memory.unload_embedder()
        return {"ok": True, "embedded": embedded, "stats": memory.stats()}, 200
    return {"error": "Неизвестное действие"}, 404
