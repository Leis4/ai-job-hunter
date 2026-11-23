"""Provenance-aware local memory: SQLite FTS5 + optional local embeddings.

Architecture (adapted from the upgrade pack's ``memory_sqlite.py``):
* one canonical document index (``memory_documents``) with FTS5;
* typed tables for facts, vacancy chunks, letters, feedback, guides, aliases;
* optional vectors from a local Ollama embedding model (bge-m3 by default),
  stored as compact float32 BLOBs and searched with cosine similarity;
* hybrid retrieval with Reciprocal Rank Fusion (k=60), FTS-only fallback.

Hard rule: employer text never becomes a candidate fact automatically.
Only rows explicitly marked ``verified`` and sourced from the user's resume may
enter ``candidate_fact`` memory.
"""
from __future__ import annotations

import array
import hashlib
import json
import math
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from typing import Iterable, Sequence

from . import db as dbm
from .mathx import reciprocal_rank_fusion
from .text import clean_text, tokens

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  ref_id TEXT,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  source TEXT,
  source_url TEXT,
  trust REAL NOT NULL DEFAULT 0.5,
  verified INTEGER NOT NULL DEFAULT 0,
  version TEXT,
  content_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_kind_idx ON memory_documents(kind, ref_id);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(title, body, tokenize='unicode61');

