"""Company leads: small startups, AI teams and paid/unpaid opportunities.

A lead is not a vacancy. It stores exactly what a public source published:
hiring text, payment terms, contact and evidence. Startup Fit, Hiring Evidence
and Compensation Confidence are separate expert estimates, never probabilities.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db as dbm
from .readiness import LABELS as READINESS_LABELS
from .settings import load_profile
from .text import clean_text, detect_work_mode

KIND_LABELS = {
    "verified_vacancy": "Вакансия с оплатой",
    "paid_internship": "Оплачиваемая стажировка",
    "paid_project": "Оплачиваемый проект",
    "company_hiring": "Набирают команду",
    "potential_employer": "Потенциальный работодатель",
    "unpaid_opportunity": "Без оплаты",
    "cofounder_search": "Поиск сооснователя",
    "unknown": "Условия неизвестны",
}
PAY_LABELS = {
    "paid": "Оплата подтверждена",
    "unpaid": "Оплата не предусмотрена",
    "equity": "Доля / партнёрство",
    "unknown": "Оплата не подтверждена",
}
STATE_LABELS = {
    "discovered": "Обнаружено",
    "researching": "Изучается",
    "qualified": "Подходит",
    "draft_ready": "Черновик готов",
    "awaiting_approval": "Ждёт подтверждения",
    "contacted": "Отправлено",
    "replied": "Получен ответ",
    "interview": "Интервью",
    "offer": "Оффер",
    "rejected": "Отказ",
}
PRIORITY = {
    "verified_vacancy": 4.0, "paid_internship": 3.6, "paid_project": 3.2,
    "company_hiring": 2.5, "potential_employer": 2.0, "unknown": 1.6,
    "cofounder_search": 1.2, "unpaid_opportunity": 0.8,
}

_GROUP_NEEDLES = {
    "Python": ("python", "питон"),
    "FastAPI": ("fastapi",),
    "SQL": ("sql", "postgres", "clickhouse", "duckdb"),
    "Data": ("данн", "data", "pandas", "аналит", "analytics", "bi"),
    "ML": ("ml", "machine learning", "машинн", "scikit", "модел"),
    "LLM": ("llm", "gpt", "языков", "chatgpt", "prompt"),
    "RAG": ("rag", "retrieval", "вектор", "embedding"),
    "AI": (" ai", "ии", "искусствен", "нейросет"),
    "Backend": ("backend", "бэкенд", "сервер", "api"),
    "Docker": ("docker", "контейнер"),
    "Computer Vision": ("vision", " cv", "компьютерное зрение"),
    "Автоматизация": ("автоматизац", "бот", "парсер", "интеграц"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_company(name: str) -> str:
    low = clean_text(name or "").casefold()
    low = re.sub(r"[«»\"'()]", " ", low)
    low = re.sub(r"\b(ооо|оао|зао|пао|ао|ип|llc|ltd|inc)\b", " ", low)
    low = re.sub(r"[^a-zа-я0-9]+", " ", low)
    return re.sub(r"\s+", " ", low).strip()


def lead_id(source: str, company: str, source_url: str) -> str:
    key = f"{source}:{canonical_company(company)}:{source_url}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]


def _candidate_skills(profile: dict, resume: dict | None = None) -> list[str]:
    names = [str(x) for x in (profile.get("verified_skills") or [])]
    for item in (resume or {}).get("skills") or []:
        if isinstance(item, dict) and item.get("verified") is not False:
            names.append(str(item.get("name") or ""))
    for project in (resume or {}).get("projects") or []:
        if isinstance(project, dict):
            names.extend(str(x) for x in project.get("stack") or [])
    seen, out = set(), []
    for name in names:
        key = name.casefold().strip()
        if key and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def _groups(text: str) -> list[str]:
    low = f" {text.casefold()} "
    if "python" in low or "питон" in low:
        low += " python"
    return [name for name, needles in _GROUP_NEEDLES.items()
            if any(needle in low for needle in needles)]


def _age_days(value: str) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - dt).days)


def evaluate(lead: dict, profile: dict, resume: dict) -> dict:
    """Separate fit, hiring evidence and pay confidence; never call them odds."""
    candidate = _candidate_skills(profile, resume)
    blob = " ".join([lead.get("role", ""), lead.get("description", ""),
                     lead.get("hiring_status", ""), " ".join(lead.get("stack") or []),
                     lead.get("product", "")])
    job_groups = _groups(blob)
    own_groups = _groups(" ".join(candidate) + " FastAPI SQL Pandas")
    matched = [g for g in job_groups if g in own_groups]
    missing = [g for g in job_groups if g not in own_groups]

    tech = min(50.0, 9.0 * len(matched) + (6.0 if "AI" in matched or "LLM" in matched else 0))
    text_low = f"{lead.get('role','')} {lead.get('hiring_status','')} {lead.get('description','')}".casefold()
    role_text = f"{lead.get('product','')} {lead.get('hiring_status','')}".casefold()
    cap = 100.0
    non_tech = bool(re.search(r"продакт|product manager|маркет|продаж|дизайн|редактор|"
                              r"контент|smm|\bhr\b|рекрут|юрист|финанс|бухгалт", role_text))
    tech_role = bool(re.search(r"python|backend|бэкенд|data|аналит|\bml\b|\bai\b|llm|rag|"
                               r"разработ|инженер|developer|engineer|mlops|devops", role_text))
    if non_tech and not tech_role:
        tech = max(0.0, tech - 18.0)
        cap = 45.0
    other_stack = bool(re.search(r"c#|\.net|php|java\b|kotlin|swift|1c|битрикс|"
                                 r"android|ios|flutter|react|angular|vue", role_text))
    python_focus = bool(re.search(r"python|data|\bml\b|\bai\b|llm|rag|fastapi|аналит", role_text))
    if other_stack and not python_focus:
        tech = max(0.0, tech * 0.55 - 6.0)
        cap = min(cap, 55.0)
    role_fit = 0.0
    if re.search(r"стаж|интерн|intern|junior|джуниор|начинающ", text_low):
        role_fit += 12
    if re.search(r"part-?time|частичн|гибк|подработ", text_low):
        role_fit += 4
    if re.search(r"senior|ведущ|lead|руководит|cto|10\+ лет", text_low):
        role_fit -= 8
    role_fit = max(0.0, min(15.0, role_fit))

    mode = lead.get("work_mode") or ""
    location = lead.get("location") or ""
    schedule = 0.0
    if mode in ("remote", "hybrid"):
        schedule += 8
    elif mode == "office":
        schedule += 6 if "Москв" in location else 2
    else:
        schedule += 4
    if "Москв" in location or not location:
        schedule += 4
    if re.search(r"очн\w* обучен|полный день|40 час", text_low) and mode == "office":
        schedule -= 3
    schedule = max(0.0, min(15.0, schedule))

    age = _age_days(lead.get("published_at") or lead.get("last_checked_at") or "")
    freshness = 10.0 if age is not None and age <= 30 else \
        7.0 if age is not None and age <= 90 else 3.0

    contact = 10.0 if lead.get("contact_kind") in ("telegram", "email") else \
        6.0 if lead.get("contact") else 0.0
    fit = round(min(cap, tech + role_fit + schedule + freshness + contact), 1)

    kind = lead.get("kind", "unknown")
    hiring = {"verified_vacancy": 80.0, "paid_internship": 75.0, "paid_project": 68.0,
              "company_hiring": 62.0, "unpaid_opportunity": 55.0,
              "cofounder_search": 42.0, "potential_employer": 15.0, "unknown": 20.0}[kind]
    if not lead.get("evidence_url"):
        hiring -= 15
    if age is not None and age > 60:
        hiring -= 20
    elif age is not None and age > 30:
        hiring -= 8
    hiring = max(0.0, min(100.0, hiring))

    pay = lead.get("pay_status", "unknown")
    comp = {"paid": 72.0, "unpaid": 95.0, "equity": 45.0, "unknown": 12.0}[pay]
    if pay == "paid" and re.search(r"\d[\d\s]{2,}\s*(?:₽|руб)|зарплат|оклад", text_low):
        comp = 88.0
    if lead.get("contact_kind") in ("telegram", "email"):
        comp = min(100.0, comp + 4)

    rationale = {
        "facts": [t for t in [lead.get("hiring_status"), lead.get("evidence_text")] if t][:2],
        "matches": matched,
        "gaps": missing,
        "assumptions": (["Технологии определены по публичному описанию продукта, а не по внутреннему стеку."]
                        if lead.get("stack") else []),
        "notes": [f"Оплата: {PAY_LABELS.get(pay, pay)}"],
    }
    return {"fit_score": fit, "hiring_evidence": round(hiring, 1),
            "compensation_confidence": round(comp, 1), "rationale": rationale,
            "matched": matched, "missing": missing}


def _merge(existing: sqlite3.Row, incoming: dict) -> dict:
    detail = json.loads(existing["detail"] or "{}")
    sources = {s.get("url"): s for s in detail.get("other_sources") or [] if isinstance(s, dict)}
    sources[existing["source_url"]] = {"source": existing["source"], "url": existing["source_url"]}
    sources[incoming["source_url"]] = {"source": incoming["source"], "url": incoming["source_url"]}
    detail["other_sources"] = list(sources.values())[:10]
    detail["kinds_seen"] = sorted(set(detail.get("kinds_seen") or []) | {incoming["kind"]})
    return detail


def save_leads(db: sqlite3.Connection, leads: list[dict], *, profile: dict | None = None,
               resume: dict | None = None) -> dict:
    profile = profile or load_profile()
    from .resume import load_resume, sync_from_profile
    resume = resume or sync_from_profile(load_resume())
    added = updated = skipped = 0
    for lead in leads:
        company_key = canonical_company(lead.get("company") or "")
        if company_key in ("", "без названия", "компания не указана"):
            skipped += 1
            continue
        existing = db.execute(
            "SELECT * FROM company_leads WHERE source_url=? LIMIT 1",
            (lead.get("source_url"),)).fetchone()
        scores = evaluate(lead, profile, resume)
        detail = json.loads(existing["detail"] or "{}") if existing else {}
        if existing:
            detail = _merge(existing, lead)
            merged_kinds = detail.get("kinds_seen") or []
            best_kind = max(merged_kinds, key=lambda k: PRIORITY.get(k, 0)) if merged_kinds else lead["kind"]
            values = {
                "kind": best_kind,
                "website": lead.get("website") or existing["website"],
                "description": lead.get("description") or existing["description"],
                "stack": json.dumps(sorted(set(json.loads(existing["stack"] or "[]")) |
                                           set(lead.get("stack") or [])), ensure_ascii=False),
                "contact": lead.get("contact") or existing["contact"],
                "contact_kind": lead.get("contact_kind") or existing["contact_kind"],
                "evidence_url": lead.get("evidence_url") or existing["evidence_url"],
                "evidence_text": lead.get("evidence_text") or existing["evidence_text"],
                "hiring_status": lead.get("hiring_status") or existing["hiring_status"],
                "pay_status": _best_pay(existing["pay_status"], lead.get("pay_status")),
                "work_mode": lead.get("work_mode") or existing["work_mode"],
                "location": lead.get("location") or existing["location"],
                "role": lead.get("role") or existing["role"],
                "fit_score": max(existing["fit_score"] or 0, scores["fit_score"]),
                "hiring_evidence": scores["hiring_evidence"],
                "compensation_confidence": scores["compensation_confidence"],
                "rationale": json.dumps(scores["rationale"], ensure_ascii=False),
                "last_checked_at": _now(),
                "updated_at": dbm.now(),
                "detail": json.dumps(detail, ensure_ascii=False),
            }
            if not lead.get("published_at") and existing["published_at"]:
                values["published_at"] = existing["published_at"]
            elif lead.get("published_at"):
                values["published_at"] = lead["published_at"]
            if scores["fit_score"] > (existing["fit_score"] or 0) + 0.5:
                values.update({
                    "source": lead["source"], "source_url": lead["source_url"],
                    "description": lead.get("description") or existing["description"],
                    "role": lead.get("role") or existing["role"],
                    "hiring_status": lead.get("hiring_status") or existing["hiring_status"],
                    "evidence_url": lead.get("evidence_url") or existing["evidence_url"],
                    "evidence_text": lead.get("evidence_text") or existing["evidence_text"],
                    "product": lead.get("product") or existing["product"],
                })
            assignments = ", ".join(f"{key}=?" for key in values)
            db.execute(f"UPDATE company_leads SET {assignments} WHERE id=?",
                       (*values.values(), existing["id"]))
            updated += 1
            continue
        ident = lead_id(lead["source"], lead["company"], lead["source_url"])
        detail = {"other_sources": [{"source": lead["source"], "url": lead["source_url"]}],
                  "kinds_seen": [lead["kind"]], "company_key": canonical_company(lead["company"]),
                  "founders": lead.get("founders") or [], "badges": lead.get("badges") or []}
        db.execute(
            """INSERT INTO company_leads(
                 id,kind,status,company,product,website,description,stack,contact,contact_kind,
                 source,source_url,evidence_url,evidence_text,hiring_status,pay_status,work_mode,
                 location,role,fit_score,hiring_evidence,compensation_confidence,rationale,
                 first_seen_at,last_checked_at,published_at,created_at,updated_at,detail)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ident, lead["kind"], "discovered" if lead["kind"] == "potential_employer" else "qualified",
             lead["company"], lead.get("product") or lead["company"], lead.get("website", ""),
             lead.get("description", ""), json.dumps(lead.get("stack") or [], ensure_ascii=False),
             lead.get("contact", ""), lead.get("contact_kind", ""), lead["source"],
             lead["source_url"], lead.get("evidence_url", ""), lead.get("evidence_text", ""),
             lead.get("hiring_status", ""), lead.get("pay_status", "unknown"),
             lead.get("work_mode", ""), lead.get("location", ""), lead.get("role", ""),
             scores["fit_score"], scores["hiring_evidence"], scores["compensation_confidence"],
             json.dumps(scores["rationale"], ensure_ascii=False), _now(), _now(),
             lead.get("published_at", ""), dbm.now(), dbm.now(),
             json.dumps(detail, ensure_ascii=False)))
        added += 1
    db.commit()
    return {"added": added, "updated": updated, "skipped": skipped}


