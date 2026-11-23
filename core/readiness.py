"""Application readiness: exactly what blocks a real submission.

Readiness is a preparation state, not a probability and not a claim that an
application was sent. It is recomputed from the job, the letter, the resume
contact block and the last browser-form snapshot.
"""
from __future__ import annotations

import json
import sqlite3
from urllib.parse import urlsplit

from . import db as dbm
from .resume import VARIANTS, load_resume, sync_from_profile
from .settings import load_settings

READINESS = (
    "needs_resume", "needs_letter", "needs_contact_info", "needs_verification",
    "ready_to_review", "ready_to_submit", "requires_user_action", "submitted",
    "submission_unverified", "blocked",
)
LABELS = {
    "needs_resume": "Нужно резюме",
    "needs_letter": "Нужно письмо",
    "needs_contact_info": "Нужны контактные данные",
    "needs_verification": "Нужна проверка",
    "ready_to_review": "Готово к проверке",
    "ready_to_submit": "Готово к отправке",
    "requires_user_action": "Требуется действие",
    "submitted": "Отправлено",
    "submission_unverified": "Отправка не подтверждена",
    "blocked": "Заблокировано",
}
AGGREGATORS = ("youngjunior.", "vseti.app", "getmatch.", "talanto.", "wantapply.",
               "ods.ai", "lnkd.in", "h.careers", "career.habr.com")
DIRECT_FORM_HOSTS = ("changellenge.", "job.mts.", "team.vk.", "rabota.sber.",
                     "jobs.", "careers.", "apply.")


def _contact_gaps(contact: dict, *, form: bool) -> list[str]:
    gaps = []
    if not str(contact.get("first_name") or "").strip():
        gaps.append("имя")
    if not str(contact.get("last_name") or "").strip():
        gaps.append("фамилия")
    if not str(contact.get("phone") or "").strip():
        gaps.append("телефон")
    if form and not str(contact.get("email") or "").strip():
        gaps.append("email")
    return gaps


def _label_gap(label: str, contact: dict) -> str:
    low = (label or "").casefold()
    if "email" in low or "почт" in low or "e-mail" in low:
        return "" if str(contact.get("email") or "").strip() else "email"
    if "фамил" in low or "last" in low or "surname" in low:
        return "" if str(contact.get("last_name") or "").strip() else "фамилия"
    if "имя" in low or "first" in low or "name" in low:
        return "" if str(contact.get("first_name") or "").strip() else "имя"
    if "тел" in low or "phone" in low:
        return "" if str(contact.get("phone") or "").strip() else "телефон"
    return ""


def _unresolved_required(required: list[str], contact: dict) -> list[str]:
    return [label for label in required if _label_gap(label, contact)]


def _gaps_from_required(required: list[str], contact: dict) -> list[str]:
    gaps = []
    for label in required:
        gap = _label_gap(label, contact)
        if gap:
            gaps.append(gap)
    return sorted(set(gaps))


def _method(job, detail: dict) -> tuple[str, str]:
    url = str(detail.get("apply_url") or job["url"] or "").strip()
    host = (urlsplit(url).hostname or "").casefold()
    if not url:
        return "none", ""
    if host.endswith("hh.ru") or "hh.ru" in host:
        return "hh", url
    if any(item in host for item in AGGREGATORS):
        return "aggregator", url
    if job["source"] == "telegram" and host in ("t.me", "telegram.me"):
        return "telegram_post", url
    if any(item in host for item in DIRECT_FORM_HOSTS):
        return "direct_form", url
    return "direct", url


