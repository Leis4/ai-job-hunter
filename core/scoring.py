"""Transparent mathematical ranking.

Design notes
------------
* Two stages: a cheap BM25/role pass over list cards, then a deep pass with the
  full description only for shortlisted vacancies. Detail fetches are the
  expensive resource, so they are bounded.
* The published number is a **Match Score** (0-100), not a probability.
  Every score carries an 80% uncertainty interval derived from missing data.
* A calibrated reply probability is trained only from real outcomes, and only
  when the sample is large enough; it is reported with Brier score and AUC.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import db as dbm
from .mathx import ROUTER
from .text import (
    LEAD_LEVELS, SENIOR_LEVELS, analyze_job_text, canonical_mode, canonical_skill,
    detect_work_mode, jaccard, normalize, parse_experience_years, resume_skill_set,
    seniority_level, token_overlap, tokens,
)

MODEL_VERSION = "eligibility-v7-evidence"

# A task performed *after hiring* is not evidence that the applicant must
# already have commercial experience. Only explicit past professional / live
# production experience can trigger this hard constraint. Numeric years are
# handled independently by parse_experience_years().
COMMERCIAL_PATTERNS = (
    (r"коммерческ\w*\s+опыт\w*|опыт\w*\s+коммерческ\w+\s+(?:разработк|работ|проект)", "коммерческий опыт"),
    (r"опыт\w*[^.!?;\n]{0,70}?(?:в продакшн|в продакшен|в production|production[- ]environment|на промышленн\w+ эксплуатации)",
     "опыт эксплуатации в production"),
    (r"(?:опыт\w*|раньше|ранее)\s+[^.!?;\n]{0,55}?(?:вывод\w*|запуск\w*|разв[её]рт\w*)\s+[^.!?;\n]{0,35}?(?:в продакшн|в продакшен|в production)",
     "опыт запуска в production"),
)
OPTIONAL_MARKERS = ("будет плюсом", "плюсом будет", "желательно", "не обязательно",
                    "nice to have", "преимуществом", "не обязателен", "опционально")
FOREIGN_LOCATION_MARKERS = (
    "united states", "usa", " u.s.", "canada", "north america", "new york", "san francisco",
    "seattle", "boston", "austin", "chicago", "los angeles", "paris", "france", "london",
    "united kingdom", "berlin", "germany", "poland", "warsaw", "sweden", "stockholm",
    "netherlands", "amsterdam", "singapore", "australia", "india", "israel", "spain",
    "portugal", "ireland", "switzerland", "dubai", "uae", "tokyo", "japan", "brazil",
)
GLOBAL_REMOTE_MARKERS = ("worldwide", "anywhere", "global", "russia", "россия", "москва",
                         "moscow", "remote, europe", "emea")


def commercial_signals(text: str) -> list[str]:
    """Only explicit *prior* commercial/production requirements.

    A vacancy asking to CREATE chatbots or WRITE production-ready code is not
    asking for past commercial experience. Evaluate each sentence separately
    to avoid treating a separate optional clause as a hard requirement.
    """
    from .text import sentences
    found: list[str] = []
    for sentence in sentences(text or ""):
        low = normalize(sentence)
        if any(marker in low for marker in OPTIONAL_MARKERS):
            continue
        for pattern, label in COMMERCIAL_PATTERNS:
            if re.search(pattern, low) and label not in found:
                found.append(label)
    return found[:4]


def qualitative_experience_requirement(text: str) -> bool:
    """Explicit experience obligation without a numeric minimum."""
    from .text import sentences
    for sentence in sentences(text or ""):
        low = normalize(sentence)
        if any(marker in low for marker in OPTIONAL_MARKERS):
            continue
        if re.search(r"(?:требу\w*|необходим\w*|обязател\w*|нужен|ожидаем)"
                     r"[^.!?;\n]{0,65}опыт|опыт[^.!?;\n]{0,45}обязател\w*", low):
            return True
    return False


def schedule_evidence(description: str, availability: str, schedule_pref: str = "") -> dict:
    """Warn on fixed schedules when candidate explicitly needs to combine work/study.

    Do NOT infer an absolute ban on full-time work from student status alone.
    An explicit preference for 5/2 makes a fixed schedule compatible.
    """
    text = normalize(description or "")
    avail = normalize(availability or "")
    pref = normalize(schedule_pref or "")
    if not avail and not pref:
        return {"status": "unknown", "signals": []}
    combines_study = bool(re.search(r"(?:совмещ|учеб|университет|заняти|студент)", avail))
    fixed = bool(re.search(r"(?:5\s*[/\-]\s*2|пн\s*[-–]\s*пт|понедельник\w* по пятниц|строго.{0,20}(?:офис|график)|фиксированн\w* график|40\s*час)", text))
    flexible = bool(re.search(r"(?:гибк\w* график|совмещ\w* с учеб|индивидуальн\w* график|частичн\w* занят|неполн\w* рабоч|part[- ]time)", text))
    prefers_fixed = bool(re.search(r"(?:5\s*[/\-]\s*2|полн\w* день|полн\w* занят)", pref))
    if fixed and prefers_fixed:
        return {"status": "compatible", "signals": []}
    if fixed and combines_study and not flexible:
        explicitly_impossible = bool(re.search(r"(?:не могу|исключаю|недоступен|только неполн|не рассматриваю).{0,40}(?:5\s*[/\-]\s*2|полный|офис|фикс)", avail))
        return {"status": "incompatible" if explicitly_impossible else "review",
                "signals": ["фиксированный график 5/2 — проверить совместимость с учёбой"]}
    return {"status": "compatible" if flexible else "unknown", "signals": []}


# --------------------------------------------------------------------------- BM25

class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.N = max(1, len(docs))
        self.doc_len = [len(d) for d in docs]
        self.avgdl = max(1.0, sum(self.doc_len) / self.N)
        self.tf: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for doc in docs:
            counts: dict[str, int] = {}
            for term in doc:
                counts[term] = counts.get(term, 0) + 1
            self.tf.append(counts)
            for term in counts:
                df[term] = df.get(term, 0) + 1
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}
        self._default_idf = math.log(1 + (self.N + 0.5) / 0.5)

    def score(self, query: dict[str, float], index: int) -> float:
        counts = self.tf[index]
        dl = self.doc_len[index] or 1
        total = 0.0
        for term, qweight in query.items():
            tf = counts.get(term, 0)
            if not tf:
                continue
            idf = self.idf.get(term, self._default_idf)
            denom = tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            total += qweight * idf * (tf * (self.k1 + 1) / denom)
        return total


def build_query(profile: dict, resume: dict, role_keywords: Iterable[str]) -> dict[str, float]:
    query: dict[str, float] = {}
    for skill in resume_skill_set(profile, resume):
        canonical = canonical_skill(skill)
        for token in tokens(canonical):
            query[token] = max(query.get(token, 0), 3.0)
    for project in (resume.get("projects") or [])[:6]:
        if isinstance(project, dict) and project.get("verified"):
            for token in tokens(f"{project.get('name','')} {project.get('description','')} {project.get('role','')}"):
                query[token] = max(query.get(token, 0), 1.6)
    for token in tokens(resume.get("headline", "") + " " + resume.get("summary", "")):
        query[token] = max(query.get(token, 0), 1.4)
    for keyword in role_keywords:
        for token in tokens(keyword):
            query[token] = max(query.get(token, 0), 1.0)
    return query


def _job_doc(row) -> str:
    return " ".join([
        row["title"] or "", row["company"] or "", row["description"] or "",
        " ".join(dbm.job_skills(row)),
    ])


# --------------------------------------------------------------------------- context

@dataclass
class ScoreContext:
    settings: dict
    profile: dict
    resume: dict
    bm25: BM25 | None = None
    rows: list = field(default_factory=list)
    query: dict[str, float] = field(default_factory=dict)
    median_score: float = 0.0
    bm25_scores: list[float] = field(default_factory=list)
    text_scale: float = 1.0


def build_context(rows: list, settings: dict, profile: dict, resume: dict) -> ScoreContext:
    prefs = profile.get("preferences") or {}
    query = build_query(profile, resume, prefs.get("role_keywords") or [])
    docs = [tokens(_job_doc(row)) for row in rows]
    bm25 = BM25(docs, float(settings["scoring"].get("bm25_k1", 1.5)),
                float(settings["scoring"].get("bm25_b", 0.75))) if docs else None
    ctx = ScoreContext(settings=settings, profile=profile, resume=resume, bm25=bm25, rows=rows, query=query)
    if bm25:
        ctx.bm25_scores = [bm25.score(query, i) for i in range(len(docs))]
        ordered = sorted(ctx.bm25_scores)
        ctx.median_score = ordered[len(ordered) // 2] if ordered else 0.0
        # A 75th-percentile document is the reference "strong match" for the batch.
        ctx.text_scale = max(1.0, ordered[int(len(ordered) * 0.75)] if ordered else 1.0)
    return ctx


# --------------------------------------------------------------------------- helpers

def _realism(skill_match: float, exp_info: dict, level: str, commercial: list[str],
             coverage: float, months: float, profile: dict, resume: dict) -> tuple[float, list[str]]:
    """How realistic an invitation is for *this* candidate (estimate, not probability)."""
    reasons: list[str] = []
    have = float(exp_info.get("candidate_years") or 0.0)
    required = exp_info.get("required_years")
    gap = max(0.0, float(required) - have) if required is not None else 0.0
    skill_part = max(0.0, min(1.0, skill_match / 100.0))
    exp_part = 1.0 if gap <= 0 else max(0.0, 1.0 - gap / 3.0)
    level_part = {"intern": 1.0, "junior": 0.9, "middle": 0.5,
                  "unspecified": 0.7, "senior": 0.2, "lead": 0.1,
                  "principal": 0.1, "architect": 0.1, "head": 0.1}.get(level, 0.5)
    value = 10.0 + 50.0 * skill_part + 20.0 * exp_part + 10.0 * level_part
    if commercial:
        value -= 15.0
        reasons.append("нужен коммерческий/production-опыт")
    if months <= 0 and level in ("middle", *SENIOR_LEVELS):
        value -= 12.0
        reasons.append("нет коммерческого опыта")
    if coverage < 0.45:
        value -= 8.0
        reasons.append("низкое покрытие обязательных навыков")
    if gap > 0:
        reasons.append(f"требуют {gap:.0f} г. опыта сверх подтверждённого")
    verified = len(profile.get("verified_skills") or [])
    projects = sum(1 for p in (resume.get("projects") or []) if isinstance(p, dict) and p.get("verified"))
    if verified >= 8 and projects >= 1:
        value += 5.0
    if level in ("intern", "junior"):
        reasons.append("уровень стажёра/джуна")
    value = max(0.0, min(96.0, value))
    return round(value, 1), reasons[:4]


def _candidate_course(resume: dict) -> int | None:
    for item in resume.get("education") or []:
        if not isinstance(item, dict) or not item.get("verified"):
            continue
        evidence = str(item.get("evidence") or "")
        match = re.search(r"(\d)\s*курс", evidence, re.I)
        if match:
            return int(match.group(1))
        start = str(item.get("start") or "")
        if start.isdigit():
            return max(1, min(6, datetime.now().year - int(start) + 1))
    return None


def _required_courses(text: str) -> set[int]:
    low = normalize(text or "")
    courses: set[int] = set()
    for match in re.finditer(r"(\d)\s*(?:[-–—,и]\s*(\d)\s*)?курс", low):
        courses.add(int(match.group(1)))
        if match.group(2):
            courses.add(int(match.group(2)))
    if "старшие курсы" in low:
        courses.update({3, 4})
    return courses


def _candidate_years(profile: dict, resume: dict) -> tuple[float, bool]:
    months = resume.get("total_experience_months")
    if isinstance(months, (int, float)) and months >= 0:
        return float(months) / 12.0, True
    years = profile.get("professional_experience_years")
    if isinstance(years, (int, float)) and years >= 0:
        return float(years), bool(years)
    return 0.0, False


def _age_days(row) -> float | None:
    raw = row["published_at"] or row["fetched_at"] or row["created_at"]
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    dt = None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt, length in (("%Y-%m-%dT%H:%M:%S%z", 24), ("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
            try:
                dt = datetime.strptime(text[:length], fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)


def _role_fit(row, query_ctx: ScoreContext) -> tuple[float, float]:
    prefs = query_ctx.profile.get("preferences") or {}
    title = row["title"] or ""
    title_tokens = set(tokens(title))
    keywords = [k for k in (prefs.get("role_keywords") or []) if k]
    keyword_factors = []
    for keyword in keywords:
        keyword_tokens = set(tokens(keyword))
        if not keyword_tokens:
            continue
        overlap = token_overlap(keyword_tokens, title_tokens)
        keyword_factors.append(1.0 if overlap >= 0.99 else (0.5 if overlap >= 0.5 else 0.0))
    keyword_factor = min(1.0, sum(keyword_factors) / 1.5) if keyword_factors else 0.0
    headline_tokens = set(tokens(query_ctx.resume.get("headline", "") + " " + " ".join(
        (query_ctx.resume.get("target_roles") or []))))
    overlap = token_overlap(headline_tokens, title_tokens) if headline_tokens else 0.0
    level_bonus = 0.0
    level = seniority_level(title)
    if level in ("intern", "junior"):
        level_bonus = 0.08
    elif level in ("senior", "lead", "principal", "architect", "head"):
        level_bonus = -0.10
    value = max(keyword_factor, min(1.0, overlap * 2.5)) * 0.95 + level_bonus
    return max(0.0, min(1.0, value)), 0.06 if keywords else 0.20


def _skill_strength(level: str) -> float:
    return {"confident": 1.0, "practical": 1.0, "basic": 0.55,
            "learning": 0.25, "unknown": 1.0}.get(str(level or "").casefold(), 1.0)


def _skills_fit(row, query_ctx: ScoreContext) -> tuple[float, float, dict]:
    conf = query_ctx.settings["scoring"]
    signals = analyze_job_text(
        _job_doc(row), dbm.job_skills(row),
        required_weight=float(conf.get("required_skill_weight", 2.0)),
        nice_weight=float(conf.get("nice_skill_weight", 0.9)),
        listed_weight=float(conf.get("listed_skill_weight", 1.5)),
    )
    verified = resume_skill_set(query_ctx.profile, query_ctx.resume)
    levels: dict[str, str] = {}
    for skill in (query_ctx.resume or {}).get("skills") or []:
        if isinstance(skill, dict) and skill.get("verified") and skill.get("name"):
            levels[canonical_skill(str(skill["name"]))] = str(skill.get("level") or "unknown")
    for skill in (query_ctx.profile or {}).get("verified_skills") or []:
        levels.setdefault(canonical_skill(str(skill)), "unknown")
    aliases = query_ctx.profile.get("skill_aliases") or {}
    matched, missing = [], []
    got = 0.0
    total = 0.0
    for req in signals.skills:
        total += req.weight
        canonical = canonical_skill(req.name)
        hit_as = None
        hit_key = canonical
        if canonical in verified:
            hit_as = verified[canonical]
        else:
            for alias in aliases.get(canonical, []) + aliases.get(req.name, []):
                if canonical_skill(alias) in verified:
                    hit_key = canonical_skill(alias)
                    hit_as = verified[hit_key]
                    break
        if hit_as:
            got += req.weight * _skill_strength(levels.get(hit_key, "unknown"))
            matched.append(req.name)
        else:
            missing.append({"name": req.name, "weight": req.weight, "requirement": req.requirement})
    prior = float(conf.get("skill_prior", 0.25))
    alpha = 2.0
    coverage = (got + prior * alpha) / (total + alpha) if total else prior
    # Missing a *required* skill is worse than missing a nice-to-have.
    required_missing = [m for m in missing if m["requirement"] == "required"]
    if required_missing:
        coverage *= 0.85
    detail = {
        "matched": sorted(set(matched)),
        "missing": sorted(missing, key=lambda m: -m["weight"])[:12],
        "required_missing": [m["name"] for m in required_missing][:8],
        "coverage": round(coverage, 3),
        "demanded": round(total, 2),
    }
    sigma = 0.16 if total else 0.30
    return max(0.0, min(1.0, coverage)), sigma, detail


def _text_fit(row, ctx: ScoreContext, index: int | None) -> tuple[float, float]:
    if not ctx.bm25 or index is None or index >= len(ctx.bm25_scores):
        return 0.5, 0.25
    raw = ctx.bm25_scores[index]
    if raw <= 0:
        return 0.05, 0.15
    value = raw / (raw + ctx.text_scale)
    return max(0.0, min(1.0, value)), 0.10


def _experience_fit(row, ctx: ScoreContext) -> tuple[float, float, dict]:
    required = parse_experience_years({
        "experience_years": row["experience_years"],
        "experience_text": row["experience_text"],
        "description": row["description"],
    })
    have, confirmed = _candidate_years(ctx.profile, ctx.resume)
    tolerance = float(ctx.settings.get("experience_tolerance_years", 1))
    hard = bool(ctx.settings["scoring"].get("hard_experience", True))
    info = {"required_years": required, "candidate_years": round(have, 1), "hard_fail": False}
    if required is None:
        # "No numeric experience mentioned" is not "one year required".
        # Qualitative experience requirements stay visible for review below.
        return 0.72, 0.20, info
    gap = required - have
    if gap <= 0:
        value = 1.0
        sigma = 0.03 if confirmed else 0.12
    elif gap <= tolerance:
        value = 0.72
        sigma = 0.08
    else:
        value = max(0.0, 0.55 - 0.16 * gap)
        sigma = 0.10
        info["hard_fail"] = hard and gap > tolerance + 0.5
    return value, sigma, info


def _mode_fit(row, ctx: ScoreContext) -> tuple[float, float]:
    mode = row["work_mode"] or detect_work_mode(row["description"] or "")
    prefs = [normalize(str(x)) for x in ((ctx.profile.get("preferences") or {}).get("work_modes") or [])]
    if not mode:
        return 0.6, 0.20
    mode_norm = normalize(mode)
    canonical = canonical_mode(mode)
    aliases = {
        "remote": ("remote", "удал", "дистанц"),
        "hybrid": ("hybrid", "гибрид"),
        "office": ("office", "офис", "полный", "на месте"),
    }.get(canonical, (mode_norm,))
    if any(p and (p in aliases or any(a in p for a in aliases)) for p in prefs):
        return 1.0, 0.03
    return 0.32, 0.08


def _location_fit(row, ctx: ScoreContext) -> tuple[float, float]:
    prefs = ctx.profile.get("preferences") or {}
    # Work mode explicitly declared by the source takes precedence over
    # incidental mentions of remote work in a vacancy's description.
    mode = canonical_mode(row["work_mode"] or "")
    loc_raw = normalize(row["location"] or "")
    foreign = any(marker in loc_raw for marker in FOREIGN_LOCATION_MARKERS)
    global_remote = any(marker in loc_raw for marker in GLOBAL_REMOTE_MARKERS)
    if mode == "remote":
        if foreign and not global_remote:
            return 0.2, 0.10
        return 1.0, 0.05
    if not mode and canonical_mode(detect_work_mode(row["description"] or "")) == "remote":
        return 1.0, 0.18
    locations = [normalize(str(x)) for x in (prefs.get("locations") or [])]
    loc = normalize(row["location"] or "")
    if not loc:
        return 0.55, 0.22
    if any(x and x in loc for x in locations):
        return 1.0, 0.04
    if prefs.get("relocation"):
        return 0.6, 0.12
    return 0.2, 0.06


def _salary_fit(row, ctx: ScoreContext) -> tuple[float, float]:
    target = (ctx.profile.get("preferences") or {}).get("salary_target_rub")
    currency = (row["currency"] or "RUB").upper()
    low, high = row["salary_min"] or 0, row["salary_max"] or 0
    if not target:
        return 0.7, 0.15
    try:
        target = float(target)
    except (TypeError, ValueError):
        return 0.7, 0.15
    if not low and not high:
        return 0.6, 0.2
    if currency not in ("RUB", "RUR", ""):
        return 0.55, 0.22
    best = high or low
    if low >= target:
        return 1.0, 0.03
    if best >= target:
        return 0.9, 0.05
    ratio = best / target if target else 0
    return max(0.15, ratio), 0.08


def _freshness_fit(row, ctx: ScoreContext) -> tuple[float, float]:
    age = _age_days(row)
    if age is None:
        return 0.62, 0.16
    fresh_days = float(ctx.settings["scoring"].get("fresh_days", 21))
    return max(0.35, math.exp(-age / max(5.0, fresh_days * 2))), 0.04


WEIGHT_KEYS = ("role", "skills", "text", "experience", "work_mode", "location", "salary", "freshness")


def score_row(row, ctx: ScoreContext, index: int | None = None) -> dict:
    prefs = ctx.profile.get("preferences") or {}
    title = row["title"] or ""
    excluded = [x for x in (prefs.get("excluded_titles") or []) if x and normalize(x) in normalize(title)]
    weights = {k: float((ctx.settings.get("weights") or {}).get(k, 0)) for k in WEIGHT_KEYS}
    wsum = sum(weights.values()) or 1.0

    role, role_sigma = _role_fit(row, ctx)
    skills, skills_sigma, skills_detail = _skills_fit(row, ctx)
    text, text_sigma = _text_fit(row, ctx, index)
    experience, exp_sigma, exp_info = _experience_fit(row, ctx)
    mode, mode_sigma = _mode_fit(row, ctx)
    location, loc_sigma = _location_fit(row, ctx)
    salary, salary_sigma = _salary_fit(row, ctx)
    freshness, fresh_sigma = _freshness_fit(row, ctx)

    factors = {
        "role": (role, role_sigma), "skills": (skills, skills_sigma), "text": (text, text_sigma),
        "experience": (experience, exp_sigma), "work_mode": (mode, mode_sigma),
        "location": (location, loc_sigma), "salary": (salary, salary_sigma),
        "freshness": (freshness, fresh_sigma),
    }
    z = float(ctx.settings["scoring"].get("uncertainty_z", 1.28))
    factor_input: dict[str, dict[str, float]] = {}
    for key in WEIGHT_KEYS:
        value, sigma = factors[key]
        factor_input[key] = {
            "score": value,
            "weight": weights[key],
            "low": max(0.0, value - z * sigma),
            "high": min(1.0, value + z * sigma),
        }
    explanation = ROUTER.run("score.explain", factors=factor_input, limit=3).value

    candidate_years, _ = _candidate_years(ctx.profile, ctx.resume)
    candidate_months = ctx.resume.get("total_experience_months")
    if not isinstance(candidate_months, (int, float)):
        candidate_months = candidate_years * 12.0
    if not ctx.resume.get("experience"):
        candidate_months = 0.0
    level = seniority_level(title)
    senior_threshold = 2.0 if level == "senior" else 3.0
    seniority_fail = (ctx.settings["scoring"].get("hard_seniority", True)
                      and level in SENIOR_LEVELS and candidate_years < senior_threshold)
    if seniority_fail:
        exp_info["seniority_fail"] = True
    min_cov = float(ctx.settings["scoring"].get("min_skill_coverage", 0))
    coverage = skills_detail["coverage"]
    job_text = _job_doc(row)
    commercial = commercial_signals(job_text)
    commercial_required = bool(commercial) and candidate_months < 1 and \
        bool(ctx.settings["scoring"].get("hard_commercial_experience", True))
    qualitative_experience = (exp_info.get("required_years") is None and
                              qualitative_experience_requirement(job_text))
    availability = str(prefs.get("availability") or "")
    schedule = schedule_evidence(row["description"] or "", availability,
                                 str(prefs.get("schedule") or ""))
    candidate_course = _candidate_course(ctx.resume)
    required_courses = _required_courses(_job_doc(row))
    course_mismatch = bool(required_courses and candidate_course is not None
                           and candidate_course not in required_courses)
    course_unknown = bool(required_courses and candidate_course is None)
    location_mismatch = (bool(prefs.get("locations")) and not prefs.get("relocation") and
                         (((canonical_mode(row["work_mode"] or "") in ("office", "hybrid"))
                           and location < 0.4)
                          or (any(m in normalize(row["location"] or "")
                                  for m in FOREIGN_LOCATION_MARKERS)
                              and not any(m in normalize(row["location"] or "")
                                          for m in GLOBAL_REMOTE_MARKERS))))

    job_low = normalize(_job_doc(row))
    education_have = bool(ctx.profile.get("education_confirmed") or
                          any(isinstance(e, dict) and e.get("verified") for e in ctx.resume.get("education") or []))
    education_required = bool(re.search(r"высшее образование|бакалавр|магистр|степень", job_low))
    education_strict = bool(re.search(r"обязательно (высшее|бакалавр|магистр)|строго высшее", job_low))
    license_required = bool(re.search(r"водительск\w* прав|права категории|лицензи|допуск сро|сертификат обязателен", job_low))
    license_have = bool(ctx.resume.get("certificates") or any(
        "прав" in normalize(str(x)) or "лиценз" in normalize(str(x))
        for x in ctx.profile.get("verified_skills") or []))
    hard_constraints: dict[str, bool | None] = {
        "excluded_category": False if excluded else True,
        # Unknown numeric years are NOT a mandatory constraint.
        # A clearly stated but unquantified requirement deserves review.
        "experience": (False if exp_info.get("hard_fail")
                       else (None if qualitative_experience else True)),
        "seniority": False if seniority_fail else True,
        "skills_floor": False if (min_cov and coverage < min_cov) else True,
        "commercial_experience": False if commercial_required else True,
        "education": (False if (education_required and education_strict and not education_have)
                      else (None if (education_required and not education_have) else True)),
        "license": (None if (license_required and not license_have) else True),
        "schedule": ({"incompatible": False, "review": None}.get(schedule["status"], True)),
        "course": False if course_mismatch else (None if course_unknown else True),
        # A job based in another city with required office attendance should
        # not be auto-queued for a candidate who explicitly refuses relocation.
        "location_compatibility": None if location_mismatch else True,
    }
    if ctx.settings["scoring"].get("hard_mode") and factors["work_mode"][0] < 0.4:
        hard_constraints["work_mode"] = False
    if ctx.settings["scoring"].get("hard_location") and factors["location"][0] < 0.4:
        hard_constraints["location"] = False

    match = ROUTER.run("match.score", factors=factor_input, hard_constraints=hard_constraints).value
    base_score = float(match["score"])
    low, high = match["sensitivity_range"]
    skill_match = round(100 * factors["skills"][0], 1)
    career_keys = ("role", "work_mode", "location", "salary", "freshness")
    career_weight = sum(weights[k] for k in career_keys) or 1.0
    career_fit = round(100 * sum(weights[k] * factors[k][0] for k in career_keys) / career_weight, 1)
    realism, realism_reasons = _realism(skill_match, exp_info, level, commercial, coverage,
                                        candidate_months, ctx.profile, ctx.resume)

    red_flags = analyze_job_text(_job_doc(row), dbm.job_skills(row)).red_flags
    penalty = min(12, 4 * len(red_flags))
    base_score = max(0.0, round(base_score - penalty, 1))
    low = max(0.0, round(low - penalty, 1))
    high = max(0.0, round(high - penalty, 1))
    hard_fail = bool(match["failed_constraints"])
    needs_review = match["eligible"] is None
    min_skill = float(ctx.settings["scoring"].get("min_skill_match", 0))
    min_realism = float(ctx.settings["scoring"].get("min_invite_realism", 35))
    low_skill = bool(min_skill and skill_match < min_skill)
    if hard_fail or excluded:
        score, low, high = 0.0, 0.0, 0.0
        eligibility = "rejected"
    elif needs_review:
        score = base_score
        eligibility = "review"
    elif low_skill or realism < min_realism:
        score = base_score
        eligibility = "weak"
    else:
        score = base_score
        eligibility = "eligible"
    eligible = bool(eligibility == "eligible" and score >= float(ctx.settings.get("threshold", 65)))
    utility = 0.0 if eligibility == "rejected" else round(
        0.55 * skill_match + 0.30 * career_fit + 0.15 * 100 * factors["experience"][0], 1)
    confidence = 0.0 if eligibility == "rejected" else round(
        max(0.0, min(1.0, 1 - (high - low) / 100)), 2)
    schedule_fit = {"compatible": 100.0, "unknown": 60.0, "review": 45.0,
                    "incompatible": 0.0}.get(schedule.get("status", "unknown"), 60.0)
    level_growth = {"intern": 1.0, "junior": 0.9, "unspecified": 0.75, "middle": 0.45,
                    "senior": 0.15}.get(level, 0.15)
    growth = round(100 * (0.45 * factors["role"][0] + 0.25 * (1 - coverage) +
                          0.20 * level_growth + 0.10 * factors["freshness"][0]), 1)
    role_fit = factors["role"][0]
    category_reasons: list[str] = []
    if hard_fail or excluded:
        category = "rejected"
        category_reasons = ["есть подтверждённое критическое несоответствие"] + \
            [str(x) for x in (match.get("failed_constraints") or [])][:3]
    elif eligible:
        category = "strong"
        category_reasons = ["обязательные требования и порог пройдены"]
    elif (score >= 35 and not seniority_fail and schedule.get("status") != "incompatible"
          and (role_fit >= 0.35 or skill_match >= 40 or level in ("intern", "junior"))):
        category = "opportunity"
        if skill_match < float(ctx.settings["scoring"].get("min_skill_match", 50)):
            category_reasons.append(f"навыки {skill_match:.0f}/100 ниже строгого порога")
        if needs_review:
            category_reasons.append("часть требований не указана")
        if level in ("intern", "junior"):
            category_reasons.append("уровень входа")
        if realism < float(ctx.settings["scoring"].get("min_invite_realism", 35)):
            category_reasons.append("реалистичность ниже порога")
        if not category_reasons:
            category_reasons.append("роль близка профилю, есть пространство для роста")
    else:
        category = "rejected"
        if role_fit < 0.35:
            category_reasons.append("роль далека от профиля")
        if seniority_fail:
            category_reasons.append("уровень выше подтверждённого опыта")
        if schedule.get("status") == "incompatible":
            category_reasons.append("несовместимый график")
        if score < 35:
            category_reasons.append("низкое соответствие")
        if not category_reasons:
            category_reasons.append("нет достаточных оснований для отклика")

    reason = _reason(score, eligible, factors, skills_detail, exp_info, excluded, red_flags,
                     low, high, explanation, needs_review, skill_match, career_fit, eligibility,
                     commercial=commercial, realism=realism, realism_reasons=realism_reasons)
    if schedule["signals"]:
        reason += "; " + ", ".join(schedule["signals"])
    if course_mismatch:
        reason += f"; требуется {', '.join(map(str, sorted(required_courses)))} курс, у вас {candidate_course}"
    if course_unknown:
        reason += "; курс обучения не подтверждён — нужна проверка"
    if location_mismatch:
        reason += "; гибрид/офис в другом городе — уточнить присутствие и переезд"
    detail = {
        "model": MODEL_VERSION,
        "scores": {
            "skill_match": skill_match,
            "career_fit": career_fit,
            "utility": utility,
            "confidence": confidence,
            "match_score": score,
            "invite_realism": realism,
            "schedule_fit": schedule_fit,
            "growth_opportunity": growth,
            "experience_gap_years": (None if exp_info.get("required_years") is None
                                     else round(float(exp_info["required_years"]) - candidate_years, 1)),
        },
        "eligibility": eligibility,
        "category": category,
        "category_reasons": category_reasons,
        "realism": {"value": realism, "reasons": realism_reasons,
                    "positive": [
                        text for ok, text in (
                            (skill_match >= 60, f"навыки {skill_match:.0f}/100"),
                            (exp_info.get("required_years") is None or
                             float(exp_info.get("required_years") or 0) <= candidate_years,
                             "опыт соответствует"),
                            (level in ("intern", "junior"), "уровень входа"),
                            (coverage >= 0.45, "достаточное покрытие требований"),
                        ) if ok
                    ],
                    "confidence": confidence,
                    "note": "Экспертная оценка реалистичности приглашения, не вероятность."},
        "commercial_experience": {"required": commercial_required,
                                  "signals": commercial,
                                  "candidate_months": round(float(candidate_months or 0), 1)},
        "schedule": schedule,
        "location_mismatch": bool(location_mismatch),
        "qualitative_experience_review": bool(qualitative_experience),
        "factors": {k: {"value": round(v[0], 3), "weight": round(weights[k] / wsum, 3),
                        "sigma": round(v[1], 3),
                        "low": round(factor_input[k]["low"], 3),
                        "high": round(factor_input[k]["high"], 3)} for k, v in factors.items()},
        "interval": {"low": low, "high": high, "kind": "sensitivity"},
        "explanation": explanation,
        "skills": skills_detail,
        "experience": exp_info,
        "education": {"required": education_required, "strict": education_strict,
                      "confirmed": education_have, "required_courses": sorted(required_courses),
                      "candidate_course": candidate_course, "course_mismatch": course_mismatch},
        "license": {"required": license_required, "confirmed": license_have},
        "red_flags": red_flags,
        "penalty": penalty,
        "hard_fail": hard_fail,
        "failed_constraints": match["failed_constraints"],
        "needs_review": needs_review,
        "seniority": level,
        "excluded_hit": excluded[:3],
        "math_method": "eligibility + skill/career/utility decomposition",
    }
    return {"score": score, "low": low, "high": high, "eligible": eligible,
            "needs_review": needs_review, "reason": reason, "detail": detail,
            "sigma": math.sqrt(sum((weights[k] * factors[k][1]) ** 2 for k in WEIGHT_KEYS)) / wsum}


def _reason(score, eligible, factors, skills_detail, exp_info, excluded, red_flags, low, high,
            explanation=None, needs_review=False, skill_match=0, career_fit=0,
            eligibility="eligible", commercial=None, realism=None,
            realism_reasons=None) -> str:
    if excluded:
        return f"Исключённая категория: {', '.join(excluded[:2])}"
    parts = [f"Соответствие {score:.0f}/100 (диапазон {low:.0f}–{high:.0f}); "
             f"навыки {skill_match:.0f}/100, карьера {career_fit:.0f}/100; статус: {eligibility}"]
    if needs_review:
        parts.append("нужна проверка: часть обязательных требований не указана")
    if eligibility == "weak":
        parts.append(f"низкое соответствие: навыки {skill_match:.0f}/100 ниже порога")
    if commercial:
        parts.append("нужен production/коммерческий опыт: " + ", ".join(commercial[:3]))
    if exp_info.get("hard_fail"):
        parts.append(f"жёсткий фильтр опыта: нужно {exp_info['required_years']:g} г., подтверждено {exp_info['candidate_years']:g} г.")
    if exp_info.get("seniority_fail"):
        parts.append(f"уровень {exp_info.get('seniority', 'senior/lead')} не соответствует подтверждённому опыту")
    if skills_detail.get("required_missing"):
        parts.append("не подтверждено: " + ", ".join(skills_detail["required_missing"][:4]))
    elif skills_detail.get("matched"):
        parts.append("совпало: " + ", ".join(skills_detail["matched"][:5]))
    if explanation:
        top = ", ".join(f"{x['factor']} +{x['contribution']:.0f}" for x in explanation)
        parts.append("сильнее всего: " + top)
    weak = [k for k, (v, _) in factors.items() if v < 0.4]
    if weak:
        parts.append("слабо: " + ", ".join(weak))
    if red_flags:
        parts.append("флаги: " + ", ".join(red_flags))
    if realism is not None:
        reasons = f" ({', '.join(realism_reasons)})" if realism_reasons else ""
        parts.append(f"реалистичность приглашения {realism:.0f}/100{reasons}")
    if not eligible and score >= 60 and not needs_review and eligibility != "weak":
        parts.append("ниже порога")
    return "; ".join(parts)


# --------------------------------------------------------------------------- batch

def stage_a_scores(rows: list, ctx: ScoreContext) -> list[tuple[float, str]]:
    """Cheap pass used before paying for detail requests."""
    out = []
    for i, row in enumerate(rows):
        role, _ = _role_fit(row, ctx)
        text, _ = _text_fit(row, ctx, i)
        price = 0.0
        if row["salary_min"]:
            price = 0.7
        title = normalize(row["title"] or "")
        bad = any(normalize(x) in title for x in ((ctx.profile.get("preferences") or {}).get("excluded_titles") or []))
        value = 100 * (0.45 * role + 0.4 * text + 0.15 * price) - (40 if bad else 0)
        out.append((round(value, 2), row["id"]))
    return out


def rank_pending(db: sqlite3.Connection, settings: dict, profile: dict, resume: dict,
                 *, limit: int | None = None) -> dict:
    rows = db.execute(
        "SELECT * FROM jobs WHERE duplicate_of IS NULL ORDER BY fetched_at DESC LIMIT ?",
        (limit or 5000,),
    ).fetchall()
    if not rows:
        return {"scored": 0, "eligible": 0, "shortlist": 0}
    ctx = build_context(rows, settings, profile, resume)
    index_by_id = {row["id"]: i for i, row in enumerate(rows)}
    scored = eligible = 0
    for row in rows:
        result = score_row(row, ctx, index_by_id.get(row["id"]))
        existing = dbm.job_detail(row)
        for key in ("enrichment", "apply_url", "source_post_url", "enriched_at",
                    "enrichment_error", "enrich_attempted_at"):
            if key in existing:
                result["detail"][key] = existing[key]
        db.execute(
            """UPDATE jobs SET score=?, score_low=?, score_high=?, eligible=?, needs_review=?,
                      reason=?, detail=?, stage='scored', updated_at=? WHERE id=?""",
            (result["score"], result["low"], result["high"], int(result["eligible"]),
             int(result.get("needs_review", False)),
             result["reason"], json.dumps(result["detail"], ensure_ascii=False), dbm.now(), row["id"]),
        )
        if row["score"] != result["score"] or bool(row["eligible"]) != bool(result["eligible"]):
            scores = result["detail"].get("scores") or {}
            db.execute(
                """INSERT INTO score_history(job_id,model,score,skill_match,career_fit,utility,
                     eligibility,features,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (row["id"], MODEL_VERSION, result["score"], scores.get("skill_match"),
                 scores.get("career_fit"), scores.get("utility"), result["detail"].get("eligibility"),
                 json.dumps(scores, ensure_ascii=False), dbm.now()))
        scored += 1
        eligible += int(result["eligible"])
    db.commit()
    shortlist = [r["id"] for r in db.execute(
        "SELECT id FROM jobs WHERE duplicate_of IS NULL AND eligible=1 ORDER BY score DESC LIMIT ?",
        (int(settings["scoring"].get("shortlist_limit", 150)),)
    ).fetchall()]
    return {"scored": scored, "eligible": eligible, "shortlist": len(shortlist)}


