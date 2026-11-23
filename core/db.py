"""SQLite storage with explicit migrations.

All runtime state is local. The schema keeps an audit trail (``events``) for
every state transition so an application can never be sent invisibly.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .paths import DATA, DB_PATH, ensure_dirs

SCHEMA_VERSION = 9

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_id TEXT,
  title TEXT NOT NULL,
  company TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  skills TEXT NOT NULL DEFAULT '[]',
  experience_years REAL,
  experience_text TEXT,
  salary_min INTEGER,
  salary_max INTEGER,
  currency TEXT,
  location TEXT,
  work_mode TEXT,
  published_at TEXT,
  fetched_at TEXT,
  stage TEXT NOT NULL DEFAULT 'discovered',
  detail_fetched INTEGER NOT NULL DEFAULT 0,
  score REAL,
  score_low REAL,
  score_high REAL,
  eligible INTEGER NOT NULL DEFAULT 0,
  needs_review INTEGER NOT NULL DEFAULT 0,
  favorite INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  detail TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'new',
  letter TEXT NOT NULL DEFAULT '',
  letter_hash TEXT,
  letter_source TEXT,
  fingerprint TEXT,
  duplicate_of TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_source_uq ON jobs(source, source_id);
CREATE INDEX IF NOT EXISTS jobs_rank_idx ON jobs(eligible, score DESC);
CREATE INDEX IF NOT EXISTS jobs_fp_idx ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_fetched_idx ON jobs(fetched_at DESC);

CREATE TABLE IF NOT EXISTS source_state (
  source TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 0,
  last_run_at TEXT,
  last_status TEXT,
  last_error TEXT,
  requests INTEGER NOT NULL DEFAULT 0,
  items INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_publications (
  copy_id TEXT PRIMARY KEY,
  canonical_id TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  source_id TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  company TEXT NOT NULL DEFAULT '',
  similarity REAL NOT NULL DEFAULT 1.0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_publications_canonical_idx ON job_publications(canonical_id);

CREATE TABLE IF NOT EXISTS telegram_channels (
  channel TEXT PRIMARY KEY,
  url TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  last_message_id INTEGER NOT NULL DEFAULT 0,
  last_run_at TEXT,
  last_status TEXT,
  last_error TEXT,
  posts INTEGER NOT NULL DEFAULT 0,
  vacancies INTEGER NOT NULL DEFAULT 0,
  new_jobs INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_candidates (  channel TEXT PRIMARY KEY,
  source TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'discovered',
  title TEXT NOT NULL DEFAULT '',
  topic TEXT NOT NULL DEFAULT '',
  last_post_at TEXT,
  posts INTEGER NOT NULL DEFAULT 0,
  vacancies INTEGER NOT NULL DEFAULT 0,
  internships INTEGER NOT NULL DEFAULT 0,
  junior INTEGER NOT NULL DEFAULT 0,
  ads INTEGER NOT NULL DEFAULT 0,
  relevance REAL NOT NULL DEFAULT 0,
  quality REAL NOT NULL DEFAULT 0,
  error TEXT,
  discovered_at TEXT NOT NULL,
  validated_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS telegram_candidates_status_idx ON telegram_candidates(status);

CREATE TABLE IF NOT EXISTS automation_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  last_run_at TEXT,
  last_full_at TEXT,
  last_discovery_at TEXT,
  last_startups_at TEXT,
  last_status TEXT,
  last_error TEXT,
  next_run_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_leads (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'unknown',
  status TEXT NOT NULL DEFAULT 'discovered',
  company TEXT NOT NULL DEFAULT '',
  product TEXT NOT NULL DEFAULT '',
  website TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  stack TEXT NOT NULL DEFAULT '[]',
  contact TEXT NOT NULL DEFAULT '',
  contact_kind TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  evidence_url TEXT NOT NULL DEFAULT '',
  evidence_text TEXT NOT NULL DEFAULT '',
  hiring_status TEXT NOT NULL DEFAULT '',
  pay_status TEXT NOT NULL DEFAULT 'unknown',
  work_mode TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT '',
  fit_score REAL NOT NULL DEFAULT 0,
  hiring_evidence REAL NOT NULL DEFAULT 0,
  compensation_confidence REAL NOT NULL DEFAULT 0,
  rationale TEXT NOT NULL DEFAULT '{}',
  outreach TEXT NOT NULL DEFAULT '',
  outreach_subject TEXT NOT NULL DEFAULT '',
  outreach_updated_at TEXT,
  duplicate_of TEXT,
  detail TEXT NOT NULL DEFAULT '{}',
  first_seen_at TEXT,
  last_checked_at TEXT,
  published_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS company_leads_rank_idx ON company_leads(fit_score DESC, hiring_evidence DESC);
CREATE INDEX IF NOT EXISTS company_leads_status_idx ON company_leads(status);
CREATE INDEX IF NOT EXISTS company_leads_company_idx ON company_leads(company);
CREATE INDEX IF NOT EXISTS company_leads_source_url_idx ON company_leads(source_url);

CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  channel TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',
  letter_hash TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  note TEXT,
  external_id TEXT,
  resume_variant TEXT,
  resume_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  approved_at TEXT,
  sent_at TEXT,
  UNIQUE(job_id, channel)
);
CREATE INDEX IF NOT EXISTS applications_state_idx ON applications(state);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT,
  application_id INTEGER,
  type TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'system',
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_job_idx ON events(job_id, id DESC);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT,
  application_id INTEGER,
  external_id TEXT,
  direction TEXT NOT NULL DEFAULT 'in',
  sender TEXT,
  body TEXT NOT NULL,
  category TEXT,
  confidence REAL,
  needs_reply INTEGER NOT NULL DEFAULT 0,
  draft_reply TEXT,
  url TEXT,
  hash TEXT NOT NULL,
  received_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(hash)
);
CREATE INDEX IF NOT EXISTS messages_job_idx ON messages(job_id);

CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT,
  application_id INTEGER,
  stage TEXT NOT NULL,
  source TEXT,
  score REAL,
  features TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS outcomes_job_idx ON outcomes(job_id, id DESC);

CREATE TABLE IF NOT EXISTS application_outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  external_key TEXT NOT NULL UNIQUE,
  negotiation_id TEXT NOT NULL DEFAULT '',
  application_id INTEGER,
  job_id TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  response_type TEXT NOT NULL DEFAULT 'job',
  employer TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  sent_at TEXT,
  last_event_at TEXT,
  resume_variant TEXT,
  skills TEXT NOT NULL DEFAULT '[]',
  experience_required REAL,
  seniority TEXT NOT NULL DEFAULT 'unknown',
  direction TEXT NOT NULL DEFAULT 'other',
  salary_min INTEGER,
  salary_max INTEGER,
  currency TEXT,
  location TEXT NOT NULL DEFAULT '',
  work_mode TEXT NOT NULL DEFAULT '',
  match_score REAL,
  status TEXT NOT NULL DEFAULT 'unknown',
  status_source TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0.5,
  feature_json TEXT NOT NULL DEFAULT '{}',
  raw_json TEXT NOT NULL DEFAULT '{}',
  job_description TEXT NOT NULL DEFAULT '',
  description_source TEXT NOT NULL DEFAULT '',
  description_fetched_at TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  completeness REAL NOT NULL DEFAULT 0,
  raw_status TEXT NOT NULL DEFAULT '',
  status_checked_at TEXT,
  status_source_url TEXT NOT NULL DEFAULT '',
  negotiation_url TEXT NOT NULL DEFAULT '',
  vacancy_url TEXT NOT NULL DEFAULT '',
  vacancy_status TEXT NOT NULL DEFAULT 'unknown',
  vacancy_checked_at TEXT,
  vacancy_http_status INTEGER,
  vacancy_evidence TEXT NOT NULL DEFAULT '',
  vacancy_published_at TEXT,
  messages_count INTEGER NOT NULL DEFAULT 0,
  has_messages INTEGER NOT NULL DEFAULT 0,
  description_after_submission INTEGER NOT NULL DEFAULT 0,
  viewed_at TEXT,
  invited_at TEXT,
  rejected_at TEXT,
  enrichment_attempts INTEGER NOT NULL DEFAULT 0,
  last_enrich_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS application_outcomes_status_idx ON application_outcomes(status, sent_at);
CREATE INDEX IF NOT EXISTS application_outcomes_job_idx ON application_outcomes(job_id);

CREATE TABLE IF NOT EXISTS application_outcome_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  outcome_id INTEGER NOT NULL REFERENCES application_outcomes(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  event_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0.5,
  note TEXT NOT NULL DEFAULT '',
  raw_json TEXT NOT NULL DEFAULT '{}',
  raw_status TEXT NOT NULL DEFAULT '',
  evidence_url TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS application_outcome_events_idx
  ON application_outcome_events(outcome_id, event_at);

CREATE TABLE IF NOT EXISTS enrichment_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_key TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  next_attempt_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(entity_type, entity_key)
);
CREATE INDEX IF NOT EXISTS enrichment_queue_state_idx
  ON enrichment_queue(state, next_attempt_at, id);

CREATE TABLE IF NOT EXISTS probability_models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'candidate',
  trained_at TEXT NOT NULL,
  data_from TEXT,
  data_to TEXT,
  n INTEGER NOT NULL DEFAULT 0,
  positives INTEGER NOT NULL DEFAULT 0,
  negatives INTEGER NOT NULL DEFAULT 0,
  params TEXT NOT NULL DEFAULT '{}',
  metrics TEXT NOT NULL DEFAULT '{}',
  reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS probability_models_status_idx ON probability_models(status, id DESC);

CREATE TABLE IF NOT EXISTS candidate_facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL,
  value TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0,
  usable INTEGER NOT NULL DEFAULT 1,
  UNIQUE(key, source)
);

CREATE TABLE IF NOT EXISTS autopilot_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  employer TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  vacancy_id TEXT NOT NULL DEFAULT '',
  application_id INTEGER,
  mode TEXT NOT NULL DEFAULT 'observer',
  reason TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS autopilot_actions_idx ON autopilot_actions(id DESC);

CREATE TABLE IF NOT EXISTS autopilot_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  employer TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  vacancy_id TEXT NOT NULL DEFAULT '',
  application_id INTEGER,
  outcome_id INTEGER,
  question TEXT NOT NULL DEFAULT '',
  context TEXT NOT NULL DEFAULT '',
  proposed TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  urgency TEXT NOT NULL DEFAULT 'normal',
  status TEXT NOT NULL DEFAULT 'pending',
  decided_at TEXT,
  answer TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS autopilot_decisions_idx ON autopilot_decisions(status, id DESC);

CREATE TABLE IF NOT EXISTS score_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  model TEXT,
  score REAL,
  skill_match REAL,
  career_fit REAL,
  utility REAL,
  eligibility TEXT,
  features TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS score_history_job_idx ON score_history(job_id, id DESC);

CREATE TABLE IF NOT EXISTS llm_cache (
  key TEXT PRIMARY KEY,
  model TEXT,
  kind TEXT,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT,
  model TEXT,
  input_chars INTEGER NOT NULL DEFAULT 0,
  output_chars INTEGER NOT NULL DEFAULT 0,
  est_cost_usd REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resume_docs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  variant TEXT NOT NULL,
  format TEXT NOT NULL,
  path TEXT NOT NULL,
  sha TEXT,
  facts_hash TEXT,
  created_at TEXT NOT NULL
);
"""

