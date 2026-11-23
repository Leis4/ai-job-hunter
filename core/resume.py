"""Resume Studio: one verified-facts base, derived variants, DOCX/PDF export.

Truth policy: nothing is marked verified automatically. Imported text is
``verified: false`` until the user confirms each item in the dashboard.
Generated variants may reorder and emphasise facts, never invent them.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import db as dbm
from .paths import CONFIG, OUTPUT, RESUME_FILES, ensure_dirs
from .settings import load_profile, save_profile
from .text import clean_text, normalize

RESUME_PATH = CONFIG / "resume.json"

DEFAULT_RESUME: dict[str, Any] = {
    "schema": 1,
    "status": "draft",
    "contact": {"first_name": "", "last_name": "", "middle_name": "", "email": "", "phone": "",
                "city": "", "citizenship": "", "relocation": False},
    "headline": "",
    "summary": "",
    "target_roles": [],
    "total_experience_months": 0,
    "skills": [],
    "experience": [],
    "education": [],
    "projects": [],
    "languages": [],
    "certificates": [],
    "courses": [],
    "constraints": {"work_modes": [], "locations": [], "salary_target_rub": None, "availability": ""},
    "updated_at": "",
}

VARIANTS: dict[str, dict] = {
    "python": {
        "label": "Python / Backend",
        "roles": ["Python-разработчик", "Backend-разработчик", "Разработчик Python"],
        "skills": ["Python", "FastAPI", "Django", "Flask", "asyncio", "PostgreSQL", "SQLite", "Docker",
                   "REST", "Git", "Linux", "Bash", "Celery", "Redis", "Testing"],
        "summary": "Python-разработка: сервисы, автоматизация, работа с данными и API.",
    },
    "data": {
        "label": "Data / Analytics",
        "roles": ["Аналитик данных", "Data Analyst", "Data Engineer", "BI-аналитик"],
        "skills": ["SQL", "Python", "Pandas", "NumPy", "PostgreSQL", "Excel", "Power BI", "Tableau",
                   "Statistics", "A/B testing", "ETL", "Airflow", "Data Warehouse", "Data Quality", "BI"],
        "summary": "Аналитика данных: SQL, Python, метрики, отчётность и проверка качества данных.",
    },
    "ai": {
        "label": "AI / ML",
        "roles": ["ML-инженер", "AI-инженер", "LLM-инженер", "Data Scientist"],
        "skills": ["Python", "ML", "LLM", "RAG", "NLP", "scikit-learn", "PyTorch", "Pandas", "NumPy",
                   "Deep Learning", "MLOps", "Statistics", "Optimization"],
        "summary": "Прикладные ML/LLM-задачи: подготовка данных, оценка качества, интеграция моделей.",
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = json.loads(json.dumps(base, ensure_ascii=False))
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_resume() -> dict:
    ensure_dirs()
    if RESUME_PATH.exists():
        try:
            return _merge(DEFAULT_RESUME, json.loads(RESUME_PATH.read_text(encoding="utf-8-sig")))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"config/resume.json повреждён: {exc}") from exc
    return json.loads(json.dumps(DEFAULT_RESUME, ensure_ascii=False))


def save_resume(resume: dict) -> None:
    ensure_dirs()
    resume = dict(resume)
    resume["updated_at"] = date.today().isoformat()
    payload = json.dumps(resume, ensure_ascii=False, indent=2) + "\n"
    previous = RESUME_PATH.read_text(encoding="utf-8") if RESUME_PATH.exists() else ""
    RESUME_PATH.write_text(payload, encoding="utf-8")
    if previous and previous != payload:
        try:
            history = OUTPUT / "resume_versions"
            history.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            (history / f"resume-{stamp}.json").write_text(previous, encoding="utf-8")
            versions = sorted(history.glob("resume-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in versions[20:]:
                old.unlink()
        except OSError:
            pass


def list_resume_versions(limit: int = 8) -> list[dict]:
    history = OUTPUT / "resume_versions"
    if not history.exists():
        return []
    out = []
    for path in sorted(history.glob("resume-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        out.append({"name": path.name, "created": datetime.fromtimestamp(path.stat().st_mtime).strftime("%d.%m %H:%M"),
                    "size": path.stat().st_size})
    return out


def restore_version(name: str) -> dict:
    history = (OUTPUT / "resume_versions").resolve()
    path = (history / Path(name).name).resolve()
    if path.parent != history or not path.exists():
        raise ValueError("Версия не найдена")
    payload = json.loads(path.read_text(encoding="utf-8"))
    save_resume(payload)
    return payload


def sync_from_profile(resume: dict | None = None) -> dict:
    """Profile verified_skills are a valid fact source; they are added as verified."""
    resume = resume or load_resume()
    profile = load_profile()
    names = {str(s.get("name", "")).casefold() for s in resume.get("skills") or [] if isinstance(s, dict)}
    for skill in profile.get("verified_skills") or []:
        if str(skill).casefold() not in names:
            resume.setdefault("skills", []).append(
                {"name": str(skill), "level": "", "evidence": "Указано в профиле как подтверждённое", "verified": True}
            )
    existing_projects = {str(p.get("name", "")).casefold() for p in resume.get("projects") or [] if isinstance(p, dict)}
    for project in profile.get("verified_projects") or []:
        if not isinstance(project, dict):
            continue
        name = str(project.get("name", "")).casefold()
        if not name:
            continue
        duplicate = any(name == existing or name in existing or existing in name
                        for existing in existing_projects if existing)
        if not duplicate:
            item = dict(project)
            item.setdefault("verified", True)
            resume.setdefault("projects", []).append(item)
            existing_projects.add(name)
    if profile.get("education_confirmed") and not resume.get("education"):
        resume["education"] = [{
            "institution": str(profile["education_confirmed"]), "degree": "", "field": "",
            "start": "", "end": "", "verified": True,
        }]
    return resume


def verified_skills(resume: dict) -> list[dict]:
    return [s for s in resume.get("skills") or [] if isinstance(s, dict) and s.get("verified") and s.get("name")]


def verified_projects(resume: dict) -> list[dict]:
    return [p for p in resume.get("projects") or [] if isinstance(p, dict) and p.get("verified") and p.get("name")]


def facts_pack(resume: dict, profile: dict, *, max_projects: int = 3) -> dict:
    """Minimal, verified-only payload for the LLM."""
    skills = [s["name"] for s in verified_skills(resume)] or list(profile.get("verified_skills") or [])
    months = int(resume.get("total_experience_months") or 0)
    education = [e for e in resume.get("education") or [] if isinstance(e, dict) and e.get("verified")]
    projects = verified_projects(resume)[:max_projects]
    return {
        "headline": resume.get("headline", ""),
        "total_experience_months": months,
        "verified_skills": skills[:30],
        "skill_levels": {s["name"]: (s.get("level") or "unknown")
                         for s in verified_skills(resume)},
        "education": [{"institution": e.get("institution", ""), "degree": e.get("degree", ""),
                       "field": e.get("field", ""), "end": e.get("end", "")} for e in education],
        "projects": [{"name": p.get("name", ""), "role": p.get("role", ""),
                      "stack": p.get("stack", []), "description": p.get("description", "")[:400]}
                     for p in projects],
        "availability": (resume.get("constraints") or {}).get("availability")
                        or (profile.get("preferences") or {}).get("availability", ""),
    }


def facts_hash(facts: dict) -> str:
    return hashlib.sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- import

def import_resume(path: Path, *, source_name: str = "") -> dict:
    path = Path(path)
    text = ""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        text = _docx_text(path)
    elif suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("Для PDF установите pypdf: py -3 -m pip install pypdf") from exc
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    elif suffix in (".txt", ".md", ".markdown"):
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    elif suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return _merge(DEFAULT_RESUME, payload) if isinstance(payload, dict) else DEFAULT_RESUME
    else:
        raise ValueError("Поддерживаются .pdf, .docx, .txt, .md и .json.")
    return parse_resume_text(text, source_name=path.name)


def _docx_text(path: Path) -> str:
    try:
        import docx  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Для .docx установите python-docx: py -3 -m pip install python-docx") from exc
    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


SECTION_ALIASES = {
    "experience": ("опыт работы", "опыт", "experience", "трудовой стаж", "работа"),
    "education": ("образование", "education", "учёба", "учеба"),
    "skills": ("навыки", "ключевые навыки", "skills", "технологии", "компетенции"),
    "projects": ("проекты", "projects", "portfolio", "портфолио"),
    "languages": ("языки", "languages"),
    "certificates": ("сертификаты", "certificates", "сертификаты и курсы"),
    "courses": ("курсы", "courses", "обучение"),
}


def parse_resume_text(text: str, *, source_name: str = "") -> dict:
    resume = json.loads(json.dumps(DEFAULT_RESUME, ensure_ascii=False))
    resume["import_meta"] = {"source": source_name, "imported_at": date.today().isoformat(),
                             "note": "Все импортированные факты помечены verified=false до ручного подтверждения."}
    lines = [clean_text(line) for line in text.replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line]
    current = ""
    buckets: dict[str, list[str]] = {}
    for line in lines:
        low = line.strip(" :•-—").casefold()
        matched = None
        for section, aliases in SECTION_ALIASES.items():
            if any(low == alias or low.startswith(alias + ":") or low.startswith(alias + " ") for alias in aliases):
                if len(line) < 60:
                    matched = section
                    break
        if matched:
            current = matched
            buckets.setdefault(current, [])
            remainder = re.sub(r"^[^:]{2,40}:\s*", "", line).strip()
            if remainder and remainder != line:
                buckets[current].append(remainder)
            continue
        if current:
            buckets.setdefault(current, []).append(line)
        elif not resume["headline"] and len(line) < 120 and not re.search(r"@|\+7|телефон", line):
            resume["headline"] = line
    contact = _extract_contacts(text)
    resume["contact"].update(contact)
    if buckets.get("skills"):
        seen = set()
        for raw in buckets["skills"]:
            for part in re.split(r"[,;|•·]", raw):
                name = part.strip(" .-")
                if not name or len(name) > 40:
                    continue
                key = name.casefold()
                if key in seen:
                    continue
                seen.add(key)
                resume["skills"].append({"name": name, "level": "", "evidence": "", "verified": False})
    for section, key in (("education", "education"), ("experience", "experience"),
                         ("projects", "projects"), ("languages", "languages"),
                         ("certificates", "certificates"), ("courses", "courses")):
        entries = buckets.get(section) or []
        if not entries:
            continue
        if section == "languages":
            resume["languages"] = [{"name": e, "level": "", "verified": False} for e in entries][:10]
        elif section == "certificates":
            resume["certificates"] = [{"name": e, "issuer": "", "year": "", "verified": False} for e in entries][:20]
        elif section == "courses":
            resume["courses"] = [{"name": e, "provider": "", "year": "", "verified": False} for e in entries][:20]
        elif section == "skills":
            continue
        elif section == "projects":
            resume["projects"] = _projects_from_lines(entries)
        else:
            resume[key] = _entries_from_lines(entries, kind=key)
    # Experience is counted from the experience section only; education years
    # must never inflate professional experience.
    total_months = 0
    for entry in resume.get("experience") or []:
        start = re.search(r"(19|20)\d{2}", str(entry.get("start") or ""))
        end_raw = str(entry.get("end") or "")
        end = re.search(r"(19|20)\d{2}", end_raw)
        if not start:
            continue
        start_year = int(start.group(0))
        end_year = int(end.group(0)) if end else (date.today().year if end_raw else start_year)
        total_months += max(0, min(50, end_year - start_year)) * 12
    if total_months:
        resume["total_experience_months"] = total_months
    return resume


def _entries_from_lines(lines: list[str], *, kind: str) -> list[dict]:
    entries: list[dict] = []
    for line in lines:
        line = line.strip(" •-—\t")
        if not line:
            continue
        years = re.findall(r"(?:19|20)\d{2}", line)
        parts = [p.strip() for p in re.split(r"\s*\|\s*|\s+[—–]\s+|\s+-\s+", line) if p.strip()]
        parts = [p for p in parts if not re.fullmatch(r"(?:19|20)\d{2}(?:\s*[—–-]\s*(?:19|20)\d{2})?", p)]
        if kind == "education":
            if years or not entries:
                entries.append({"institution": (parts[0] if parts else line)[:160], "degree": "", "field": "",
                                "start": years[0] if years else "", "end": years[-1] if len(years) > 1 else "",
                                "verified": False})
            else:
                last = entries[-1]
                last["field"] = (last["field"] + " " + line).strip()[:160]
        elif kind == "experience":
            header = bool(years) or bool(re.search(r"\s[—–]\s", line)) or "|" in line
            if entries and not header:
                last = entries[-1]
                last["description"] = (last["description"] + " " + line).strip()[:1500]
            else:
                entries.append({"company": (parts[0] if parts else line)[:120],
                                "position": (parts[1] if len(parts) > 1 else "")[:120],
                                "start": years[0] if years else "", "end": years[-1] if len(years) > 1 else "",
                                "description": "", "achievements": [], "verified": False})
        elif kind == "projects":
            entries.append({"name": line[:120], "role": "", "stack": [], "description": "", "url": "",
                            "verified": False})
    return entries


def _projects_from_lines(lines: list[str]) -> list[dict]:
    """Group 'Название — стек' lines with the following description lines."""
    projects: list[dict] = []
    for raw in lines:
        line = raw.strip(" •-—\t")
        if not line:
            continue
        parts = re.split(r"\s+[—–-]\s+", line, maxsplit=1)
        if len(parts) == 2 and len(parts[1]) <= 80:
            name, rest = parts
            stack = [x.strip() for x in re.split(r"[,;/]", rest) if 0 < len(x.strip()) <= 40]
            projects.append({"name": name[:120], "role": "", "stack": stack[:12],
                             "description": "", "url": "", "verified": False})
        elif projects and (len(line) > 45 or line.endswith(".")):
            projects[-1]["description"] = (projects[-1]["description"] + " " + line).strip()[:600]
        else:
            projects.append({"name": line[:120], "role": "", "stack": [], "description": "", "url": "",
                             "verified": False})
    return projects[:12]


def _extract_contacts(text: str) -> dict:
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    phone = re.search(r"\+7[\s()\d-]{9,20}", text)
    city = ""
    for candidate in ("Москва", "Санкт-Петербург", "Тестоград", "Новосибирск", "Екатеринбург"):
        if candidate.casefold() in text.casefold():
            city = candidate
            break
    return {"email": email.group(0) if email else "", "phone": phone.group(0).strip() if phone else "", "city": city}


def confirm_items(resume: dict, section: str, indexes: list[int]) -> dict:
    items = resume.get(section) or []
    for index in indexes:
        if 0 <= index < len(items) and isinstance(items[index], dict):
            items[index]["verified"] = True
    if section == "skills":
        profile = load_profile()
        names = [s["name"] for s in items if isinstance(s, dict) and s.get("verified")]
        profile["verified_skills"] = sorted({*profile.get("verified_skills", []), *names})
        save_profile(profile)
    return resume


# --------------------------------------------------------------------------- variants

def select_variant(resume: dict, job: dict, details: dict | None = None) -> str:
    text = f"{job.get('title','')} {job.get('description','')}".casefold()
    scores = {}
    for name, variant in VARIANTS.items():
        scores[name] = sum(1 for skill in variant["skills"] if skill.casefold() in text)
    details = details or {}
    for skill in (details.get("skills") or {}).get("matched", []):
        for name, variant in VARIANTS.items():
            if skill in variant["skills"]:
                scores[name] += 2
    # The vacancy title is a stronger signal than a shared generic skill (Python/SQL).
    title = normalize(str(job.get("title") or ""))
    for name, markers in {
        "data": ("аналитик", "analyst", "data", "bi ", "витрин", "dwh"),
        "ai": ("ml", "machine learning", "ai ", "ai-", "llm", "nlp", "data scientist", "нейро"),
        "python": ("python", "backend", "разработчик", "developer"),
    }.items():
        if any(marker in title for marker in markers):
            scores[name] = scores.get(name, 0) + 4
    best = max(scores, key=scores.get) if scores else "python"
    return best if scores.get(best, 0) > 0 else "python"


def variant_view(resume: dict, variant: str, job: dict | None = None,
                 details: dict | None = None) -> dict:
    definition = VARIANTS.get(variant) or VARIANTS["python"]
    preferred = [s.casefold() for s in definition["skills"]]
    skills = verified_skills(resume)
    ordered = sorted(skills, key=lambda s: (0 if s["name"].casefold() in preferred else 1,
                                            preferred.index(s["name"].casefold()) if s["name"].casefold() in preferred else 99))
    projects = verified_projects(resume)
    job_skills = []
    if job is not None:
        try:
            job_skills = dbm.job_skills(job)
        except Exception:
            job_skills = []
    matched = [m for m in ((details or {}).get("skills") or {}).get("matched", []) if m]
    focus = [m.casefold() for m in (matched or job_skills)]
    relevant_projects = [p for p in projects if any(
        str(skill).casefold() in json.dumps(p, ensure_ascii=False).casefold()
        for skill in (focus or definition["skills"])
    )] or projects[:2]
    summary = (resume.get("summary") or "").strip() or definition["summary"]
    # Tailoring only re-orders and names verified facts; nothing is invented.
    if matched:
        shown = [s["name"] for s in ordered if s["name"].casefold() in {m.casefold() for m in matched[:6]}]
        if shown:
            summary = (summary + " Релевантно вакансии: " + ", ".join(shown[:6]) + ".").strip()
    evidence = [p.get("description", "") for p in relevant_projects if p.get("description")]
    if evidence and evidence[0] not in summary:
        summary = (summary + " " + evidence[0][:240]).strip()
    target_roles = list(definition["roles"])
    if job is not None and job["title"]:
        target_roles = [job["title"]] + [role for role in target_roles if role.casefold() != job["title"].casefold()]
    return {
        "variant": variant, "label": definition["label"],
        "headline": (resume.get("headline") or definition["roles"][0]),
        "target_roles": target_roles,
        "summary": summary,
        "skills": [s["name"] for s in ordered],
        "experience": [e for e in resume.get("experience") or [] if e.get("verified")],
        "education": [e for e in resume.get("education") or [] if e.get("verified")],
        "projects": relevant_projects,
        "languages": [l for l in resume.get("languages") or [] if l.get("verified")],
        "certificates": [c for c in resume.get("certificates") or [] if c.get("verified")],
        "facts_hash": facts_hash(facts_pack(resume, load_profile())),
    }


def variant_summary(resume: dict) -> dict:
    return {
        name: {
            "label": definition["label"],
            "skills": len([s for s in verified_skills(resume) if s["name"].casefold() in
                           [x.casefold() for x in definition["skills"]]]),
            "projects": len(variant_view(resume, name)["projects"]),
        }
        for name, definition in VARIANTS.items()
    }


def diff_variants(resume: dict, a: str, b: str) -> dict:
    va, vb = variant_view(resume, a), variant_view(resume, b)
    return {
        "only_a": sorted(set(va["skills"]) - set(vb["skills"])),
        "only_b": sorted(set(vb["skills"]) - set(va["skills"])),
        "summary_changed": va["summary"] != vb["summary"],
        "a": va, "b": vb,
    }


# --------------------------------------------------------------------------- export

def render_html(view: dict, contact: dict) -> str:
    e = html.escape
    name = " ".join(x for x in (contact.get("first_name"), contact.get("last_name")) if x) or "Резюме"
    lines = [f"<h1>{e(name)}</h1>", f"<p class='role'>{e(view['headline'])}</p>"]
    meta = " · ".join(x for x in (contact.get("city"), contact.get("email"), contact.get("phone")) if x)
    if meta:
        lines.append(f"<p class='meta'>{e(meta)}</p>")
    lines.append(f"<h2>О себе</h2><p>{e(view['summary'])}</p>")
    if view["skills"]:
        lines.append("<h2>Навыки</h2><p>" + " · ".join(e(s) for s in view["skills"]) + "</p>")
    if view["experience"]:
        lines.append("<h2>Опыт работы</h2>")
        for item in view["experience"]:
            period = " — ".join(x for x in (item.get("start", ""), item.get("end", "")) if x)
            lines.append(f"<div class='item'><b>{e(item.get('position') or '')}</b>, {e(item.get('company') or '')}"
                         f"<span class='period'>{e(period)}</span><p>{e(item.get('description') or '')}</p></div>")
    if view["projects"]:
        lines.append("<h2>Проекты</h2>")
        for item in view["projects"]:
            stack = ", ".join(item.get("stack") or [])
            lines.append(f"<div class='item'><b>{e(item.get('name') or '')}</b>"
                         f"<span class='period'>{e(stack)}</span><p>{e(item.get('description') or '')}</p></div>")
    if view["education"]:
        lines.append("<h2>Образование</h2>")
        for item in view["education"]:
            period = " — ".join(x for x in (item.get("start", ""), item.get("end", "")) if x)
            lines.append(f"<div class='item'><b>{e(item.get('institution') or '')}</b>"
                         f"<span class='period'>{e(period)}</span><p>{e(item.get('degree') or '')} {e(item.get('field') or '')}</p></div>")
    if view["languages"]:
        lines.append("<h2>Языки</h2><p>" + ", ".join(e(f"{l.get('name','')} {l.get('level','')}".strip()) for l in view["languages"]) + "</p>")
    return """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Резюме</title>