def shortlist_ids(db: sqlite3.Connection, settings: dict, limit: int | None = None) -> list[str]:
    limit = limit or int(settings["scoring"].get("shortlist_limit", 150))
    return [r["id"] for r in db.execute(
        """SELECT id FROM jobs WHERE duplicate_of IS NULL AND stage='scored'
           ORDER BY eligible DESC, score DESC LIMIT ?""", (limit,)
    ).fetchall()]


# --------------------------------------------------------------------------- calibration

def extract_features(row) -> list[float]:
    detail = dbm.job_detail(row)
    f = detail.get("factors") or {}
    return [
        1.0,
        float(row["score"] or 0) / 100.0,
        float((f.get("skills") or {}).get("value", 0)) ** 1.5,
        float((f.get("role") or {}).get("value", 0)),
        float((f.get("experience") or {}).get("value", 0)),
        1.0 if row["letter"] else 0.0,
    ]


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def train_calibration(db: sqlite3.Connection, settings: dict | None = None) -> dict:
    """Chronological train/test split on confirmed, non-synthetic outcomes.

    Positives are invitations/offers. Rejections are negatives. Mere replies and
    pending applications are not used. The model is not trained on demo data.
    """
    from .mathx import calibration_metrics, fit_logistic, predict_logistic
    conf = ((settings or {}).get("calibration") or {})
    min_samples = int(conf.get("min_samples", 60))
    min_positives = int(conf.get("min_positives", 12))
    min_negatives = int(conf.get("min_negatives", 12))
    min_test = int(conf.get("min_test", 15))
    rows = db.execute(
        """SELECT j.*, MAX(CASE WHEN o.stage IN ('invited','offer') THEN 1 ELSE 0 END) AS positive,
                  MIN(o.created_at) AS first_outcome, MAX(o.created_at) AS last_outcome
           FROM outcomes o JOIN jobs j ON j.id=o.job_id
           WHERE o.stage IN ('invited','offer','rejected')
             AND j.source NOT IN ('demo','synthetic','fixture')
             AND j.id NOT LIKE 'synthetic-%'
           GROUP BY j.id
           ORDER BY last_outcome ASC"""
    ).fetchall()
    n = len(rows)
    positives = sum(int(r["positive"] or 0) for r in rows)
    negatives = n - positives
    if n < min_samples or positives < min_positives or negatives < min_negatives:
        return {"trained": False, "samples": n, "positives": positives, "negatives": negatives,
                "reason": (f"нужно ≥{min_samples} подтверждённых исходов, ≥{min_positives} приглашений "
                           f"и ≥{min_negatives} отказов; есть {n}/{positives}/{negatives}")}
    split = max(1, min(n - 1, int(n * 0.7)))
    train, test = rows[:split], rows[split:]
    y_train = [int(r["positive"] or 0) for r in train]
    y_test = [int(r["positive"] or 0) for r in test]
    if len(test) < min_test:
        return {"trained": False, "samples": n, "positives": positives, "negatives": negatives,
                "reason": f"тестовая часть слишком мала: {len(test)} < {min_test}"}
    if len(set(y_train)) < 2 or len(set(y_test)) < 2:
        return {"trained": False, "samples": n, "positives": positives, "negatives": negatives,
                "reason": "в хронологическом train/test split нет обоих классов; нужно больше данных"}
    X_train = [extract_features(r) for r in train]
    X_test = [extract_features(r) for r in test]
    weights = fit_logistic(X_train, y_train)
    p_train = [predict_logistic(weights, x) for x in X_train]
    p_test = [predict_logistic(weights, x) for x in X_test]
    train_metrics = calibration_metrics(y_train, p_train)
    test_metrics = calibration_metrics(y_test, p_test)
    model = {
        "trained": True, "weights": weights,
        "samples": n, "train_n": len(train), "test_n": len(test),
        "positives": positives, "negatives": negatives,
        "features": ["intercept", "match_score", "skills^1.5", "role", "experience", "has_letter"],
        "base_rate": round(positives / n, 4),
        "train": train_metrics, "test": test_metrics,
        "brier": test_metrics["brier"], "log_loss": test_metrics["log_loss"],
        "auc": test_metrics["auc"], "ece": test_metrics["ece"],
        "reliability": test_metrics["reliability"],
        "split": "chronological 70/30",
        "excluded_sources": ["demo", "synthetic", "fixture"],
        "trained_at": dbm.now(),
        "data_through": str(rows[-1]["last_outcome"] or ""),
    }
    from .paths import DATA
    (DATA / "calibration.json").write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
    return model


def calibrated_probability(score: float, skills: float, role: float, experience: float,
                           has_letter: bool) -> float | None:
    from .paths import DATA
    path = DATA / "calibration.json"
    if not path.exists():
        return None
    try:
        model = json.loads(path.read_text(encoding="utf-8"))
        w = model.get("weights") or []
        if not model.get("trained") or len(w) != 6:
            return None
        x = [1.0, score / 100.0, max(0.0, skills) ** 1.5, role, experience, 1.0 if has_letter else 0.0]
        return round(_sigmoid(sum(a * b for a, b in zip(w, x))), 3)
    except (OSError, json.JSONDecodeError):
        return None
