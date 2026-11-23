"""Persistent candidate memory facade.

The authoritative structured data stays in the existing files:

* ``config/resume.json`` — contact, education, verified skills/projects/experience;
* ``config/profile.json`` — search preferences and confirmed profile facts;
* ``applications/events/messages`` — application history;
* Windows DPAPI files under ``data/`` — secrets.

This module is the single read/write path between those stores and the rest of
the application. It does not create a second profile database.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from . import db as dbm
from .memory import Memory
from .resume import load_resume, save_resume, sync_from_profile, verified_projects, verified_skills
from .secure_store import (AITUNNEL_SECRET, aitunnel_key_source, delete_secret,
                           load_secret, store_secret)
from .settings import load_profile, load_settings, save_profile

SMTP_SECRET = "smtp_password.bin"
ALLOWED_SECRETS = {"aitunnel": AITUNNEL_SECRET, "smtp": SMTP_SECRET}

CONTACT_KEYS = {
    "contact.first_name": "first_name",
    "contact.last_name": "last_name",
    "contact.middle_name": "middle_name",
    "contact.email": "email",
    "contact.phone": "phone",
    "contact.city": "city",
    "contact.telegram": "telegram",
    "contact.citizenship": "citizenship",
}
PREFERENCE_KEYS = {
    "preference.locations": ("locations", "list"),
    "preference.work_modes": ("work_modes", "list"),
    "preference.roles": ("role_keywords", "list"),
    "preference.employment": ("employment", "list"),
    "preference.excluded_titles": ("excluded_titles", "list"),
    "preference.salary": ("salary_target_rub", "number"),
    "preference.availability": ("availability", "text"),
    "preference.schedule": ("schedule", "text"),
    "preference.relocation": ("relocation", "bool"),
    "preference.business_trips": ("business_trips", "bool"),
}
PROFILE_SCHEMA = """
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in re.split(r"[,;]", str(value or "")) if x.strip()]


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in ("1", "true", "yes", "да", "on")


