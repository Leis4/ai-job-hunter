"""Resume import and continuous skill learning.

This is an extension of ``CandidateMemory``: it uses the same SQLite, the same
FTS/vector memory and the same structured files. Imported resume facts are
proposed, never silently applied; confirmation merges them without deleting
existing skills/projects.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .candidate_memory import CandidateMemory
from .resume import load_resume, save_resume, verified_projects, verified_skills
from .settings import load_profile, save_profile
from .text import canonical_skill

SCHEMA = """
CREATE TABLE IF NOT EXISTS resume_imports (
  id TEXT PRIMARY KEY,
  source_name TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  source_path TEXT,
  imported_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  method TEXT,
  items TEXT NOT NULL DEFAULT '[]',
  same_count INTEGER NOT NULL DEFAULT 0,
  applied_at TEXT
);
CREATE TABLE IF NOT EXISTS learning_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  description TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'user',
  skills TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'confirmed',
  confirmed INTEGER NOT NULL DEFAULT 1
);
"""

STATUS_LABELS = {
    "learning": "Изучаю",
    "basic": "Базовые знания",
    "practical": "Практический опыт",
    "confident": "Уверенно владею",
    "unknown": "Уровень не указан",
}
LEVEL_RANK = {"learning": 1, "basic": 2, "practical": 3, "confident": 4, "unknown": 0}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


class ResumeMemory:
    def __init__(self, db: sqlite3.Connection, settings: dict, memory: CandidateMemory | None = None):
        self.db = db
        self.settings = settings
        self.cm = memory or CandidateMemory(db, settings)
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ------------------------------------------------------------------ import
    def _extract(self, path: Path, *, use_qwen: bool) -> dict:
        suffix = path.suffix.lower()
        if suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("JSON-резюме должно быть объектом")
            return payload
        from .onboarding import extract_text
        text = extract_text(path)
        if not text.strip():
            raise ValueError("В файле не найден текст (скан без OCR не поддерживается)")
        if use_qwen:
            try:
                from .qwen_engine import QwenEngine
                engine = QwenEngine(self.db, self.settings)
                available, _reason = engine.local_available()
                if available:
                    current = self.cm.snapshot()
                    parsed = engine.analyze_resume(text, {
                        "skills": current.get("skills") or [],
                        "projects": current.get("projects") or [],
                    })
                    if parsed.get("skills") or parsed.get("projects") or parsed.get("experience"):
                        parsed["method"] = f"qwen:{engine.local_model}"
                        return parsed
            except Exception:
                pass
        from .onboarding import extract_facts
        return extract_facts(text, source_name=path.name, method="local")

    def _current(self) -> tuple[dict, dict]:
        resume = self.cm.resume()
        profile = self.cm.profile()
        return resume, profile

    @staticmethod
    def _project_key(item: dict) -> str:
        return _norm(item.get("name") or item.get("title") or "")

    @staticmethod
    def _education_key(item: dict) -> str:
        return _norm(item.get("institution") or item.get("school") or "")

    @staticmethod
    def _experience_key(item: dict) -> str:
        return _norm(f"{item.get('company','')}|{item.get('position','')}")

    def _diff(self, extracted: dict) -> tuple[list[dict], int]:
        resume, profile = self._current()
        old_skills: dict[str, dict] = {}
        for item in list(resume.get("skills") or []) + list(profile.get("verified_skills") or []):
            if isinstance(item, dict):
                name = str(item.get("name") or "")
                old_skills[canonical_skill(name).casefold()] = item
            elif str(item).strip():
                old_skills[canonical_skill(str(item)).casefold()] = {"name": str(item), "level": ""}
        old_projects = {self._project_key(p): p for p in resume.get("projects") or [] if isinstance(p, dict)}
        old_education = {self._education_key(e): e for e in resume.get("education") or [] if isinstance(e, dict)}
        old_experience = {self._experience_key(e): e for e in resume.get("experience") or [] if isinstance(e, dict)}

        items: list[dict] = []
        same = 0

        def add(kind: str, key: str, label: str, old, new, *, level_old: str = "", level_new: str = ""):
            nonlocal same
            if old is None:
                status = "new"
            elif level_new and level_old and level_new != level_old:
                status = "conflict" if LEVEL_RANK.get(level_new, 0) < LEVEL_RANK.get(level_old, 0) else "changed"
            elif level_new and not level_old:
                status = "changed"
            elif old != new:
                status = "changed"
            else:
                same += 1
                return
            items.append({"key": f"{kind}:{key}", "kind": kind, "label": label,
                          "old": old, "new": new, "status": status,
                          "level_old": level_old, "level_new": level_new})

        for skill in extracted.get("skills") or []:
            if not isinstance(skill, dict):
                continue
            name = str(skill.get("name") or "").strip()
            if not name:
                continue
            canonical = canonical_skill(name).casefold()
            old = old_skills.get(canonical)
            level_new = str(skill.get("level") or skill.get("status") or "unknown").casefold()
            if level_new == "unknown":
                level_new = ""
            level_old = str((old or {}).get("level") or "unknown").casefold()
            add("skill", canonical, name, old.get("name") if old else None, name,
                level_old=level_old if old else "", level_new=level_new)

        for project in extracted.get("projects") or []:
            if not isinstance(project, dict):
                continue
            key = self._project_key(project)
            if not key:
                continue
            old = old_projects.get(key)
            add("project", key, project.get("name") or project.get("title") or key,
                old, project)

        for item in extracted.get("education") or []:
            if not isinstance(item, dict):
                continue
            key = self._education_key(item)
            if key:
                add("education", key, item.get("institution") or key,
                    old_education.get(key), item)

        for item in extracted.get("experience") or []:
            if not isinstance(item, dict):
                continue
            key = self._experience_key(item)
            if key.strip("|"):
                add("experience", key, f"{item.get('company','')} — {item.get('position','')}".strip(" —"),
                    old_experience.get(key), item)

        contact = self.cm.contact()
        for field in ("first_name", "last_name", "email", "phone", "city"):
            new = str((extracted.get("contact") or {}).get(field) or "").strip()
            old = str(contact.get(field) or "").strip()
            if new and new != old:
                add("contact", field, field, old or None, new)
        return items, same

    def import_file(self, path: Path, *, use_qwen: bool = True) -> dict:
        path = Path(path)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        existing = self.db.execute("SELECT * FROM resume_imports WHERE id=?", (digest,)).fetchone()
        if existing:
            return {"ok": True, "duplicate": True, "status": existing["status"],
                    "import_id": digest, "items": json.loads(existing["items"] or "[]"),
                    "same_count": existing["same_count"],
                    "reason": "Этот файл уже импортирован"}
        extracted = self._extract(path, use_qwen=use_qwen)
        items, same = self._diff(extracted)
        self.db.execute(
            """INSERT OR REPLACE INTO resume_imports(id,source_name,source_hash,source_path,
                 imported_at,status,method,items,same_count)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (digest, path.name, digest, str(path), _now(), "pending",
             str(extracted.get("method") or "local"), json.dumps(items, ensure_ascii=False), same))
        self.db.commit()
        return {"ok": True, "duplicate": False, "import_id": digest, "status": "pending",
                "items": items, "same_count": same, "method": extracted.get("method") or "local"}

    def confirm_import(self, import_id: str, selected: Iterable[str]) -> dict:
        row = self.db.execute("SELECT * FROM resume_imports WHERE id=?", (import_id,)).fetchone()
        if not row:
            raise ValueError("Импорт не найден")
        if row["status"] == "confirmed":
            return {"ok": True, "already": True, "import_id": import_id}
        items = [x for x in json.loads(row["items"] or "[]") if x.get("key") in set(selected)]
        resume, profile = self._current()
        skills = list(resume.get("skills") or [])
        projects = list(resume.get("projects") or [])
        education = list(resume.get("education") or [])
        experience = list(resume.get("experience") or [])
        profile_skills = list(profile.get("verified_skills") or [])
        profile_projects = list(profile.get("verified_projects") or [])
        contact_updates = {}

        for item in items:
            kind, key = item["kind"], item["key"].split(":", 1)[-1]
            if kind == "skill":
                name = str(item.get("new") or item.get("label") or "").strip()
                found = next((s for s in skills if str(s.get("name", "")).casefold() == name.casefold()), None)
                if found is None:
                    found = {"name": name, "level": item.get("level_new") or "unknown",
                             "evidence": "Импортировано из резюме", "verified": True}
                    skills.append(found)
                else:
                    found["level"] = item.get("level_new") or found.get("level") or "unknown"
                    found["verified"] = True
                if name not in profile_skills:
                    profile_skills.append(name)
            elif kind == "project":
                data = item.get("new") if isinstance(item.get("new"), dict) else {}
                name = str(data.get("name") or item.get("label") or "").strip()
                if name and not any(str(p.get("name", "")).casefold() == name.casefold() for p in projects):
                    projects.append({**data, "verified": True})
                    if not any(str(p.get("name", "")).casefold() == name.casefold() for p in profile_projects):
                        profile_projects.append(dict(data))
            elif kind == "education":
                data = item.get("new") if isinstance(item.get("new"), dict) else {}
                if data.get("institution") and not any(
                        str(e.get("institution", "")).casefold() == str(data["institution"]).casefold()
                        for e in education):
                    education.append({**data, "verified": True})
            elif kind == "experience":
                data = item.get("new") if isinstance(item.get("new"), dict) else {}
                signature = (data.get("company"), data.get("position"))
                if any((e.get("company"), e.get("position")) == signature for e in experience):
                    continue
                experience.append({**data, "verified": True})
            elif kind == "contact":
                contact_updates[key] = item.get("new") or ""

        resume["skills"] = skills
        resume["projects"] = projects
        resume["education"] = education
        resume["experience"] = experience
        save_resume(resume)
        profile["verified_skills"] = profile_skills
        profile["verified_projects"] = profile_projects
        save_profile(profile)
        if contact_updates:
            self.cm.update_contact(contact_updates)
        new_skills = [i.get("label") for i in items if i.get("kind") == "skill"]
        if new_skills:
            self.add_learning(
                "Импортированы навыки: " + ", ".join(new_skills[:10]),
                source=f"resume:{row['source_name']}", skills=new_skills, status="imported")
        self.db.execute("UPDATE resume_imports SET status='confirmed', applied_at=? WHERE id=?",
                        (_now(), import_id))
        self.db.commit()
        self.cm.sync(embed=True)
        return {"ok": True, "import_id": import_id, "applied": len(items),
                "skills": len(skills), "projects": len(projects)}

    # ------------------------------------------------------------------ continuous skills
    @staticmethod
    def _status_from_text(text: str) -> str:
        low = (text or "").casefold()
        if re.search(r"начал\w*\s+изуча|только начал|учу\b", low):
            return "learning"
        if re.search(r"базов|основы|начальн|знаком", low):
            return "basic"
        if re.search(r"практич|реализовал|использовал|проект|опыт", low):
            return "practical"
        if re.search(r"уверен|свободно|владею|освоил|отлично", low):
            return "confident"
        return "unknown"

    def propose_skill(self, text: str) -> dict:
        from .text import extract_skills
        found = list(extract_skills(text or "").keys())
        if not found:
            import re
            match = re.search(r"(?:освоил|изуча|владею|работаю с|использую)\s+([A-Za-zА-Яа-я0-9.+#_-]{2,40})", text or "", re.I)
            name = match.group(1) if match else ""
        else:
            name = found[0]
        if not name:
            return {"ok": False, "reason": "Не удалось определить навык"}
        status = self._status_from_text(text)
        existing = self._skill_by_name(name)
        return {"ok": True, "skill": name, "status": status,
                "status_label": STATUS_LABELS.get(status, status),
                "current": existing.get("level") if existing else "",
                "evidence": text[:300], "requires_confirmation": True}

    def _skill_by_name(self, name: str) -> dict | None:
        for item in self.cm.resume().get("skills") or []:
            if str(item.get("name", "")).casefold() == str(name).casefold():
                return item
        return None

    def apply_skill(self, name: str, status: str, *, evidence: str = "", source: str = "user") -> dict:
        name = str(name or "").strip()
        if not name:
            raise ValueError("Нужно название навыка")
        status = status if status in STATUS_LABELS else "unknown"
        resume = self.cm.resume()
        skills = list(resume.get("skills") or [])
        found = next((s for s in skills if str(s.get("name", "")).casefold() == name.casefold()), None)
        if found is None:
            found = {"name": name, "level": status, "evidence": evidence or "Добавлено пользователем",
                     "verified": True}
            skills.append(found)
        else:
            found["level"] = status
            found["verified"] = True
            if evidence:
                found["evidence"] = evidence
        resume["skills"] = skills
        save_resume(resume)
        profile = self.cm.profile()
        profile["verified_skills"] = sorted(set(profile.get("verified_skills") or []) | {name})
        save_profile(profile)
        self.cm.set_fact(f"skill.{name}", f"{name}: {STATUS_LABELS[status]}",
                         source=source, kind="skill", index=True)
        if self.cm.memory.embed_enabled:
            self.cm.memory.embed_pending(limit=10)
        self.add_learning(f"{name}: {STATUS_LABELS[status]}", source=source,
                          skills=[name], status=status, description_extra=evidence)
        return {"ok": True, "skill": name, "status": status,
                "status_label": STATUS_LABELS[status]}

    def add_project(self, project: dict, *, source: str = "user") -> dict:
        name = str(project.get("name") or "").strip()
        if not name:
            raise ValueError("Нужно название проекта")
        resume = self.cm.resume()
        projects = list(resume.get("projects") or [])
        if not any(str(p.get("name", "")).casefold() == name.casefold() for p in projects):
            projects.append({**project, "verified": True})
            resume["projects"] = projects
            save_resume(resume)
            profile = self.cm.profile()
            profile["verified_projects"] = list(profile.get("verified_projects") or []) + [project]
            save_profile(profile)
        self.cm.set_fact(f"project.{name}", json.dumps(project, ensure_ascii=False),
                         source=source, kind="project", index=True)
        if self.cm.memory.embed_enabled:
            self.cm.memory.embed_pending(limit=10)
        self.add_learning(f"Проект: {name}", source=source, skills=list(project.get("stack") or []),
                          status="project", description_extra=str(project.get("description") or "")[:500])
        return {"ok": True, "project": name}

    # ------------------------------------------------------------------ learning history
    def add_learning(self, description: str, *, source: str = "user",
                     skills: list[str] | None = None, status: str = "confirmed",
                     description_extra: str = "") -> int:
        body = str(description or "").strip()
        if description_extra:
            body += " " + str(description_extra).strip()
        cur = self.db.execute(
            """INSERT INTO learning_history(created_at,description,source,skills,status,confirmed)
               VALUES (?,?,?,?,?,1)""",
            (_now(), body, source, json.dumps(skills or [], ensure_ascii=False), status))
        self.db.commit()
        return int(cur.lastrowid)

    def history(self, limit: int = 40) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM learning_history ORDER BY id DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------------ resume refresh
    def preview_resume_update(self) -> dict:
        resume = self.cm.resume()
        existing = {str(s.get("name", "")).casefold() for s in resume.get("skills") or []}
        skills = [s for s in self.cm.resume().get("skills") or [] if str(s.get("name", "")).casefold() not in existing]
        # Facts can add skills that are not yet in resume.
        for fact in self.cm.facts(200):
            if fact.get("key", "").startswith("skill."):
                name = fact["key"][6:]
                if name.casefold() not in existing:
                    skills.append({"name": name, "level": "unknown", "evidence": "Подтверждённая память",
                                   "verified": True})
                    existing.add(name.casefold())
        existing_projects = {str(p.get("name", "")).casefold() for p in resume.get("projects") or []}
        projects = []
        for fact in self.cm.facts(200):
            if fact.get("key", "").startswith("project."):
                try:
                    data = json.loads(fact.get("value") or "{}")
                except ValueError:
                    data = {}
                name = str(data.get("name") or fact["key"][8:])
                if name.casefold() not in existing_projects and name.strip():
                    projects.append(data)
                    existing_projects.add(name.casefold())
        return {"skills": skills, "projects": projects, "requires_confirmation": True}

    def apply_resume_update(self, skips_skills: Iterable[str] = (), skips_projects: Iterable[str] = (),
                            variant: str = "data") -> dict:
        preview = self.preview_resume_update()
        skip_s = {str(x).casefold() for x in skips_skills}
        skip_p = {str(x).casefold() for x in skips_projects}
        resume = self.cm.resume()
        skills = list(resume.get("skills") or [])
        for item in preview["skills"]:
            if str(item.get("name", "")).casefold() not in skip_s:
                skills.append({**item, "verified": True})
        projects = list(resume.get("projects") or [])
        for item in preview["projects"]:
            if str(item.get("name", "")).casefold() not in skip_p:
                projects.append({**item, "verified": True})
        resume["skills"], resume["projects"] = skills, projects
        save_resume(resume)
        profile = self.cm.profile()
        profile["verified_skills"] = sorted({str(s.get("name", "")) for s in skills if s.get("name")})
        profile["verified_projects"] = projects
        save_profile(profile)
        self.cm.sync(embed=True)
        exports = {}
        try:
            from .resume import export_variant
            exports = export_variant(resume, variant, formats=["pdf", "docx"])
        except Exception as exc:
            exports = {"error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "skills": len(skills), "projects": len(projects), "exports": exports}