def _note(app) -> dict:
    try:
        data = json.loads(app["note"] or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def application(db: sqlite3.Connection, app, *, resume: dict | None = None,
                settings: dict | None = None) -> dict:
    settings = settings or load_settings()
    resume = resume or sync_from_profile(load_resume())
    contact = resume.get("contact") or {}
    job = dbm.get_job(db, app["job_id"])
    if not job:
        return _set(db, app["id"], "blocked", "Вакансия не найдена")
    state = app["state"]
    if state == "submitted" or app["sent_at"] and app["verification_method"]:
        return _set(db, app["id"], "submitted", "Отправка подтверждена")
    if state == "submission_unverified":
        return _set(db, app["id"], "submission_unverified", "Нет подтверждения площадки")
    if state == "blocked":
        return _set(db, app["id"], "blocked", app["last_error"] or "Площадка заблокировала действие")
    if state == "cancelled":
        return _set(db, app["id"], "blocked", "Отклик отменён")
    if job["duplicate_of"]:
        return _set(db, app["id"], "blocked", "Дубль вакансии; используйте основную публикацию")
    if not job["eligible"]:
        return _set(db, app["id"], "blocked",
                    "Не проходит обязательные условия: " + (job["reason"] or "")[:160])
    if not (job["letter"] or "").strip():
        return _set(db, app["id"], "needs_letter", "Сопроводительное письмо не подготовлено")
    if (job["letter_hash"] or "") != (app["letter_hash"] or ""):
        return _set(db, app["id"], "needs_verification",
                    "Письмо изменилось после постановки в очередь; перечитайте и одобрите заново")
    variant = app["resume_variant"] or "data"
    if not (app["resume_variant"] or "").strip():
        return _set(db, app["id"], "needs_resume",
                    "Выберите подходящий вариант резюме (Python / Data / AI)")
    if variant not in VARIANTS:
        return _set(db, app["id"], "needs_resume", f"Неизвестный вариант резюме: {variant}")

    detail = dbm.job_detail(job)
    method, url = _method(job, detail)
    note = _note(app)
    form = bool(note.get("has_form"))
    required = [str(x) for x in (note.get("empty_required") or [])]
    if form and required:
        gaps = _gaps_from_required(required, contact)
        unresolved = _unresolved_required(required, contact)
    elif method in ("direct_form", "direct"):
        gaps = _contact_gaps(contact, form=True)
        unresolved = []
    else:
        gaps = []
        unresolved = []
    if gaps:
        return _set(db, app["id"], "needs_contact_info",
                    "В профиле не хватает: " + ", ".join(gaps))

    if unresolved:
        return _set(db, app["id"], "requires_user_action",
                    "Форма открыта, но остались обязательные поля: " +
                    ", ".join(unresolved[:4]))
    if form and note.get("filled"):
        if state == "approved":
            return _set(db, app["id"], "ready_to_submit",
                        "Форма заполнена, финальная кнопка не нажата")
        return _set(db, app["id"], "ready_to_review",
                    "Форма подготовлена; проверьте и одобрите")
    if method == "telegram_post":
        return _set(db, app["id"], "requires_user_action",
                    "Отклик через Telegram-пост/HR — откройте публикацию и напишите вручную")
    if method == "hh":
        return _set(db, app["id"], "requires_user_action",
                    "hh.ru требует авторизацию/OAuth; с этого IP API отвечает 403")
    if method == "aggregator":
        return _set(db, app["id"], "requires_user_action",
                    "Агрегатор: откройте первоисточник и форму работодателя")
    if method == "none":
        return _set(db, app["id"], "needs_contact_info", "Нет ссылки на вакансию или контакт")
    if state == "approved":
        return _set(db, app["id"], "ready_to_submit",
                    "Письмо и резюме готовы; откройте оригинал и отправьте вручную")
    return _set(db, app["id"], "ready_to_review",
                "Документы готовы; проверьте письмо и одобрите")


def _set(db: sqlite3.Connection, app_id: int, readiness: str, reason: str) -> dict:
    db.execute("""UPDATE applications SET readiness=?, readiness_reason=?, readiness_updated_at=?
                  WHERE id=?""", (readiness, reason[:500], dbm.now(), app_id))
    return {"readiness": readiness, "readiness_label": LABELS.get(readiness, readiness),
            "reason": reason}


def lead(db: sqlite3.Connection, row, *, resume: dict | None = None) -> dict:
    resume = resume or sync_from_profile(load_resume())
    contact = resume.get("contact") or {}
    status, readiness, reason = row["status"], "", ""
    if status in ("contacted", "replied", "interview", "offer"):
        readiness, reason = "submitted", "Обращение подтверждено пользователем"
    elif status == "rejected":
        readiness, reason = "blocked", "Компания отказала"
    elif not (row["outreach"] or "").strip():
        readiness, reason = "needs_letter", "Персональное обращение не подготовлено"
    elif not (row["contact"] or "").strip():
        readiness, reason = "needs_contact_info", "Нет публичного контакта компании"
    elif row["contact_kind"] == "website":
        readiness, reason = "requires_user_action", "Только сайт: найдите форму/контакт на сайте"
    elif row["contact_kind"] == "profile":
        readiness, reason = "requires_user_action", "Контакт через профиль площадки — нужна авторизация"
    else:
        readiness, reason = "ready_to_review", "Текст и публичный контакт готовы"
    db.execute("""UPDATE company_leads SET readiness=?, readiness_reason=?, readiness_updated_at=?
                  WHERE id=?""", (readiness, reason[:500], dbm.now(), row["id"]))
    return {"readiness": readiness, "readiness_label": LABELS.get(readiness, readiness),
            "reason": reason}


def compute_all(db: sqlite3.Connection, settings: dict | None = None) -> dict:
    settings = settings or load_settings()
    resume = sync_from_profile(load_resume())
    from .resume import select_variant
    apps = db.execute("SELECT * FROM applications").fetchall()
    for app in apps:
        if not (app["resume_variant"] or "").strip():
            job = dbm.get_job(db, app["job_id"])
            if job:
                try:
                    variant = select_variant(resume, dict(job), dbm.job_detail(job))
                except Exception:
                    variant = "data"
                db.execute("UPDATE applications SET resume_variant=?, updated_at=? WHERE id=?",
                           (variant, dbm.now(), app["id"]))
    db.commit()
    apps = db.execute("SELECT * FROM applications").fetchall()
    counts: dict[str, int] = {}
    for app in apps:
        result = application(db, app, resume=resume, settings=settings)
        counts[result["readiness"]] = counts.get(result["readiness"], 0) + 1
    leads = db.execute("SELECT * FROM company_leads").fetchall()
    lead_counts: dict[str, int] = {}
    for row in leads:
        result = lead(db, row, resume=resume)
        lead_counts[result["readiness"]] = lead_counts.get(result["readiness"], 0) + 1
    db.commit()
    return {"applications": counts, "leads": lead_counts,
            "total_applications": len(apps), "total_leads": len(leads)}


def summary(db: sqlite3.Connection) -> dict:
    rows = db.execute("""SELECT COALESCE(readiness,'') r, COUNT(*) n FROM applications
                         GROUP BY readiness""").fetchall()
    apps = {r["r"]: r["n"] for r in rows}
    lead_rows = db.execute("""SELECT COALESCE(readiness,'') r, COUNT(*) n FROM company_leads
                              GROUP BY readiness""").fetchall()
    leads = {r["r"]: r["n"] for r in lead_rows}
    return {
        "applications": apps, "leads": leads,
        "ready_to_submit": apps.get("ready_to_submit", 0) + leads.get("ready_to_submit", 0),
        "ready_to_review": apps.get("ready_to_review", 0) + leads.get("ready_to_review", 0),
        "needs_action": sum(apps.get(k, 0) for k in
                            ("needs_resume", "needs_letter", "needs_contact_info",
                             "needs_verification", "requires_user_action")),
        "submitted": apps.get("submitted", 0) + leads.get("submitted", 0),
        "unverified": apps.get("submission_unverified", 0),
    }