class CandidateMemory:
    def __init__(self, db: sqlite3.Connection, settings: dict | None = None,
                 memory: Memory | None = None):
        self.db = db
        self.settings = settings or load_settings()
        self.memory = memory or Memory(db, self.settings)
        self._migrate()
        self._seed_structured_facts()

    def _seed_structured_facts(self) -> None:
        """Mirror existing structured data once, without overwriting user edits."""
        try:
            resume = sync_from_profile(load_resume())
            profile = load_profile()
        except Exception:
            return
        active = self._active_facts()
        rows: list[tuple[str, str, str, str]] = []
        contact = resume.get("contact") or {}
        for key, field in CONTACT_KEYS.items():
            value = str(contact.get(field) or "").strip()
            if value and key not in active:
                rows.append((key, value, "config/resume.json", "contact"))
        prefs = profile.get("preferences") or {}
        for key, (field, kind) in PREFERENCE_KEYS.items():
            value = prefs.get(field)
            if kind == "list":
                value = ", ".join(str(x) for x in value or []) if isinstance(value, list) else value
            if value not in (None, "", []) and key not in active:
                rows.append((key, str(value), "config/profile.json", "preference"))
        for skill in verified_skills(resume):
            name = str(skill.get("name") or "").strip()
            key = f"skill.{name}"
            if name and key not in active:
                rows.append((key, name, "config/resume.json#skills", "skill"))
        for key, value, source, kind in rows:
            self.db.execute(
                """INSERT INTO candidate_facts(key,value,source,updated_at,verified,usable,kind,confirmed_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (key, value, source, _now(), 1, 1, kind, _now()))
        if rows:
            self.db.commit()

    def _migrate(self) -> None:
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(candidate_facts)")}
        if "kind" not in columns:
            self.db.execute("ALTER TABLE candidate_facts ADD COLUMN kind TEXT NOT NULL DEFAULT 'fact'")
        if "confirmed_at" not in columns:
            self.db.execute("ALTER TABLE candidate_facts ADD COLUMN confirmed_at TEXT")
        self.db.commit()

    # ------------------------------------------------------------------ structured
    def _active_facts(self, prefix: str = "") -> dict[str, dict]:
        sql = """SELECT key,value,source,updated_at,verified,usable,kind,confirmed_at
                 FROM candidate_facts WHERE usable=1 AND verified=1"""
        params: tuple = ()
        if prefix:
            sql += " AND key LIKE ?"
            params = (prefix + "%",)
        sql += " ORDER BY updated_at DESC, id DESC"
        out: dict[str, dict] = {}
        for row in self.db.execute(sql, params):
            key = str(row["key"])
            out.setdefault(key, dict(row))
        return out

    def contact(self) -> dict:
        resume = sync_from_profile(load_resume())
        contact = dict(resume.get("contact") or {})
        for key, field in CONTACT_KEYS.items():
            fact = self._active_facts("contact.").get(key)
            if fact and str(fact.get("value") or "").strip():
                contact[field] = str(fact["value"]).strip()
        return contact

    def resume(self) -> dict:
        resume = sync_from_profile(load_resume())
        resume["contact"] = self.contact()
        return resume

    def profile(self) -> dict:
        return load_profile()

    def snapshot(self) -> dict:
        resume = self.resume()
        profile = self.profile()
        prefs = dict(profile.get("preferences") or {})
        for key, (field, _kind) in PREFERENCE_KEYS.items():
            fact = self._active_facts("preference.").get(key)
            if fact:
                prefs[field] = fact["value"]
        return {
            "contact": resume.get("contact") or {},
            "education": resume.get("education") or [],
            "skills": [s.get("name") for s in verified_skills(resume)],
            "projects": verified_projects(resume),
            "experience": resume.get("experience") or [],
            "preferences": prefs,
            "resumes": self._resume_versions(),
        }

    def _resume_versions(self) -> list[dict]:
        rows = self.db.execute("SELECT key,value,updated_at FROM candidate_facts WHERE key LIKE 'resume.%' ORDER BY updated_at DESC").fetchall()
        out = []
        for row in rows:
            try:
                value = json.loads(row["value"])
            except (TypeError, ValueError):
                continue
            out.append({"id": value.get("id", ""), "title": value.get("title", ""),
                        "updated_at": row["updated_at"]})
        return out

    # ------------------------------------------------------------------ facts
    def get_fact(self, key: str) -> dict | None:
        key = str(key or "").strip()
        if not key:
            return None
        fact = self._active_facts().get(key)
        if fact:
            return fact
        if key in CONTACT_KEYS:
            value = self.contact().get(CONTACT_KEYS[key], "")
            return {"key": key, "value": value, "source": "config/resume.json",
                    "verified": 1, "usable": 1, "kind": "contact"} if value else None
        if key in PREFERENCE_KEYS:
            value = (self.profile().get("preferences") or {}).get(PREFERENCE_KEYS[key][0])
            return {"key": key, "value": value, "source": "config/profile.json",
                    "verified": 1, "usable": 1, "kind": "preference"} if value not in (None, "", []) else None
        return None

    def _delete_fact_docs(self, key: str, title: str = "") -> None:
        docs = self.db.execute(
            "SELECT id FROM memory_documents WHERE kind='candidate_fact' AND (ref_id=? OR title=?)",
            (key, title or key)).fetchall()
        for row in docs:
            self.db.execute("DELETE FROM memory_documents WHERE id=?", (row["id"],))
            if self.memory.has_fts:
                self.db.execute("DELETE FROM memory_fts WHERE rowid=?", (row["id"],))
            self.db.execute("DELETE FROM memory_vectors WHERE doc_id=?", (row["id"],))
        self.db.commit()

    def set_fact(self, key: str, value, *, source: str = "user", confirmed: bool = True,
                 kind: str = "fact", index: bool = False) -> dict:
        key = str(key or "").strip()
        if not key:
            raise ValueError("key is required")
        if not confirmed:
            raise ValueError("Only confirmed facts can enter candidate memory")
        if any(word in key.casefold() for word in ("token", "password", "api_key", "cookie", "secret")):
            raise ValueError("Secrets must use the secure vault")
        value_text = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value or "").strip()
        self.db.execute("UPDATE candidate_facts SET usable=0 WHERE key=? AND usable=1", (key,))
        self.db.execute(
            """INSERT INTO candidate_facts(key,value,source,updated_at,verified,usable,kind,confirmed_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (key, value_text, source[:120], _now(), 1, 1, kind[:40], _now()))
        self.db.commit()
        if key in CONTACT_KEYS:
            self._apply_contact(key, value_text)
        elif key in PREFERENCE_KEYS:
            self._apply_preference(key, value_text)
        elif key.startswith("skill."):
            self._apply_skill(key, value_text)
        if index or kind in ("skill", "project", "experience", "education", "preference", "decision"):
            self._delete_fact_docs(key)
            self.memory.add("candidate_fact", key, value_text or key, ref_id=key,
                            source=source, trust=1.0, verified=True, version="memory-v1")
        return self.get_fact(key) or {"key": key, "value": value_text, "source": source,
                                      "verified": 1, "usable": 1, "kind": kind}

    def update_fact(self, key: str, value, *, source: str = "user") -> dict:
        return self.set_fact(key, value, source=source, confirmed=True,
                             kind=(self.get_fact(key) or {}).get("kind", "fact"), index=True)

    def forget_fact(self, key: str) -> bool:
        key = str(key or "").strip()
        if not key:
            return False
        self.db.execute("UPDATE candidate_facts SET usable=0, verified=0 WHERE key=?", (key,))
        self._delete_fact_docs(key)
        self.db.execute("DELETE FROM verified_candidate_facts WHERE fact_id=? OR title=?", (key, key))
        self.db.commit()
        if key in CONTACT_KEYS:
            self._apply_contact(key, "")
        elif key in PREFERENCE_KEYS:
            self._apply_preference(key, "")
        elif key.startswith("skill."):
            name = key[6:].strip()
            self._remove_skill(name)
            self.sync(embed=False)
            self._delete_fact_docs(key, name)
        return True

    def facts(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute(
            """SELECT key,value,source,updated_at,verified,usable,kind,confirmed_at
               FROM candidate_facts WHERE usable=1 AND verified=1
                 AND key NOT LIKE 'contact.%'
               ORDER BY updated_at DESC, id DESC LIMIT ?""", (max(1, min(500, limit)),)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ structured writes
    def _apply_contact(self, key: str, value: str) -> None:
        field = CONTACT_KEYS.get(key)
        if not field:
            return
        resume = sync_from_profile(load_resume())
        contact = dict(resume.get("contact") or {})
        contact[field] = str(value or "").strip()
        resume["contact"] = contact
        save_resume(resume)

    def _apply_preference(self, key: str, value: str) -> None:
        field, kind = PREFERENCE_KEYS[key]
        profile = load_profile()
        prefs = dict(profile.get("preferences") or {})
        if kind == "list":
            prefs[field] = _as_list(value)
        elif kind == "number":
            try:
                prefs[field] = float(value) if str(value).strip() else None
            except ValueError:
                prefs[field] = None
        elif kind == "bool":
            prefs[field] = _as_bool(value)
        else:
            prefs[field] = str(value or "").strip()
        profile["preferences"] = prefs
        save_profile(profile)

    def _apply_skill(self, key: str, value: str) -> None:
        name = key[6:].strip() or value.strip()
        if not name:
            return
        profile = load_profile()
        skills = list(profile.get("verified_skills") or [])
        if name not in skills:
            skills.append(name)
        profile["verified_skills"] = skills
        save_profile(profile)
        resume = sync_from_profile(load_resume())
        if not any(str(s.get("name", "")).casefold() == name.casefold() for s in resume.get("skills") or []):
            resume.setdefault("skills", []).append({
                "name": name, "level": "", "evidence": "подтверждено пользователем",
                "verified": True,
            })
            save_resume(resume)

    def _remove_skill(self, name: str) -> None:
        if not name:
            return
        profile = load_profile()
        profile["verified_skills"] = [s for s in profile.get("verified_skills") or []
                                       if str(s).casefold() != name.casefold()]
        save_profile(profile)
        resume = sync_from_profile(load_resume())
        resume["skills"] = [s for s in resume.get("skills") or []
                            if str(s.get("name", "")).casefold() != name.casefold()]
        save_resume(resume)

    def update_contact(self, contact: dict) -> dict:
        for key, field in CONTACT_KEYS.items():
            if field in contact:
                self.set_fact(key, str(contact.get(field) or "").strip(), source="user",
                              kind="contact")
        return self.contact()

    # ------------------------------------------------------------------ RAG
    def search_memory(self, query: str, *, kinds: list[str] | None = None,
                      limit: int = 8) -> list[dict]:
        results = self.memory.search(query, kinds=kinds or ["candidate_fact"], limit=limit)
        return [r for r in results if r.get("kind") == "candidate_fact" or r.get("verified")]

    def sync(self, *, embed: bool = True) -> dict:
        resume = self.resume()
        profile = self.profile()
        from .resume import facts_hash, facts_pack
        facts = facts_pack(resume, profile)
        added = self.memory.sync_facts(resume, profile, facts_hash(facts))
        self.memory.sync_aliases(profile.get("skill_aliases") or {})
        embedded = {}
        if embed and self.memory.embed_enabled:
            embedded = self.memory.embed_pending(
                limit=int((self.settings.get("memory") or {}).get("max_docs_per_cycle", 40)))
        return {"facts": added, "embed": embedded, "stats": self.memory.stats()}

    def get_candidate_context(self, job_id: str | None = None) -> dict:
        snapshot = self.snapshot()
        query = " ".join(filter(None, [snapshot.get("contact", {}).get("city", ""),
                                       " ".join(snapshot.get("skills") or [])]))
        if job_id:
            job = dbm.get_job(self.db, job_id)
            if job:
                query = " ".join([str(job["title"] or ""), str(job["company"] or ""),
                                  " ".join(dbm.job_skills(job)), query])
        context = self.memory.context(query, limit=6) if query.strip() else {"facts": [], "letters": [], "guides": [], "mode": "none"}
        return {"contact": snapshot.get("contact") or {},
                "preferences": snapshot.get("preferences") or {},
                "facts": context.get("facts") or [],
                "memory_mode": context.get("mode", "none")}

    def get_application_context(self, application_id: int) -> dict:
        app = self.db.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
        if not app:
            return {}
        job = dbm.get_job(self.db, app["job_id"])
        events = [dict(r) for r in self.db.execute(
            "SELECT type,actor,payload,created_at FROM events WHERE application_id=? OR job_id=? ORDER BY id DESC LIMIT 20",
            (application_id, app["job_id"]))]
        messages = [dict(r) for r in self.db.execute(
            "SELECT direction,body,category,created_at FROM messages WHERE application_id=? ORDER BY id DESC LIMIT 20",
            (application_id,))]
        return {"application": dict(app), "job": dict(job) if job else {},
                "events": events, "messages": messages}

    # ------------------------------------------------------------------ integrations / secrets
    def get_integration_status(self) -> dict:
        from .submission import capabilities
        try:
            from .qwen_engine import QwenEngine
            qwen = QwenEngine(self.db, self.settings).status()
        except Exception:
            qwen = {}
        caps = capabilities(self.settings)
        return {
            "qwen": {"local": qwen.get("local", {}).get("available", False),
                     "cloud": qwen.get("cloud", {}).get("available", False),
                     "key_source": qwen.get("cloud", {}).get("key_source", "none")},
            "aitunnel_key": aitunnel_key_source(self.settings) != "none",
            "smtp": caps.get("email", {}).get("available", False),
            "hh": caps.get("official_api", {}).get("available", False),
            "telegram": caps.get("telegram_contact", {}).get("available", False),
        }

    def status(self) -> dict:
        resume = self.resume()
        contact = resume.get("contact") or {}
        missing = [label for key, label in (("first_name", "имя"), ("last_name", "фамилия"),
                                            ("email", "email"), ("phone", "телефон"))
                   if not str(contact.get(key) or "").strip()]
        return {
            "contact_filled": not missing, "missing_contact": missing,
            "education": len(resume.get("education") or []),
            "skills": len(verified_skills(resume)),
            "projects": len(verified_projects(resume)),
            "preferences": bool((self.profile().get("preferences") or {}).get("work_modes")),
            "facts": len(self.facts(500)),
            "memory": self.memory.stats(),
            "integrations": self.get_integration_status(),
        }

    def get_secret(self, name: str) -> str:
        key = ALLOWED_SECRETS.get(str(name))
        if not key:
            raise ValueError("Unknown secret")
        return load_secret(key)

    def set_secret(self, name: str, value: str) -> bool:
        key = ALLOWED_SECRETS.get(str(name))
        if not key:
            raise ValueError("Unknown secret")
        store_secret(key, value)
        return True

    def forget_secret(self, name: str) -> bool:
        key = ALLOWED_SECRETS.get(str(name))
        if not key:
            raise ValueError("Unknown secret")
        return delete_secret(key)


def persistent_memory(db: sqlite3.Connection, settings: dict | None = None) -> CandidateMemory:
    return CandidateMemory(db, settings)
