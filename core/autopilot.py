"""Autopilot policy, safe apply and recruiter-message actions.

The default mode is OBSERVER. Nothing can be sent unless the user explicitly
switches to ASSISTED/AUTOPILOT and enables the corresponding flag. All real
operations use the official hh.ru OAuth client.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timezone

from . import db as dbm
from . import hh_oauth
from . import probability_engine
from .hh_api_client import HHAPIError, HHClient
from .resume import load_resume, select_variant, sync_from_profile
from .settings import load_profile, load_settings, save_settings
from .sources.hh_api import normalize

MODES = ("observer", "assisted", "autopilot")
QUERIES = (
    "Junior Python", "Python стажер", "Backend Intern", "FastAPI",
    "Data Analyst Junior", "Junior Data Scientist", "ML Intern",
    "Machine Learning Engineer Intern", "Data Engineer Intern",
    "AI Engineer", "LLM Engineer Intern", "AI Automation Developer",
    "Junior SQL Developer", "Junior Backend Developer",
)
CATEGORY_LABELS = {
    "recruiter_question": "Вопрос рекрутера",
    "interview_invitation": "Приглашение на интервью",
    "scheduling_request": "Согласование времени",
    "resume_request": "Запрос резюме",
    "salary_question": "Вопрос о зарплате",
    "work_format_question": "Вопрос о графике",
    "technical_question": "Технический вопрос",
    "test_assignment": "Тестовое задание",
    "rejection": "Отказ",
    "offer": "Оффер",
    "automated_notification": "Автоматическое уведомление",
    "other": "Другое",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _policy(settings: dict) -> dict:
    return dict(settings.get("autopilot") or {})


def _client() -> HHClient:
    return HHClient()


def _log(db: sqlite3.Connection, kind: str, *, employer: str = "", title: str = "",
         vacancy_id: str = "", application_id: int | None = None, mode: str = "observer",
         reason: str = "", result: str = "", payload: dict | None = None) -> None:
    db.execute("""INSERT INTO autopilot_actions(created_at,kind,employer,title,vacancy_id,
                  application_id,mode,reason,result,payload) VALUES (?,?,?,?,?,?,?,?,?,?)""",
               (_now(), kind, employer[:300], title[:300], vacancy_id, application_id,
                mode, reason[:500], result[:500], json.dumps(payload or {}, ensure_ascii=False)))
    db.commit()


def _decision(db: sqlite3.Connection, kind: str, *, employer: str = "", title: str = "",
              vacancy_id: str = "", application_id: int | None = None,
              question: str = "", context: str = "", proposed: str = "",
              reason: str = "", urgency: str = "normal", outcome_id: int | None = None) -> int:
    cur = db.execute("""INSERT INTO autopilot_decisions(created_at,updated_at,kind,employer,title,
                         vacancy_id,application_id,outcome_id,question,context,proposed,reason,
                         urgency,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
                     (_now(), _now(), kind, employer[:300], title[:300], vacancy_id,
                      application_id, outcome_id, question[:2000], context[:4000],
                      proposed[:4000], reason[:1000], urgency))
    db.commit()
    return int(cur.lastrowid)


def _decision_if_new(db: sqlite3.Connection, kind: str, **kwargs) -> int:
    vacancy_id = str(kwargs.get("vacancy_id") or "")
    row = db.execute("""SELECT id FROM autopilot_decisions
                        WHERE status='pending' AND kind=? AND vacancy_id=?
                        ORDER BY id DESC LIMIT 1""", (kind, vacancy_id)).fetchone()
    if row:
        return int(row["id"])
    return _decision(db, kind, **kwargs)


def _allowed_job(job, settings: dict) -> tuple[bool, str]:
    policy = _policy(settings)
    if job["duplicate_of"]:
        return False, "дубль"
    if (job["score"] or 0) < float(policy.get("min_match", 75)):
        return False, "низкий Match Score"
    if job["company"] and job["company"].casefold() in {
            str(x).casefold() for x in policy.get("excluded_employers") or []}:
        return False, "работодатель исключён"
    mode = (job["work_mode"] or "").casefold()
    allowed_modes = [str(x).casefold() for x in policy.get("work_modes") or []]
    if allowed_modes and mode and mode not in allowed_modes:
        return False, "график не разрешён"
    locations = [str(x).casefold() for x in policy.get("locations") or []]
    if locations and job["location"] and not any(x in job["location"].casefold() for x in locations):
        return False, "город не разрешён"
    if job["eligible"] != 1 or job["needs_review"]:
        return False, "не проходит обязательные требования"
    if not (job["letter"] or "").strip():
        return False, "нет письма"
    if str(job["letter_source"] or "").startswith(("factual_", "qwen_unavailable")):
        return False, "черновик требует ручной проверки"
    return True, ""


