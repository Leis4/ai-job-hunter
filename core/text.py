"""Text analysis: normalization, tokenization, light stemming, skills, salary.

This module is deterministic and dependency-free. It deliberately avoids
"magic": every extracted requirement keeps the sentence that produced it.
"""
from __future__ import annotations

import html
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

WORD_RE = re.compile(r"[a-zа-я0-9][a-zа-я0-9+#._/-]*", re.IGNORECASE)
COMPOUND_TOKENS = {"c++", "c#", "scikit-learn", "node.js", "ci/cd", "a/b", "1c", "ms sql"}
SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[ \t\u00a0\u202f]+")

RU_SUFFIXES = (
    "иями", "ями", "ами", "ов", "ев", "ий", "ый", "ая", "яя", "ое", "ее", "ые", "ие",
    "ой", "ей", "ом", "ем", "ах", "ях", "ую", "юю", "ым", "им", "ых", "их", "ою", "ею",
    "а", "я", "ы", "и", "о", "е", "у", "ю", "ь",
)
EN_SUFFIXES = ("ations", "ation", "ings", "ing", "ers", "er", "ies", "ied", "ed", "es", "s")

# Canonical concept -> surface forms. Order matters only for display.
SYNONYM_GROUPS: dict[str, tuple[str, ...]] = {
    "разработ": ("разработчик", "разработка", "разрабатывать", "разработке", "developer", "development", "develop"),
    "аналит": ("аналитик", "аналитика", "анализ", "analyst", "analytics", "analysis", "analyze"),
    "данн": ("данные", "данных", "data", "dataset", "datasets"),
    "тест": ("тестирование", "тестировщик", "тесты", "testing", "qa", "test"),
    "поддержк": ("поддержка", "сопровождение", "support"),
    "руковод": ("руководитель", "руководство", "lead", "leader", "head"),
    "менедж": ("менеджер", "менеджмент", "manager", "management"),
    "инженер": ("инженер", "engineer", "engineering"),
    "систем": ("система", "системный", "system", "systems"),
    "проект": ("проект", "проектный", "project", "projects"),
    "обучен": ("обучение", "обучать", "machine learning", "ml", "обучение модели"),
}

