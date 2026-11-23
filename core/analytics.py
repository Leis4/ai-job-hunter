"""Employment analytics: funnel, per-source efficiency, score calibration data."""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import db as dbm
from .mathx import ROUTER
from .paths import EXPORTS, ensure_dirs
from .queue import queue_stats
from .messages import message_stats

STAGES = [
    ("fetched", "Найдено"),
    ("eligible", "Прошли фильтры"),
    ("drafted", "Письма готовы"),
    ("queued", "В очереди"),
    ("sent", "Отклики отправлены"),
    ("replied", "Ответы"),
    ("invited", "Приглашения"),
    ("offer", "Офферы"),
]


def _scalar(db: sqlite3.Connection, sql: str, params=()) -> int:
    row = db.execute(sql, params).fetchone()
    return int((row[0] if row else 0) or 0)


def outcome_posterior(db: sqlite3.Connection) -> dict:
    """Beta-binomial historical invitation rate, excluding pending applications.

    Demo/synthetic rows are excluded: they must never feed a forecast.
    """
    positive = _scalar(db, """SELECT COUNT(DISTINCT j.id) FROM jobs j
        WHERE j.status IN ('invited','offer')
          AND j.source NOT IN ('demo','synthetic','fixture')""")
    negative = _scalar(db, """SELECT COUNT(DISTINCT j.id) FROM jobs j
        WHERE j.status='rejected'
          AND j.source NOT IN ('demo','synthetic','fixture')""")
    pending = _scalar(db, """SELECT COUNT(*) FROM applications a JOIN jobs j ON j.id=a.job_id
        WHERE a.sent_at IS NOT NULL AND j.status NOT IN ('invited','offer','rejected')
          AND j.source NOT IN ('demo','synthetic','fixture')""")
    posterior = ROUTER.run("outcomes.response_rate", successes=positive,
                           resolved_failures=negative, pending=pending).value
    posterior["method"] = "Beta-binomial (historical group rate, not a per-job probability)"
    return posterior


def summary(db: sqlite3.Connection) -> dict:
    total = _scalar(db, "SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NULL")
    eligible = _scalar(db, "SELECT COUNT(*) FROM jobs WHERE eligible=1 AND duplicate_of IS NULL")
    drafted = _scalar(db, "SELECT COUNT(*) FROM jobs WHERE letter<>'' AND eligible=1 AND duplicate_of IS NULL")
    sent = _scalar(db, "SELECT COUNT(*) FROM applications WHERE sent_at IS NOT NULL")
    replied = _scalar(db, "SELECT COUNT(DISTINCT job_id) FROM messages WHERE category IN ('question','test_task','invitation','offer')")
    invited = _scalar(db, "SELECT COUNT(*) FROM jobs WHERE status IN ('invited','offer')")
    offers = _scalar(db, "SELECT COUNT(*) FROM jobs WHERE status='offer'")
    rejected = _scalar(db, "SELECT COUNT(*) FROM jobs WHERE status='rejected'")
    avg_score = db.execute(
        "SELECT AVG(score) FROM jobs WHERE score IS NOT NULL AND score > 0 AND duplicate_of IS NULL"
    ).fetchone()[0]
    return {
        "jobs": total, "eligible": eligible, "drafted": drafted, "sent": sent,
        "replied": replied, "invited": invited, "offers": offers, "rejected": rejected,
        "avg_score": round(float(avg_score or 0), 1),
        "reply_rate": round(replied / sent, 3) if sent else 0.0,
        "invite_rate": round(invited / sent, 3) if sent else 0.0,
        "queue": queue_stats(db),
        "messages": message_stats(db),
        "posterior": outcome_posterior(db),
    }


