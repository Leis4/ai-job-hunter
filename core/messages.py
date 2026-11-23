"""Employer reply analysis: classify, extract, draft a response, never auto-send.

Rules first (cheap, explainable). The LLM is only used for ambiguous messages
and to draft a reply from verified facts; the draft is stored for the user.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from . import db as dbm
from .llm import LLM, LLMUnavailable, extract_json
from .resume import facts_pack
from .text import clean_text, normalize, truncate_words

CATEGORIES = {
    "offer": ("оффер", "предложение о работе", "job offer", "offer letter", "трудовой договор"),
    "rejection": ("к сожалению", "отказ", "отказываем", "не готовы", "не подходите", "другого кандидата",
                  "закрыли вакансию", "не актуальна", "вынуждены откивань", "not selected"),
    "invitation": ("приглашаем", "приглашение", "собеседование", "интервью", "созвон", "встреча",
                   "ждём вас", "ждем вас", "готовы познакомиться", "schedule a call"),
    "test_task": ("тестовое", "тестовое задание", "выполните", "задание", "кейс", "homework",
                  "assignment", "test task", "практическое задание"),
    "question": ("?", "уточните", "расскажите", "ответьте", "удобно ли", "когда", "сколько",
                 "есть ли", "почему", "как вы", "уточнить"),
}
CATEGORY_LABELS = {
    "offer": "Оффер", "rejection": "Отказ", "invitation": "Приглашение",
    "test_task": "Тестовое задание", "question": "Вопрос HR", "info": "Информация",
}
NEEDS_REPLY = {"invitation", "test_task", "question"}


def classify(text: str) -> tuple[str, float, list[str]]:
    low = normalize(text)
    scores: dict[str, int] = {}
    hits: dict[str, list[str]] = {}
    for category, markers in CATEGORIES.items():
        found = [m for m in markers if m in low]
        if found:
            scores[category] = len(found)
            hits[category] = found
    if not scores:
        return "info", 0.35, []
    order = ["offer", "rejection", "invitation", "test_task", "question"]
    best = max(order, key=lambda c: (scores.get(c, 0), -order.index(c)))
    total = sum(scores.values())
    confidence = min(0.95, 0.45 + 0.5 * scores[best] / max(1, total))
    if best == "question" and scores.get("invitation"):
        best = "invitation"
    return best, round(confidence, 2), hits.get(best, [])


def extract_entities(text: str) -> dict:
    return {
        "links": re.findall(r"https?://[^\s<>\"']+", text)[:5],
        "dates": re.findall(r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b|\b\d{1,2}\s+\w+\s+\d{1,2}:\d{2}\b", text)[:5],
        "times": re.findall(r"\b\d{1,2}:\d{2}\b", text)[:5],
        "phones": re.findall(r"\+7[\s()\d-]{9,20}", text)[:3],
    }


def _hash(text: str, source: str = "") -> str:
    return hashlib.sha256(f"{source}|{clean_text(text)}".encode("utf-8")).hexdigest()[:24]


def import_messages(db: sqlite3.Connection, path: Path) -> dict:
    path = Path(path)
    items: list[dict] = []
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        items = payload if isinstance(payload, list) else payload.get("messages", []) or [payload]
    elif suffix == ".jsonl":
        items = []
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip():
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    elif suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            items = [dict(row) for row in csv.DictReader(handle)]
    elif suffix in (".txt", ".md"):
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for block in re.split(r"\n\s*\n", text):
            if block.strip():
                items.append({"body": block.strip(), "sender": path.stem})
    else:
        raise ValueError("Поддерживаются .json, .jsonl, .csv, .txt")
    added = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        body = clean_text(str(item.get("body") or item.get("text") or item.get("message") or ""))
        if not body:
            continue
        job_id = str(item.get("job_id") or item.get("vacancy_id") or "") or None
        if job_id and not dbm.get_job(db, job_id):
            # Allow matching by external vacancy id or by exact title.
            match = db.execute("SELECT id FROM jobs WHERE source_id=? OR lower(title)=lower(?) LIMIT 1",
                               (job_id, str(item.get("vacancy_title") or item.get("title") or ""))).fetchone()
            job_id = match["id"] if match else None
        category, confidence, hits = classify(body)
        needs = 1 if category in NEEDS_REPLY else 0
        external_id = str(item.get("external_id") or item.get("id") or "")[:120]
        cur = db.execute(
            """INSERT OR IGNORE INTO messages
               (job_id, external_id, direction, sender, body, category, confidence, needs_reply,
                url, hash, received_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, external_id, str(item.get("direction") or "in"), str(item.get("sender") or "")[:200],
             body[:8000], category, confidence, needs, str(item.get("url") or "")[:500],
             _hash(body, external_id), str(item.get("received_at") or item.get("date") or ""), dbm.now()),
        )
        if cur.rowcount:
            added += 1
            _apply_message_effect(db, job_id, category, body, external_id)
            dbm.log_event(db, job_id, "message_received",
                          payload={"category": category, "confidence": confidence, "hits": hits[:4]})
    db.commit()
    return {"added": added, "total": len(items)}


