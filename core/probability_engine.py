"""Personal application-outcome statistics and probability estimates.

The first model is a Beta-Binomial posterior. Logistic regression is trained
only after a sufficient, chronologically validated sample exists. All learning
is local; no LLM and no paid API is involved.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import db as dbm
from .paths import DATA, ensure_dirs
from .text import parse_salary

STATUSES = ("submitted", "not_viewed", "viewed", "responded", "waiting", "no_response_yet",
            "rejected", "invited", "interview", "offer", "withdrawn", "unknown")
RESOLVED_POSITIVE = ("invited", "offer")
RESOLVED_NEGATIVE = ("rejected",)
PENDING = ("submitted", "not_viewed", "viewed", "responded", "waiting", "no_response_yet",
           "interview", "withdrawn", "unknown")
STATUS_LABELS = {
    "submitted": "Отправлено", "not_viewed": "Не просмотрено", "viewed": "Просмотрено",
    "responded": "Ответ без решения", "waiting": "Ожидает ответа",
    "no_response_yet": "Пока нет ответа", "rejected": "Отказ", "invited": "Приглашение",
    "interview": "Интервью", "offer": "Оффер", "withdrawn": "Отозвано", "unknown": "Неизвестно",
}
SOURCE_CONFIDENCE = {"hh_api": 0.95, "manual": 0.9, "csv": 0.8, "json": 0.8,
                     "html": 0.6, "email": 0.7, "application": 0.9}
MIN_PUBLISH_N = 20
MIN_PUBLISH_POS = 5
MIN_PUBLISH_NEG = 5
MAX_PUBLISH_WIDTH = 0.30
MIN_ML_N = 60
MIN_ML_POS = 15
MIN_ML_NEG = 15
PRIOR = (1.0, 1.0)  # transparent weakly informative uniform prior
MODEL_PATH = DATA / "probability_model.json"

_STATUS_MAP = {
    "invitation": "invited", "invited": "invited", "приглашение": "invited",
    "invite": "invited", "offer": "offer", "оффер": "offer", "hired": "offer",
    "interview": "interview", "интервью": "interview",
    "discard": "rejected", "discard_after_interview": "rejected",
    "rejected": "rejected", "rejection": "rejected", "отказ": "rejected",
    "viewed": "viewed", "просмотрено": "viewed",
    "not_viewed": "not_viewed", "not viewed": "not_viewed", "не просмотрено": "not_viewed",
    "waiting": "waiting", "ожидает ответа": "waiting", "в ожидании": "waiting",
    "response": "waiting", "no_response_yet": "waiting",
    "no response": "waiting", "нет ответа": "waiting", "ожидание": "waiting",
    "consider": "viewed", "рассматривается": "viewed",
    "withdrawn": "withdrawn", "отозвано": "withdrawn", "отозван": "withdrawn",
    "submitted": "submitted", "отправлено": "submitted",
    "responded": "responded", "ответ": "responded", "replied": "responded",
    "unknown": "unknown", "неизвестно": "unknown",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_json(value, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


def canonical_status(value: str) -> str:
    low = re.sub(r"\s+", " ", str(value or "").strip().casefold())
    if low in _STATUS_MAP:
        return _STATUS_MAP[low]
    for key, mapped in _STATUS_MAP.items():
        if key and key in low:
            return mapped
    return "unknown"


def seniority_of(value: str) -> str:
    low = (value or "").casefold()
    if re.search(r"стаж|intern|trainee", low):
        return "intern"
    if re.search(r"junior|джуниор|начинающ", low):
        return "junior"
    if re.search(r"middle|средн", low):
        return "middle"
    if re.search(r"senior|ведущ|lead|руковод", low):
        return "senior"
    return "unknown"


def direction_of(value: str) -> str:
    low = (value or "").casefold()
    if re.search(r"computer vision|\bcv\b|vision", low):
        return "cv"
    if re.search(r"\bml\b|machine learning|машинн|data scientist|ds\b", low):
        return "ml"
    if re.search(r"data engineer|data-инженер|дата-инженер", low):
        return "data_engineering"
    if re.search(r"analyst|аналит", low):
        return "analytics"
    if re.search(r"backend|бэкенд|python|fastapi|django", low):
        return "backend"
    if re.search(r"frontend|фронтенд|react|vue", low):
        return "frontend"
    return "other"


def _external_key(source: str, data: dict) -> str:
    raw = data.get("negotiation_id") or data.get("external_id")
    if raw:
        return f"{source}:{raw}"
    basis = "|".join(str(data.get(k) or "") for k in
                     ("job_id", "url", "employer", "title", "sent_at"))
    return f"{source}:{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:24]}"


def _value(data: dict, *names):
    for name in names:
        value = data.get(name)
        if value not in (None, ""):
            return value
    return ""


def _number(value):
    if value in (None, ""):
        return None
    try:
        match = re.search(r"-?\d+(?:[.,]\d+)?", str(value).replace(" ", ""))
        return float(match.group(0).replace(",", ".")) if match else None
    except (TypeError, ValueError):
        return None


def _iso(value: str) -> str:
    if not value:
        return ""
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d", "%d.%m.%Y", "%d.%m.%Y %H:%M"):
        try:
            dt = datetime.strptime(text.replace("Z", "+0000"), fmt)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            continue
    return text


def normalize_row(data: dict, source: str) -> dict:
    """Map a HH/CSV/JSON row to the local outcome shape without inventing values."""
    vacancy = data.get("vacancy") if isinstance(data.get("vacancy"), dict) else {}
    employer = data.get("employer")
    if isinstance(employer, dict):
        employer = employer.get("name")
    state = data.get("state")
    if isinstance(state, dict):
        state = state.get("id") or state.get("name")
    resume = data.get("resume")
    resume_title = ""
    if isinstance(resume, dict):
        resume_title = resume.get("title") or resume.get("id") or ""
    skills = data.get("skills") or []
    if isinstance(skills, str):
        try:
            skills = json.loads(skills)
        except ValueError:
            skills = [x.strip() for x in skills.split(",") if x.strip()]
    title = (vacancy.get("name") if isinstance(vacancy, dict) else "") or \
        _value(data, "title", "position", "vacancy_name")
    url = (vacancy.get("alternate_url") if isinstance(vacancy, dict) else "") or \
        _value(data, "url", "vacancy_url", "link")
    job_id = (str(vacancy.get("id")) if isinstance(vacancy, dict) and vacancy.get("id")
              else "") or str(_value(data, "job_id", "vacancy_id") or "")
    sent = _iso(str(_value(data, "sent_at", "created_at", "date", "applied_at")))
    last = _iso(str(_value(data, "last_event_at", "updated_at", "last_status_at"))) or sent
    status = canonical_status(str(state or _value(data, "status", "result", "outcome")))
    direction = data.get("direction") or direction_of(title)
    seniority = data.get("seniority") or seniority_of(title)
    features = data.get("features") if isinstance(data.get("features"), dict) else {}
    if features:
        features = dict(features)
        features.setdefault("at_submission", False)
        features.setdefault("captured_at", "")
    row = {
        "negotiation_id": str(_value(data, "negotiation_id", "external_id", "id")),
        "application_id": data.get("application_id"),
        "job_id": job_id,
        "employer": str(employer or _value(data, "company", "employer_name")),
        "title": str(title),
        "url": str(url),
        "source": source,
        "response_type": str(data.get("response_type") or "job"),
        "sent_at": sent,
        "last_event_at": last,
        "resume_variant": str(data.get("resume_variant") or resume_title),
        "skills": skills if isinstance(skills, list) else [],
        "experience_required": _number(_value(data, "experience_required", "experience_years",
                                             "required_experience")),
        "seniority": str(seniority),
        "direction": str(direction),
        "salary_min": _number(_value(data, "salary_min")),
        "salary_max": _number(_value(data, "salary_max")),
        "currency": str(_value(data, "currency") or ""),
        "location": str(_value(data, "location", "area", "city")),
        "work_mode": str(_value(data, "work_mode", "schedule", "mode")),
        "match_score": _number(_value(data, "match_score", "score")),
        "status": status,
        "status_source": str(data.get("status_source") or source),
        "confidence": float(data.get("confidence") or SOURCE_CONFIDENCE.get(source, 0.5)),
        "raw_status": str(data.get("raw_status") or state or ""),
        "status_checked_at": _iso(str(data.get("status_checked_at") or "")),
        "status_source_url": str(data.get("status_source_url") or ""),
        "negotiation_url": str(data.get("negotiation_url") or ""),
        "vacancy_url": str(data.get("vacancy_url") or url or ""),
        "vacancy_status": str(data.get("vacancy_status") or "unknown"),
        "vacancy_checked_at": _iso(str(data.get("vacancy_checked_at") or "")),
        "vacancy_http_status": data.get("vacancy_http_status"),
        "vacancy_evidence": str(data.get("vacancy_evidence") or ""),
        "vacancy_published_at": _iso(str(data.get("vacancy_published_at") or "")),
        "messages_count": int(data.get("messages_count") or 0),
        "has_messages": 1 if data.get("has_messages") else 0,
        "description_after_submission": 1 if data.get("description_after_submission") else 0,
        "viewed_at": _iso(str(data.get("viewed_at") or "")),
        "invited_at": _iso(str(data.get("invited_at") or "")),
        "rejected_at": _iso(str(data.get("rejected_at") or "")),
        "enrichment_attempts": int(data.get("enrichment_attempts") or 0),
        "last_enrich_error": str(data.get("last_enrich_error") or ""),
        "feature_json": features,
        "raw_json": data,
        "job_description": str(data.get("job_description") or data.get("description") or ""),
        "description_source": str(data.get("description_source") or ""),
        "description_fetched_at": str(data.get("description_fetched_at") or ""),
        "details_json": data.get("details") if isinstance(data.get("details"), dict) else {},
        "completeness": float(data.get("completeness") or 0.0),
    }
    row["external_key"] = _external_key(source, row)
    row["negotiation_id"] = str(data.get("negotiation_id") or data.get("external_id") or "")
    return row


def snapshot_features(job, detail: dict | None = None) -> dict:
    detail = detail if detail is not None else dbm.job_detail(job)
    scores = detail.get("scores") or {}
    education = detail.get("education") or {}
    freshness = None
    published = job["published_at"] or job["fetched_at"]
    if published:
        try:
            dt = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            freshness = max(0, (datetime.now(timezone.utc) - dt).days)
        except ValueError:
            pass
    return {
        "skill_match": scores.get("skill_match"),
        "experience_gap_years": scores.get("experience_gap_years"),
        "career_fit": scores.get("career_fit"),
        "required_experience": job["experience_years"],
        "seniority": seniority_of(detail.get("seniority") or job["title"]),
        "direction": direction_of(job["title"]),
        "work_mode": job["work_mode"] or "",
        "location": job["location"] or "",
        "education_required": bool(education.get("required")),
        "company_type": "",
        "vacancy_type": "internship" if re.search(r"стаж|intern|trainee", job["title"], re.I) else "job",
        "freshness_days": freshness,
        "at_submission": True,
        "captured_at": _now(),
    }


def record_submitted(db: sqlite3.Connection, app, job) -> dict:
    detail = dbm.job_detail(job)
    features = snapshot_features(job, detail)
    sent = app["sent_at"] or _now()
    row = normalize_row({
        "application_id": app["id"], "job_id": job["id"], "employer": job["company"],
        "title": job["title"], "url": job["url"], "sent_at": sent,
        "last_event_at": sent, "resume_variant": app["resume_variant"] or "",
        "skills": (detail.get("skills") or {}).get("matched") or [],
        "experience_required": job["experience_years"], "seniority": features["seniority"],
        "salary_min": job["salary_min"], "salary_max": job["salary_max"],
        "currency": job["currency"], "location": job["location"],
        "work_mode": job["work_mode"], "match_score": job["score"],
        "status": "submitted", "status_source": "application", "confidence": 0.9,
        "features": features,
        "negotiation_id": app["external_id"] if "external_id" in app.keys() else "",
    }, "application")
    row["external_key"] = f"application:{app['id']}"
    if row.get("negotiation_id"):
        row["negotiation_url"] = f"https://hh.ru/negotiations/{row['negotiation_id']}"
    outcome_id = _upsert(db, row, event_note="Отклик подтверждён пользователем")
    db.commit()
    return {"ok": True, "outcome_id": outcome_id}


def record_status(db: sqlite3.Connection, application_id: int, status: str,
                  *, source: str = "user", confidence: float = 0.9,
                  note: str = "") -> dict:
    mapped = canonical_status(status)
    exists = db.execute("SELECT * FROM application_outcomes WHERE application_id=?",
                        (application_id,)).fetchone()
    if not exists:
        app = db.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
        job = dbm.get_job(db, app["job_id"]) if app else None
        if not app or not job or not app["sent_at"]:
            return {"ok": False, "reason": "Нет отправленного отклика для обучения"}
        record_submitted(db, app, job)
        exists = db.execute("SELECT * FROM application_outcomes WHERE application_id=?",
                            (application_id,)).fetchone()
    _event(db, exists["id"], mapped, _now(), source, confidence, note)
    db.execute("""UPDATE application_outcomes SET status=?, last_event_at=?, status_source=?,
                  confidence=?, updated_at=? WHERE id=?""",
               (mapped, _now(), source, confidence, _now(), exists["id"]))
    db.commit()
    return {"ok": True, "status": mapped, "outcome_id": exists["id"]}


def _hh_headers(token: str) -> dict:
    return {"User-Agent": "AI-Job-Hunter/4.0 (personal local job search)",
            "Authorization": f"Bearer {token}"}


def check_status(db: sqlite3.Connection, row, *, token: str | None = None,
                 timeout: int = 20) -> dict:
    """Fetch one negotiation status/messages from the official HH API only."""
    negotiation = str(row["negotiation_id"] or "").strip()
    if not negotiation:
        if row["status_checked_at"]:
            return {"ok": True, "changed": False, "status": row["status"], "skipped": "no_negotiation_id"}
        db.execute("""UPDATE application_outcomes SET status_checked_at=?, status_source=COALESCE(NULLIF(status_source,''),'imported_html'),
                      updated_at=? WHERE id=?""", (_now(), _now(), row["id"]))
        db.commit()
        return {"ok": True, "changed": False, "status": row["status"], "skipped": "no_negotiation_id"}
    token = (token or os.environ.get("HH_ACCESS_TOKEN") or "").strip()
    if not token:
        if row["source"] in ("html", "manual", "email", "csv", "json"):
            db.execute("""UPDATE application_outcomes SET status_checked_at=?,
                          status_source=COALESCE(NULLIF(status_source,''),'imported_html'),
                          updated_at=? WHERE id=?""", (_now(), _now(), row["id"]))
            db.commit()
            return {"ok": True, "changed": False, "status": row["status"],
                    "skipped": "no_token_html_status_used"}
        return {"ok": False, "retry": False, "reason": "HH_ACCESS_TOKEN не задан",
                "status": row["status"], "skipped": "no_token"}
    headers = _hh_headers(token)
    try:
        response = requests.get(f"https://api.hh.ru/negotiations/{negotiation}",
                                headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return {"ok": False, "retry": True, "reason": f"{type(exc).__name__}: {exc}"[:200]}
    evidence_url = f"https://hh.ru/negotiations/{negotiation}"
    if response.status_code == 429:
        return {"ok": False, "retry": True, "retry_after": 90, "http_status": 429,
                "reason": "HTTP 429: превышен лимит hh.ru"}
    if response.status_code in (401, 403):
        db.execute("""UPDATE application_outcomes SET last_enrich_error=?, updated_at=? WHERE id=?""",
                   (f"HTTP {response.status_code}: доступ к отклику запрещён", _now(), row["id"]))
        db.commit()
        return {"ok": False, "retry": False, "http_status": response.status_code,
                "reason": f"HTTP {response.status_code}: защита не обходится"}
    if response.status_code != 200:
        return {"ok": False, "retry": True, "http_status": response.status_code,
                "reason": f"HTTP {response.status_code}"}
    payload = response.json()
    state = payload.get("state") or {}
    raw_status = str(state.get("id") or state.get("name") or "")
    status = canonical_status(raw_status)
    messages: list = []
    try:
        msg_response = requests.get(f"https://api.hh.ru/negotiations/{negotiation}/messages",
                                    headers=headers, timeout=timeout)
        if msg_response.status_code == 200:
            messages = (msg_response.json() or {}).get("items") or []
    except requests.RequestException:
        messages = []
    if messages and status in ("waiting", "submitted", "not_viewed", "unknown"):
        status = "responded"
    old = row["status"]
    sent = _iso(str(payload.get("created_at") or row["sent_at"] or ""))
    last = _iso(str(payload.get("updated_at") or _now()))
    viewed_at = _iso(str(payload.get("viewed_at") or "")) if status in ("viewed", "responded", "invited",
                                                                        "interview", "offer") else row["viewed_at"]
    invited_at = last if status in ("invited", "interview", "offer") else row["invited_at"]
    rejected_at = last if status == "rejected" else row["rejected_at"]
    db.execute("""UPDATE application_outcomes SET status=?, raw_status=?, status_source='hh_api',
                  status_source_url=?, status_checked_at=?, negotiation_url=?,
                  sent_at=COALESCE(NULLIF(?,''),sent_at), last_event_at=?,
                  viewed_at=?, invited_at=?, rejected_at=?, messages_count=?,
                  has_messages=?, confidence=MAX(confidence,0.95), last_enrich_error=NULL,
                  updated_at=? WHERE id=?""",
               (status, raw_status, evidence_url, _now(), evidence_url, sent, last,
                viewed_at or "", invited_at or "", rejected_at or "", len(messages),
                1 if messages else 0, _now(), row["id"]))
    if status != old:
        _event(db, row["id"], status, last, "hh_api", 0.95,
               f"hh.ru state={raw_status}; messages={len(messages)}", raw_status, evidence_url)
    db.commit()
    return {"ok": True, "changed": status != old, "status": status,
            "raw_status": raw_status, "messages": len(messages), "evidence_url": evidence_url}


def _upsert(db: sqlite3.Connection, row: dict, *, event_note: str = "") -> int:
    existing = db.execute("SELECT * FROM application_outcomes WHERE external_key=?",
                          (row["external_key"],)).fetchone()
    # Deduplicate only by the stable external key. A date/vacancy fallback can
    # merge two genuinely different applications sent on the same day.
    if existing:
        values = {
            "application_id": row.get("application_id") or existing["application_id"],
            "job_id": row.get("job_id") or existing["job_id"],
            "employer": row.get("employer") or existing["employer"],
            "title": row.get("title") or existing["title"],
            "url": row.get("url") or existing["url"],
            "sent_at": min(x for x in (existing["sent_at"], row.get("sent_at")) if x) if
            existing["sent_at"] or row.get("sent_at") else None,
            "resume_variant": row.get("resume_variant") or existing["resume_variant"],
            "skills": json.dumps(row.get("skills") or _safe_json(existing["skills"], []), ensure_ascii=False),
            "experience_required": row.get("experience_required") if row.get("experience_required") is not None
            else existing["experience_required"],
            "seniority": row.get("seniority") or existing["seniority"],
            "direction": row.get("direction") or existing["direction"],
            "salary_min": row.get("salary_min") if row.get("salary_min") is not None else existing["salary_min"],
            "salary_max": row.get("salary_max") if row.get("salary_max") is not None else existing["salary_max"],
            "currency": row.get("currency") or existing["currency"],
            "location": row.get("location") or existing["location"],
            "work_mode": row.get("work_mode") or existing["work_mode"],
            "match_score": row.get("match_score") if row.get("match_score") is not None else existing["match_score"],
            "status": row["status"],
            "negotiation_id": row.get("negotiation_id") or existing["negotiation_id"],
            "status_source": row.get("status_source") or existing["status_source"],
            "raw_status": row.get("raw_status") or existing["raw_status"],
            "status_checked_at": row.get("status_checked_at") or existing["status_checked_at"],
            "status_source_url": row.get("status_source_url") or existing["status_source_url"],
            "negotiation_url": row.get("negotiation_url") or existing["negotiation_url"],
            "vacancy_url": row.get("vacancy_url") or existing["vacancy_url"],
            "vacancy_status": row.get("vacancy_status") or existing["vacancy_status"],
            "vacancy_checked_at": row.get("vacancy_checked_at") or existing["vacancy_checked_at"],
            "vacancy_http_status": row.get("vacancy_http_status") if row.get("vacancy_http_status") is not None
            else existing["vacancy_http_status"],
            "vacancy_evidence": row.get("vacancy_evidence") or existing["vacancy_evidence"],
            "vacancy_published_at": row.get("vacancy_published_at") or existing["vacancy_published_at"],
            "messages_count": max(int(existing["messages_count"] or 0), int(row.get("messages_count") or 0)),
            "has_messages": 1 if (existing["has_messages"] or row.get("has_messages")) else 0,
            "description_after_submission": 1 if row.get("description_after_submission") else
            int(existing["description_after_submission"] or 0),
            "viewed_at": row.get("viewed_at") or existing["viewed_at"],
            "invited_at": row.get("invited_at") or existing["invited_at"],
            "rejected_at": row.get("rejected_at") or existing["rejected_at"],
            "enrichment_attempts": max(int(existing["enrichment_attempts"] or 0),
                                       int(row.get("enrichment_attempts") or 0)),
            "last_enrich_error": row.get("last_enrich_error") or existing["last_enrich_error"],
            "confidence": max(float(existing["confidence"] or 0), float(row.get("confidence") or 0)),
            "feature_json": json.dumps(row.get("feature_json") or _safe_json(existing["feature_json"], {}),
                                       ensure_ascii=False),
            "raw_json": json.dumps(row.get("raw_json") or {}, ensure_ascii=False),
            "job_description": row.get("job_description") or existing["job_description"],
            "description_source": row.get("description_source") or existing["description_source"],
            "description_fetched_at": row.get("description_fetched_at") or existing["description_fetched_at"],
            "details_json": json.dumps(row.get("details_json") or _safe_json(existing["details_json"], {}),
                                       ensure_ascii=False),
            "completeness": max(float(existing["completeness"] or 0), float(row.get("completeness") or 0)),
            "last_event_at": row.get("last_event_at") or existing["last_event_at"],
            "updated_at": _now(),
        }
        assignments = ", ".join(f"{key}=?" for key in values)
        db.execute(f"UPDATE application_outcomes SET {assignments} WHERE id=?",
                   (*values.values(), existing["id"]))
        if row["status"] != existing["status"]:
            _event(db, existing["id"], row["status"], row.get("last_event_at") or _now(),
                   row.get("status_source") or "import", row.get("confidence") or 0.5, event_note)
        return existing["id"]
    cur = db.execute(
        """INSERT INTO application_outcomes(
             external_key,negotiation_id,application_id,job_id,source,response_type,employer,title,url,sent_at,
             last_event_at,resume_variant,skills,experience_required,seniority,direction,salary_min,
             salary_max,currency,location,work_mode,match_score,status,status_source,confidence,             feature_json,raw_json,job_description,description_source,description_fetched_at,
             details_json,completeness,raw_status,status_checked_at,status_source_url,
             negotiation_url,vacancy_url,vacancy_status,vacancy_checked_at,vacancy_http_status,
             vacancy_evidence,vacancy_published_at,messages_count,has_messages,
             description_after_submission,viewed_at,invited_at,rejected_at,
             enrichment_attempts,last_enrich_error,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (row["external_key"], row.get("negotiation_id") or "",
         row.get("application_id"), row.get("job_id"), row["source"],
         row.get("response_type") or "job", row.get("employer") or "", row.get("title") or "",
         row.get("url") or "", row.get("sent_at"), row.get("last_event_at"), row.get("resume_variant"),
         json.dumps(row.get("skills") or [], ensure_ascii=False), row.get("experience_required"),
         row.get("seniority") or "unknown", row.get("direction") or "other",
         row.get("salary_min"), row.get("salary_max"),
         row.get("currency"), row.get("location") or "", row.get("work_mode") or "",
         row.get("match_score"), row["status"], row.get("status_source") or "",
         float(row.get("confidence") or 0.5), json.dumps(row.get("feature_json") or {}, ensure_ascii=False),
         json.dumps(row.get("raw_json") or {}, ensure_ascii=False),
         row.get("job_description") or "", row.get("description_source") or "",
         row.get("description_fetched_at") or "",
         json.dumps(row.get("details_json") or {}, ensure_ascii=False),
         float(row.get("completeness") or 0.0), row.get("raw_status") or "",
         row.get("status_checked_at") or "", row.get("status_source_url") or "",
         row.get("negotiation_url") or "", row.get("vacancy_url") or "",
         row.get("vacancy_status") or "unknown", row.get("vacancy_checked_at") or "",
         row.get("vacancy_http_status"), row.get("vacancy_evidence") or "",
         row.get("vacancy_published_at") or "", int(row.get("messages_count") or 0),
         1 if row.get("has_messages") else 0, 1 if row.get("description_after_submission") else 0,
         row.get("viewed_at") or "", row.get("invited_at") or "", row.get("rejected_at") or "",
         int(row.get("enrichment_attempts") or 0), row.get("last_enrich_error") or "",
         _now(), _now()))
    _event(db, cur.lastrowid, row["status"], row.get("last_event_at") or row.get("sent_at") or _now(),
           row.get("status_source") or "import", row.get("confidence") or 0.5, event_note)
    return int(cur.lastrowid)