def _best_pay(current: str, incoming: str) -> str:
    rank = {"paid": 4, "unpaid": 3, "equity": 2, "unknown": 1}
    return current if rank.get(current, 0) >= rank.get(incoming, 0) else incoming


def list_leads(db: sqlite3.Connection, *, kinds: list[str] | None = None,
               paid_only: bool = False, query: str = "", limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM company_leads WHERE 1=1"
    params: list = []
    if kinds:
        sql += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
        params.extend(kinds)
    if paid_only:
        sql += " AND pay_status='paid'"
    if query:
        sql += " AND (company LIKE ? OR product LIKE ? OR description LIKE ?)"
        like = f"%{query}%"
        params.extend([like, like, like])
    sql += " ORDER BY fit_score DESC LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in db.execute(sql, params)]
    return sorted(rows, key=lambda r: (PRIORITY.get(r["kind"], 0),
                                       r["fit_score"], r["hiring_evidence"]), reverse=True)


def get(db: sqlite3.Connection, ident: str) -> dict | None:
    row = db.execute("SELECT * FROM company_leads WHERE id=?", (ident,)).fetchone()
    return dict(row) if row else None


def payload(row: dict) -> dict:
    detail = json.loads(row.get("detail") or "{}")
    rationale = json.loads(row.get("rationale") or "{}")
    return {
        "id": row["id"], "kind": row["kind"], "kind_label": KIND_LABELS.get(row["kind"], row["kind"]),
        "status": row["status"], "state_label": STATE_LABELS.get(row["status"], row["status"]),
        "company": row["company"], "product": row["product"], "website": row["website"],
        "description": row["description"], "stack": json.loads(row["stack"] or "[]"),
        "contact": row["contact"], "contact_kind": row["contact_kind"],
        "source": row["source"], "source_url": row["source_url"],
        "evidence_url": row["evidence_url"], "evidence_text": row["evidence_text"],
        "hiring_status": row["hiring_status"], "pay_status": row["pay_status"],
        "pay_label": PAY_LABELS.get(row["pay_status"], row["pay_status"]),
        "work_mode": row["work_mode"], "location": row["location"], "role": row["role"],
        "fit_score": row["fit_score"], "hiring_evidence": row["hiring_evidence"],
        "compensation_confidence": row["compensation_confidence"],
        "rationale": rationale, "outreach": row["outreach"],
        "outreach_subject": row["outreach_subject"],
        "outreach_updated_at": row["outreach_updated_at"] or "",
        "readiness": row.get("readiness") or "",
        "readiness_label": READINESS_LABELS.get(row.get("readiness") or "", ""),
        "readiness_reason": row.get("readiness_reason") or "",
        "published_at": row.get("published_at") or "",
        "last_checked_at": row["last_checked_at"] or "",
        "other_sources": detail.get("other_sources") or [],
        "founders": detail.get("founders") or [], "badges": detail.get("badges") or [],
        "is_paid": row["pay_status"] == "paid",
    }


def stats(db: sqlite3.Connection) -> dict:
    rows = db.execute("SELECT kind, COUNT(*) n FROM company_leads GROUP BY kind").fetchall()
    by_kind = {r["kind"]: r["n"] for r in rows}
    by_state = {r["state"]: r["n"] for r in
                db.execute("SELECT status state, COUNT(*) n FROM company_leads GROUP BY status")}
    return {
        "total": sum(by_kind.values()), "by_kind": by_kind, "by_state": by_state,
        "paid": db.execute("SELECT COUNT(*) FROM company_leads WHERE pay_status='paid'").fetchone()[0],
        "unpaid": by_kind.get("unpaid_opportunity", 0),
        "hiring": db.execute(
            """SELECT COUNT(*) FROM company_leads WHERE kind IN
               ('verified_vacancy','paid_internship','paid_project','company_hiring')""").fetchone()[0],
        "drafts": db.execute(
            "SELECT COUNT(*) FROM company_leads WHERE outreach <> ''").fetchone()[0],
        "awaiting": by_state.get("draft_ready", 0) + by_state.get("awaiting_approval", 0),
        "contacted": by_state.get("contacted", 0),
        "sources": {r["source"]: r["n"] for r in
                    db.execute("SELECT source, COUNT(*) n FROM company_leads GROUP BY source")},
        "last_checked": db.execute("SELECT MAX(last_checked_at) FROM company_leads").fetchone()[0] or "",
    }


def _short(text: str, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", clean_text(text or "")).strip()
    return (text[:limit].rstrip() + "…") if len(text) > limit else text


def prepare_draft(db: sqlite3.Connection, ident: str, *, profile: dict | None = None,
                  resume: dict | None = None) -> dict:
    from .resume import load_resume, sync_from_profile
    profile = profile or load_profile()
    resume = resume or sync_from_profile(load_resume())
    row = db.execute("SELECT * FROM company_leads WHERE id=?", (ident,)).fetchone()
    if not row:
        return {"error": "Компания не найдена"}
    lead = dict(row)
    if lead["status"] in ("contacted", "replied", "interview", "offer"):
        return {"error": "Обращение уже отправлено — повторное сообщение не готовится автоматически",
                "warning": True, "outreach": lead["outreach"]}
    other = db.execute(
        """SELECT company, product, status FROM company_leads
           WHERE id<>? AND lower(company)=lower(?) AND
                 (outreach<>'' OR status IN ('contacted','replied','interview','offer'))""",
        (ident, lead["company"])).fetchall()
    duplicate_warning = ""
    if other:
        duplicate_warning = (f"Внимание: по компании {lead['company']} уже есть "
                             f"{len(other)} подготовленное обращение(й). Не отправляйте дубль.")
    candidate = _candidate_skills(profile, resume)
    scores = evaluate(lead, profile, resume)
    matched = scores["matched"]
    skills = ", ".join(candidate[:9])
    projects = [p for p in (resume.get("projects") or []) if isinstance(p, dict)]
    project_line = ""
    if projects:
        project = projects[0]
        project_line = (f"Из практики: проект «{project.get('name')}» "
                        f"({', '.join(project.get('stack') or [])}).")
    company = lead["company"]
    product = lead["product"] or company
    facts = _short(re.sub(r"^[^:]{1,40}:\s*", "", lead["hiring_status"] or lead["description"]), 240)
    mode = {"remote": "удалённый формат", "hybrid": "гибридный формат",
            "office": "офисный формат"}.get(lead["work_mode"], "формат обсуждаем")
    focus = ", ".join(matched[:4]) if matched else "Python и работу с данными"
    pay = lead["pay_status"]
    if pay == "unpaid":
        ask = ("Вижу, что на старте оплата не предусмотрена. Готов обсудить формат, "
               "при котором участие будет оплачиваемым или вырастет до оплачиваемой роли.")
    elif pay == "paid":
        ask = "Готов обсудить оплачиваемую стажировку или частичную занятость."
    elif pay == "equity":
        ask = "Готов обсудить формат сотрудничества и условия вознаграждения."
    else:
        ask = ("Уточните, пожалуйста, предусмотрена ли оплата и какой формат занятости "
               "возможен для студента 2 курса.")
    subject = f"{focus} — помощь команде {product}"
    body = (
        f"Здравствуйте!\n\n"
        f"Посмотрел {product}: {_short(lead['description'] or lead['hiring_status'], 220)}\n\n"
        f"Я начинающий Python/Data-специалист, студент 2 курса (очное обучение). "
        f"Подтверждённые навыки: {skills}. {project_line}\n\n"
        f"В вашем объявлении: «{facts}». Могу быть полезен в направлении {focus}: "
        f"разбирать данные, писать Python-скрипты и API, готовить прототипы и автоматизацию.\n\n"
        f"Коммерческого опыта пока нет — не выдаю учебные проекты за рабочую практику. "
        f"Рассматриваю {mode}, частичную занятость и стажировку. {ask}\n\n"
        f"Если направление актуально, подскажите, с кем можно обсудить детали.\n"
    ).strip()
    status = "draft_ready" if lead["status"] in ("discovered", "qualified", "researching") else lead["status"]
    db.execute("""UPDATE company_leads SET outreach=?, outreach_subject=?, outreach_updated_at=?,
                  status=?, updated_at=? WHERE id=?""",
               (body, subject, _now(), status, dbm.now(), ident))
    dbm.log_event(db, None, "company_outreach_drafted",
                  payload={"lead_id": ident, "company": company, "kind": lead["kind"]})
    db.commit()
    return {"ok": True, "outreach": body, "subject": subject, "status": status,
            "fit_score": scores["fit_score"], "rationale": scores["rationale"],
            "warning": duplicate_warning}


def set_state(db: sqlite3.Connection, ident: str, state: str, note: str = "") -> dict:
    if state not in STATE_LABELS:
        return {"error": "Неизвестное состояние"}
    row = db.execute("SELECT * FROM company_leads WHERE id=?", (ident,)).fetchone()
    if not row:
        return {"error": "Компания не найдена"}
    if state == "contacted" and not (row["outreach"] or "").strip():
        return {"error": "Сначала подготовьте обращение"}
    db.execute("UPDATE company_leads SET status=?, outreach_updated_at=?, updated_at=? WHERE id=?",
               (state, _now() if state == "contacted" else row["outreach_updated_at"], dbm.now(), ident))
    dbm.log_event(db, None, "company_lead_state",
                  payload={"lead_id": ident, "company": row["company"], "state": state,
                           "verification": "user_attestation" if state == "contacted" else "",
                           "note": note[:300]})
    db.commit()
    return {"ok": True, "status": state, "state_label": STATE_LABELS[state]}


def save_note(db: sqlite3.Connection, ident: str, note: str) -> dict:
    row = db.execute("SELECT * FROM company_leads WHERE id=?", (ident,)).fetchone()
    if not row:
        return {"error": "Компания не найдена"}
    detail = json.loads(row["detail"] or "{}")
    notes = detail.get("notes") or []
    notes.append({"at": _now(), "text": note[:1000]})
    detail["notes"] = notes[-30:]
    db.execute("UPDATE company_leads SET detail=?, updated_at=? WHERE id=?",
               (json.dumps(detail, ensure_ascii=False), dbm.now(), ident))
    db.commit()
    return {"ok": True, "notes": detail["notes"]}


def rescore(db: sqlite3.Connection, *, profile: dict | None = None,
            resume: dict | None = None) -> dict:
    """Re-evaluate saved leads after scoring rules change; no network requests."""
    from .resume import load_resume, sync_from_profile
    profile = profile or load_profile()
    resume = resume or sync_from_profile(load_resume())
    rows = db.execute("SELECT * FROM company_leads").fetchall()
    for row in rows:
        lead = dict(row)
        scores = evaluate(lead, profile, resume)
        db.execute("""UPDATE company_leads SET fit_score=?, hiring_evidence=?,
                      compensation_confidence=?, rationale=?, updated_at=? WHERE id=?""",
                   (scores["fit_score"], scores["hiring_evidence"],
                    scores["compensation_confidence"],
                    json.dumps(scores["rationale"], ensure_ascii=False),
                    dbm.now(), lead["id"]))
    db.commit()
    return {"rescored": len(rows)}


def prune_irrelevant(db: sqlite3.Connection) -> int:
    """Drop non-technical posts that slipped through earlier filters.

    Only untouched leads are removed; drafts and contacted leads are kept.
    """
    pattern = re.compile(r"продаж|sales|маркет|smm|дизайн|\bhr\b|рекрут|продюсер|"
                         r"контент|юрист|финанс|бухгалт|product manager|продакт|"
                         r"бизнес-партн|партнёра|развитию бизнеса|business development", re.I)
    tech = re.compile(r"python|backend|data|\bml\b|\bai\b|llm|rag|разработ|"
                      r"инженер|developer|engineer|devops|mlops|аналит", re.I)
    removed = 0
    rows = db.execute("""SELECT id, hiring_status, status, outreach FROM company_leads
                         WHERE status IN ('discovered','qualified') AND outreach=''""").fetchall()
    for row in rows:
        title = re.sub(r"^Опубликовано на StartupFellows:\s*", "", row["hiring_status"] or "")
        if pattern.search(title) and not tech.search(title):
            db.execute("DELETE FROM company_leads WHERE id=?", (row["id"],))
            removed += 1
    db.commit()
    return removed


def refresh(db: sqlite3.Connection, settings: dict | None = None, *,
            force: bool = False) -> dict:
    from .sources import productradar, startupfellows
    cfg = (settings or {}).get("startups") or {}
    max_details = max(1, min(80, int(cfg.get("max_details", 40))))
    max_pages = max(1, min(8, int(cfg.get("max_pages", 4))))
    report: dict = {"sources": {}, "errors": []}
    all_leads: list[dict] = []
    for module, kwargs in ((startupfellows, {"max_pages": max_pages}),
                           (productradar, {})):
        try:
            result = module.collect(db, max_details=max_details, force=force,
                                    settings=settings, **kwargs)
            report["sources"][result["source"]] = {k: v for k, v in result.items()
                                                   if k not in ("leads", "errors")}
            report["errors"].extend(result.get("errors") or [])
            all_leads.extend(result.get("leads") or [])
        except Exception as exc:
            report["errors"].append(f"{module.__name__}: {type(exc).__name__}: {exc}")
            report["sources"][module.__name__.split(".")[-1]] = {"error": str(exc)[:300]}
    saved = save_leads(db, all_leads)
    pruned = prune_irrelevant(db)
    drafts = 0
    if cfg.get("prepare_drafts", True):
        limit = max(0, min(20, int(cfg.get("draft_limit", 6))))
        rows = list_leads(db, limit=40)
        for row in rows:
            if drafts >= limit:
                break
            if row["status"] not in ("discovered", "qualified"):
                continue
            if row["fit_score"] < float(cfg.get("draft_min_fit", 60)):
                continue
            if row["kind"] in ("potential_employer", "unknown", "unpaid_opportunity"):
                continue
            if not (row["contact"] or row["website"]):
                continue
            if prepare_draft(db, row["id"]).get("ok"):
                drafts += 1
    report.update(saved)
    report["pruned"] = pruned
    report["drafts"] = drafts
    report["stats"] = stats(db)
    db.execute("""UPDATE automation_state SET updated_at=? WHERE id=1""", (dbm.now(),))
    db.commit()
    return report