def funnel(db: sqlite3.Connection) -> list[dict]:
    counts = {
        "fetched": _scalar(db, "SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NULL"),
        "eligible": _scalar(db, "SELECT COUNT(*) FROM jobs WHERE eligible=1 AND duplicate_of IS NULL"),
        "drafted": _scalar(db, "SELECT COUNT(*) FROM jobs WHERE letter<>'' AND eligible=1 AND duplicate_of IS NULL"),
        "queued": _scalar(db, "SELECT COUNT(*) FROM applications WHERE state IN ('queued','approved','submitted')"),
        "sent": _scalar(db, "SELECT COUNT(*) FROM applications WHERE sent_at IS NOT NULL"),
        "replied": _scalar(db, "SELECT COUNT(DISTINCT job_id) FROM messages WHERE category IN ('question','test_task','invitation','offer')"),
        "invited": _scalar(db, "SELECT COUNT(*) FROM jobs WHERE status IN ('invited','offer')"),
        "offer": _scalar(db, "SELECT COUNT(*) FROM jobs WHERE status='offer'"),
    }
    top = max(1, counts["fetched"])
    return [{"key": key, "label": label, "count": counts[key],
             "share": round(counts[key] / top, 3)} for key, label in STAGES]


def by_source(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        """SELECT j.source,
                  COUNT(*) AS jobs,
                  SUM(j.eligible) AS eligible,
                  SUM(CASE WHEN j.letter<>'' THEN 1 ELSE 0 END) AS drafted,
                  SUM(CASE WHEN j.status IN ('submitted','viewed','replied','invited','offer') THEN 1 ELSE 0 END) AS touched
           FROM jobs j WHERE j.duplicate_of IS NULL GROUP BY j.source ORDER BY jobs DESC"""
    ).fetchall()
    out = []
    for row in rows:
        sent = _scalar(db, """SELECT COUNT(*) FROM applications a JOIN jobs j ON j.id=a.job_id
                              WHERE j.source=? AND a.sent_at IS NOT NULL""", (row["source"],))
        replied = _scalar(db, """SELECT COUNT(DISTINCT m.job_id) FROM messages m
                                 JOIN jobs j ON j.id=m.job_id WHERE j.source=?
                                 AND m.category IN ('question','test_task','invitation','offer')""", (row["source"],))
        invited = _scalar(db, """SELECT COUNT(*) FROM jobs j WHERE j.source=?
                                 AND j.status IN ('invited','offer')""", (row["source"],))
        out.append({
            "source": row["source"], "jobs": row["jobs"], "eligible": int(row["eligible"] or 0),
            "drafted": row["drafted"], "sent": sent, "replied": replied, "invited": invited,
            "reply_rate": round(replied / sent, 3) if sent else None,
        })
    return out


def score_bands(db: sqlite3.Connection) -> list[dict]:
    bands = [(0, 40), (40, 60), (60, 70), (70, 80), (80, 90), (90, 101)]
    out = []
    for low, high in bands:
        jobs = _scalar(db, """SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NULL
                              AND score>=? AND score<?""", (low, high))
        sent = _scalar(db, """SELECT COUNT(*) FROM applications a JOIN jobs j ON j.id=a.job_id
                              WHERE a.sent_at IS NOT NULL AND j.score>=? AND j.score<?""", (low, high))
        replied = _scalar(db, """SELECT COUNT(DISTINCT m.job_id) FROM messages m JOIN jobs j ON j.id=m.job_id
                                 WHERE j.score>=? AND j.score<? AND m.category IN ('question','test_task','invitation','offer')""",
                          (low, high))
        out.append({"band": f"{low}–{min(100, high - 1)}", "jobs": jobs, "sent": sent,
                    "replied": replied, "reply_rate": round(replied / sent, 3) if sent else None})
    return out


def daily_series(db: sqlite3.Connection, days: int = 30) -> list[dict]:
    today = date.today()
    series = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        series.append({
            "date": day,
            "jobs": _scalar(db, "SELECT COUNT(*) FROM jobs WHERE substr(created_at,1,10)=?", (day,)),
            "letters": _scalar(db, "SELECT COUNT(*) FROM jobs WHERE substr(updated_at,1,10)=? AND letter<>''", (day,)),
            "sent": _scalar(db, "SELECT COUNT(*) FROM applications WHERE substr(sent_at,1,10)=?", (day,)),
            "replies": _scalar(db, "SELECT COUNT(*) FROM messages WHERE substr(created_at,1,10)=?", (day,)),
        })
    return series


