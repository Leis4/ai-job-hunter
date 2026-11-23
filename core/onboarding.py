"""Profile onboarding: resume upload -> structured facts -> explicit approval.

Design rules:
* every extracted fact is ``verified: false`` until the user confirms it;
* AI output is grounded against the source text and dropped when unsupported;
* no experience/education/project is invented;
* AITUNNEL is used when configured; deterministic local extraction is the
  fallback and is labelled as such.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from . import db as dbm
from .paths import DATA, ensure_dirs
from .settings import load_profile, load_settings, save_profile
from .text import SKILLS, canonical_skill, clean_text, normalize, scrub_contacts

UPLOADS = DATA / "uploads"
DRAFT_PATH = DATA / "onboarding.json"

SECTION_NAMES = (
    "контакты", "локация", "о себе", "образование", "опыт работы", "проекты",
    "навыки", "языки", "сертификаты", "курсы", "дополнительно",
)

EXTRA_SKILL_PHRASES = (
    "Data Analysis", "Data Preprocessing", "Feature Engineering", "Random Forest",
    "Computer Vision", "Machine Learning", "Deep Learning", "Data Science",
    "EfficientNet-B0", "Optuna", "DuckDB", "Pydantic", "FastAPI", "PyTorch",
    "scikit-learn", "XGBoost", "CatBoost", "pandas", "NumPy", "SQL", "Python",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("Для PDF установите pypdf: py -3 -m pip install pypdf") from exc
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if suffix == ".docx":
        try:
            import docx
        except ImportError as exc:
            raise RuntimeError("Для DOCX установите python-docx: py -3 -m pip install python-docx") from exc
        document = docx.Document(str(path))
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(parts)
    if suffix in (".txt", ".md", ".markdown"):
        return path.read_text(encoding="utf-8-sig", errors="replace")
    raise ValueError("Поддерживаются .pdf, .docx, .txt и .md")


def _lines(text: str) -> list[str]:
    return [clean_text(line) for line in text.replace("\r", "\n").split("\n") if clean_text(line)]


def _section(lines: list[str], name: str) -> list[str]:
    low = name.casefold()
    start = None
    for index, line in enumerate(lines):
        if line.strip(" :").casefold() == low:
            start = index + 1
            break
    if start is None:
        return []
    out = []
    for line in lines[start:]:
        if line.strip(" :").casefold() in SECTION_NAMES:
            break
        out.append(line)
    return out


def _extract_contacts(text: str, lines: list[str]) -> dict:
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    phone = re.search(r"\+7[\s()\d-]{9,20}", text)
    city = ""
    location = _section(lines, "Локация")
    if location:
        city = location[0].strip()
    if not city:
        for candidate in ("Москва", "Санкт-Петербург", "Тестоград", "Новосибирск", "Екатеринбург"):
            if candidate.casefold() in text.casefold():
                city = candidate
                break
    return {"email": email.group(0) if email else "", "phone": phone.group(0).strip() if phone else "",
            "city": city, "relocation": False}


def _headline(lines: list[str]) -> str:
    for line in lines[:5]:
        if re.search(r"data|machine|ml|analyst|разработ|инженер|scientist", line, re.I):
            return re.sub(r"\s*\d[\d\s]*₽.*$", "", line).strip()
    return lines[1] if len(lines) > 1 else ""


def _skills(text: str, lines: list[str]) -> list[dict]:
    source = "Навыки"
    section = _section(lines, "Навыки")
    haystack = " ".join(section) if section else text
    found: dict[str, dict] = {}
    # Known aliases, longest first, so "Machine Learning" wins over "ML".
    phrases = set(EXTRA_SKILL_PHRASES)
    for canonical, aliases in SKILLS.items():
        phrases.add(canonical)
        phrases.update(aliases)
    for phrase in sorted(phrases, key=len, reverse=True):
        if len(phrase) < 2:
            continue
        pattern = re.compile(r"(?<![a-zа-я0-9])" + re.escape(phrase) + r"(?![a-zа-я0-9])", re.I)
        if not pattern.search(haystack):
            continue
        name = canonical_skill(phrase)
        if name in found:
            continue
        evidence = next((line for line in section if pattern.search(line)), source)
        found[name] = {"name": name, "evidence": evidence[:240], "confidence": 0.9,
                       "verified": False, "basis": "explicit"}
    # The curated phrase list above already covers the explicit technologies;
    # splitting raw words would create noise like "Machine" / "Forest".
    priority = {name: index for index, name in enumerate((
        "Python", "SQL", "ML", "Pandas", "NumPy", "scikit-learn", "CatBoost",
        "XGBoost", "PyTorch", "Computer Vision", "Data Analysis", "Feature Engineering",
        "Data Preprocessing", "Random Forest", "FastAPI", "Pydantic", "Docker", "Git",
        "DuckDB", "Optuna", "EfficientNet-B0"))}
    return sorted(found.values(), key=lambda item: (priority.get(item["name"], 99), item["name"].casefold()))


def _education(lines: list[str]) -> list[dict]:
    section = _section(lines, "Образование")
    if not section:
        return []
    joined = " ".join(section)
    degree_match = re.search(r"(неоконченное высшее|высшее|бакалавриат|магистратура)", joined, re.I)
    institution_part = joined[:degree_match.start()].strip() if degree_match else section[0]
    institution_part = re.sub(r"^(настоящее время|сейчас)\s*", "", institution_part, flags=re.I).strip()
    field = ""
    field_match = re.search(r"(?:неоконченное высшее|высшее|бакалавриат|магистратура)\s*·\s*(.+?)(?:\.\s*\d{4}|$)",
                            joined, re.I)
    if field_match:
        field = field_match.group(1).strip()
    elif len(section) > 1:
        field = section[1]
    years = re.findall(r"(?:19|20)\d{2}", joined)
    return [{
        "institution": institution_part[:240],
        "degree": degree_match.group(1).lower() if degree_match else "",
        "field": field[:240],
        "start": years[0] if years else "",
        "end": years[-1] if len(years) > 1 else "",
        "evidence": joined[:300],
        "verified": False,
        "basis": "explicit",
    }]


def _projects(lines: list[str]) -> list[dict]:
    section = _section(lines, "Проекты")
    if not section:
        return []
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in section:
        if re.match(r"^(даты не указаны|20\d{2}|проект\b)", line, re.I):
            if current:
                blocks.append(current)
            current = [re.sub(r"^даты не указаны\s*", "", line, flags=re.I)]
        elif current:
            current.append(line)
        else:
            current = [line]
    if current:
        blocks.append(current)
    projects = []
    for block in blocks:
        if not block:
            continue
        header = block[0]
        parts = re.split(r"\s+[—–-]\s+", header, maxsplit=1)
        name = header.strip()
        if not name or name.casefold().startswith("проект"):
            continue
        role = block[1] if len(block) > 1 and len(block[1]) < 140 else ""
        description = " ".join(block[2:] if role else block[1:]).strip()
        stack = []
        for skill in _skills(description, [description]):
            if skill["name"] not in stack:
                stack.append(skill["name"])
        projects.append({
            "name": name[:160],
            "role": role[:160],
            "stack": stack[:12],
            "description": description[:1200],
            "url": "",
            "evidence": " ".join(block)[:400],
            "verified": False,
            "basis": "explicit",
        })
    return projects


def _experience(lines: list[str]) -> list[dict]:
    section = _section(lines, "Опыт работы")
    if not section:
        return []
    entries = []
    for line in section:
        years = re.findall(r"(?:19|20)\d{2}", line)
        parts = [p.strip() for p in re.split(r"\s*\|\s*|\s+[—–]\s+", line) if p.strip()]
        entries.append({
            "company": (parts[0] if parts else line)[:160],
            "position": (parts[1] if len(parts) > 1 else "")[:160],
            "start": years[0] if years else "",
            "end": years[-1] if len(years) > 1 else "",
            "description": line[:800] if len(parts) < 2 else "",
            "achievements": [],
            "evidence": line[:300],
            "verified": False,
            "basis": "explicit",
        })
    return entries


def _languages(lines: list[str]) -> list[dict]:
    section = _section(lines, "Навыки")
    out = []
    text = " ".join(section)
    for match in re.finditer(r"(Русский|Английский|Татарский|Немецкий|Французский)\s+([A-C][12]|Носитель|Родной)", text, re.I):
        out.append({"name": match.group(1), "level": match.group(2), "verified": False,
                    "basis": "explicit"})
    return out


def _suggestions(text: str, headline: str, summary: str) -> dict:
    low = normalize(f"{headline} {summary}")
    directions: list[str] = []

    def add(label: str) -> None:
        if label and label not in directions:
            directions.append(label)

    if "data science" in low or "data scientist" in low:
        add("Data Science"); add("Data Scientist")
    if "machine learning" in low or re.search(r"\bml\b", low):
        add("Machine Learning"); add("ML Engineer")
    if "applied ml" in low:
        add("Applied ML")
    if "качеств" in low or "оценк" in low:
        add("ML Quality")
    if "data analysis" in low or "анализ" in low:
        add("Data Analysis")
    if re.search(r"junior|стажер|стажёр|intern|trainee", low):
        add("Junior"); add("Intern")
    if not directions:
        directions = ["Data Science", "Machine Learning"]
    salary = re.search(r"(\d{2,3})\s*000\s*₽", text)
    constraints = {
        "locations": ["Москва"] if "москв" in text.casefold() else [],
        "work_modes": [mode for mode in ("удалённо", "гибрид", "офис") if mode in text.casefold()],
        "employment": [kind for kind in ("стажировка", "частичная", "полная") if kind in text.casefold()],
        "relocation": False,
        "salary_target_rub": int(salary.group(1)) * 1000 if salary else None,
        "availability": ("Совмещение с очным обучением; график обсуждается." if "учёб" in text.casefold()
                         or "учеб" in text.casefold() else ""),
    }
    return {"directions": directions, "constraints": constraints}


def extract_facts(text: str, *, source_name: str = "", method: str = "local") -> dict:
    # The uploaded resume is the user's own document: do NOT scrub their phone
    # and email. Scrubbing is applied to external vacancy text elsewhere.
    text = clean_text(text or "")
    lines = _lines(text)
    headline = _headline(lines)
    summary_lines = _section(lines, "О себе")
    summary = " ".join(summary_lines).strip()
    facts = {
        "source_name": source_name,
        "source_chars": len(text),
        "method": method,
        "contact": _extract_contacts(text, lines),
        "headline": headline,
        "summary": summary[:1200],
        "skills": _skills(text, lines),
        "education": _education(lines),
        "experience": _experience(lines),
        "projects": _projects(lines),
        "languages": _languages(lines),
        "suggestions": _suggestions(text, headline, summary),
        "created_at": _now(),
        "status": "review",
        "verified": False,
    }
    return facts


# --------------------------------------------------------------------------- AITUNNEL
def _grounded(value: str, source: str) -> bool:
    if not value:
        return False
    return normalize(value)[:60] in normalize(source)


def extract_with_aitunnel(db: sqlite3.Connection, text: str, settings: dict, *,
                          source_name: str = "") -> dict | None:
    from .aitunnel import AITunnelRouter, AIUnavailable
    router = AITunnelRouter(db, settings)
    if not router.configured:
        return None
    try:
        router.refresh_catalog(force=False)
        # Contact details are extracted locally; the external model gets the
        # resume without phone/email/telegram.
        ai_text = scrub_contacts(text)
        prompt = (
            "Извлеки из резюме ТОЛЬКО явно написанные факты. Не придумывай опыт, годы, компании, "
            "навыки и достижения. Верни строго JSON:\n"
            '{"headline":"", "summary":"", "contact":{"email":"","phone":"","city":""},'
            '"skills":[{"name":"","evidence":"точная цитата"}],'
            '"education":[{"institution":"","degree":"","field":"","start":"","end":"","evidence":""}],'
            '"experience":[{"company":"","position":"","start":"","end":"","description":"","evidence":""}],'
            '"projects":[{"name":"","role":"","stack":[],"description":"","evidence":""}],'
            '"languages":[{"name":"","level":"","evidence":""}],'
            '"directions":["..."], "constraints":{"locations":[],"work_modes":[],"employment":[],'
            '"relocation":false,"salary_target_rub":null,"availability":""}}\n'
            "Если данных нет — пустая строка/массив/null. evidence должна быть дословной цитатой."
        )
        result = router.complete("extract_requirements",
                                 [{"role": "system", "content": "Ты извлекаешь факты без домыслов. Только JSON."},
                                  {"role": "user", "content": prompt + "\n\nРЕЗЮМЕ:\n" + ai_text[:12000]}],
                                 max_tokens=2200, temperature=0.1, json_mode=True)
    except (AIUnavailable, Exception):
        return None
    try:
        from .llm import extract_json
        payload = extract_json(result.content)
    except Exception:
        return None
    if not payload:
        return None
    return merge_model_payload(text, payload, source_name=source_name,
                               method=f"aitunnel:{result.model} + local check",
                               cost_rub=result.cost_rub)


def merge_model_payload(text: str, payload: dict, *, source_name: str = "",
                        method: str = "model", cost_rub: float = 0.0) -> dict:
    """Merge a model's JSON with deterministic extraction, grounding every item.

    The local pass is the baseline; the model may add grounded items but never
    removes explicit facts. All merged items stay ``verified: false``.
    """
    from .text import SKILLS, canonical_skill

    def as_list(value):
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            out = []
            for nested in value.values():
                out.extend(as_list(nested))
            return out
        return []

    payload = dict(payload or {})
    if not isinstance(payload.get("skills"), list):
        # Some models nest skills as {"technical": [...], "languages": [...]}.
        payload["skills"] = [{"name": str(x)} if not isinstance(x, dict) else x
                             for x in as_list(payload.get("skills"))]
    for key in ("education", "experience", "projects", "languages"):
        if payload.get(key) and not isinstance(payload.get(key), list):
            payload[key] = as_list(payload.get(key))
    language_names = {"русский", "английский", "татарский", "немецкий", "французский",
                      "испанский", "китайский", "знание языков"}
    local = extract_facts(text, source_name=source_name, method="local")
    for section, fields in (("skills", ("name",)), ("education", ("institution",)),
                            ("projects", ("name",)), ("experience", ("company",))):
        kept = []
        for item in payload.get(section) or []:
            if not isinstance(item, dict):
                continue
            if section == "skills":
                name = canonical_skill(str(item.get("name") or item.get("skill") or ""))
                if not name or normalize(name) in language_names:
                    continue
                item = dict(item)
                item["name"] = name
            if not any(_grounded(str(item.get(f, "")), text) for f in fields):
                continue
            item = dict(item)
            item["verified"] = False
            item["basis"] = "explicit"
            item["confidence"] = 0.85
            item["evidence"] = str(item.get("evidence") or item.get(fields[0], ""))[:300]
            kept.append(item)
        base = list(local.get(section) or [])
        seen = {normalize(str(item.get(fields[0], ""))) for item in base}
        for item in kept:
            key = normalize(str(item.get(fields[0], "")))
            if key and key not in seen:
                base.append(item)
                seen.add(key)
        local[section] = base
    contact = payload.get("contact") or {}
    for key in ("email", "phone", "city"):
        if contact.get(key) and _grounded(str(contact[key]), text):
            local["contact"][key] = str(contact[key])[:200]
    suggestions = payload.get("suggestions") or {}
    directions = payload.get("directions") or suggestions.get("directions")
    if isinstance(directions, list) and directions:
        current = local["suggestions"].setdefault("directions", [])
        for direction in directions:
            value = str(direction)[:80]
            if value and value not in current:
                current.append(value)
    constraints = payload.get("constraints") or suggestions.get("constraints")
    if isinstance(constraints, dict):
        target = local["suggestions"]["constraints"]
        for key, value in constraints.items():
            if isinstance(value, list):
                merged = list(target.get(key) or [])
                for entry in value:
                    if entry not in merged:
                        merged.append(entry)
                target[key] = merged
            elif value not in (None, ""):
                target[key] = value
    local["method"] = method
    local["cost_rub"] = round(float(cost_rub), 6)
    return local


# --------------------------------------------------------------------------- draft
def save_draft(draft: dict) -> None:
    ensure_dirs()
    DRAFT_PATH.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")


def load_draft() -> dict | None:
    if not DRAFT_PATH.exists():
        return None
    try:
        return json.loads(DRAFT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def discard() -> None:
    if DRAFT_PATH.exists():
        DRAFT_PATH.unlink()


def extract_file(path: Path, *, db: sqlite3.Connection | None = None,
                 settings: dict | None = None) -> dict:
    text = extract_text(path)
    if not text.strip():
        raise ValueError("В файле не найден текст (возможно, скан без OCR).")
    ensure_dirs()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    text_path = UPLOADS / (path.stem[:80] + ".txt")
    text_path.write_text(text, encoding="utf-8")
    settings = settings or load_settings()
    draft = None
    if db is not None:
        draft = extract_with_aitunnel(db, text, settings, source_name=path.name)
    if draft is None:
        draft = extract_facts(text, source_name=path.name, method="local")
    draft["source_text_path"] = str(text_path)
    draft["source_file"] = str(path)
    save_draft(draft)
    return draft


def total_months(experience: list[dict]) -> int:
    total = 0
    for entry in experience:
        start = re.search(r"(?:19|20)\d{2}", str(entry.get("start") or ""))
        end = re.search(r"(?:19|20)\d{2}", str(entry.get("end") or ""))
        if not start:
            continue
        start_year = int(start.group(0))
        end_year = int(end.group(0)) if end else date.today().year
        total += max(0, min(50, end_year - start_year)) * 12
    return total


def _selected_indexes(form: dict, name: str) -> set[int]:
    raw = form.get(name) or []
    if isinstance(raw, str):
        raw = [raw]
    return {int(x) for x in raw if str(x).isdigit()}


def apply_confirmation(draft: dict, form: dict) -> dict:
    """Write selected facts to resume/profile. Unselected items are discarded."""
    from .resume import load_resume, save_resume, sync_from_profile
    from .resume import facts_hash, facts_pack

    resume = load_resume()
    skills = [item for index, item in enumerate(draft.get("skills") or [])
              if index in _selected_indexes(form, "skills")]
    projects = [item for index, item in enumerate(draft.get("projects") or [])
                if index in _selected_indexes(form, "projects")]
    education = [item for index, item in enumerate(draft.get("education") or [])
                 if index in _selected_indexes(form, "education")]
    experience = [item for index, item in enumerate(draft.get("experience") or [])
                  if index in _selected_indexes(form, "experience")]
    languages = [item for index, item in enumerate(draft.get("languages") or [])
                 if index in _selected_indexes(form, "languages")]

    resume["contact"] = {**(resume.get("contact") or {}), **(draft.get("contact") or {})}
    for key in ("email", "phone", "city"):
        value = str(form.get(key) or "").strip()
        if value:
            resume["contact"][key] = value
    resume["headline"] = str(form.get("headline") or draft.get("headline") or "").strip()[:240]
    resume["summary"] = str(form.get("summary") or draft.get("summary") or "").strip()[:2000]
    resume["skills"] = [{"name": x["name"], "level": x.get("level", ""), "evidence": x.get("evidence", ""),
                         "verified": True} for x in skills]
    resume["projects"] = [{**x, "verified": True} for x in projects]
    resume["education"] = [{**x, "verified": True} for x in education]
    resume["experience"] = [{**x, "verified": True} for x in experience]
    resume["languages"] = [{**x, "verified": True} for x in languages]
    resume["total_experience_months"] = total_months(experience)

    directions = form.get("directions") or []
    if isinstance(directions, str):
        directions = [directions]
    directions = [str(x).strip() for x in directions if str(x).strip()][:10]
    resume["target_roles"] = directions or resume.get("target_roles") or []

    constraints = draft.get("suggestions", {}).get("constraints") or {}
    locations = [x.strip() for x in str(form.get("locations") or "").split(",") if x.strip()]
    salary_raw = str(form.get("salary_target_rub") or "").strip()
    try:
        salary = int(re.sub(r"\D", "", salary_raw)) if salary_raw else constraints.get("salary_target_rub")
    except ValueError:
        salary = constraints.get("salary_target_rub")
    modes = [label for key, label in (("mode_remote", "удалённо"), ("mode_hybrid", "гибрид"),
                                      ("mode_office", "офис")) if form.get(key) == "1"]
    resume["constraints"] = {
        "work_modes": modes or constraints.get("work_modes") or [],
        "locations": locations or constraints.get("locations") or [],
        "salary_target_rub": salary,
        "availability": str(form.get("availability") or constraints.get("availability") or "")[:300],
    }
    resume["status"] = "confirmed"
    resume["updated_at"] = date.today().isoformat()
    save_resume(resume)

    profile = load_profile()
    profile["verified_skills"] = sorted({x["name"] for x in skills})
    profile["professional_experience_years"] = round(total_months(experience) / 12, 1)
    profile["verified_projects"] = [{k: v for k, v in x.items() if k != "verified"}
                                    for x in projects]
    profile["education_confirmed"] = education[0]["institution"] if education else ""
    prefs = profile.setdefault("preferences", {})
    prefs["role_keywords"] = directions or prefs.get("role_keywords") or []
    prefs["locations"] = resume["constraints"]["locations"] or prefs.get("locations") or []
    prefs["work_modes"] = resume["constraints"]["work_modes"] or prefs.get("work_modes") or []
    prefs["salary_target_rub"] = salary
    prefs["availability"] = resume["constraints"]["availability"]
    save_profile(profile)

    draft["status"] = "confirmed"
    draft["confirmed_at"] = _now()
    draft["selected"] = {"skills": sorted(_selected_indexes(form, "skills")),
                         "projects": sorted(_selected_indexes(form, "projects"))}
    save_draft(draft)
    return {"resume": resume, "profile": profile,
            "facts_hash": facts_hash(facts_pack(sync_from_profile(resume), profile))}