def status(db: sqlite3.Connection, settings: dict | None = None) -> dict:
    settings = settings or load_settings()
    policy = _policy(settings)
    oauth = hh_oauth.status()
    try:
        from .browser import hh_profile_status
        browser = hh_profile_status()
    except Exception as exc:
        browser = {"profile_exists": False, "error": str(exc)}
    stats = db.execute("""SELECT
        (SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NULL) jobs,
        (SELECT COUNT(*) FROM jobs WHERE eligible=1 AND duplicate_of IS NULL) qualified,
        (SELECT COUNT(*) FROM applications WHERE sent_at IS NOT NULL) sent,
        (SELECT COUNT(*) FROM applications WHERE state='submitted') submitted,
        (SELECT COUNT(*) FROM applications WHERE state IN
            ('queued','manual_review','requires_user_action','approved','form_filling',
             'ready_to_review','opening_application','exported')) prepared,
        (SELECT COUNT(*) FROM applications WHERE state IN
            ('failed','blocked','submission_unverified')) errors,
        (SELECT COUNT(*) FROM messages WHERE direction='in') messages,
        (SELECT COUNT(*) FROM autopilot_decisions WHERE status='pending') pending_decisions,
        (SELECT COUNT(*) FROM autopilot_actions) actions""").fetchone()
    last = db.execute("SELECT created_at FROM autopilot_actions ORDER BY id DESC LIMIT 1").fetchone()
    qwen = {}
    try:
        from .qwen_engine import QwenEngine
        status_data = QwenEngine(db, settings).status()
        qwen = {"mode": status_data.get("mode"),
                "local": status_data.get("local"),
                "cloud": status_data.get("cloud"),
                "spend": status_data.get("spend")}
    except Exception:
        qwen = {}
    submission = {}
    try:
        from .submission import capabilities
        submission = capabilities(settings)
    except Exception:
        submission = {}
    return {
        "oauth": oauth, "browser": browser,
        "mode": policy.get("mode", "observer"),
        "enabled": bool(policy.get("enabled")), "auto_apply": bool(policy.get("auto_apply")),
        "auto_replies": bool(policy.get("auto_replies")),
        "policy": policy, "stats": dict(stats),
        "qwen": qwen, "submission": submission,
        "last_action_at": last["created_at"] if last else "",
        "resumes": _cached_resumes(db),
    }