# Columns added after the first public build. Migrations are additive only.
ADDED_COLUMNS = {
    "application_outcomes": {
        "direction": "TEXT NOT NULL DEFAULT 'other'",
        "negotiation_id": "TEXT NOT NULL DEFAULT ''",        "job_description": "TEXT NOT NULL DEFAULT ''",
        "description_source": "TEXT NOT NULL DEFAULT ''",
        "description_fetched_at": "TEXT",
        "details_json": "TEXT NOT NULL DEFAULT '{}'",
        "completeness": "REAL NOT NULL DEFAULT 0",
        "raw_status": "TEXT NOT NULL DEFAULT ''",
        "status_checked_at": "TEXT",
        "status_source_url": "TEXT NOT NULL DEFAULT ''",
        "negotiation_url": "TEXT NOT NULL DEFAULT ''",
        "vacancy_url": "TEXT NOT NULL DEFAULT ''",
        "vacancy_status": "TEXT NOT NULL DEFAULT 'unknown'",
        "vacancy_checked_at": "TEXT",
        "vacancy_http_status": "INTEGER",
        "vacancy_evidence": "TEXT NOT NULL DEFAULT ''",
        "vacancy_published_at": "TEXT",
        "messages_count": "INTEGER NOT NULL DEFAULT 0",
        "has_messages": "INTEGER NOT NULL DEFAULT 0",
        "description_after_submission": "INTEGER NOT NULL DEFAULT 0",
        "viewed_at": "TEXT",
        "invited_at": "TEXT",
        "rejected_at": "TEXT",
        "enrichment_attempts": "INTEGER NOT NULL DEFAULT 0",
        "last_enrich_error": "TEXT",
    },
    "automation_state": {
        "last_startups_at": "TEXT",
    },
    "application_outcome_events": {
        "raw_status": "TEXT NOT NULL DEFAULT ''",
        "evidence_url": "TEXT NOT NULL DEFAULT ''",
    },
    "company_leads": {
        "published_at": "TEXT",
        "detail": "TEXT DEFAULT '{}'",
        "readiness": "TEXT",
        "readiness_reason": "TEXT",
        "readiness_updated_at": "TEXT",
    },
    "jobs": {
        "source_id": "TEXT", "score_low": "REAL", "score_high": "REAL", "detail": "TEXT DEFAULT '{}'",
        "experience_text": "TEXT", "salary_max": "INTEGER", "currency": "TEXT", "published_at": "TEXT",
        "fetched_at": "TEXT", "stage": "TEXT DEFAULT 'discovered'", "letter_hash": "TEXT",
        "letter_source": "TEXT", "fingerprint": "TEXT", "duplicate_of": "TEXT",
        "detail_fetched": "INTEGER NOT NULL DEFAULT 0",
        "needs_review": "INTEGER NOT NULL DEFAULT 0",
        "favorite": "INTEGER NOT NULL DEFAULT 0",
    },
    "applications": {
        "resume_variant": "TEXT",        "resume_path": "TEXT",
        "readiness": "TEXT",
        "readiness_reason": "TEXT",
        "readiness_updated_at": "TEXT",
        "verification_method": "TEXT",
        "verification_source": "TEXT",
        "verified_at": "TEXT",
        "followup_at": "TEXT",
        "outcome": "TEXT",
    },
    "telegram_channels": {
        "title": "TEXT", "topic": "TEXT", "last_post_at": "TEXT",
        "posts_30d": "INTEGER NOT NULL DEFAULT 0",
        "vacancies_30d": "INTEGER NOT NULL DEFAULT 0",
        "internships": "INTEGER NOT NULL DEFAULT 0",
        "junior": "INTEGER NOT NULL DEFAULT 0",
        "ads_ratio": "REAL NOT NULL DEFAULT 0",
        "relevance": "REAL NOT NULL DEFAULT 0",
        "quality": "REAL NOT NULL DEFAULT 0",
        "auto_paused": "INTEGER NOT NULL DEFAULT 0",
        "content_hash": "TEXT",
    },
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    path = db_path or DB_PATH
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=15000")
    return db


def init_db(db: sqlite3.Connection | None = None) -> sqlite3.Connection:
    own = db is None
    db = db or connect()
    db.executescript(SCHEMA)
    for table, columns in ADDED_COLUMNS.items():
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    db.commit()
    if own:
        return db
    return db


def _clean(value: Any, limit: int | None = None) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text).strip()
    return text[:limit] if limit else text


def normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith(("utm_", "from", "hhtm", "ref"))]
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, urlencode(query), ""))


def job_id_for(source: str, source_id: str | None, url: str, title: str, company: str) -> str:
    basis = source_id or normalize_url(url) or f"{source}|{_clean(title).casefold()}|{_clean(company).casefold()}"
    return hashlib.sha256(f"{source}|{basis}".encode("utf-8")).hexdigest()[:24]


def fingerprint_for(title: str, company: str) -> str:
    words = re.findall(r"[\w+#.]+", f"{title} {company}".casefold().replace("ё", "е"))
    noise = {"ооо", "оао", "зао", "ип", "пао", "ао", "llc", "ltd", "inc", "company", "компания"}
    core = [w for w in words if w not in noise][:14]
    return hashlib.sha1(" ".join(core).encode("utf-8")).hexdigest()[:20]


def upsert_job(db: sqlite3.Connection, job: dict) -> tuple[str, bool]:
    """Insert a normalized job card. Returns (id, created)."""
    source = _clean(job.get("source") or "manual", 60)
    source_id = _clean(job.get("source_id") or "", 200)
    url = normalize_url(_clean(job.get("url")))
    title = _clean(job.get("title"), 300)
    company = _clean(job.get("company"), 300)
    if not title:
        raise ValueError("Вакансия без названия")
    ident = job.get("id") or job_id_for(source, source_id, url, title, company)
    fp = fingerprint_for(title, company)
    skills = job.get("skills") or []
    if isinstance(skills, str):
        skills = [s.strip() for s in re.split(r"[,;]", skills) if s.strip()]
    skills_json = json.dumps([_clean(s, 80) for s in skills if s][:60], ensure_ascii=False)
    description = _clean(job.get("description"), 60000)
    existing = db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
    ts = now()
    if existing:
        # Fill gaps only; never overwrite a better description with a snippet.
        richer = len(description) > len(existing["description"] or "")
        db.execute(
            """UPDATE jobs SET
                 title=?, company=?, description=?, url=?, skills=?,
                 experience_years=COALESCE(?, experience_years),
                 experience_text=COALESCE(?, experience_text),
                 salary_min=COALESCE(?, salary_min), salary_max=COALESCE(?, salary_max),
                 currency=COALESCE(?, currency), location=COALESCE(NULLIF(?,''), location),
                 work_mode=COALESCE(NULLIF(?,''), work_mode),
                 published_at=COALESCE(NULLIF(?,''), published_at),
                 stage=CASE WHEN ? THEN 'detailed' ELSE stage END,
                 detail_fetched=CASE WHEN ? THEN 1 ELSE detail_fetched END,
                 fetched_at=COALESCE(fetched_at, ?), updated_at=?
               WHERE id=?""",
            (title or existing["title"], company or existing["company"],
             description if richer else existing["description"], url or existing["url"],
             skills_json if skills else existing["skills"],
             job.get("experience_years"), job.get("experience_text"),
             job.get("salary_min"), job.get("salary_max"), job.get("currency"),
             _clean(job.get("location"), 200), _clean(job.get("work_mode"), 60),
             _clean(job.get("published_at"), 60), richer, richer, ts, ts, ident),
        )
        db.commit()
        return ident, False

    duplicate_of = None
    prior = db.execute(
        "SELECT id, source FROM jobs WHERE fingerprint=? AND duplicate_of IS NULL ORDER BY length(description) DESC LIMIT 1",
        (fp,),
    ).fetchone()
    if prior and prior["source"] != source:
        duplicate_of = prior["id"]

    db.execute(
        """INSERT INTO jobs
           (id, source, source_id, title, company, description, url, skills,
            experience_years, experience_text, salary_min, salary_max, currency,
            location, work_mode, published_at, fetched_at, stage, detail_fetched, status,
            fingerprint, duplicate_of, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ident, source, source_id, title, company, description, url, skills_json,
         job.get("experience_years"), _clean(job.get("experience_text"), 300),
         job.get("salary_min"), job.get("salary_max"), _clean(job.get("currency"), 20),
         _clean(job.get("location"), 200), _clean(job.get("work_mode"), 60),
         _clean(job.get("published_at"), 60), ts,
         "detailed" if len(description) > 500 else "discovered",
         1 if len(description) > 500 else 0,
         "new", fp, duplicate_of, ts, ts),
    )
    db.commit()
    log_event(db, ident, "job_imported", payload={"source": source, "duplicate_of": duplicate_of})
    return ident, True


def log_event(db: sqlite3.Connection, job_id: str | None, event_type: str,
              payload: dict | None = None, actor: str = "system",
              application_id: int | None = None) -> None:
    db.execute(
        "INSERT INTO events(job_id, application_id, type, actor, payload, created_at) VALUES (?,?,?,?,?,?)",
        (job_id, application_id, event_type, actor, json.dumps(payload or {}, ensure_ascii=False), now()),
    )
    db.commit()


def set_job_fields(db: sqlite3.Connection, ident: str, **fields: Any) -> None:
    if not fields:
        return
    allowed = {"title", "company", "description", "url", "skills", "experience_years",
               "experience_text", "salary_min", "salary_max", "currency", "location",
               "work_mode", "published_at", "stage", "score", "score_low", "score_high",
               "eligible", "needs_review", "reason", "detail", "status", "letter", "letter_hash",
               "letter_source", "duplicate_of", "fetched_at", "detail_fetched", "favorite"}
    values = {k: v for k, v in fields.items() if k in allowed}
    if not values:
        return
    values["updated_at"] = now()
    assignments = ", ".join(f"{k}=?" for k in values)
    db.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*values.values(), ident))
    db.commit()


def get_job(db: sqlite3.Connection, ident: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()


def job_skills(row: sqlite3.Row | dict) -> list[str]:
    raw = row["skills"] if isinstance(row, sqlite3.Row) else row.get("skills")
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    try:
        value = json.loads(raw or "[]")
        return [str(x) for x in value] if isinstance(value, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def job_detail(row: sqlite3.Row | dict) -> dict:
    raw = row["detail"] if isinstance(row, sqlite3.Row) else row.get("detail")
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def source_state(db: sqlite3.Connection, source: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM source_state WHERE source=?", (source,)).fetchone()


def mark_source_run(db: sqlite3.Connection, source: str, status: str, error: str = "",
                    requests: int = 0, items: int = 0, enabled: bool = True) -> None:
    db.execute(
        """INSERT INTO source_state(source, enabled, last_run_at, last_status, last_error, requests, items, updated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(source) DO UPDATE SET
             enabled=excluded.enabled, last_run_at=excluded.last_run_at, last_status=excluded.last_status,
             last_error=excluded.last_error, requests=source_state.requests+excluded.requests,
             items=source_state.items+excluded.items, updated_at=excluded.updated_at""",
        (source, int(enabled), now(), status, _clean(error, 500), requests, items, now()),
    )
    db.commit()


def list_jobs(db: sqlite3.Connection, *, status: str | None = None, source: str | None = None,
              eligible_only: bool = False, min_score: float | None = None, search: str = "",
              limit: int = 200, offset: int = 0, role: str = "", max_experience: float | None = None,
              min_salary: int | None = None, work_mode: str = "", favorite_only: bool = False,
              exclude_company: str = "", exclude_words: str = "", all_required: bool = False,
              skill: str = "", sort: str = "score") -> list[sqlite3.Row]:
    clauses, params = ["duplicate_of IS NULL"], []
    if status:
        clauses.append("status=?"); params.append(status)
    if source:
        clauses.append("source=?"); params.append(source)
    if eligible_only:
        clauses.append("eligible=1")
    if min_score is not None:
        clauses.append("score>=?"); params.append(min_score)
    if search:
        clauses.append("(title LIKE ? OR company LIKE ? OR description LIKE ?)")
        params += [f"%{search}%", f"%{search}%", f"%{search}%"]
    if role:
        clauses.append("title LIKE ?"); params.append(f"%{role}%")
    if skill:
        clauses.append("(skills LIKE ? OR description LIKE ?)")
        params += [f"%{skill}%", f"%{skill}%"]
    if max_experience is not None:
        clauses.append("(experience_years IS NULL OR experience_years<=?)"); params.append(max_experience)
    if min_salary:
        clauses.append("COALESCE(salary_max, salary_min, 0)>=?"); params.append(min_salary)
    if work_mode:
        clauses.append("work_mode=?"); params.append(work_mode)
    if favorite_only:
        clauses.append("favorite=1")
    if exclude_company:
        clauses.append("company NOT LIKE ?"); params.append(f"%{exclude_company}%")
    for word in [w.strip() for w in re.split(r"[,;]", exclude_words) if w.strip()]:
        clauses.append("(title NOT LIKE ? AND description NOT LIKE ?)")
        params += [f"%{word}%", f"%{word}%"]
    if all_required:
        clauses.append("detail LIKE '%\"required_missing\": []%'")
    order = {
        "score": "eligible DESC, score DESC, fetched_at DESC",
        "date": "published_at DESC, fetched_at DESC",
        "salary": "COALESCE(salary_max, salary_min, 0) DESC",
    }.get(sort, "eligible DESC, score DESC, fetched_at DESC")
    sql = ("SELECT * FROM jobs WHERE " + " AND ".join(clauses) +
           f" ORDER BY {order} LIMIT ? OFFSET ?")
    return db.execute(sql, (*params, limit, offset)).fetchall()


def pending_detail_jobs(db: sqlite3.Connection, limit: int, source: str | None = None) -> list[sqlite3.Row]:
    clauses = ["duplicate_of IS NULL", "detail_fetched=0", "status='new'"]
    params: list[Any] = []
    if source:
        clauses.append("source=?")
        params.append(source)
    return db.execute(
        "SELECT * FROM jobs WHERE " + " AND ".join(clauses) +
        " ORDER BY score DESC NULLS LAST, fetched_at DESC LIMIT ?", (*params, limit)
    ).fetchall()


def save_outcome(db: sqlite3.Connection, job: sqlite3.Row | dict, stage: str,
                 application_id: int | None = None) -> None:
    features = job_detail(job)
    db.execute(
        "INSERT INTO outcomes(job_id, application_id, stage, source, score, features, created_at) VALUES (?,?,?,?,?,?,?)",
        (job["id"], application_id, stage, job["source"], job["score"], json.dumps(features, ensure_ascii=False), now()),
    )
    db.commit()


def vacuum_old(db: sqlite3.Connection, retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    cur = db.execute(
        """DELETE FROM jobs WHERE status IN ('archived','skipped')
           AND julianday('now') - julianday(updated_at) > ?""", (retention_days,)
    )
    db.commit()
    return cur.rowcount


def dump_jobs(db: sqlite3.Connection, where: str = "1=1", params: Iterable[Any] = ()) -> list[dict]:
    rows = db.execute(f"SELECT * FROM jobs WHERE {where}", tuple(params)).fetchall()
    return [dict(r) for r in rows]
