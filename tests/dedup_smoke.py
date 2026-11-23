"""Focused checks for near-duplicate clustering and commercial-experience gates."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ajh-dedup-"))
for name in ("config", "data", "inbox"):
    (TMP / name).mkdir(parents=True, exist_ok=True)
os.environ["JOB_HUNTER_CONFIG_DIR"] = str(TMP / "config")
os.environ["JOB_HUNTER_DATA_DIR"] = str(TMP / "data")
os.environ["JOB_HUNTER_INBOX_DIR"] = str(TMP / "inbox")
sys.path.insert(0, str(ROOT))

from core import db as dbm  # noqa: E402
from core.dedup import deduplicate_jobs  # noqa: E402
from core.scoring import commercial_signals  # noqa: E402

PASS = []


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAIL: {name} {detail}")
    PASS.append(name)
    print(f"  ok  {name}{(' — ' + detail) if detail else ''}")


def insert(db, ident, title, company, description, url):
    ts = dbm.now()
    db.execute(
        """INSERT INTO jobs(id, source, source_id, title, company, description, url, skills,
                            salary_min, salary_max, location, experience_years, fetched_at,
                            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ident, "test", ident, title, company, description, url, "[]", 100000, 140000,
         "Город Москва", 0.0, ts, ts, ts),
    )


def main():
    db = dbm.init_db()
    long_text = ("Решение задач по внедрению LLM, разработке RAG-агентов и созданию ML-платформы. "
                 "Участие в полном цикле разработки, создание высоконагруженных ML-сервисов. "
                 "Опыт в Data Science и NLP, чат-боты и голосовые помощники. ") * 3
    insert(db, "job-a", "Аналитик данных", 'АО "РТ ЛАБС"', long_text, "https://example.test/a")
    insert(db, "job-b", "Аналитик данных", 'АО "РТ ЛАБС"', long_text, "https://example.test/b")
    insert(db, "job-c", "Стажер Backend-разработчик (Laravel)", "PREAX",
           "Общие слова о стажировке, команде и обучении. " * 10, "https://example.test/c")
    insert(db, "job-d", "Стажер Frontend-разработчик (React)", "PREAX",
           "Общие слова о стажировке, команде и обучении. " * 10, "https://example.test/d")
    db.commit()

    report = deduplicate_jobs(db)
    check("dedup: identical copies merged", report["merged"] == 1, json.dumps(report))
    copy = db.execute("SELECT duplicate_of FROM jobs WHERE id='job-b'").fetchone()[0]
    check("dedup: canonical link", copy == "job-a", str(copy))
    pubs = db.execute("SELECT copy_id, url FROM job_publications WHERE canonical_id='job-a'").fetchall()
    check("dedup: all publication links kept",
          {r["copy_id"] for r in pubs} == {"job-a", "job-b"} and
          {r["url"] for r in pubs} == {"https://example.test/a", "https://example.test/b"},
          str([dict(r) for r in pubs]))
    check("dedup: different roles at one company stay separate",
          db.execute("SELECT COUNT(*) FROM jobs WHERE id IN ('job-c','job-d') AND duplicate_of IS NULL").fetchone()[0] == 2)

    duties = commercial_signals("Нужен production-ready код, опыт в NLP и внедрение моделей в продукты.")
    check("scoring: duties are not past commercial employment", duties == [], str(duties))
    required = commercial_signals("Обязателен коммерческий опыт разработки и опыт запуска в production.")
    check("scoring: explicit prior commercial experience detected", bool(required), str(required))
    optional = commercial_signals("Коммерческий опыт разработки будет плюсом, но не обязателен.")
    check("scoring: optional experience is not a hard signal", optional == [], str(optional))

    second = deduplicate_jobs(db)
    check("dedup: idempotent", second["merged"] == 0, json.dumps(second))
    db.close()
    print(f"\nDEDUP SMOKE: {len(PASS)} проверок пройдено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