def _cached_resumes(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("""SELECT key,value,source,updated_at FROM candidate_facts
                         WHERE key LIKE 'resume.%' ORDER BY updated_at DESC""").fetchall()
    out = []
    for row in rows:
        try:
            value = json.loads(row["value"])
        except ValueError:
            continue
        out.append({"id": value.get("id", ""), "title": value.get("title", ""),
                    "updated_at": row["updated_at"]})
    return out


def sync_profile(db: sqlite3.Connection) -> dict:
    try:
        client = _client()
        me = client.me()
        resumes = client.resumes()
    except HHAPIError as exc:
        return {"ok": False, "status": "api_error", "reason": exc.reason,
                "http_status": exc.status, "kind": exc.kind}
    facts = [("contact.first_name", me.get("first_name") or ""),
             ("contact.last_name", me.get("last_name") or ""),
             ("contact.email", me.get("email") or ""),
             ("contact.phone", me.get("phone") or ""),
             ("contact.city", (me.get("area") or {}).get("name") or "")]
    for key, value in facts:
        if value:
            db.execute("""INSERT INTO candidate_facts(key,value,source,updated_at,verified,usable)
                          VALUES (?,?,?,?,1,1)
                          ON CONFLICT(key,source) DO UPDATE SET value=excluded.value,
                          updated_at=excluded.updated_at, verified=1""",
                       (key, value, "hh_api", _now()))
    try:
        from .resume import load_resume, save_resume
        resume_data = load_resume()
        contact = dict(resume_data.get("contact") or {})
        for key, value in facts:
            field = key.split(".", 1)[1]
            if value:
                contact[field] = value
        resume_data["contact"] = contact
        save_resume(resume_data)
    except Exception:
        pass
    for resume in resumes:
        rid = str(resume.get("id") or "")
        if not rid:
            continue
        value = {"id": rid, "title": resume.get("title") or "",
                 "status": (resume.get("status") or {}).get("id") or "",
                 "updated_at": resume.get("updated_at") or ""}
        db.execute("""INSERT INTO candidate_facts(key,value,source,updated_at,verified,usable)
                      VALUES (?,?,?,?,1,1)
                      ON CONFLICT(key,source) DO UPDATE SET value=excluded.value,
                      updated_at=excluded.updated_at, verified=1""",
                   (f"resume.{rid}", json.dumps(value, ensure_ascii=False), "hh_api", _now()))
    db.commit()
    return {"ok": True, "profile": me, "resumes": resumes, "facts_saved": len(facts) + len(resumes),
            "preview_only": True}


def sync_history(db: sqlite3.Connection, settings: dict, *, pages: int = 500) -> dict:
    token = hh_oauth.access_token()
    if not token:
        return {"ok": False, "status": "no_token", "reason": "hh.ru не подключён"}
    preview = probability_engine.preview_hh_api(db, token=token, pages=pages)
    if not preview.get("ok"):
        return preview
    committed = probability_engine.commit(db, "hh_api", preview.get("rows") or [])
    enqueued = probability_engine.enqueue_all(db)
    return {"ok": True, "preview": {k: v for k, v in preview.items() if k != "rows"},
            "committed": committed, "queue": enqueued}


def search(db: sqlite3.Connection, settings: dict, *, pages: int = 3) -> dict:
    token = hh_oauth.access_token()
    if not token:
        return {"ok": False, "status": "no_token", "reason": "hh.ru не подключён"}
    try:
        client = _client()
    except HHAPIError as exc:
        return {"ok": False, "status": "api_error", "reason": exc.reason}
    added = 0
    queries = list(QUERIES)
    for query in queries:
        try:
            items = client.search_vacancies(query, area=1, per_page=50, max_pages=pages)
        except HHAPIError as exc:
            return {"ok": False, "status": "api_error", "reason": exc.reason,
                    "http_status": exc.status}
        for item in items:
            card = normalize(item)
            if not card:
                continue
            job = card.to_job()
            ident, created = dbm.upsert_job(db, job)
            added += int(created)
    db.commit()
    from .scoring import rank_pending
    rank = rank_pending(db, settings, load_profile(), sync_from_profile(load_resume()))
    _log(db, "search", reason=f"{len(queries)} запросов hh.ru", result=f"новых {added}",
         payload={"queries": len(queries), "added": added})
    return {"ok": True, "queries": len(queries), "added": added, "ranking": rank}


def _today_sent(db: sqlite3.Connection) -> int:
    return int(db.execute("""SELECT COUNT(*) FROM applications
                             WHERE sent_at IS NOT NULL AND substr(sent_at,1,10)=?""",
                          (date.today().isoformat(),)).fetchone()[0] or 0)


def _verify_history(client: HHClient, vacancy_id: str, resume_id: str) -> dict:
    try:
        items = client.negotiations(max_pages=5)
    except Exception as exc:
        return {"confirmed": None, "reason": f"{type(exc).__name__}: {exc}"}
    for item in items:
        vacancy = item.get("vacancy") or {}
        resume = item.get("resume") or {}
        if str(vacancy.get("id") or "") == str(vacancy_id):
            if not resume_id or str(resume.get("id") or "") in ("", str(resume_id)):
                return {"confirmed": True, "negotiation_id": str(item.get("id") or "")}
    return {"confirmed": False, "reason": "Отклик не найден в доступной истории hh.ru"}


def apply_policy(db: sqlite3.Connection, settings: dict) -> dict:
    """Route each policy-eligible vacancy to its real submission channel.

    Only channels explicitly enabled for automatic sending are attempted. A
    manual/blocked channel creates a decision instead of pretending to send.
    """
    policy = _policy(settings)
    mode = policy.get("mode", "observer")
    if mode != "autopilot" or not policy.get("enabled") or not policy.get("auto_apply"):
        return {"ok": True, "skipped": "autopilot/auto_apply выключен", "sent": 0}
    from .submission import policy_allows, route as route_submission, submit_job

    limit = max(0, int(policy.get("daily_limit", 5)))
    left = max(0, limit - _today_sent(db))
    if not left:
        return {"ok": True, "skipped": "дневной лимит", "sent": 0}
    rows = db.execute("""SELECT j.* FROM jobs j
                         WHERE j.eligible=1 AND j.letter<>'' AND j.duplicate_of IS NULL
                           AND j.status IN ('new','draft','approved')
                           AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=j.id)
                         ORDER BY j.score DESC LIMIT ?""", (max(40, left * 8),)).fetchall()
    resume = sync_from_profile(load_resume())
    sent = prepared = blocked = unverified = decisions = 0
    channels: dict[str, int] = {}
    for job in rows:
        if sent >= left:
            break
        ok, why = _allowed_job(job, settings)
        if not ok:
            continue
        info = route_submission(job, settings, resume=resume)
        channels[info["channel"]] = channels.get(info["channel"], 0) + 1
        if not info["auto_allowed"] or not policy_allows(settings, info):
            if decisions < 5:
                _decision_if_new(db, "submission_channel", employer=job["company"], title=job["title"],
                                 vacancy_id=job["source_id"],
                                 question=f"Канал {info['label']} не отправляет автоматически",
                                 context=f"{info['host']}: {info['reason'] or 'нужно подтверждение'}"
                                         + (f" · {info['telegram_url']}" if info.get("telegram_url") else ""),
                                 proposed="Открыть отклик и отправить вручную",
                                 reason="Автоматическая подача для этого канала не разрешена",
                                 urgency="normal")
                decisions += 1
            continue
        result = submit_job(db, settings, job, confirm=False, resume=resume, headless=True)
        status = result.get("status", "failed")
        _log(db, "apply", employer=job["company"], title=job["title"],
             vacancy_id=job["source_id"], application_id=(result.get("route") or {}).get("application_id"),
             mode=mode, reason=f"канал {info['label']}", result=status,
             payload={k: v for k, v in result.items() if k not in ("route", "letter")})
        if status == "submitted":
            sent += 1
        elif status in ("ready_to_review", "requires_user", "requires_user_action"):
            prepared += 1
        elif status == "submission_unverified":
            unverified += 1
            if decisions < 5:
                _decision_if_new(db, "submission_unverified", employer=job["company"], title=job["title"],
                                 vacancy_id=job["source_id"],
                                 question="Отправка не подтверждена",
                                 context=result.get("reason") or "площадка не вернула подтверждение",
                                 proposed="Проверить статус на площадке вручную",
                                 reason="Повторная отправка запрещена до проверки", urgency="high")
                decisions += 1
        elif status in ("blocked", "failed", "duplicate"):
            blocked += 1
    db.commit()
    return {"ok": True, "sent": sent, "prepared": prepared, "blocked": blocked,
            "unverified": unverified, "channels": channels, "limit": limit,
            "remaining": max(0, left - sent)}


def classify_message(text: str) -> str:
    low = (text or "").casefold()
    if re.search(r"отказ|к сожалению|не готовы|другой кандидат", low):
        return "rejection"
    if re.search(r"оффер|предлагаем работу|job offer", low):
        return "offer"
    if re.search(r"тестов|test task|домашн", low):
        return "test_assignment"
    if re.search(r"интервью|собеседован|встреч", low):
        if re.search(r"когда|время|удобно|соглас|завтра|сегодня|\d{1,2}:\d{2}", low):
            return "scheduling_request"
        return "interview_invitation"
    if re.search(r"зарплат|оклад|доход|compensation", low):
        return "salary_question"
    if re.search(r"график|формат|офис|удал|час", low):
        return "work_format_question"
    if re.search(r"опыт|лет|проект", low):
        return "recruiter_question"
    if re.search(r"резюме|cv", low):
        return "resume_request"
    if re.search(r"спасибо|получен|автоматическ|уведомлен", low):
        return "automated_notification"
    return "other"


def _simple_reply(category: str, text: str, profile: dict, resume: dict) -> str:
    if category == "automated_notification":
        return ""
    if category in ("acknowledgement", "resume_request"):
        return "Здравствуйте! Спасибо, информацию получил. Резюме и подтверждённые данные доступны в моём профиле hh.ru."
    return ""


def _auto_reply_allowed(category: str, policy: dict) -> bool:
    return category in (policy.get("reply_categories") or []) and category not in (
        "salary_question", "scheduling_request", "work_format_question",
        "interview_invitation", "test_assignment", "rejection", "offer")


def sync_messages(db: sqlite3.Connection, settings: dict, *, limit: int = 50) -> dict:
    token = hh_oauth.access_token()
    if not token:
        return {"ok": False, "status": "no_token", "reason": "hh.ru не подключён"}
    policy = _policy(settings)
    client = _client()
    rows = db.execute("""SELECT * FROM application_outcomes
                         WHERE negotiation_id<>'' ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    added = replied = decisions = 0
    for outcome in rows:
        try:
            messages = client.messages(outcome["negotiation_id"])
        except HHAPIError as exc:
            if exc.status in (401, 403, 429):
                return {"ok": False, "status": "api_error", "reason": exc.reason,
                        "http_status": exc.status, "kind": exc.kind,
                        "added": added, "replied": replied}
            continue
        for msg in messages:
            text = str(msg.get("text") or msg.get("body") or "").strip()
            if not text:
                continue
            external_id = str(msg.get("id") or "")
            category = classify_message(text)
            from .messages import save_incoming
            result = save_incoming(db, outcome["job_id"], text,
                                   sender=(msg.get("author") or {}).get("name") or "HR",
                                   url=f"https://hh.ru/negotiations/{outcome['negotiation_id']}",
                                   external_id=external_id,
                                   received_at=msg.get("created_at") or "")
            added += int(result.get("added"))
            if not result.get("added"):
                continue
            if policy.get("auto_replies") and policy.get("enabled") and policy.get("mode") == "autopilot" and \
                    _auto_reply_allowed(category, policy):
                body = _simple_reply(category, text, load_profile(), sync_from_profile(load_resume()))
                if body:
                    already = db.execute("""SELECT 1 FROM autopilot_actions
                                            WHERE kind='reply' AND payload LIKE ? LIMIT 1""",
                                         (f"%{external_id}%",)).fetchone()
                    if not already:
                        try:
                            client.send_message(outcome["negotiation_id"], body)
                            replied += 1
                            _log(db, "reply", employer=outcome["employer"], title=outcome["title"],
                                 vacancy_id=outcome["job_id"], mode="autopilot",
                                 reason=f"allowed category {category}", result="sent",
                                 payload={"message_id": external_id, "category": category})
                        except HHAPIError as exc:
                            _log(db, "reply", employer=outcome["employer"], title=outcome["title"],
                                 vacancy_id=outcome["job_id"], mode="autopilot",
                                 reason=f"API error {exc.status}", result="failed",
                                 payload={"message_id": external_id})
                else:
                    _decision(db, "reply", employer=outcome["employer"], title=outcome["title"],
                              vacancy_id=outcome["job_id"], application_id=None,
                              question=text[:1500], context=f"Категория: {CATEGORY_LABELS.get(category, category)}",
                              proposed="Подготовить ответ вручную",
                              reason="Категория требует решения пользователя", urgency="high",
                              outcome_id=outcome["id"])
                    decisions += 1
            else:
                _decision(db, "reply", employer=outcome["employer"], title=outcome["title"],
                          vacancy_id=outcome["job_id"], question=text[:1500],
                          context=f"Категория: {CATEGORY_LABELS.get(category, category)}",
                          proposed="Подготовить ответ вручную",
                          reason="Автоответы выключены или категория не разрешена",
                          urgency="high" if category in ("interview_invitation", "offer", "test_assignment") else "normal",
                          outcome_id=outcome["id"])
                decisions += 1
    db.commit()
    return {"ok": True, "checked": len(rows), "added": added, "replied": replied,
            "decisions": decisions}


def run_once(db: sqlite3.Connection, settings: dict | None = None) -> dict:
    settings = settings or load_settings()
    policy = _policy(settings)
    token = bool(hh_oauth.access_token())
    result: dict = {"started_at": _now(), "mode": policy.get("mode", "observer"),
                    "hh_connected": token}
    try:
        if token:
            result["history"] = sync_history(db, settings)
            result["queue"] = probability_engine.process_queue(db, limit=20, use_api=True)
            result["search"] = search(db, settings)
        else:
            result["hh"] = {"skipped": "hh.ru не подключён; отправка через другие каналы"}
        try:
            from .letters import ensure_letters
            from .memory import Memory
            limit = max(1, min(10, int(policy.get("daily_limit", 5))))
            result["letters"] = ensure_letters(db, settings, load_profile(),
                                               sync_from_profile(load_resume()), limit=limit,
                                               memory=Memory(db, settings))
        except Exception as exc:
            result["letters"] = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        result["apply"] = apply_policy(db, settings)
        if token:
            result["messages"] = sync_messages(db, settings)
        result["ok"] = True
    except Exception as exc:
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        _log(db, "run_error", mode=result.get("mode", "observer"), reason=result["error"])
    _log(db, "run", mode=result.get("mode", "observer"),
         reason="плановый цикл", result="ok" if result.get("ok") else "error",
         payload={k: v for k, v in result.items() if k not in ("history", "queue", "search")})
    return result


def tick(db: sqlite3.Connection, settings: dict | None = None) -> dict:
    """Light background tick: messages, status checks and policy-limited actions."""
    settings = settings or load_settings()
    policy = _policy(settings)
    token = bool(hh_oauth.access_token())
    result: dict = {"ok": True, "mode": policy.get("mode", "observer"), "hh_connected": token}
    if token:
        result["messages"] = sync_messages(db, settings, limit=30)
    result["apply"] = apply_policy(db, settings)
    last_search = db.execute("""SELECT created_at FROM autopilot_actions
                                WHERE kind='search' ORDER BY id DESC LIMIT 1""").fetchone()
    search_due = True
    if last_search:
        try:
            dt = datetime.fromisoformat(last_search["created_at"])
            search_due = (datetime.now(timezone.utc) - dt).total_seconds() > 6 * 3600
        except ValueError:
            search_due = True
    if token and search_due:
        result["search"] = search(db, settings, pages=2)
    _log(db, "tick", mode=result["mode"], reason="periodic",
         result="ok" if result.get("ok") else "error",
         payload={k: v for k, v in result.items() if k not in ("messages", "apply", "search")})
    return result


def set_policy(db: sqlite3.Connection, settings: dict, policy: dict) -> dict:
    current = _policy(settings)
    allowed = {"mode", "enabled", "auto_apply", "auto_replies", "min_match", "daily_limit",
               "directions", "work_modes", "locations", "excluded_employers",
               "allowed_resumes", "reply_categories", "poll_minutes"}
    for key, value in policy.items():
        if key in allowed:
            current[key] = value
    if current.get("mode") not in MODES:
        return {"ok": False, "reason": "Неизвестный режим"}
    if current.get("mode") != "autopilot":
        current["auto_apply"] = False
        current["auto_replies"] = False
    settings["autopilot"] = current
    save_settings(settings)
    _log(db, "policy", mode=current.get("mode", "observer"), reason="policy updated",
         payload=current)
    return {"ok": True, "policy": current}


def stop(db: sqlite3.Connection, settings: dict) -> dict:
    policy = _policy(settings)
    policy["enabled"] = False
    policy["auto_apply"] = False
    policy["auto_replies"] = False
    settings["autopilot"] = policy
    save_settings(settings)
    _log(db, "stop", mode=policy.get("mode", "observer"), reason="остановлено пользователем",
         result="stopped")
    return {"ok": True, "policy": policy}


def decisions(db: sqlite3.Connection, status: str = "pending") -> list[dict]:
    clause = "WHERE status=?" if status else ""
    rows = db.execute(f"""SELECT * FROM autopilot_decisions {clause}
                          ORDER BY CASE urgency WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                          ELSE 2 END, id DESC LIMIT 200""",
                      (status,) if status else ()).fetchall()
    return [dict(r) for r in rows]


def decide(db: sqlite3.Connection, decision_id: int, status: str, answer: str = "") -> dict:
    if status not in ("approved", "rejected", "postponed"):
        return {"ok": False, "reason": "Неизвестное решение"}
    row = db.execute("SELECT * FROM autopilot_decisions WHERE id=?", (decision_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "Решение не найдено"}
    db.execute("""UPDATE autopilot_decisions SET status=?, answer=?, decided_at=?,
                  updated_at=? WHERE id=?""",
               (status, answer[:4000], _now(), _now(), decision_id))
    db.commit()
    return {"ok": True, "status": status}