def skill_gap(db: sqlite3.Connection, limit: int = 15) -> list[dict]:
    demand: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    for row in db.execute("SELECT detail FROM jobs WHERE detail<>'' AND duplicate_of IS NULL LIMIT 3000"):
        detail = dbm.job_detail(row)
        for skill in (detail.get("skills") or {}).get("matched", []):
            demand[skill] += 1
        for item in (detail.get("skills") or {}).get("missing", []):
            name = item.get("name") if isinstance(item, dict) else str(item)
            if name:
                demand[name] += 1
                missing[name] += 1
    out = []
    for name, count in demand.most_common(limit):
        miss = missing.get(name, 0)
        out.append({"skill": name, "demand": count, "missing": miss,
                    "coverage": round(1 - miss / count, 2) if count else 1.0})
    return out


def insights(db: sqlite3.Connection) -> list[str]:
    from .settings import load_profile
    notes = []
    has_facts = bool((load_profile().get("verified_skills") or []))
    if not has_facts:
        return ["Профиль не заполнен: подтверждённых навыков нет, поэтому Match Score занижен. "
                "Заполните config/profile.json или резюме в панели."]
    bands = [b for b in score_bands(db) if b["sent"] > 0]
    if bands:
        best = max(bands, key=lambda b: (b["reply_rate"] or 0, b["sent"]))
        notes.append(f"Лучший отклик: диапазон {best['band']} — {best['replied']}/{best['sent']} ответов.")
    sources = [s for s in by_source(db) if s["sent"] >= 3]
    if sources:
        best = max(sources, key=lambda s: s["reply_rate"])
        notes.append(f"Лучший источник по ответам: {best['source']} ({best['replied']}/{best['sent']}).")
    gap = [g for g in skill_gap(db) if g["missing"] >= 3][:3]
    if gap:
        notes.append("Чаще всего не хватает: " + ", ".join(f"{g['skill']} ({g['missing']}×)" for g in gap) + ".")
    posterior = outcome_posterior(db)
    if posterior["resolved_n"] >= 5:
        notes.append(
            f"Историческая доля приглашений: {posterior['mean']:.0%} "
            f"(80% интервал {posterior['interval_80'][0]:.0%}–{posterior['interval_80'][1]:.0%}, "
            f"N={posterior['resolved_n']}, ожидают ответа {posterior['pending_n']}).")
    if summary(db)["sent"] < 10:
        notes.append("Выборка откликов мала: статистика ответов пока описательная, не прогноз.")
    return notes


def calibration_status() -> dict:
    from .paths import DATA
    path = DATA / "calibration.json"
    if not path.exists():
        return {"trained": False, "reason": "модель не обучена"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"trained": False, "reason": "файл модели повреждён"}


def dashboard_data(db: sqlite3.Connection) -> dict:
    return {
        "summary": summary(db), "funnel": funnel(db), "by_source": by_source(db),
        "score_bands": score_bands(db), "series": daily_series(db),
        "skills": skill_gap(db), "insights": insights(db),
        "calibration": calibration_status(),
        "posterior": outcome_posterior(db),
        "math_operations": ROUTER.operations(),
    }


def export_all(db: sqlite3.Connection, fmt: str = "json") -> Path:
    ensure_dirs()
    data = dashboard_data(db)
    today = date.today().isoformat()
    if fmt == "json":
        path = EXPORTS / f"analytics-{today}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
    path = EXPORTS / f"jobs-{today}.csv"
    rows = db.execute(
        """SELECT id, source, title, company, location, work_mode, score, score_low, score_high,
                  eligible, status, url, letter, created_at, updated_at
           FROM jobs WHERE duplicate_of IS NULL ORDER BY score DESC"""
    ).fetchall()
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(rows[0].keys() if rows else ["id"])
        for row in rows:
            writer.writerow([row[key] for key in row.keys()])
    return path