CREATE TABLE IF NOT EXISTS memory_vectors (
  doc_id INTEGER PRIMARY KEY,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS embedding_meta (
  model TEXT PRIMARY KEY,
  dim INTEGER NOT NULL,
  version TEXT,
  count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verified_candidate_facts (
  fact_id TEXT PRIMARY KEY,
  doc_id INTEGER,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  source TEXT NOT NULL,
  trust REAL NOT NULL DEFAULT 1.0,
  verified INTEGER NOT NULL DEFAULT 1,
  version TEXT,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vacancy_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id INTEGER,
  job_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  source_url TEXT,
  trust REAL NOT NULL DEFAULT 0.7,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(job_id, chunk_index, content_hash)
);
CREATE TABLE IF NOT EXISTS job_application_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT,
  application_id INTEGER,
  event TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS letters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id INTEGER,
  job_id TEXT,
  variant TEXT,
  body TEXT NOT NULL,
  facts_hash TEXT,
  model TEXT,
  cost_rub REAL,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS employer_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id INTEGER,
  job_id TEXT,
  application_id INTEGER,
  category TEXT,
  body TEXT NOT NULL,
  sender TEXT,
  received_at TEXT,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verified_guides (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id INTEGER,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  source_url TEXT NOT NULL,
  published_at TEXT,
  inspected_at TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.5,
  verified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS skill_aliases (
  alias TEXT PRIMARY KEY,
  canonical TEXT NOT NULL,
  source TEXT,
  verified INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
"""

FACT_KINDS = {"candidate_fact", "vacancy", "feedback", "guideline", "letter", "employer"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:32]


def _pack(vector: Sequence[float]) -> bytes:
    return array.array("f", [float(x) for x in vector]).tobytes()


def _unpack(blob: bytes) -> list[float]:
    values = array.array("f")
    values.frombytes(blob)
    return list(values)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _chunk(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    text = clean_text(text)
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind(". ", start + size // 2, end)
            if cut > 0:
                end = cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return [c for c in chunks if c]


class OllamaEmbedder:
    """Local multilingual embeddings through the already running Ollama.

    The model is loaded on demand and explicitly unloaded after a batch so no
    heavy model stays resident. Failure degrades to FTS-only retrieval.
    """

    def __init__(self, model: str = "bge-m3", endpoint: str = "http://127.0.0.1:11434",
                 timeout: int = 180):
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.dim: int | None = None
        self.version = "ollama"

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(self.endpoint + "/api/tags", timeout=5) as response:
                payload = json.load(response)
            names = {m.get("name", "") for m in payload.get("models", [])}
            return self.model in names or f"{self.model}:latest" in names
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = json.dumps({"model": self.model, "input": texts, "keep_alive": "30s"}).encode()
        request = urllib.request.Request(
            self.endpoint + "/api/embed", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.load(response)
        vectors = payload.get("embeddings") or []
        if len(vectors) != len(texts):
            raise RuntimeError("Ollama returned an unexpected number of embeddings")
        if vectors:
            self.dim = len(vectors[0])
        return vectors

    def unload(self) -> None:
        """Ask Ollama to drop the model from memory (keep_alive=0)."""
        try:
            body = json.dumps({"model": self.model, "prompt": "", "stream": False,
                               "keep_alive": 0, "options": {"num_predict": 1}}).encode()
            request = urllib.request.Request(
                self.endpoint + "/api/generate", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=20):
                pass
        except Exception:
            pass


class Memory:
    def __init__(self, db: sqlite3.Connection, settings: dict | None = None,
                 embedder: OllamaEmbedder | None = None):
        self.db = db
        self.settings = settings or {}
        conf = (self.settings.get("memory") or {})
        self.enabled = bool(conf.get("enabled", True))
        self.embed_enabled = bool(conf.get("embed_enabled", False))
        self.embed_model = conf.get("embedding_model") or os.environ.get(
            "JOB_HUNTER_EMBED_MODEL", "bge-m3")
        self.embedder = embedder
        if self.embedder is None and self.embed_enabled and self.enabled:
            self.embedder = OllamaEmbedder(self.embed_model,
                                           endpoint=conf.get("ollama_url", "http://127.0.0.1:11434"))
        self.has_fts = True
        self.db.executescript(MEMORY_SCHEMA)
        try:
            self.db.execute("SELECT rowid FROM memory_fts LIMIT 1").fetchall()
        except sqlite3.DatabaseError:
            self.has_fts = False
        self.db.commit()

    # ------------------------------------------------------------------ documents
    def add(self, kind: str, title: str, body: str, *, ref_id: str | None = None,
            source: str = "", source_url: str = "", trust: float = 0.5,
            verified: bool = False, version: str = "") -> int:
        if kind not in FACT_KINDS:
            raise ValueError(f"Unknown memory kind: {kind}")
        if kind == "candidate_fact" and not verified:
            raise ValueError("Candidate facts must be verified before indexing")
        title = clean_text(title)[:300]
        body = clean_text(body)
        if not title and not body:
            raise ValueError("Empty document")
        digest = _hash(kind, str(ref_id or ""), title, body)
        existing = self.db.execute(
            "SELECT id FROM memory_documents WHERE content_hash=?", (digest,)).fetchone()
        if existing:
            return int(existing["id"] if isinstance(existing, sqlite3.Row) else existing[0])
        cursor = self.db.execute(
            """INSERT INTO memory_documents(kind,ref_id,title,body,source,source_url,trust,verified,
                 version,content_hash,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (kind, ref_id, title, body, source[:300], source_url[:600], float(trust),
             int(verified), version[:80], digest, _now(), _now()))
        doc_id = int(cursor.lastrowid)
        if self.has_fts:
            self.db.execute("INSERT INTO memory_fts(rowid,title,body) VALUES (?,?,?)",
                            (doc_id, title, body))
        self.db.commit()
        return doc_id

    # ------------------------------------------------------------------ facts
    def sync_facts(self, resume: dict, profile: dict, facts_hash: str = "") -> int:
        """Mirror only *verified* resume facts. Removed facts disappear on next sync."""
        if not self.enabled:
            return 0
        current: dict[str, dict] = {}

        def add_fact(kind: str, title: str, body: str, source: str) -> None:
            fact_id = _hash("fact", kind, title, body)
            current[fact_id] = {"kind": kind, "title": title, "body": body, "source": source}

        for skill in resume.get("skills") or []:
            if isinstance(skill, dict) and skill.get("verified") and skill.get("name"):
                evidence = skill.get("evidence") or "подтверждено пользователем"
                add_fact("skill", str(skill["name"]),
                         f"Подтверждённый навык: {skill['name']}. Уровень: {skill.get('level') or 'не указан'}. "
                         f"Основание: {evidence}", "config/resume.json#skills")
        for project in resume.get("projects") or []:
            if isinstance(project, dict) and project.get("verified") and project.get("name"):
                stack = ", ".join(project.get("stack") or [])
                add_fact("project", str(project["name"]),
                         f"Проект: {project['name']}. Роль: {project.get('role') or 'не указана'}. "
                         f"Стек: {stack or 'не указан'}. {project.get('description') or ''}",
                         "config/resume.json#projects")
        for item in resume.get("experience") or []:
            if isinstance(item, dict) and item.get("verified"):
                title = f"{item.get('position') or 'Опыт'} — {item.get('company') or ''}".strip(" —")
                add_fact("experience", title,
                         f"{title}. {item.get('start','')} — {item.get('end','')}. "
                         f"{item.get('description') or ''}", "config/resume.json#experience")
        for item in resume.get("education") or []:
            if isinstance(item, dict) and item.get("verified"):
                title = str(item.get("institution") or "Образование")
                add_fact("education", title,
                         f"{title}. {item.get('degree','')} {item.get('field','')}. "
                         f"{item.get('start','')} — {item.get('end','')}", "config/resume.json#education")
        prefs = (profile or {}).get("preferences") or {}
        for label, field in (("Города", "locations"), ("Форматы работы", "work_modes"),
                             ("Целевые роли", "role_keywords"), ("Занятость", "employment"),
                             ("Желаемый график", "schedule"), ("Доступность", "availability")):
            value = prefs.get(field)
            if isinstance(value, list):
                value = ", ".join(str(x) for x in value if str(x).strip())
            if str(value or "").strip():
                add_fact("preference", f"Предпочтение: {label}",
                         f"{label}: {value}", "config/profile.json#preferences")
        salary = prefs.get("salary_target_rub")
        if salary:
            add_fact("preference", "Предпочтение: зарплата",
                     f"Желаемая зарплата: {salary} ₽", "config/profile.json#preferences")

        added = 0
        for fact_id, fact in current.items():
            doc_id = self.add("candidate_fact", fact["title"], fact["body"], ref_id=fact_id,
                              source=fact["source"], trust=1.0, verified=True,
                              version=facts_hash or "v1")
            self.db.execute(
                """INSERT INTO verified_candidate_facts
                   (fact_id,doc_id,kind,title,body,source,trust,verified,version,content_hash,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(fact_id) DO UPDATE SET
                     doc_id=excluded.doc_id, title=excluded.title, body=excluded.body,
                     version=excluded.version, updated_at=excluded.updated_at""",
                (fact_id, doc_id, fact["kind"], fact["title"], fact["body"], fact["source"],
                 1.0, 1, facts_hash, _hash(fact_id, fact["body"]), _now(), _now()))
            added += 1
        if current:
            placeholders = ",".join("?" for _ in current)
            stale = self.db.execute(
                f"SELECT fact_id, doc_id FROM verified_candidate_facts WHERE fact_id NOT IN ({placeholders})",
                tuple(current)).fetchall()
            for row in stale:
                self.db.execute("DELETE FROM memory_documents WHERE id=?", (row["doc_id"],))
                if self.has_fts:
                    self.db.execute("DELETE FROM memory_fts WHERE rowid=?", (row["doc_id"],))
                self.db.execute("DELETE FROM memory_vectors WHERE doc_id=?", (row["doc_id"],))
            self.db.execute(
                f"DELETE FROM verified_candidate_facts WHERE fact_id NOT IN ({placeholders})",
                tuple(current))
        else:
            # All verified facts were removed by the user: clear the fact collection.
            rows = self.db.execute("SELECT doc_id FROM verified_candidate_facts").fetchall()
            for row in rows:
                self.db.execute("DELETE FROM memory_documents WHERE id=?", (row["doc_id"],))
                if self.has_fts:
                    self.db.execute("DELETE FROM memory_fts WHERE rowid=?", (row["doc_id"],))
                self.db.execute("DELETE FROM memory_vectors WHERE doc_id=?", (row["doc_id"],))
            self.db.execute("DELETE FROM verified_candidate_facts")
        self.db.commit()
        return added

    # ------------------------------------------------------------------ vacancies / letters
    def index_job(self, job: dict, *, max_chunks: int = 4) -> int:
        if not self.enabled or job.get("duplicate_of"):
            return 0
        title = f"{job.get('title','')} — {job.get('company','')}".strip(" —")
        skills = ", ".join(dbm.job_skills(job)) if isinstance(job, dict) else ""
        chunks = _chunk(f"{title}\nНавыки: {skills}\n\n{job.get('description','')}", size=1200,
                        overlap=150)[:max_chunks]
        expected = {_hash("vacancy", str(job.get("id", "")), str(index), chunk)
                    for index, chunk in enumerate(chunks)}
        existing = {row["content_hash"]: row["doc_id"] for row in self.db.execute(
            "SELECT content_hash, doc_id FROM vacancy_chunks WHERE job_id=?", (str(job.get("id", "")),))}
        if existing and set(existing) == expected:
            return 0
        if existing:
            # The vacancy text changed: remove stale chunks before re-indexing.
            for doc_id in existing.values():
                self.db.execute("DELETE FROM memory_documents WHERE id=?", (doc_id,))
                if self.has_fts:
                    self.db.execute("DELETE FROM memory_fts WHERE rowid=?", (doc_id,))
                self.db.execute("DELETE FROM memory_vectors WHERE doc_id=?", (doc_id,))
            self.db.execute("DELETE FROM vacancy_chunks WHERE job_id=?", (str(job.get("id", "")),))
            self.db.commit()
        added = 0
        for index, chunk in enumerate(chunks):
            digest = _hash("vacancy", str(job.get("id", "")), str(index), chunk)
            if digest in existing:
                continue
            doc_id = self.add("vacancy", f"{title} (часть {index + 1})", chunk,
                              ref_id=str(job.get("id", "")), source="vacancy",
                              source_url=str(job.get("url", "")), trust=0.7, verified=False,
                              version="job-v1")
            self.db.execute(
                """INSERT OR IGNORE INTO vacancy_chunks(doc_id,job_id,chunk_index,title,body,
                     source_url,trust,content_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (doc_id, str(job.get("id", "")), index, title, chunk, str(job.get("url", "")),
                 0.7, digest, _now()))
            added += 1
        self.db.commit()
        return added

    def index_jobs(self, rows: list) -> int:
        return sum(self.index_job(dict(row)) for row in rows)

    def index_letter(self, job: dict, body: str, *, variant: str = "custom",
                     facts_hash: str = "", model: str = "", cost_rub: float | None = None) -> int:
        if not self.enabled or not body.strip():
            return 0
        doc_id = self.add("letter", f"Письмо: {job.get('title','')}", body,
                          ref_id=str(job.get("id", "")), source="generated", trust=0.9,
                          verified=False, version=facts_hash or "v1")
        self.db.execute(
            """INSERT INTO letters(doc_id,job_id,variant,body,facts_hash,model,cost_rub,
                 content_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (doc_id, str(job.get("id", "")), variant, body, facts_hash,
             model, cost_rub, _hash("letter", str(job.get("id", "")), body), _now()))
        self.db.commit()
        return doc_id

    def add_feedback(self, job_id: str | None, body: str, *, category: str = "info",
                     sender: str = "", application_id: int | None = None,
                     received_at: str = "") -> int:
        if not self.enabled or not body.strip():
            return 0
        doc_id = self.add("feedback", f"Ответ работодателя: {category}", body,
                          ref_id=job_id or "", source="employer", trust=0.8, verified=False,
                          version="msg-v1")
        self.db.execute(
            """INSERT INTO employer_feedback(doc_id,job_id,application_id,category,body,sender,
                 received_at,content_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (doc_id, job_id, application_id, category, body, sender, received_at,
             _hash("feedback", job_id or "", body), _now()))
        self.db.commit()
        return doc_id

    def add_guide(self, title: str, body: str, *, source_url: str, published_at: str = "",
                  confidence: float = 0.7, verified: bool = False) -> int:
        if not source_url.startswith(("http://", "https://")):
            raise ValueError("A guide needs an exact source URL")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be in [0,1]")
        digest = _hash("guide", title, body, source_url)
        doc_id = self.add("guideline", title, body, ref_id=digest, source="guide",
                          source_url=source_url, trust=confidence, verified=verified,
                          version="guide-v1")
        self.db.execute(
            """INSERT OR IGNORE INTO verified_guides(doc_id,title,body,source_url,published_at,
                 inspected_at,content_hash,confidence,verified) VALUES (?,?,?,?,?,?,?,?,?)""",
            (doc_id, title, body, source_url, published_at, date.today().isoformat(),
             digest, confidence, int(verified)))
        self.db.commit()
        return doc_id

    def sync_aliases(self, aliases: dict[str, list[str]] | None = None) -> int:
        from .text import SKILLS
        rows = []
        for canonical, values in SKILLS.items():
            for alias in values:
                rows.append((alias.casefold(), canonical, "curated", 1))
        for canonical, values in (aliases or {}).items():
            for alias in values:
                rows.append((str(alias).casefold(), canonical, "profile", 1))
        for alias, canonical, source, verified in rows:
            self.db.execute(
                """INSERT INTO skill_aliases(alias,canonical,source,verified,updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(alias) DO UPDATE SET canonical=excluded.canonical,
                     source=excluded.source, verified=excluded.verified, updated_at=excluded.updated_at""",
                (alias, canonical, source, verified, _now()))
        self.db.commit()
        return len(rows)

    def application_event(self, job_id: str | None, event: str, *,
                          application_id: int | None = None, payload: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO job_application_events(job_id,application_id,event,payload,created_at) VALUES (?,?,?,?,?)",
            (job_id, application_id, event, json.dumps(payload or {}, ensure_ascii=False), _now()))
        self.db.commit()

    # ------------------------------------------------------------------ embeddings
    def _vector_ready(self) -> bool:
        return bool(self.embedder and self.embed_enabled)

    def embed_pending(self, *, batch_size: int = 16, limit: int = 120) -> dict:
        if not self._vector_ready():
            return {"embedded": 0, "reason": "vectors disabled"}
        if not self.embedder.available():
            return {"embedded": 0, "reason": f"model {self.embed_model} not available in Ollama"}
        pending = self.db.execute(
            """SELECT d.id, d.title, d.body FROM memory_documents d
               LEFT JOIN memory_vectors v ON v.doc_id=d.id
               WHERE v.doc_id IS NULL ORDER BY d.id LIMIT ?""", (limit,)).fetchall()
        if not pending:
            return {"embedded": 0, "reason": "up to date"}
        embedded = 0
        model = self.embed_model
        for start in range(0, len(pending), batch_size):
            batch = pending[start:start + batch_size]
            texts = [f"{row['title']}\n{row['body']}" for row in batch]
            try:
                vectors = self.embedder.embed(texts)
            except Exception as exc:
                return {"embedded": embedded, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
            dim = len(vectors[0]) if vectors else 0
            meta = self.db.execute("SELECT model, dim FROM embedding_meta ORDER BY updated_at DESC LIMIT 1").fetchone()
            if meta and (meta["model"] != model or int(meta["dim"]) != dim):
                self.db.execute("DELETE FROM memory_vectors")
            for row, vector in zip(batch, vectors):
                self.db.execute(
                    "INSERT OR REPLACE INTO memory_vectors(doc_id,model,dim,vector) VALUES (?,?,?,?)",
                    (row["id"], model, dim, _pack(vector)))
                embedded += 1
            if dim:
                self.db.execute(
                    """INSERT INTO embedding_meta(model,dim,version,count,updated_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(model) DO UPDATE SET dim=excluded.dim, version=excluded.version,
                         count=(SELECT COUNT(*) FROM memory_vectors WHERE model=excluded.model),
                         updated_at=excluded.updated_at""",
                    (model, dim, "bge-m3-ollama", embedded, _now()))
            self.db.commit()
        if (self.settings.get("memory") or {}).get("unload_after_batch", True):
            self.embedder.unload()
        return {"embedded": embedded, "model": model}

    # ------------------------------------------------------------------ search
    def _fts_search(self, query: str, kinds: list[str] | None, limit: int) -> list[int]:
        # FTS stores raw surface forms; do not stem the query (stemming is for BM25 scoring).
        terms = [t.casefold().replace("ё", "е") for t in re.findall(r"[a-zа-я0-9][a-zа-я0-9+#._/-]*", query, re.I)]
        terms = [t.strip("._/-") for t in terms if len(t.strip("._/-")) >= 2][:14]
        if not terms or not self.has_fts:
            return []
        match = " OR ".join('"' + t.replace('"', '') + '"*' for t in terms)
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            rows = self.db.execute(
                f"""SELECT f.rowid FROM memory_fts f JOIN memory_documents d ON d.id=f.rowid
                    WHERE memory_fts MATCH ? AND d.kind IN ({placeholders})
                    ORDER BY bm25(memory_fts) LIMIT ?""",
                (match, *kinds, max(limit * 4, 20))).fetchall()
        else:
            rows = self.db.execute(
                """SELECT f.rowid FROM memory_fts f WHERE memory_fts MATCH ?
                   ORDER BY bm25(memory_fts) LIMIT ?""",
                (match, max(limit * 4, 20))).fetchall()
        return [int(r[0]) for r in rows]

    def _vector_search(self, query: str, kinds: list[str] | None, limit: int) -> tuple[list[int], str]:
        if not self._vector_ready():
            return [], "fts-only"
        try:
            if not self.embedder.available():
                return [], "fts-only"
            meta = self.db.execute("SELECT model, dim FROM embedding_meta ORDER BY updated_at DESC LIMIT 1").fetchone()
            if not meta:
                return [], "fts-only"
            vector = self.embedder.embed([query])[0]
            if len(vector) != int(meta["dim"]):
                return [], "fts-only"
        except Exception as exc:
            return [], f"fts-only ({type(exc).__name__})"
        clause, params = "", []
        if kinds:
            clause = " AND d.kind IN (" + ",".join("?" for _ in kinds) + ")"
            params = list(kinds)
        rows = self.db.execute(
            f"""SELECT v.doc_id, v.vector FROM memory_vectors v
                JOIN memory_documents d ON d.id=v.doc_id
                WHERE v.model=? {clause}""",
            (meta["model"], *params)).fetchall()
        scored = []
        for row in rows:
            score = _cosine(vector, _unpack(row["vector"]))
            if score > 0.05:
                scored.append((score, int(row["doc_id"])))
        scored.sort(reverse=True)
        return [doc_id for _, doc_id in scored[:max(limit * 6, 30)]], "hybrid"

    def search(self, query: str, *, kinds: Iterable[str] | None = None,
               limit: int = 8) -> list[dict]:
        if not self.enabled or not query.strip():
            return []
        limit = max(1, min(50, int(limit)))
        kind_list = [k for k in (kinds or []) if k in FACT_KINDS] or None
        keyword_ids = self._fts_search(query, kind_list, limit)
        vector_ids, mode = self._vector_search(query, kind_list, limit)
        if not vector_ids:
            mode = "fts-only"
        fused = reciprocal_rank_fusion([keyword_ids, vector_ids]) if vector_ids else [
            {"id": doc_id, "score": 1.0 / (60 + rank)}
            for rank, doc_id in enumerate(keyword_ids, 1)]
        out = []
        for item in fused[:limit]:
            row = self.db.execute(
                """SELECT id,kind,ref_id,title,body,source,source_url,trust,verified,created_at
                   FROM memory_documents WHERE id=?""", (item["id"],)).fetchone()
            if row:
                out.append({**dict(row), "rrf": item["score"], "mode": mode})
        return out

    # ------------------------------------------------------------------ diagnostics
    def unload_embedder(self) -> None:
        if self.embedder is not None:
            self.embedder.unload()

    def stats(self) -> dict:
        kinds = {row["kind"]: row["n"] for row in self.db.execute(
            "SELECT kind, COUNT(*) n FROM memory_documents GROUP BY kind")}
        vectors = self.db.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]
        meta = self.db.execute(
            "SELECT model,dim,count,updated_at FROM embedding_meta ORDER BY updated_at DESC LIMIT 1").fetchone()
        return {
            "documents": sum(kinds.values()), "kinds": kinds,
            "vectors": int(vectors), "embedding": dict(meta) if meta else None,
            "fts": self.has_fts, "embedding_enabled": self.embed_enabled,
            "embedding_model": self.embed_model,
        }

    def context(self, query: str, *, limit: int = 6) -> dict:
        """Provenance-aware context for a letter prompt."""
        facts = self.search(query, kinds=["candidate_fact"], limit=min(4, limit))
        letters = self.search(query, kinds=["letter"], limit=2)
        guides = self.search(query, kinds=["guideline"], limit=1)
        return {"facts": facts, "letters": letters, "guides": guides,
                "mode": (facts + letters + guides)[0]["mode"] if (facts or letters or guides) else "none"}


def context_prompt(context: dict) -> str:
    """Render retrieved memory with internal IDs; never include raw resume text."""
    lines = []
    for index, fact in enumerate(context.get("facts") or [], 1):
        lines.append(f"[F{index}] {fact['title']}: {fact['body']} (source: {fact['source']})")
    for index, item in enumerate(context.get("letters") or [], 1):
        lines.append(f"[L{index}] Похожее письмо: {item['body'][:400]}")
    for index, item in enumerate(context.get("guides") or [], 1):
        lines.append(f"[G{index}] {item['title']}: {item['body'][:300]} ({item['source_url']})")
    return "\n".join(lines)


def memory_from_db(db: sqlite3.Connection, settings: dict) -> Memory:
    return Memory(db, settings)
