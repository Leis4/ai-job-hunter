"""Career search strategy: queries generated from the candidate profile.

The source adapters are deliberately dumb; this module decides *what* to look
for. It mixes Russian and English titles, entry-level variants, adjacent roles
and the candidate's verified skills. Queries rotate between runs so a single
cycle does not have to scan the whole market.
"""
from __future__ import annotations

from datetime import date

from .text import normalize

GROUPS: list[tuple[str, list[str]]] = [
    ("analytics", [
        "аналитик данных", "data analyst", "junior analyst", "python analyst",
        "sql analyst", "продуктовый аналитик", "аналитик автоматизации",
        "аналитик цифрового аудита", "аналитик данных стажер",
    ]),
    ("data_science", [
        "data scientist", "junior data scientist", "ml intern", "machine learning",
        "ml engineer", "стажер машинное обучение", "исследователь ml",
        "прикладной ml", "junior ml",
    ]),
    ("python", [
        "python", "python стажер", "python intern", "junior python",
        "backend junior", "fastapi", "python automation", "автоматизация python",
    ]),
    ("data_engineering", [
        "data engineer", "junior data engineer", "data engineer intern", "etl",
        "sql developer", "аналитик хранилищ данных", "python etl",
    ]),
    ("ai_llm", [
        "llm", "llm intern", "nlp", "nlp intern", "ai research", "ai evaluation",
        "ai agent", "качество llm", "стажер nlp",
    ]),
    ("adjacent", [
        "model validation", "model risk", "data quality", "risk analytics",
        "fraud analytics", "process mining", "аналитик бизнес-процессов",
        "цифровой аудит", "аналитик рисков",
    ]),
]

LEVEL_WORDS = ("стажер", "стажёр", "intern", "trainee", "junior", "начинающий")


def _profile_groups(profile: dict) -> list[str]:
    """Priority order from explicit target roles, then the default order."""
    prefs = profile.get("preferences") or {}
    text = normalize(" ".join(str(x) for x in (prefs.get("role_keywords") or [])))
    priority: list[str] = []
    if any(w in text for w in ("аналит", "analyst", "bi", "данн")):
        priority += ["analytics"]
    if any(w in text for w in ("data scien", "ml", "machine", "машин", "нейро")):
        priority += ["data_science"]
    if any(w in text for w in ("python", "backend", "разработ")):
        priority += ["python"]
    if any(w in text for w in ("data engineer", "etl", "хранилищ")):
        priority += ["data_engineering"]
    if any(w in text for w in ("llm", "nlp", "ai ")):
        priority += ["ai_llm"]
    for name, _ in GROUPS:
        if name not in priority:
            priority.append(name)
    return priority


def _skill_queries(profile: dict, resume: dict) -> list[str]:
    skills = [str(s) for s in (profile.get("verified_skills") or [])]
    useful = [s for s in skills if s.casefold() in
              ("python", "sql", "pandas", "pytorch", "fastapi", "docker", "git")]
    return [f"{s} стажер" for s in useful[:3]]


def strategy_queries(profile: dict, settings: dict, source: str,
                     limit: int | None = None, resume: dict | None = None) -> list[str]:
    """Round-robin over career groups, with deterministic per-day rotation."""
    search = settings.get("search") or {}
    cap = int(limit or (search.get("max_queries") or {}).get(source, 8))
    groups = {name: list(qs) for name, qs in GROUPS}
    order = _profile_groups(profile)
    personal = _skill_queries(profile, resume or {})
    if personal:
        groups.setdefault("personal", []).extend(personal)
        order.append("personal")

    offset = date.today().toordinal() % max(1, len(order))
    order = order[offset:] + order[:offset]
    seen: set[str] = set()
    out: list[str] = []
    index = 0
    while len(out) < cap:
        added = False
        for name in order:
            pool = groups.get(name) or []
            if index < len(pool):
                query = pool[index]
                key = normalize(query)
                if key and key not in seen:
                    seen.add(key)
                    out.append(query)
                    added = True
                    if len(out) >= cap:
                        break
        if not added:
            break
        index += 1
    return out