def _event(db: sqlite3.Connection, outcome_id: int, status: str, event_at: str,
           source: str, confidence: float, note: str = "", raw_status: str = "",
           evidence_url: str = "") -> None:
    db.execute("""INSERT INTO application_outcome_events(outcome_id,status,event_at,source,
                  confidence,note,raw_json,raw_status,evidence_url)
                  VALUES (?,?,?,?,?,?,?,?,?)""",
               (outcome_id, status, event_at, source, confidence, note[:500], "{}",
                raw_status[:120], evidence_url[:500]))


def parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader if any(str(v or "").strip() for v in row.values())]


def parse_json(text: str) -> list[dict]:
    data = json.loads(text)
    if isinstance(data, dict):
        for key in ("items", "negotiations", "rows", "outcomes"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    raise ValueError("JSON должен быть массивом или объектом с items")


def parse_html(text: str, source: str = "html") -> list[dict]:
    """Tolerant parser for saved HH negotiation pages; unknown fields stay empty."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        BeautifulSoup = None
    rows: list[dict] = []
    if BeautifulSoup:
        soup = BeautifulSoup(text, "html.parser")
        seen = set()
        blocks = soup.select('[data-qa*="negotiation"], [data-qa*="vacancy-response"]')
        for block in blocks:
            text_block = " ".join(block.stripped_strings)
            link = block.select_one('a[href*="/vacancy/"]')
            if not link:
                continue
            href = link.get("href") or ""
            if href.startswith("/"):
                href = "https://hh.ru" + href
            title = " ".join(link.stripped_strings)
            job_match = re.search(r"/vacancy/(\d+)", href)
            job_id = job_match.group(1) if job_match else ""
            neg_match = re.search(r"/negotiations?/(\d+)", str(block))
            negotiation_id = (block.get("data-negotiation-id") or
                              block.get("data-id") or
                              (neg_match.group(1) if neg_match else ""))
            employer = ""
            for selector in (".vacancy-response__company", '[data-qa*="company"]',
                             '[data-qa*="employer"]'):
                node = block.select_one(selector)
                if node:
                    employer = " ".join(node.stripped_strings)
                    break
            date_node = block.select_one("time[datetime], [data-qa*='date']")
            date = (date_node.get("datetime") if date_node and date_node.get("datetime") else
                    (date_node.get_text(" ", strip=True) if date_node else ""))
            if not date:
                match = re.search(r"\d{2}\.\d{2}\.\d{4}", text_block)
                date = match.group(0) if match else ""
            key = negotiation_id or f"{job_id}|{title}|{date}"
            if key in seen:
                continue
            seen.add(key)
            raw_status = ""
            for phrase in ("Не просмотрено", "Просмотрено", "Приглашение", "Отказ",
                           "Интервью", "Оффер", "Ожидание", "Отозвано"):
                if phrase.casefold() in text_block.casefold():
                    raw_status = phrase
                    break
            rows.append({
                "negotiation_id": str(negotiation_id or ""),
                "job_id": job_id,
                "title": title,
                "employer": employer,
                "url": href,
                "sent_at": date,
                "last_event_at": date,
                "status": canonical_status(text_block),
                "raw_status": raw_status,
                "status_source": "html", "confidence": 0.6,
            })
    if not rows:
        for match in re.finditer(r'<a[^>]+href="([^"]*/vacancy/(\d+)[^"]*)"[^>]*>(.*?)</a>', text, re.S):
            job_id = match.group(2)
            rows.append({"job_id": job_id, "title": re.sub(r"<[^>]+>", " ", match.group(3)).strip(),
                         "url": match.group(1), "status": "unknown",
                         "status_source": "html", "confidence": 0.4})
    return rows


def _extract_conditions(text: str) -> dict:
    low = (text or "").casefold()
    experience = None
    match = re.search(r"(?:опыт|experience)[^\d]{0,20}(\d+(?:[.,]\d+)?)\s*(?:год|лет|year)", low)
    if match:
        experience = _number(match.group(1))
    courses = re.findall(r"(\d)\s*(?:[-–—,и]\s*(\d)\s*)?курс", low)
    required_courses = sorted({int(x) for pair in courses for x in pair if x})
    english = ""
    match = re.search(r"(?:английск\w*|english)[^\n.]{0,40}?(beginner|elementary|pre-?intermediate|"
                      r"intermediate|upper-?intermediate|advanced|b2|c1|c2|a2|a1)", low)
    if match:
        english = match.group(1)
    return {
        "experience_required": experience,
        "required_courses": required_courses,
        "education": ("высшее" if "высшее" in low else
                      "неоконченное высшее" if "неоконченное высшее" in low else ""),
        "english": english,
        "math": bool(re.search(r"математи|statistics|статистик|probability|вероятност", low)),
        "part_time": bool(re.search(r"part-?time|частичн\w* занят|неполн\w* занят", low)),
        "hours_week": (_number(re.search(r"(\d{1,2})\s*час\w*\s*в\s*неделю", low).group(1))
                       if re.search(r"(\d{1,2})\s*час\w*\s*в\s*неделю", low) else None),
        "internship": bool(re.search(r"стаж|intern|trainee", low)),
        "seniority": seniority_of(text),
        "work_mode": ("remote" if re.search(r"удал[её]н|remote", low) else
                      "hybrid" if re.search(r"гибрид|hybrid", low) else
                      "office" if re.search(r"офис|office", low) else ""),
    }


def parse_vacancy_html(text: str, source: str = "html") -> dict:
    """Extract a full vacancy page supplied by the user (no invention)."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {}
    soup = BeautifulSoup(text, "html.parser")
    title_node = soup.select_one('[data-qa="vacancy-title"], h1')
    body_node = soup.select_one('[data-qa="vacancy-description"], .vacancy-description, article')
    if not title_node or not body_node:
        return {}
    vacancy_id = ""
    link = soup.select_one('link[rel="canonical"], meta[property="og:url"]')
    raw_url = ""
    if link:
        raw_url = link.get("href") if link.name == "link" else link.get("content") or ""
    match = re.search(r"/vacancy/(\d+)", raw_url + " " + text[:5000])
    if match:
        vacancy_id = match.group(1)
    title = title_node.get_text(" ", strip=True)
    description = body_node.get_text("\n", strip=True)
    company = ""
    node = soup.select_one('[data-qa="vacancy-company-name"], [data-qa="bloko-header-2"]')
    if node:
        company = node.get_text(" ", strip=True)
    location = ""
    for selector in ('[data-qa="vacancy-view-location"]', '[data-qa="vacancy-view-raw-address"]'):
        node = soup.select_one(selector)
        if node:
            location = node.get_text(" ", strip=True)
            break
    experience = ""
    node = soup.select_one('[data-qa="vacancy-experience"]')
    if node:
        experience = node.get_text(" ", strip=True)
    skills = [n.get_text(" ", strip=True) for n in
              soup.select('[data-qa="bloko-tag__text"], [data-qa*="skill"]')]
    education = ""
    match = re.search(r"(высшее|неоконченное высшее|среднее|бакалавр|магистр)[^.\n]{0,120}",
                      description, re.I)
    if match:
        education = match.group(0).strip()
    conditions = _extract_conditions(description)
    salary = ""
    node = soup.select_one('[data-qa="vacancy-salary"]')
    if node:
        salary = node.get_text(" ", strip=True)
    published = ""
    node = soup.select_one('meta[property="article:published_time"], time[datetime]')
    if node:
        published = node.get("content") if node.name == "meta" else node.get("datetime") or ""
    return {
        "job_id": vacancy_id, "title": title, "employer": company,
        "description": description[:30000], "skills": list(dict.fromkeys(skills)),
        "experience_text": experience, "location": location,
        "education": education, "salary_text": salary,
        "published_at": published or "",
        "vacancy_status": "unknown", "source": source,
        "fetched_at": _now(),
        **conditions,
    }


def _extract_vacancy_pages(files: list[tuple[str, bytes]]) -> dict[str, dict]:
    pages: dict[str, dict] = {}
    for name, data in files:
        text = _decode_file(name, data)
        if not text:
            continue
        page = parse_vacancy_html(text, "html")
        if page.get("job_id"):
            pages[page["job_id"]] = page
    return pages


def _decode_file(name: str, data: bytes) -> str:
    suffix = Path(name).suffix.casefold()
    if suffix not in (".html", ".htm", ".txt", ".json", ".csv"):
        return ""
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def parse_history_files(db: sqlite3.Connection, files: list[tuple[str, bytes]],
                        *, max_unzip_mb: int = 40) -> dict:
    """Read multiple HTML pages and ZIP archives, then merge by negotiation/vacancy."""
    import zipfile
    expanded: list[tuple[str, bytes]] = []
    errors: list[str] = []
    for name, data in files:
        suffix = Path(name).suffix.casefold()
        if suffix == ".zip":
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    total = sum(info.file_size for info in archive.infolist())
                    if total > max_unzip_mb * 1024 * 1024:
                        errors.append(f"{name}: распакованный размер больше {max_unzip_mb} МБ")
                        continue
                    for info in archive.infolist():
                        if info.is_dir() or info.file_size == 0:
                            continue
                        if Path(info.filename).suffix.casefold() not in (".html", ".htm", ".txt", ".json", ".csv"):
                            continue
                        expanded.append((f"{name}!{info.filename}", archive.read(info)))
            except zipfile.BadZipFile:
                errors.append(f"{name}: повреждённый ZIP")
        else:
            expanded.append((name, data))

    vacancy_pages = _extract_vacancy_pages(expanded)
    raw_rows: list[dict] = []
    pages = 0
    for name, data in expanded:
        text = _decode_file(name, data)
        if not text:
            continue
        pages += 1
        if Path(name).suffix.casefold() in (".json", ".csv"):
            try:
                source = "json" if name.casefold().endswith(".json") else "csv"
                raw_rows.extend(_rows_from_source(source, text))
            except Exception as exc:
                errors.append(f"{name}: {exc}")
            continue
        parsed = parse_html(text)
        for row in parsed:
            row.setdefault("source_file", name)
            raw_rows.append(row)

    merged: dict[str, dict] = {}
    for row in raw_rows:
        key = str(row.get("negotiation_id") or "") or f"{row.get('job_id')}|{row.get('url')}|{row.get('sent_at')}"
        if key in merged:
            continue
        page = vacancy_pages.get(str(row.get("job_id") or ""))
        if page:
            row.setdefault("title", page.get("title"))
            row.setdefault("employer", page.get("employer"))
            row["job_description"] = page.get("description") or ""
            row["description_source"] = "user_html"
            row["description_fetched_at"] = page.get("fetched_at") or _now()
            row["skills"] = page.get("skills") or []
            row["location"] = page.get("location") or row.get("location") or ""
            row["details"] = page
            row["completeness"] = _completeness(page)
        merged[key] = row

    result = preview(db, "html", data=list(merged.values()))
    result.update({"files": len(files), "expanded_files": len(expanded), "pages": pages,
                   "vacancy_pages": len(vacancy_pages), "errors": errors[:20],
                   "with_job_id": sum(1 for row in result["rows"] if row.get("job_id"))})
    return result


def _completeness(details: dict) -> float:
    if not details:
        return 0.0
    fields = ("title", "employer", "description", "skills", "experience_text",
              "location", "education")
    return round(sum(1 for key in fields if details.get(key)) / len(fields), 3)


def enrich_outcome(db: sqlite3.Connection, row, *, use_api: bool = True,
                   api_token: str | None = None) -> dict:
    """Attach a vacancy description from SQLite, user vacancy HTML or official API."""
    job_id = str(row["job_id"] or "").strip()
    if not job_id and row["url"]:
        match = re.search(r"/vacancy/(\d+)", row["url"])
        job_id = match.group(1) if match else ""
    details: dict = {}
    source = ""
    job = None
    if job_id:
        job = db.execute("""SELECT * FROM jobs WHERE source_id=? OR id=? OR url LIKE ?
                            ORDER BY fetched_at DESC LIMIT 1""",
                         (job_id, job_id, f"%/vacancy/{job_id}%")).fetchone()
        if not job:
            publication = db.execute("""SELECT canonical_id FROM job_publications
                                        WHERE source_id=? OR url LIKE ?
                                        ORDER BY created_at DESC LIMIT 1""",
                                     (job_id, f"%/vacancy/{job_id}%")).fetchone()
            if publication:
                job = dbm.get_job(db, publication["canonical_id"])
    if job:
        detail = dbm.job_detail(job)
        details = {
            "job_id": job_id, "title": job["title"], "employer": job["company"],
            "description": job["description"], "skills": (detail.get("skills") or {}).get("matched") or [],
            "experience_text": job["experience_text"] or "",
            "location": job["location"] or "", "work_mode": job["work_mode"] or "",
            "education": "требуется" if (detail.get("education") or {}).get("required") else "",
            "published_at": job["published_at"] or "",
            "vacancy_status": "unknown",
            "source": "sqlite", "fetched_at": dbm.now(),
            **_extract_conditions(job["description"] or ""),
        }
        source = "sqlite"
    raw = _safe_json(row["details_json"], {})
    if not details and raw.get("description"):
        details = raw
        source = row["description_source"] or "user_html"
    if not details and use_api and job_id:
        token = (api_token or os.environ.get("HH_ACCESS_TOKEN") or "").strip()
        if token:
            try:
                response = requests.get(f"https://api.hh.ru/vacancies/{job_id}",
                                        headers={"User-Agent": "AI-Job-Hunter/4.0",
                                                 "Authorization": f"Bearer {token}"}, timeout=20)
                if response.status_code == 200:
                    payload = response.json()
                    description = re.sub(r"<[^>]+>", "\n", payload.get("description") or "")
                    archived = payload.get("archived")
                    experience_name = (payload.get("experience") or {}).get("name") or ""
                    conditions = _extract_conditions(description + "\n" + experience_name)
                    details = {
                        "job_id": job_id, "title": payload.get("name") or "",
                        "employer": (payload.get("employer") or {}).get("name") or "",
                        "description": description,
                        "skills": [x.get("name") for x in payload.get("key_skills") or [] if x.get("name")],
                        "experience_text": experience_name,
                        "location": (payload.get("area") or {}).get("name") or "",
                        "published_at": payload.get("published_at") or "",
                        "salary_text": (payload.get("salary") or {}).get("from") or "",
                        "employment": (payload.get("employment") or {}).get("name") or "",
                        "schedule": (payload.get("schedule") or {}).get("name") or "",
                        "vacancy_status": ("archived" if archived else "active"),
                        "vacancy_http_status": 200,
                        "education": conditions.get("education") or "",
                        "source": "hh_api", "fetched_at": _now(),
                        **conditions,
                    }
                    source = "hh_api"
                elif response.status_code == 404:
                    details = {"error": "HTTP 404", "vacancy_status": "removed",
                               "vacancy_http_status": 404, "source": "hh_api",
                               "vacancy_evidence": "hh.ru API: вакансия не найдена"}
                    source = "hh_api"
                elif response.status_code in (403, 429):
                    details = {"error": f"HTTP {response.status_code}",
                               "request_id": response.headers.get("X-Request-Id", ""),
                               "vacancy_status": "unavailable",
                               "vacancy_http_status": response.status_code,
                               "vacancy_evidence": f"hh.ru API HTTP {response.status_code}"}
                    source = "hh_api"
            except requests.RequestException:
                pass
    if not details:
        return {"ok": False, "job_id": job_id, "source": "none",
                "reason": "Описание недоступно: нет в SQLite, HTML или API"}

    values = {
        "job_id": job_id or row["job_id"],
        "job_description": details.get("description") or "",
        "description_source": source,
        "description_fetched_at": details.get("fetched_at") or _now(),
        "details_json": json.dumps(details, ensure_ascii=False),
        "completeness": _completeness(details),
        "title": row["title"] or details.get("title") or "",
        "employer": row["employer"] or details.get("employer") or "",
        "skills": json.dumps(details.get("skills") or _safe_json(row["skills"], []), ensure_ascii=False),
        "location": row["location"] or details.get("location") or "",
        "work_mode": row["work_mode"] or details.get("work_mode") or "",
        "vacancy_url": row["vacancy_url"] or row["url"] or "",
        "vacancy_status": details.get("vacancy_status") or "unknown",
        "vacancy_checked_at": _now(),
        "vacancy_http_status": details.get("vacancy_http_status"),
        "vacancy_evidence": details.get("vacancy_evidence") or source,
        "vacancy_published_at": details.get("published_at") or "",
        "description_after_submission": 1 if (details.get("fetched_at") or _now()) >
        (row["sent_at"] or "") else 0,
    }
    if details.get("experience_required") is not None:
        values["experience_required"] = details["experience_required"]
    if details.get("seniority") and details["seniority"] != "unknown":
        values["seniority"] = details["seniority"]
    if details.get("salary_text"):
        parsed = parse_salary(str(details["salary_text"]))
        if parsed[0] is not None:
            values["salary_min"] = parsed[0]
        if parsed[1] is not None:
            values["salary_max"] = parsed[1]
    existing_features = _safe_json(row["feature_json"], {})
    if not existing_features.get("at_submission"):
        derived = dict(existing_features)
        derived.update({
            "seniority": seniority_of(details.get("title") or row["title"] or ""),
            "direction": direction_of(details.get("title") or row["title"] or ""),
            "work_mode": details.get("work_mode") or row["work_mode"] or "",
            "location": details.get("location") or row["location"] or "",
            "education_required": bool(re.search(r"высшее|образование", str(details.get("education") or ""), re.I)),
            "vacancy_type": "internship" if re.search(r"стаж|intern|trainee",
                                                       details.get("title") or row["title"] or "", re.I) else "job",
            "at_submission": False,
            "captured_at": details.get("fetched_at") or _now(),
            "derived_from": "imported_description",
        })
        values["feature_json"] = json.dumps(derived, ensure_ascii=False)
    assignments = ", ".join(f"{key}=?" for key in values)
    if job_id:
        db.execute(f"UPDATE application_outcomes SET {assignments}, updated_at=? WHERE job_id=?",
                   (*values.values(), _now(), job_id))
        affected = db.execute("SELECT COUNT(*) FROM application_outcomes WHERE job_id=?", (job_id,)).fetchone()[0]
    else:
        db.execute(f"UPDATE application_outcomes SET {assignments}, updated_at=? WHERE id=?",
                   (*values.values(), _now(), row["id"]))
        affected = 1
    db.commit()
    return {"ok": True, "job_id": job_id, "source": source,
            "completeness": values["completeness"], "affected": affected,
            "vacancy_status": values.get("vacancy_status", "unknown")}


def enrich_batch(db: sqlite3.Connection, *, limit: int = 10, use_api: bool = True) -> dict:
    rows = db.execute("""SELECT * FROM application_outcomes
                         WHERE job_description='' OR job_description IS NULL
                         ORDER BY id LIMIT ?""", (max(1, min(50, limit)),)).fetchall()
    results = [enrich_outcome(db, row, use_api=use_api) for row in rows]
    total = db.execute("SELECT COUNT(*) FROM application_outcomes").fetchone()[0]
    complete = db.execute("""SELECT COUNT(*) FROM application_outcomes
                             WHERE completeness>=0.75""").fetchone()[0]
    partial = db.execute("""SELECT COUNT(*) FROM application_outcomes
                            WHERE completeness>0 AND completeness<0.75""").fetchone()[0]
    missing = db.execute("""SELECT COUNT(*) FROM application_outcomes
                            WHERE job_description='' OR job_description IS NULL""").fetchone()[0]
    return {"checked": len(rows), "ok": sum(1 for r in results if r.get("ok")),
            "failed": sum(1 for r in results if not r.get("ok")),
            "details": results, "total": total, "complete": complete,
            "partial": partial, "missing": missing,
            "remaining": missing}


def enqueue_all(db: sqlite3.Connection) -> dict:
    """Checkpointed queue: every outcome gets a status task, every unique vacancy a detail task."""
    created = 0
    rows = db.execute("SELECT * FROM application_outcomes").fetchall()
    for row in rows:
        for entity_type, key in (("outcome", str(row["id"])), ("vacancy", str(row["job_id"] or ""))):
            if entity_type == "vacancy" and not key:
                continue
            if entity_type == "vacancy" and key and (row["job_description"] or "").strip() \
                    and float(row["completeness"] or 0) >= 0.75:
                continue
            if entity_type == "outcome" and row["status_checked_at"] and row["status"] in (
                    "rejected", "invited", "interview", "offer", "withdrawn"):
                continue
            cur = db.execute("""INSERT OR IGNORE INTO enrichment_queue
                                (entity_type,entity_key,state,created_at,updated_at)
                                VALUES (?,?, 'pending',?,?)""",
                             (entity_type, key, _now(), _now()))
            created += int(cur.rowcount or 0)
    db.commit()
    return {"queued": created, "queue_total": db.execute(
        "SELECT COUNT(*) FROM enrichment_queue WHERE state<>'done'").fetchone()[0]}


def queue_status(db: sqlite3.Connection) -> dict:
    counts = {r["state"]: r["n"] for r in db.execute(
        "SELECT state, COUNT(*) n FROM enrichment_queue GROUP BY state")}
    return {"counts": counts, "pending": counts.get("pending", 0) + counts.get("retry", 0)}


def process_queue(db: sqlite3.Connection, *, limit: int = 20, use_api: bool = True,
                  token: str | None = None) -> dict:
    """Process due status/detail tasks with checkpoints, cache and 429 backoff."""
    if not db.execute("SELECT 1 FROM enrichment_queue LIMIT 1").fetchone():
        enqueue_all(db)
    now = _now()
    tasks = db.execute("""SELECT * FROM enrichment_queue
                          WHERE state IN ('pending','retry')
                            AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                          ORDER BY id LIMIT ?""", (now, max(1, min(50, limit)))).fetchall()
    processed = status_changed = errors = 0
    for task in tasks:
        db.execute("UPDATE enrichment_queue SET state='running', updated_at=? WHERE id=?",
                   (_now(), task["id"]))
        db.commit()
        result: dict = {}
        try:
            if task["entity_type"] == "outcome":
                row = db.execute("SELECT * FROM application_outcomes WHERE id=?",
                                 (int(task["entity_key"]),)).fetchone()
                if row:
                    result = check_status(db, row, token=token)
                    processed += 1
                    status_changed += int(bool(result.get("changed")))
            else:
                row = db.execute("""SELECT * FROM application_outcomes
                                    WHERE job_id=? ORDER BY id LIMIT 1""",
                                 (task["entity_key"],)).fetchone()
                if row:
                    result = enrich_outcome(db, row, use_api=use_api, api_token=token)
                    processed += 1
            ok = bool(result.get("ok"))
            if ok:
                db.execute("""UPDATE enrichment_queue SET state='done', attempts=attempts+1,
                              last_error=NULL, updated_at=? WHERE id=?""", (_now(), task["id"]))
            else:
                attempts = int(task["attempts"] or 0) + 1
                errors += 1
                retry = result.get("retry", attempts < 3)
                state = "retry" if retry else "done"
                minutes = result.get("retry_after") or (2 if retry else 0)
                next_at = (_now() if not minutes else
                           datetime.fromtimestamp(
                               datetime.now(timezone.utc).timestamp() + minutes * 60,
                               tz=timezone.utc).isoformat(timespec="seconds"))
                db.execute("""UPDATE enrichment_queue SET state=?, attempts=?, last_error=?,
                              next_attempt_at=?, updated_at=? WHERE id=?""",
                           (state, attempts, str(result.get("reason") or "")[:400],
                            next_at, _now(), task["id"]))
        except Exception as exc:
            errors += 1
            db.execute("""UPDATE enrichment_queue SET state='retry', attempts=attempts+1,
                          last_error=?, next_attempt_at=?, updated_at=? WHERE id=?""",
                       (f"{type(exc).__name__}: {exc}"[:400], _now(), _now(), task["id"]))
        db.commit()
        if result.get("http_status") == 429:
            break
        time.sleep(0.25 if use_api else 0)
    status_info = dataset_status(db)
    counts = db.execute("""SELECT COUNT(*) total,
                           SUM(CASE WHEN status_checked_at IS NOT NULL THEN 1 ELSE 0 END) checked,
                           SUM(CASE WHEN status='invited' THEN 1 ELSE 0 END) invited,
                           SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) rejected,
                           SUM(CASE WHEN status='not_viewed' THEN 1 ELSE 0 END) not_viewed,
                           COUNT(DISTINCT NULLIF(job_id,'')) unique_vacancies,
                           SUM(CASE WHEN vacancy_status='unavailable' THEN 1 ELSE 0 END) unavailable,
                           SUM(CASE WHEN vacancy_status='removed' THEN 1 ELSE 0 END) removed,
                           SUM(CASE WHEN vacancy_status='archived' THEN 1 ELSE 0 END) archived
                           FROM application_outcomes""").fetchone()
    return {
        "processed": processed, "status_checked": status_info.get("status_checked", 0),
        "status_changed": status_changed, "errors": errors,
        "total_outcomes": int(counts["total"] or 0),
        "unique_vacancies": int(counts["unique_vacancies"] or 0),
        "invited": int(counts["invited"] or 0), "rejected": int(counts["rejected"] or 0),
        "not_viewed": int(counts["not_viewed"] or 0),
        "unavailable": int(counts["unavailable"] or 0) + int(counts["removed"] or 0),
        "archived": int(counts["archived"] or 0),
        "complete": status_info["complete_descriptions"],
        "partial": status_info["partial_descriptions"],
        "missing": status_info["missing_descriptions"],
        "ready": status_info["feature_ready"],
        "queue": queue_status(db),
        "dataset": status_info,
    }


def dataset_status(db: sqlite3.Connection) -> dict:
    rows = db.execute("SELECT * FROM application_outcomes").fetchall()
    complete = sum(1 for r in rows if (r["completeness"] or 0) >= 0.75)
    partial = sum(1 for r in rows if 0 < (r["completeness"] or 0) < 0.75)
    missing = sum(1 for r in rows if not (r["job_description"] or "").strip())
    feature_ready = sum(1 for r in rows
                        if _features(r).get("at_submission") is True and r["status"] in
                        (RESOLVED_POSITIVE + RESOLVED_NEGATIVE))
    resolved = sum(1 for r in rows if r["status"] in (RESOLVED_POSITIVE + RESOLVED_NEGATIVE))
    return {
        "total": len(rows), "complete_descriptions": complete,
        "partial_descriptions": partial, "missing_descriptions": missing,
        "resolved": resolved, "feature_ready": feature_ready,
        "status_checked": sum(1 for r in rows if r["status_checked_at"]),
        "statuses": {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES},
        "vacancy_statuses": {s: sum(1 for r in rows if r["vacancy_status"] == s)
                             for s in ("active", "archived", "removed", "unavailable", "unknown")},
        "unique_vacancies": len({str(r["job_id"]) for r in rows if r["job_id"]}),
        "ml_ready": feature_ready >= MIN_ML_N and
        sum(1 for r in rows if r["status"] in RESOLVED_POSITIVE) >= MIN_ML_POS and
        sum(1 for r in rows if r["status"] == "rejected") >= MIN_ML_NEG,
        "thresholds": {"n": MIN_ML_N, "positives": MIN_ML_POS, "negatives": MIN_ML_NEG},
    }


def _rows_from_source(source: str, text: str, data=None) -> list[dict]:
    if data is not None:
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            return [data]
    text = (text or "").strip()
    if not text:
        return []
    if source == "csv":
        return parse_csv(text)
    if source == "json":
        return parse_json(text)
    if source == "html":
        return parse_html(text)
    if source == "email":
        # An explicitly pasted notification. Never read a mailbox without setup.
        rows = []
        for chunk in re.split(r"\n\s*\n", text):
            if not chunk.strip():
                continue
            title = ""
            match = re.search(r"(?:вакансия|позиция)\s*[:\-]\s*(.+)", chunk, re.I)
            if match:
                title = match.group(1).strip()
            company = ""
            match = re.search(r"(?:компания|работодатель)\s*[:\-]\s*(.+)", chunk, re.I)
            if match:
                company = match.group(1).strip()
            rows.append({"title": title or chunk.splitlines()[0][:160], "employer": company,
                         "status": canonical_status(chunk), "status_source": "email",
                         "confidence": 0.7, "raw_text": chunk[:2000]})
        return rows
    # Manual/source row list
    return [{"title": text, "status": "unknown", "status_source": source}]


def preview(db: sqlite3.Connection, source: str, text: str = "", data=None) -> dict:
    source = source if source in ("hh_api", "csv", "json", "html", "email", "manual") else "json"
    rows = _rows_from_source(source, text, data)
    normalized = []
    for raw in rows:
        row = normalize_row(raw, source)
        if not row["title"] and not row["url"] and not row["employer"]:
            continue
        existing = db.execute("SELECT id,status FROM application_outcomes WHERE external_key=?",
                              (row["external_key"],)).fetchone()
        row["action"] = "update" if existing else "new"
        row["existing_status"] = existing["status"] if existing else ""
        normalized.append(row)
    return {"ok": True, "source": source, "rows": normalized, "total": len(normalized),
            "new": sum(1 for x in normalized if x["action"] == "new"),
            "updates": sum(1 for x in normalized if x["action"] == "update")}


def commit(db: sqlite3.Connection, source: str, rows: list[dict]) -> dict:
    ids, actions = [], Counter()
    for raw in rows:
        row = normalize_row(raw, source)
        for key in ("external_key", "feature_json"):
            if raw.get(key):
                row[key] = raw[key]
        if not row["title"] and not row["url"] and not row["employer"]:
            continue
        existing = db.execute("SELECT id FROM application_outcomes WHERE external_key=?",
                              (row["external_key"],)).fetchone()
        outcome_id = _upsert(db, row, event_note="Импорт истории")
        if existing:
            db.execute("""UPDATE enrichment_queue SET state='pending', next_attempt_at=NULL,
                          updated_at=? WHERE entity_type='outcome' AND entity_key=?""",
                       (_now(), str(outcome_id)))
            if row.get("job_id"):
                db.execute("""UPDATE enrichment_queue SET state='pending', next_attempt_at=NULL,
                              updated_at=? WHERE entity_type='vacancy' AND entity_key=?""",
                           (_now(), str(row["job_id"])))
        ids.append(outcome_id)
        actions["updated" if existing else "added"] += 1
    db.commit()
    queued = enqueue_all(db) if rows else {"queued": 0}
    return {"ok": True, "ids": ids, "added": actions["added"],
            "updated": actions["updated"], "total": len(ids), "queue": queued}


def set_status(db: sqlite3.Connection, outcome_id: int, status: str, *,
               source: str = "user", confidence: float = 0.9, note: str = "") -> dict:
    row = db.execute("SELECT * FROM application_outcomes WHERE id=?", (outcome_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "Исход не найден"}
    mapped = canonical_status(status)
    _event(db, outcome_id, mapped, _now(), source, confidence, note)
    db.execute("""UPDATE application_outcomes SET status=?, last_event_at=?, status_source=?,
                  confidence=?, updated_at=? WHERE id=?""",
               (mapped, _now(), source, confidence, _now(), outcome_id))
    db.commit()
    return {"ok": True, "status": mapped, "summary": summary(db)}


def preview_hh_api(db: sqlite3.Connection, token: str | None = None,
                   pages: int = 500) -> dict:
    token = (token or os.environ.get("HH_ACCESS_TOKEN") or "").strip()
    if not token:
        return {"ok": False, "status": "no_token",
                "reason": "HH_ACCESS_TOKEN не задан. Зарегистрируйте приложение на dev.hh.ru и сохраните OAuth-токен в окружении."}
    headers = {"User-Agent": "AI-Job-Hunter/4.0 (personal local job search)",
               "Authorization": f"Bearer {token}"}
    rows = []
    try:
        for page in range(max(1, min(500, pages))):
            response = requests.get("https://api.hh.ru/negotiations",
                                    params={"per_page": 100, "page": page},
                                    headers=headers, timeout=25)
            if response.status_code in (401, 403, 429):
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                values = [str(x.get("value") or x.get("type") or "")
                          for x in (payload.get("errors") or []) if isinstance(x, dict)]
                return {"ok": False,
                        "status": ("auth_required" if response.status_code == 401 else
                                   "rate_limited" if response.status_code == 429 else "blocked"),
                        "http_status": response.status_code,
                        "request_id": response.headers.get("X-Request-Id", ""),
                        "errors": values,
                        "kind": next((v for v in values if v), "forbidden"),
                        "reason": f"HTTP {response.status_code}: доступ ограничен; защита не обходится"}
            if response.status_code != 200:
                return {"ok": False, "status": "error", "http_status": response.status_code,
                        "reason": f"HTTP {response.status_code}"}
            payload = response.json()
            items = payload.get("items") or []
            rows.extend(items)
            if not items or not payload.get("pages") or page + 1 >= payload.get("pages"):
                break
    except requests.RequestException as exc:
        return {"ok": False, "status": "unavailable",
                "reason": f"{type(exc).__name__}: {exc}"[:250]}
    result = preview(db, "hh_api", data=rows)
    result["http_status"] = 200
    return result


def _counts(db: sqlite3.Connection, where: str = "", params=()) -> dict:
    rows = db.execute(f"""SELECT status, COUNT(*) n FROM application_outcomes
                          WHERE 1=1 {where} GROUP BY status""", params).fetchall()
    counts = {status: 0 for status in STATUSES}
    counts.update({r["status"]: r["n"] for r in rows})
    successes = counts["invited"] + counts["offer"]
    failures = counts["rejected"]
    pending = sum(counts[s] for s in PENDING)
    return {"counts": counts, "successes": successes, "failures": failures,
            "resolved": successes + failures, "pending": pending,
            "total": sum(counts.values())}


def _beta_interval(a: float, b: float) -> tuple[float, float]:
    try:
        from scipy.stats import beta as beta_dist
        return round(float(beta_dist.ppf(0.05, a, b)), 4), round(float(beta_dist.ppf(0.95, a, b)), 4)
    except Exception:
        import random
        rng = random.Random(42)
        samples = sorted(rng.betavariate(a, b) for _ in range(12000))
        return round(samples[int(len(samples) * 0.05)], 4), round(samples[int(len(samples) * 0.95)], 4)


def _posterior(successes: int, failures: int) -> dict:
    a = PRIOR[0] + successes
    b = PRIOR[1] + failures
    low, high = _beta_interval(a, b)
    mean = a / (a + b)
    width = max(0.0, high - low)
    confidence = max(0.0, min(1.0, (1.0 - width) * min(1.0, (successes + failures) / 20.0)))
    return {"mean": round(mean, 4), "interval_90": [low, high], "width": round(width, 4),
            "confidence": round(confidence, 3), "prior": list(PRIOR)}


def estimate(db: sqlite3.Connection, target: dict | None = None) -> dict:
    target = target or {}
    features = target.get("features") if isinstance(target.get("features"), dict) else {}
    if features:
        ml = predict_ml(features)
        if ml.get("available"):
            return {
                "available": True, "personal_probability": ml["probability"],
                "point_estimate_for_display": round(ml["probability"] * 100, 1),
                "interval_90": None, "confidence": round(max(0.0, 1.0 - float(ml.get("brier") or 0.5)), 3),
                "confidence_label": "средняя", "n": ml.get("n") or 0,
                "successes": 0, "failures": 0, "pending": 0,
                "scope": "logistic_regression", "method": ml["method"],
                "reason": f"ML-модель {ml.get('version')}; Brier={ml.get('brier')}.",
                "prior": list(PRIOR),
            }
    seniority = seniority_of(target.get("seniority") or target.get("title") or "")
    direction = direction_of(target.get("direction") or target.get("title") or "")
    if target.get("seniority") in ("intern", "junior", "middle", "senior", "unknown"):
        seniority = target["seniority"]
    if target.get("direction"):
        direction = target["direction"]

    specific = _counts(db, "AND seniority=? AND direction=?", (seniority, direction))
    if specific["resolved"] >= 12:
        segment = specific
        scope = f"{seniority}/{direction}"
    else:
        segment = _counts(db, "AND seniority=?", (seniority,))
        scope = seniority if segment["resolved"] else "prior"
    posterior = _posterior(segment["successes"], segment["failures"])
    sufficient = (segment["resolved"] >= MIN_PUBLISH_N and
                  segment["successes"] >= MIN_PUBLISH_POS and
                  segment["failures"] >= MIN_PUBLISH_NEG and
                  posterior["width"] <= MAX_PUBLISH_WIDTH)
    if not sufficient:
        reason = ("Недостаточно истории для достоверного прогноза: "
                  f"n={segment['resolved']}, приглашений={segment['successes']}, отказов={segment['failures']}.")
    else:
        reason = f"Beta-Binomial по сегменту {scope}; n={segment['resolved']}."
    return {
        "available": sufficient,
        "personal_probability": posterior["mean"] if sufficient else None,
        "point_estimate_for_display": round(posterior["mean"] * 100, 1),
        "interval_90": posterior["interval_90"],
        "confidence": posterior["confidence"],
        "confidence_label": ("высокая" if posterior["confidence"] >= 0.7 else
                             "средняя" if posterior["confidence"] >= 0.4 else "низкая"),
        "n": segment["resolved"], "successes": segment["successes"],
        "failures": segment["failures"], "pending": segment["pending"],
        "scope": scope, "reason": reason, "method": "Beta-Binomial",
        "prior": list(PRIOR),
    }


def _direction_counts(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("""SELECT seniority, status, COUNT(*) n FROM application_outcomes
                         GROUP BY seniority, status""").fetchall()
    buckets: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for row in rows:
        buckets[(row["seniority"] or "unknown", row["status"])][row["status"]] += row["n"]
    out = []
    for seniority in ("intern", "junior", "middle", "senior", "unknown"):
        segment = _counts(db, "AND seniority=?", (seniority,))
        if not segment["total"]:
            continue
        est = estimate(db, {"seniority": seniority})
        out.append({"seniority": seniority, "label": seniority, "n": segment["resolved"],
                    "successes": segment["successes"], "failures": segment["failures"],
                    "pending": segment["pending"], "available": est["available"],
                    "mean": est["point_estimate_for_display"], "interval_90": est["interval_90"]})
    return out


def summary(db: sqlite3.Connection) -> dict:
    total = _counts(db)
    model = db.execute("""SELECT * FROM probability_models WHERE status='active'
                          ORDER BY id DESC LIMIT 1""").fetchone()
    last = db.execute("""SELECT * FROM probability_models ORDER BY id DESC LIMIT 1""").fetchone()
    by_direction = []
    for direction in ("ml", "cv", "analytics", "data_engineering", "backend", "frontend", "other"):
        segment = _counts(db, "AND direction=?", (direction,))
        if segment["total"]:
            by_direction.append({"direction": direction, "n": segment["resolved"],
                                 "successes": segment["successes"], "failures": segment["failures"],
                                 "pending": segment["pending"]})
    timeline = []
    for row in db.execute("""SELECT substr(COALESCE(sent_at,last_event_at,created_at),1,7) month,
                                    status, COUNT(*) n FROM application_outcomes
                             GROUP BY month, status ORDER BY month"""):
        timeline.append(dict(row))
    return {
        "total": total["total"], "sent": db.execute(
            "SELECT COUNT(*) FROM application_outcomes WHERE sent_at IS NOT NULL AND sent_at<>''").fetchone()[0],
        "counts": total["counts"], "successes": total["successes"],
        "failures": total["failures"], "resolved": total["resolved"],
        "pending": total["pending"], "unknown": total["counts"].get("unknown", 0),
        "by_seniority": _direction_counts(db), "by_direction": by_direction,
        "timeline": timeline,
        "last_trained": (last["trained_at"] if last else ""),
        "model": ({"version": model["version"], "kind": model["kind"],
                   "metrics": _safe_json(model["metrics"], {}),
                   "trained_at": model["trained_at"]} if model else
                  {"kind": "beta_prior", "version": "prior", "metrics": {}}),
        "ml_available": bool(model),
        "ml_reason": ("ML активен" if model else
                      "ML выключен: недостаточно подтверждённых исходов или кандидат не улучшил Brier."),
        "labels": STATUS_LABELS,
    }


def _features(row) -> dict:
    raw = _safe_json(row["feature_json"], {})
    return raw if isinstance(raw, dict) else {}


def _feature_vector(features: dict) -> dict:
    return {
        "skill_match": float(features.get("skill_match") or 0.0),
        "experience_gap": float(features.get("experience_gap_years") or 0.0),
        "career_fit": float(features.get("career_fit") or 0.0),
        "required_experience": float(features.get("required_experience") or 0.0),
        "freshness_days": float(features.get("freshness_days") or 0.0),
        "seniority_intern": 1.0 if features.get("seniority") == "intern" else 0.0,
        "seniority_junior": 1.0 if features.get("seniority") == "junior" else 0.0,
        "seniority_middle": 1.0 if features.get("seniority") == "middle" else 0.0,
        "seniority_senior": 1.0 if features.get("seniority") == "senior" else 0.0,
        "direction_ml": 1.0 if features.get("direction") in ("ml", "cv") else 0.0,
        "direction_data": 1.0 if features.get("direction") in ("analytics", "data_engineering") else 0.0,
        "direction_backend": 1.0 if features.get("direction") == "backend" else 0.0,
        "mode_remote": 1.0 if features.get("work_mode") == "remote" else 0.0,
        "mode_hybrid": 1.0 if features.get("work_mode") == "hybrid" else 0.0,
        "moscow": 1.0 if "Москв" in str(features.get("location") or "") else 0.0,
        "education_required": 1.0 if features.get("education_required") else 0.0,
        "internship": 1.0 if features.get("vacancy_type") == "internship" else 0.0,
    }


FEATURE_NAMES = list(_feature_vector({}).keys())


def _metrics(y_true, y_pred, y_prob) -> dict:
    n = len(y_true)
    if not n:
        return {}
    brier = sum((p - y) ** 2 for p, y in zip(y_prob, y_true)) / n
    eps = 1e-9
    logloss = -sum(y * math.log(max(eps, p)) + (1 - y) * math.log(max(eps, 1 - p))
                   for p, y in zip(y_prob, y_true)) / n
    try:
        from sklearn.metrics import average_precision_score
        pr_auc = float(average_precision_score(y_true, y_prob)) if sum(y_true) else None
    except Exception:
        pr_auc = None
    bins = []
    for low in (0, .2, .4, .6, .8):
        idx = [i for i, p in enumerate(y_prob) if low <= p < low + .2 or (low == .8 and p >= .8)]
        if idx:
            bins.append({"range": f"{int(low*100)}-{int(low*100+20)}%",
                         "n": len(idx), "predicted": round(sum(y_prob[i] for i in idx) / len(idx), 3),
                         "observed": round(sum(y_true[i] for i in idx) / len(idx), 3)})
    return {"n": n, "brier": round(brier, 4), "log_loss": round(logloss, 4),
            "pr_auc": round(pr_auc, 4) if pr_auc is not None else None,
            "positives": int(sum(y_true)), "calibration": bins}


def _load_model() -> dict:
    if not MODEL_PATH.exists():
        return {}
    try:
        return json.loads(MODEL_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def predict_ml(features: dict) -> dict:
    model = _load_model()
    if not model or not features:
        return {"available": False, "reason": "Активная ML-модель отсутствует"}
    names = model.get("features") or FEATURE_NAMES
    vector = _feature_vector(features)
    z = float(model.get("intercept") or 0.0)
    for name, coef in zip(names, model.get("coefficients") or []):
        z += float(coef) * float(vector.get(name, 0.0))
    probability = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
    test = (model.get("metrics") or {}).get("test") or {}
    return {"available": True, "probability": round(probability, 4),
            "version": model.get("version"), "n": test.get("n"),
            "brier": test.get("brier"), "log_loss": test.get("log_loss"),
            "method": "LogisticRegression (chronological holdout)"}


def train(db: sqlite3.Connection, *, force: bool = False) -> dict:
    rows = db.execute("""SELECT * FROM application_outcomes
                         WHERE status IN ('invited','offer','rejected')
                         ORDER BY sent_at, id""").fetchall()
    usable = []
    for row in rows:
        features = _features(row)
        if features.get("at_submission") is not True:
            continue
        sent = row["sent_at"] or ""
        captured = features.get("captured_at") or ""
        if sent and captured and captured[:10] > sent[:10]:
            continue
        usable.append((row, features))
    positives = sum(1 for row, _ in usable if row["status"] in RESOLVED_POSITIVE)
    negatives = len(usable) - positives
    reason = ""
    if len(usable) < MIN_ML_N:
        reason = f"ML выключен: подтверждённых наблюдений {len(usable)} < {MIN_ML_N}."
    elif positives < MIN_ML_POS or negatives < MIN_ML_NEG:
        reason = f"ML выключен: приглашений {positives}, отказов {negatives} (нужно ≥{MIN_ML_POS} и ≥{MIN_ML_NEG})."
    if reason:
        return {"trained": False, "reason": reason, "n": len(usable),
                "positives": positives, "negatives": negatives}
    split = max(1, int(len(usable) * 0.7))
    train_rows, test_rows = usable[:split], usable[split:]
    if not test_rows:
        return {"trained": False, "reason": "Нет контрольной выборки", "n": len(usable)}
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import average_precision_score
    except ImportError as exc:
        return {"trained": False, "reason": f"scikit-learn/numpy недоступны: {exc}"}
    def matrix(items):
        return np.array([list(_feature_vector(f).values()) for _, f in items], dtype=float)
    X_train, y_train = matrix(train_rows), np.array([1 if r["status"] in RESOLVED_POSITIVE else 0
                                                     for r, _ in train_rows])
    X_test, y_test = matrix(test_rows), np.array([1 if r["status"] in RESOLVED_POSITIVE else 0
                                                  for r, _ in test_rows])
    if len(set(y_test.tolist())) < 2 or len(set(y_train.tolist())) < 2:
        return {"trained": False, "reason": "В обучающей или контрольной выборке только один класс",
                "n": len(usable), "positives": positives, "negatives": negatives}
    model = LogisticRegression(C=0.5, class_weight="balanced", max_iter=1000, random_state=42)
    model.fit(X_train, y_train)
    prob = model.predict_proba(X_test)[:, 1]
    base_rate = float(y_train.mean())
    base_prob = [base_rate] * len(y_test)
    metrics = _metrics(y_test.tolist(), model.predict(X_test).tolist(), prob.tolist())
    baseline = _metrics(y_test.tolist(), [1 if base_rate >= .5 else 0] * len(y_test),
                        base_prob)
    improved = metrics["brier"] < baseline["brier"] - 0.005
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    active = db.execute("SELECT * FROM probability_models WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
    active_brier = float(_safe_json(active["metrics"], {}).get("brier", 1.0)) if active else 1.0
    activate = improved and (active is None or metrics["brier"] < active_brier - 0.005)
    params = {"C": 0.5, "class_weight": "balanced", "features": FEATURE_NAMES,
              "prior": list(PRIOR), "split": "chronological 70/30",
              "coefficients": model.coef_[0].tolist(), "intercept": float(model.intercept_[0])}
    db.execute("""INSERT INTO probability_models(version,kind,status,trained_at,data_from,data_to,
                  n,positives,negatives,params,metrics,reason)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
               (version, "logistic_regression", "active" if activate else "candidate",
                _now(), usable[0][0]["sent_at"], usable[-1][0]["sent_at"], len(usable),
                positives, negatives, json.dumps(params, ensure_ascii=False),
                json.dumps({"train": metrics, "baseline": baseline}, ensure_ascii=False),
                "Активна" if activate else "Не лучше базовой статистики/предыдущей модели"))
    if activate:
        db.execute("UPDATE probability_models SET status='superseded' WHERE status='active' AND version<>?",
                   (version,))
        ensure_dirs()
        MODEL_PATH.write_text(json.dumps({
            "version": version, "kind": "logistic_regression",
            "features": FEATURE_NAMES, "coefficients": params["coefficients"],
            "intercept": params["intercept"], "metrics": {"test": metrics, "baseline": baseline},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    db.commit()
    return {"trained": True, "active": activate, "version": version, "n": len(usable),
            "positives": positives, "negatives": negatives,
            "metrics": metrics, "baseline": baseline, "reason": "Активна" if activate else
            "Кандидат не улучшил Brier на контрольной выборке"}


def maybe_retrain(db: sqlite3.Connection) -> dict:
    last = db.execute("""SELECT * FROM probability_models ORDER BY id DESC LIMIT 1""").fetchone()
    resolved = db.execute("""SELECT COUNT(*) FROM application_outcomes
                             WHERE status IN ('invited','offer','rejected')""").fetchone()[0]
    if last and int(last["n"] or 0) == resolved:
        return {"trained": False, "reason": "Нет новых подтверждённых исходов"}
    return train(db)


def quality(db: sqlite3.Connection) -> dict:
    active = db.execute("SELECT * FROM probability_models WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
    last = db.execute("SELECT * FROM probability_models ORDER BY id DESC LIMIT 1").fetchone()
    def model_dict(row):
        if not row:
            return None
        data = dict(row)
        data["metrics"] = _safe_json(data.get("metrics"), {})
        data["params"] = _safe_json(data.get("params"), {})
        return data
    return {
        "trained": bool(last),
        "active": model_dict(active),
        "last": model_dict(last),
        "last_trained": (last["trained_at"] if last else ""),
        "min_ml": {"n": MIN_ML_N, "positives": MIN_ML_POS, "negatives": MIN_ML_NEG},
    }