STOPWORDS = set("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только
ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни
быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они тут где
есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет
ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы
нее сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над
больше тот через эти нас про всего них какая много разве три эту моя впрочем хорошо свою
этой перед иногда лучше чуть том нельзя такой им более всегда конечно всю между
the a an and or of to in for with on at by from is are was were be been being will would
can could should may might must this that these those as it its their our your you we they
he she them his her not no if then than so such into over under about across per
""".split())

# Skill lexicon. Aliases are matched case-insensitively with word boundaries.
SKILLS: dict[str, tuple[str, ...]] = {
    "Python": ("python", "питон"),
    "SQL": ("sql", "скл"),
    "PostgreSQL": ("postgresql", "postgres", "постгрес"),
    "MySQL": ("mysql",),
    "SQLite": ("sqlite",),
    "Oracle": ("oracle", "оракл"),
    "ClickHouse": ("clickhouse", "кликхаус"),
    "MS SQL": ("ms sql", "mssql", "sql server"),
    "MongoDB": ("mongodb", "mongo"),
    "Redis": ("redis",),
    "Kafka": ("kafka", "кафка"),
    "Spark": ("spark", "pyspark", "спарк"),
    "Hadoop": ("hadoop",),
    "Airflow": ("airflow",),
    "dbt": ("dbt",),
    "ETL": ("etl", "elt"),
    "Pandas": ("pandas", "пандас"),
    "NumPy": ("numpy", "numpy"),
    "SciPy": ("scipy",),
    "scikit-learn": ("scikit-learn", "sklearn", "scikit learn"),
    "PyTorch": ("pytorch", "torch"),
    "TensorFlow": ("tensorflow", "tensor flow"),
    "Keras": ("keras",),
    "XGBoost": ("xgboost",),
    "CatBoost": ("catboost",),
    "LightGBM": ("lightgbm",),
    "ML": ("machine learning", "машинное обучение", "ml", "мл"),
    "LLM": ("llm", "large language model", "большая языковая модель", "языковая модель"),
    "RAG": ("rag", "retrieval augmented generation"),
    "NLP": ("nlp", "natural language processing", "обработка естественного языка"),
    "Computer Vision": ("computer vision", "компьютерное зрение", "cv"),
    "Deep Learning": ("deep learning", "глубокое обучение", "нейронные сети", "нейросет"),
    "MLOps": ("mlops", "ml ops"),
    "Docker": ("docker", "докер"),
    "Kubernetes": ("kubernetes", "k8s", "кубернетес"),
    "Linux": ("linux", "линукс", "unix"),
    "Bash": ("bash", "shell", "шелл"),
    "Git": ("git", "гит", "github", "gitlab"),
    "CI/CD": ("ci/cd", "cicd", "continuous integration", "непрерывная интеграция"),
    "REST": ("rest", "rest api", "restful"),
    "gRPC": ("grpc",),
    "GraphQL": ("graphql",),
    "FastAPI": ("fastapi", "фастапи"),
    "Django": ("django", "джанго"),
    "Flask": ("flask",),
    "aiohttp": ("aiohttp",),
    "asyncio": ("asyncio",),
    "Celery": ("celery",),
    "RabbitMQ": ("rabbitmq",),
    "Nginx": ("nginx",),
    "HTML": ("html", "хтмл"),
    "CSS": ("css",),
    "JavaScript": ("javascript", "js", "джаваскрипт"),
    "TypeScript": ("typescript", "ts"),
    "React": ("react", "реакт"),
    "Vue": ("vue", "vue.js", "vuejs"),
    "Node.js": ("node.js", "nodejs", "node js"),
    "Go": ("golang", "go lang", "язык go"),
    "Java": ("java", "джава"),
    "C++": ("c++", "cpp", "си++"),
    "C#": ("c#", "c sharp", "си шарп"),
    "1C": ("1c", "1с", "1c:предприятие"),
    "Excel": ("excel", "эксель", "таблицы excel"),
    "Google Sheets": ("google sheets", "гугл таблицы"),
    "Power BI": ("power bi", "powerbi", "повер би"),
    "Tableau": ("tableau",),
    "Superset": ("superset",),
    "Grafana": ("grafana",),
    "A/B testing": ("a/b", "ab тест", "a/b тест", "split test"),
    "Statistics": ("statistics", "статистика", "статистический", "матстат"),
    "Probability": ("probability", "теория вероятностей", "вероятность"),
    "Linear Algebra": ("linear algebra", "линейная алгебра"),
    "Optimization": ("optimization", "оптимизация", "градиентный спуск"),
    "Product Analytics": ("product analytics", "продуктовая аналитика"),
    "BI": ("bi", "business intelligence", "бизнес-аналитика"),
    "Data Quality": ("data quality", "качество данных"),
    "Data Warehouse": ("data warehouse", "хранилище данных", "dwh", "схема звезда"),
    "Data Engineering": ("data engineering", "инженерия данных", "data engineer"),
    "Agile": ("agile", "scrum", "спринт", "канбан", "kanban"),
    "Jira": ("jira", "джира"),
    "Confluence": ("confluence",),
    "Figma": ("figma",),
    "API": ("api", "апи", "интеграция по api"),
    "Microservices": ("microservices", "микросервис"),
    "Windows": ("windows", "виндовс"),
    "Telegram Bot": ("telegram bot", "телеграм бот", "aiogram"),
    "Playwright": ("playwright",),
    "Selenium": ("selenium",),
    "BeautifulSoup": ("beautifulsoup", "bs4"),
    "Web Scraping": ("парсинг", "scraping", "веб-скрапинг", "сбор данных с сайтов"),
}

NICE_CUES = (
    "будет плюсом", "будет преимуществом", "желательно", "приветствуется", "плюсом будет",
    "nice to have", "is a plus", "would be a plus", "preferred", "optional", "дополнительным преимуществом",
)
REQUIRED_CUES = (
    "требуется", "требования", "необходимо", "обязательно", "обязательные", "must have", "required",
    "уверенное владение", "отличное знание", "знание", "опыт работы с", "опыт работы", "не менее",
    "владение", "понимание", "hemat", "необходимые",
)
RED_FLAG_PATTERNS = {
    "без оформления": r"без оформлени|неофициальн\w* (?:трудоустройств|оформлен)",
    "без оплаты": r"без оплаты|неоплачиваем\w* (?:стажировк|испытательн)",
    "нереалистичный стек": r"опыт\w* (?:от|более) (?:7|8|9|10) лет",
    "срочный договор": r"срочный трудовой договор",
}


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|li|div|h[1-6]|tr|ul|ol)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "• ", text)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = SPACE_RE.sub(" ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_text(text: str) -> str:
    text = strip_html(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return SPACE_RE.sub(" ", text).strip()


def normalize(text: str) -> str:
    return (text or "").casefold().replace("ё", "е").replace("–", "-").replace("—", "-")


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for match in WORD_RE.finditer(normalize(text)):
        raw = match.group(0).strip("._/-")
        if not raw:
            continue
        if raw in COMPOUND_TOKENS:
            out.append(raw)
            continue
        # "Python-разработчик" must yield python + разработчик, not one token.
        for part in re.split(r"[-/._]+", raw):
            part = part.strip()
            if part:
                out.append(part)
    return out


def stem(word: str) -> str:
    if not word:
        return word
    if word in SYNONYM_GROUPS:
        return word
    for canonical, forms in SYNONYM_GROUPS.items():
        if word in forms:
            return canonical
    lowered = word
    if lowered.isascii():
        if len(lowered) > 5:
            for suffix in EN_SUFFIXES:
                if lowered.endswith(suffix) and len(lowered) - len(suffix) >= 4:
                    lowered = lowered[: -len(suffix)]
                    break
        return lowered
    if len(lowered) > 4:
        for suffix in RU_SUFFIXES:
            if lowered.endswith(suffix) and len(lowered) - len(suffix) >= 4:
                lowered = lowered[: -len(suffix)]
                break
    return lowered


def tokens(text: str, *, remove_stop: bool = True, stems: bool = True) -> list[str]:
    out = []
    for token in tokenize(text):
        if len(token) < 2 and not token.isdigit():
            continue
        if remove_stop and token in STOPWORDS:
            continue
        out.append(stem(token) if stems else token)
    return out


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_RE.split(clean_text(text)) if s.strip()]


def phrase_pattern(phrase: str) -> re.Pattern:
    parts = [re.escape(p) for p in normalize(phrase).split() if p]
    return re.compile(r"(?<![a-zа-я0-9+#])" + r"[\s\-]+".join(parts) + r"(?![a-zа-я0-9+#])")


def contains_phrase(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    return bool(phrase_pattern(phrase).search(normalize(text)))


def extract_skills(text: str) -> dict[str, dict]:
    """Return canonical skill -> {aliases, sentence, requirement, index}."""
    normalized = normalize(text)
    found: dict[str, dict] = {}
    sents = sentences(text)
    for canonical, aliases in SKILLS.items():
        for alias in aliases:
            pattern = phrase_pattern(alias)
            match = pattern.search(normalized)
            if not match:
                continue
            pos = match.start()
            sentence = _sentence_at(text, sents, alias)
            found[canonical] = {
                "alias": alias,
                "sentence": sentence[:400],
                "requirement": classify_requirement(sentence),
                "position": pos,
            }
            break
    return found


def _sentence_at(full: str, sents: list[str], alias: str) -> str:
    pattern = phrase_pattern(alias)
    for sentence in sents:
        if pattern.search(normalize(sentence)):
            return sentence
    return ""


def classify_requirement(sentence: str) -> str:
    low = normalize(sentence)
    if any(cue in low for cue in NICE_CUES):
        return "nice"
    if any(cue in low for cue in REQUIRED_CUES):
        return "required"
    if "!" in sentence and len(sentence) < 120:
        return "required"
    return "mentioned"


def resume_skill_set(profile: dict, resume: dict | None = None) -> dict[str, str]:
    """Verified skills only, canonicalized. learning_interests are excluded."""
    mapping: dict[str, str] = {}
    for skill in (profile.get("verified_skills") or []):
        canonical = canonical_skill(str(skill))
        mapping[canonical] = str(skill)
    if resume:
        for item in resume.get("skills") or []:
            if isinstance(item, dict) and item.get("verified"):
                canonical = canonical_skill(str(item.get("name", "")))
                mapping.setdefault(canonical, str(item.get("name", "")))
    return mapping


def canonical_skill(name: str) -> str:
    low = normalize(name).strip()
    if not low:
        return ""
    if low in SKILLS:
        return low
    for canonical, aliases in SKILLS.items():
        if low == canonical.casefold() or low in aliases:
            return canonical
    # Verbose list entries: "Навык работы с SQL", "Знание Python / Питон".
    best: tuple[str, str] | None = None
    for canonical, aliases in SKILLS.items():
        for alias in (canonical, *aliases):
            if len(alias) < 2:
                continue
            if phrase_pattern(alias).search(low):
                if best is None or len(alias) > len(best[1]):
                    best = (canonical, alias)
    if best:
        return best[0]
    # Typo tolerance, explicitly conservative (score >= 90, length >= 4).
    if len(low) >= 5 and " " not in low:
        fuzzy = _fuzzy_skill(low)
        if fuzzy:
            return fuzzy
    return name.strip()


def _fuzzy_skill(low: str) -> str | None:
    try:
        from rapidfuzz import fuzz, process
        canonical_by_lower = {name.casefold(): name for name in SKILLS}
        match, score, _ = process.extractOne(low, list(canonical_by_lower), scorer=fuzz.ratio)
        return canonical_by_lower.get(match) if score >= 82 else None
    except Exception:
        return None


def parse_experience_years(job: dict) -> float | None:
    """Numeric minimum experience. Never guessed when the text is ambiguous."""
    explicit = job.get("experience_years")
    if explicit not in (None, ""):
        try:
            return float(explicit)
        except (TypeError, ValueError):
            pass
    level = str(job.get("experience_level") or "").casefold()
    level_map = {
        "noexperience": 0.0, "no_experience": 0.0, "без опыта": 0.0,
        "between1and3": 1.0, "between3and6": 3.0, "morethan6": 6.0,
        "от 1 года до 3 лет": 1.0, "от 3 до 6 лет": 3.0, "более 6 лет": 6.0,
    }
    if level in level_map:
        return level_map[level]
    text = normalize(" ".join(str(job.get(k) or "") for k in ("experience_text", "requirements", "description")))
    if not text:
        return None
    if re.search(r"без опыта|опыт\w* не треб|не требуется опыт|no experience", text):
        return 0.0
    patterns = (
        r"(?:опыт\w*[^.;]{0,40}?)?(?:от|не менее|минимум|более)\s*(\d{1,2})\s*(?:\+)?\s*(?:год|лет|года)",
        r"(\d{1,2})\s*\+\s*(?:год|лет|года)\s*(?:опыт|работ)",
        r"(?:experience|опыт)\D{0,12}(\d{1,2})\s*(?:years|год|лет)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            years = float(match.group(1))
            if 0 <= years <= 30:
                return years
    # A bare mention of "опыт работы" is not evidence of one *year* of experience.
    # Distinguish missing data from an explicit numeric minimum; the scoring
    # layer can flag qualitative requirements for manual review if necessary.
    return None


def parse_salary(text: str, *, min_value=None, max_value=None) -> tuple[int | None, int | None, str]:
    if min_value not in (None, ""):
        try:
            low = int(float(min_value))
        except (TypeError, ValueError):
            low = None
    else:
        low = None
    if max_value not in (None, ""):
        try:
            high = int(float(max_value))
        except (TypeError, ValueError):
            high = None
    else:
        high = None
    currency = "RUB"
    if low or high:
        return low, high, currency
    raw = clean_text(text)
    if not raw:
        return None, None, currency
    low_part = re.search(r"(?:от|from)\s*([\d \u00a0]{4,12})", raw, re.IGNORECASE)
    high_part = re.search(r"(?:до|to)\s*([\d \u00a0]{4,12})", raw, re.IGNORECASE)
    numbers = [int(n.replace(" ", "").replace("\u00a0", ""))
               for n in re.findall(r"\d[\d \u00a0]{3,}", raw)]
    if "usd" in raw.lower() or "$" in raw:
        currency = "USD"
    elif "eur" in raw.lower() or "€" in raw:
        currency = "EUR"
    if low_part:
        low = int(low_part.group(1).replace(" ", "").replace("\u00a0", ""))
    if high_part:
        high = int(high_part.group(1).replace(" ", "").replace("\u00a0", ""))
    if not low and not high and numbers:
        low = min(numbers)
        high = max(numbers) if len(numbers) > 1 else None
    return low, high, currency


def detect_work_mode(text: str) -> str:
    low = normalize(text)
    if re.search(r"удален|remote|дистанцион", low):
        return "remote"
    if re.search(r"гибрид|hybrid", low):
        return "hybrid"
    if re.search(r"офис|office|на месте работодателя|полный рабочий день", low):
        return "office"
    return ""


def classify_pay(text: str, badges: str = "") -> str:
    """Return paid/unpaid/equity/unknown strictly from published wording.

    Conditional phrases such as "возможность продолжить на оплачиваемой
    основе" describe a possible future, not payment for this role.
    """
    low = f"{badges}\n{text}".casefold()
    if ("без оплаты" in low or re.search(
            r"неоплачива|без денег|не сможем дать денег", low)):
        return "unpaid"
    if re.search(r"оплачиваем\w*\s+(?:стажиров|ваканс|работ|проект|позиц|сотрудничеств)", low):
        return "paid"
    if re.search(r"зарплат|заработн\w*\s+плат|оклад|salary|"
                 r"\d[\d\s]{3,}\s*(?:₽|руб)|"
                 r"(?:зарплат|оклад|доход|оплата)[^.\n]{0,40}\d{2,3}\s*[кk]\b", low):
        return "paid"
    if re.search(r"за долю|доля в компании|доля в проекте|equity|вестинг", low):
        return "equity"
    return "unknown"


def canonical_mode(mode: str) -> str:
    """Map any wording to remote/hybrid/office, or '' when unknown."""
    low = normalize(mode or "")
    if not low:
        return ""
    if any(x in low for x in ("remote", "удал", "дистанц")):
        return "remote"
    if any(x in low for x in ("hybrid", "гибрид")):
        return "hybrid"
    if any(x in low for x in ("office", "офис", "полный", "на месте")):
        return "office"
    return ""


def tokens_match(a: str, b: str) -> bool:
    """Conservative morphological match for Russian/English stems."""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 5 and long.startswith(short) and len(short) / len(long) >= 0.62


def token_overlap(left: set[str], right: set[str]) -> float:
    if not left:
        return 0.0
    hits = sum(1 for token in left if any(tokens_match(token, other) for other in right))
    return hits / len(left)


def seniority_level(title: str) -> str:
    low = normalize(title or "")
    if re.search(r"стажер|стажёр|интерн|intern|trainee|практикант", low):
        return "intern"
    if re.search(r"junior|джун|младш", low):
        return "junior"
    if re.search(r"principal|принципал", low):
        return "principal"
    if re.search(r"architect|архитектор", low):
        return "architect"
    if re.search(r"director|vice president|\bvp\b|chief|head\b|руководител|директор|начальник|глава", low):
        return "head"
    if re.search(r"lead\b|ведущ|тимлид|team lead", low):
        return "lead"
    if re.search(r"senior|старш", low):
        return "senior"
    # No seniority marker != middle. Many entry-level jobs are simply titled
    # "Аналитик данных" or "Python-разработчик".
    return "unspecified"


SENIOR_LEVELS = {"senior", "lead", "principal", "architect", "head"}
LEAD_LEVELS = {"lead", "principal", "architect", "head"}


def detect_red_flags(text: str) -> list[str]:
    low = normalize(text)
    return [name for name, pattern in RED_FLAG_PATTERNS.items() if re.search(pattern, low)]


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
PHONE_RE = re.compile(r"(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}")
TELEGRAM_RE = re.compile(r"(?:@[A-Za-z0-9_]{5,32}|t\.me/[A-Za-z0-9_]{4,32})")


def scrub_contacts(text: str) -> str:
    """Remove third-party personal contact data from externally fetched text."""
    if not text:
        return ""
    text = EMAIL_RE.sub("[email скрыт]", text)
    text = PHONE_RE.sub("[телефон скрыт]", text)
    text = TELEGRAM_RE.sub("[контакт скрыт]", text)
    return text


def jaccard(a: str, b: str, n: int = 3) -> float:
    def shingles(text: str) -> set[str]:
        words = tokens(text, remove_stop=True)
        return {" ".join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))} or set(words)
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def cosine_counter(a: str, b: str) -> float:
    ca, cb = Counter(tokens(a)), Counter(tokens(b))
    if not ca or not cb:
        return 0.0
    dot = sum(v * cb.get(k, 0) for k, v in ca.items())
    na = math.sqrt(sum(v * v for v in ca.values()))
    nb = math.sqrt(sum(v * v for v in cb.values()))
    return dot / (na * nb) if na and nb else 0.0


def truncate_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut.rsplit(" ", 1)[0] + "…"


def unique_sentences(text: str) -> list[str]:
    seen, out = set(), []
    for sentence in sentences(text):
        key = re.sub(r"\W+", "", normalize(sentence))
        if key and key not in seen:
            seen.add(key)
            out.append(sentence)
    return out


def safe_int(value) -> int | None:
    try:
        return int(float(str(value).replace(" ", "").replace("\u00a0", "")))
    except (TypeError, ValueError):
        return None


@dataclass
class SkillRequirement:
    name: str
    weight: float
    requirement: str
    evidence: str = ""
    matched: bool = False
    matched_as: str = ""


@dataclass
class JobSignals:
    skills: list[SkillRequirement] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)
    nice_skills: list[str] = field(default_factory=list)


def analyze_job_text(text: str, listed_skills: Iterable[str] | None = None,
                     *, required_weight: float = 2.0, nice_weight: float = 0.9,
                     listed_weight: float = 1.5) -> JobSignals:
    found = extract_skills(text)
    weights = {"required": required_weight, "mentioned": 1.2, "nice": nice_weight}
    signals = JobSignals(red_flags=detect_red_flags(text))
    for canonical, meta in found.items():
        weight = weights.get(meta["requirement"], 1.2)
        signals.skills.append(SkillRequirement(canonical, weight, meta["requirement"], meta["sentence"]))
    existing = {s.name for s in signals.skills}
    for listed in listed_skills or []:
        canonical = canonical_skill(listed)
        if not canonical:
            continue
        if canonical in existing:
            for item in signals.skills:
                if item.name == canonical:
                    item.weight = max(item.weight, listed_weight)
                    item.requirement = "required" if item.requirement == "mentioned" else item.requirement
        else:
            signals.skills.append(SkillRequirement(canonical, listed_weight, "required", "ключевые навыки вакансии"))
            existing.add(canonical)
    signals.required_skills = [s.name for s in signals.skills if s.requirement != "nice"]
    signals.nice_skills = [s.name for s in signals.skills if s.requirement == "nice"]
    return signals