<style>
@page{size:A4;margin:18mm}
body{font-family:"Segoe UI",Arial,sans-serif;color:#111;font-size:11.5pt;line-height:1.45;max-width:760px;margin:0 auto}
h1{font-size:24pt;margin:0 0 4px}h2{font-size:13pt;margin:20px 0 6px;border-bottom:1px solid #bbb;padding-bottom:3px}
.role{font-size:13pt;color:#26527d;margin:0}.meta{color:#444;font-size:10pt}
.item{margin:8px 0}.period{float:right;color:#555;font-size:9.5pt}
p{margin:4px 0}
</style></head><body>""" + "".join(lines) + "</body></html>"


def export_variant(resume: dict, variant: str, formats: list[str] | None = None,
                   job: dict | None = None, details: dict | None = None) -> dict:
    ensure_dirs()
    formats = formats or ["docx", "pdf", "html"]
    view = variant_view(resume, variant, job=job, details=details)
    contact = resume.get("contact") or {}
    suffix = ""
    if job is not None:
        try:
            suffix = "-" + str(job["id"])[:8]
        except Exception:
            suffix = ""
    base = RESUME_FILES / f"resume-{variant}{suffix}-{date.today().isoformat()}"
    outputs: dict[str, str] = {}
    html_text = render_html(view, contact)
    (base.with_suffix(".html")).write_text(html_text, encoding="utf-8")
    outputs["html"] = str(base.with_suffix(".html"))
    if "docx" in formats:
        try:
            outputs["docx"] = str(_write_docx(base.with_suffix(".docx"), view, contact))
        except Exception as exc:  # optional dependency
            outputs["docx_error"] = str(exc)
    if "pdf" in formats:
        try:
            outputs["pdf"] = str(_write_pdf(base.with_suffix(".pdf"), html_text))
        except Exception as exc:
            outputs["pdf_error"] = str(exc)
    return outputs


def _write_docx(path: Path, view: dict, contact: dict) -> Path:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    document = Document()
    style = document.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    name = " ".join(x for x in (contact.get("first_name"), contact.get("last_name")) if x) or "Резюме"
    heading = document.add_paragraph()
    run = heading.add_run(name)
    run.bold = True
    run.font.size = Pt(20)
    heading.alignment = WD_ALIGN_PARAGRAPH.LEFT
    role = document.add_paragraph(view["headline"])
    role.runs[0].font.size = Pt(12)
    meta = " · ".join(x for x in (contact.get("city"), contact.get("email"), contact.get("phone")) if x)
    if meta:
        document.add_paragraph(meta)

    def section(title: str):
        paragraph = document.add_paragraph()
        run = paragraph.add_run(title.upper())
        run.bold = True
        run.font.size = Pt(11)

    section("О себе")
    document.add_paragraph(view["summary"])
    if view["skills"]:
        section("Навыки")
        document.add_paragraph(" · ".join(view["skills"]))
    if view["experience"]:
        section("Опыт работы")
        for item in view["experience"]:
            paragraph = document.add_paragraph()
            paragraph.add_run(f"{item.get('position','')}, {item.get('company','')}").bold = True
            period = " — ".join(x for x in (item.get("start", ""), item.get("end", "")) if x)
            if period:
                paragraph.add_run(f"  ({period})")
            if item.get("description"):
                document.add_paragraph(item["description"])
    if view["projects"]:
        section("Проекты")
        for item in view["projects"]:
            paragraph = document.add_paragraph()
            paragraph.add_run(item.get("name", "")).bold = True
            if item.get("stack"):
                paragraph.add_run(" — " + ", ".join(item["stack"]))
            if item.get("description"):
                document.add_paragraph(item["description"])
    if view["education"]:
        section("Образование")
        for item in view["education"]:
            period = " — ".join(x for x in (item.get("start", ""), item.get("end", "")) if x)
            document.add_paragraph(f"{item.get('institution','')} {period}".strip())
    if view["languages"]:
        section("Языки")
        document.add_paragraph(", ".join(f"{l.get('name','')} {l.get('level','')}".strip() for l in view["languages"]))
    document.save(str(path))
    return path


def _write_pdf(path: Path, html_text: str) -> Path:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html_text, wait_until="load")
        page.pdf(path=str(path), format="A4", print_background=True,
                 margin={"top": "16mm", "bottom": "16mm", "left": "16mm", "right": "16mm"})
        browser.close()
    return path