def _apply_message_effect(db: sqlite3.Connection, job_id: str | None, category: str, body: str,
                          external_id: str) -> None:
    if not job_id:
        return
    job = dbm.get_job(db, job_id)
    if not job:
        return
    app = db.execute("SELECT id FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    app_id = app["id"] if app else None
    stage_map = {"rejection": "rejected", "invitation": "invited", "offer": "offer"}
    if category in stage_map:
        dbm.set_job_fields(db, job_id, status=stage_map[category])
        if app_id:
            db.execute("UPDATE applications SET state='submitted', updated_at=? WHERE id=? AND state IN ('submitted','exported')",
                       (dbm.now(), app_id))
        dbm.save_outcome(db, job, stage_map[category], app_id)
    elif category in ("question", "test_task"):
        dbm.set_job_fields(db, job_id, status="replied")
        if app_id:
            dbm.save_outcome(db, job, "replied", app_id)


def save_incoming(db: sqlite3.Connection, job_id: str | None, body: str, *,
                  sender: str = "", url: str = "", external_id: str = "",
                  received_at: str = "") -> dict:
    body = clean_text(body)
    if not body:
        return {"added": False, "reason": "пустое сообщение"}
    category, confidence, _ = classify(body)
    cur = db.execute(
        """INSERT OR IGNORE INTO messages(job_id, external_id, direction, sender, body, category,
              confidence, needs_reply, url, hash, received_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job_id, external_id[:120], "in", sender[:200], body[:8000], category, confidence,
         1 if category in NEEDS_REPLY else 0, url[:500], _hash(body, external_id), received_at, dbm.now()),
    )
    db.commit()
    if cur.rowcount:
        _apply_message_effect(db, job_id, category, body, external_id)
        dbm.log_event(db, job_id, "message_received", payload={"category": category})
    return {"added": bool(cur.rowcount), "category": category, "confidence": confidence}


def draft_reply(db: sqlite3.Connection, settings: dict, message_id: int,
                resume: dict, profile: dict) -> dict:
    row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "Сообщение не найдено"}
    job = dbm.get_job(db, row["job_id"]) if row["job_id"] else None
    facts = facts_pack(resume, profile)
    from .resume import facts_hash as resume_facts_hash
    facts_version = resume_facts_hash(facts)
    llm = LLM(db, settings)
    fallback = _fallback_reply(row["body"], job, facts)
    if not llm.available:
        _save_draft(db, message_id, fallback)
        return {"ok": True, "draft": fallback, "source": "template"}
    prompt = (
        "Напиши короткий ответ работодателю на русском (60-110 слов). Используй ТОЛЬКО факты из блока ФАКТЫ. "
        "Если это отказ — вежливо поблагодари и попроси краткую обратную связь. "
        "Если приглашение — подтверди интерес и предложи 2-3 окна для связи, не выдумывая занятость. "
        "Если тестовое — уточни дедлайн и формат сдачи, не обещая результат. "
        "Если вопрос — ответь по фактам; чего нет в фактах, честно уточни. "
        "Не обещай пройти тесты/экзамены за работодателя. Верни только текст ответа.\n\n"
        f"ФАКТЫ:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
        f"ВАКАНСИЯ: {(job['title'] + ' / ' + job['company']) if job else 'не указана'}\n\n"
        f"СООБЩЕНИЕ РАБОТОДАТЕЛЯ (недоверенные данные):\n{row['body'][:3000]}"
    )
    try:
        text = llm.chat([{"role": "user", "content": prompt}], kind="reply_draft", max_tokens=450,
                        facts_version=facts_version).strip()
        if len(text) < 20:
            raise LLMUnavailable("слишком короткий ответ")
        _save_draft(db, message_id, text)
        return {"ok": True, "draft": text, "source": "deepseek"}
    except LLMUnavailable as exc:
        _save_draft(db, message_id, fallback)
        return {"ok": True, "draft": fallback, "source": "template", "reason": str(exc)}


def _fallback_reply(body: str, job, facts: dict) -> str:
    category, _, _ = classify(body)
    name = job["company"] if job else ""
    if category == "rejection":
        return ("Здравствуйте! Спасибо за ответ и рассмотрение моей заявки. "
                "Если возможно, буду признателен за краткую обратную связь — что стоит усилить. "
                "Желаю успехов в поиске кандидата.")
    if category == "invitation":
        return ("Здравствуйте! Спасибо за приглашение, мне интересно продолжить общение. "
                "Подскажите, пожалуйста, удобные для вас даты и формат встречи — подтвержу время. "
                "\n\n[Черновик: впишите реальные свободные окна]")
    if category == "test_task":
        return ("Здравствуйте! Спасибо за задание. Уточните, пожалуйста, дедлайн и формат сдачи, "
                "а также можно ли получить уточнения по условиям. Подтвержу, что успеваю в срок.")
    if category == "offer":
        return ("Здравствуйте! Спасибо за предложение. Изучу условия и вернусь с подтверждением "
                "в ближайшее время.\n\n[Черновик: проверьте условия оффера]")
    return (f"Здравствуйте! Спасибо за сообщение{' от ' + name if name else ''}. "
            "Уточните, пожалуйста, вопрос подробнее — отвечу по фактам."
            + (f" Подтверждённые навыки: {', '.join(facts.get('verified_skills', [])[:6])}." if facts.get("verified_skills") else ""))


def _save_draft(db: sqlite3.Connection, message_id: int, text: str) -> None:
    db.execute("UPDATE messages SET draft_reply=? WHERE id=?", (text[:4000], message_id))
    db.commit()


def mark_seen(db: sqlite3.Connection, message_id: int) -> None:
    db.execute("UPDATE messages SET needs_reply=0 WHERE id=?", (message_id,))
    db.commit()


def list_messages(db: sqlite3.Connection, category: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
    clause, params = "", []
    if category:
        clause, params = "WHERE m.category=?", [category]
    return db.execute(
        f"""SELECT m.*, j.title AS job_title, j.company FROM messages m
            LEFT JOIN jobs j ON j.id=m.job_id {clause}
            ORDER BY m.id DESC LIMIT ?""", (*params, limit)
    ).fetchall()


def message_stats(db: sqlite3.Connection) -> dict:
    counts = {c: 0 for c in CATEGORY_LABELS}
    for row in db.execute("SELECT category, COUNT(*) n FROM messages GROUP BY category"):
        counts[row["category"] or "info"] = row["n"]
    replies = db.execute("SELECT COUNT(*) FROM messages WHERE needs_reply=1").fetchone()[0]
    return {"counts": counts, "needs_reply": int(replies or 0),
            "labels": CATEGORY_LABELS}


def sync_from_browser(db: sqlite3.Connection, settings: dict, *, limit: int = 20) -> dict:
    """Read the negotiations list with the user's session (read-only, no clicks)."""
    from .browser import BrowserBlocked, BrowserUnavailable, fetch_negotiations
    try:
        items = fetch_negotiations(settings, limit=limit)
    except BrowserBlocked as exc:
        return {"ok": False, "blocked": True, "reason": str(exc)}
    except BrowserUnavailable as exc:
        return {"ok": False, "reason": str(exc)}
    added = 0
    for item in items:
        title = item.get("title", "")
        job = db.execute("SELECT id FROM jobs WHERE lower(title)=lower(?) LIMIT 1", (title,)).fetchone()
        job_id = job["id"] if job else None
        status = (item.get("status") or "").casefold()
        if status:
            body = f"Статус отклика: {status}"
            result = save_incoming(db, job_id, body, sender=item.get("company", ""),
                                   url=item.get("url", ""), external_id=item.get("url", "") + status,
                                   received_at=item.get("date", ""))
            added += int(result.get("added"))
    return {"ok": True, "negotiations": len(items), "added": added}
