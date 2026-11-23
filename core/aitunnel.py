"""AITUNNEL router with price-aware model selection and a local cost ledger.

Design constraints from the upgrade brief:
* keys only from the environment;
* catalog is fetched from the public endpoint and cached for 24h;
* fail closed when a model is missing or the catalog is stale;
* no silent escalation: fallback is opt-in and cost-capped;
* JSON mode is probed and falls back to validated plain JSON;
* per-task tokens, RUB cost, model, latency and cache hits are stored.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import db as dbm
from .guard import SecretLeakBlocked, assert_no_secrets
from .mathx import expected_api_cost

CATALOG_URL = "https://api.aitunnel.ru/public/aitunnel/models"
DEFAULT_BASE = "https://api.aitunnel.ru/v1"

# Fallback prices (RUB per million tokens) verified from the public catalog on
# 2026-11-18. They are only used when no catalog can be fetched.
FALLBACK_PRICES = {
    "glm-5.3-flash": (30.0, 100.0),
    "deepseek-v4.1-flash-1017": (7.2, 100.0),
    # Qwen IDs verified in the public AITUNNEL catalog on 2026-11-20.
    "qwen3.7-flash": (6.0, 26.0),
    "qwen3.8-flash": (30.0, 94.0),
    "qwen3.5-flash-02-23": (13.0, 52.0),
    "qwen3.6-35b-a3b": (20.0, 180.0),
}

DEFAULT_TASKS = {
    "classify": "cheap",
    "extract_requirements": "cheap",
    "draft_reply": "cheap",
    "deep_analysis": "quality",
    "cover_letter": "quality",
    "resume_improvement": "quality",
    "critic": "critic",
    "generic": "cheap",
}

# Reasoning is mandatory on these AITUNNEL endpoints; a too-small max_tokens is
# consumed by reasoning and leaves content empty. Floors are output caps, not
# guaranteed spend.
TASK_MIN_OUTPUT = {
    "classify": 600,
    "extract_requirements": 4000,
    "draft_reply": 1200,
    "deep_analysis": 3500,
    "cover_letter": 3000,
    "resume_improvement": 3500,
    "critic": 2500,
    "generic": 1200,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_models (
  model TEXT PRIMARY KEY,
  provider TEXT,
  prompt_cost REAL NOT NULL,
  completion_cost REAL NOT NULL,
  context_size INTEGER,
  max_output INTEGER,
  cache_discount REAL,
  raw TEXT,
  fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_catalog_meta (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  fetched_at TEXT NOT NULL,
  source_url TEXT,
  models INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ai_spend (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  task TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT 'aitunnel',
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cost_rub REAL NOT NULL,
  estimated INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  cache_hit INTEGER NOT NULL DEFAULT 0,
  prompt_hash TEXT,
  version TEXT
);
CREATE TABLE IF NOT EXISTS ai_cache (
  key TEXT PRIMARY KEY,
  task TEXT,
  model TEXT,
  content TEXT NOT NULL,
  cost_rub REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  hits INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS ai_capability (
  model TEXT PRIMARY KEY,
  supports_json INTEGER,
  checked_at TEXT
);
"""


class AIUnavailable(RuntimeError):
    pass


class AIBudgetExceeded(AIUnavailable):
    pass


class AIModelUnavailable(AIUnavailable):
    pass


@dataclass(frozen=True)
class Completion:
    content: str
    model: str
    cost_rub: float
    estimated: bool
    input_tokens: int
    output_tokens: int
    latency_ms: int = 0
    cached: bool = False
    task: str = ""


_BREAKER: dict[str, float] = {}
_BREAKER_FAILURES: dict[str, int] = {}
BREAKER_THRESHOLD = 3
BREAKER_SECONDS = 120
_CATALOG_CHECKED = False


class AITunnelRouter:
    def __init__(self, db: sqlite3.Connection, settings: dict):
        self.db = db
        self.settings = settings
        conf = (settings.get("llm") or {}).get("aitunnel") or {}
        self.conf = conf
        self.base = (os.environ.get(conf.get("base_env", "AITUNNEL_API_BASE"), "")
                     or conf.get("base") or DEFAULT_BASE).rstrip("/")
        from .secure_store import aitunnel_key
        self.api_key = aitunnel_key(settings).strip()
        self.catalog_url = conf.get("catalog_url", CATALOG_URL)
        self.max_monthly_rub = self._budget_rub()
        self.max_call_rub = float(conf.get("max_call_rub", 5.0))
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(ai_spend)")}
        if "version" not in columns:
            self.db.execute("ALTER TABLE ai_spend ADD COLUMN version TEXT")
        if "provider" not in columns:
            self.db.execute("ALTER TABLE ai_spend ADD COLUMN provider TEXT NOT NULL DEFAULT 'aitunnel'")
        self.db.commit()

    # ------------------------------------------------------------------ config
    def _budget_rub(self) -> float:
        explicit = self.conf.get("monthly_budget_rub")
        if explicit is not None:
            return float(explicit)
        usd = float((self.settings.get("llm") or {}).get("monthly_budget_usd", 3.0))
        rate = float(self.conf.get("usd_rub_rate", 90.0))
        return round(usd * rate, 2)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def task_model(self, task: str) -> str:
        tasks = self.conf.get("task_models") or {}
        if task in tasks:
            return str(tasks[task])
        alias = DEFAULT_TASKS.get(task, "cheap")
        models = self.conf.get("models") or {}
        if alias in models:
            return str(models[alias])
        legacy = {
            "cheap": "glm-5.3-flash",
            "quality": "deepseek-v4.1-flash-1017",
            "critic": "glm-5.3-flash",
            "local": "",
        }
        return legacy.get(alias, str(tasks.get(alias, "")))

    # ------------------------------------------------------------------ catalog
    def catalog_age_hours(self) -> float | None:
        row = self.db.execute("SELECT fetched_at FROM ai_catalog_meta WHERE id=1").fetchone()
        if not row or not row["fetched_at"]:
            return None
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
        except ValueError:
            return None
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - fetched).total_seconds() / 3600

    def refresh_catalog(self, *, force: bool = False, timeout: int = 15) -> dict:
        age = self.catalog_age_hours()
        if not force and age is not None and age < 24:
            return {"refreshed": False, "age_hours": round(age, 2), "models": self._catalog_count()}
        request = urllib.request.Request(self.catalog_url, headers={
            "User-Agent": "AI-Job-Hunter/2.0 (personal job search)", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return {"refreshed": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}",
                    "age_hours": round(age, 2) if age is not None else None}
        chat = payload.get("chat") or {}
        now = dbm.now()
        count = 0
        for model, info in chat.items():
            if not isinstance(info, dict):
                continue
            prompt_cost = info.get("prompt_cost")
            completion_cost = info.get("completion_cost")
            if not isinstance(prompt_cost, (int, float)) or not isinstance(completion_cost, (int, float)):
                continue
            self.db.execute(
                """INSERT INTO ai_models(model,provider,prompt_cost,completion_cost,context_size,
                     max_output,cache_discount,raw,fetched_at) VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(model) DO UPDATE SET provider=excluded.provider,
                     prompt_cost=excluded.prompt_cost, completion_cost=excluded.completion_cost,
                     context_size=excluded.context_size, max_output=excluded.max_output,
                     cache_discount=excluded.cache_discount, raw=excluded.raw, fetched_at=excluded.fetched_at""",
                (model, info.get("provider"), float(prompt_cost), float(completion_cost),
                 info.get("context_size"), info.get("max_output"), info.get("cache_discount"),
                 json.dumps(info, ensure_ascii=False)[:12000], now))
            count += 1
        self.db.execute(
            "INSERT INTO ai_catalog_meta(id,fetched_at,source_url,models) VALUES (1,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET fetched_at=excluded.fetched_at, "
            "source_url=excluded.source_url, models=excluded.models",
            (now, self.catalog_url, count))
        self.db.commit()
        return {"refreshed": True, "models": count, "age_hours": 0.0}

    def _catalog_count(self) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM ai_models").fetchone()
        return int(row[0] or 0)

    def model_info(self, model: str) -> dict | None:
        row = self.db.execute("SELECT * FROM ai_models WHERE model=?", (model,)).fetchone()
        if row:
            return dict(row)
        if model in FALLBACK_PRICES:
            prompt, completion = FALLBACK_PRICES[model]
            return {"model": model, "prompt_cost": prompt, "completion_cost": completion,
                    "provider": "fallback", "context_size": None, "max_output": None,
                    "stale": True}
        return None

    def ensure_model(self, model: str) -> dict:
        info = self.model_info(model)
        if not info:
            raise AIModelUnavailable(f"Модель {model} отсутствует в каталоге AITUNNEL")
        age = self.catalog_age_hours()
        stale = age is None or age > 168
        if info.get("stale") and not self.conf.get("allow_stale_catalog", False) and stale:
            raise AIModelUnavailable(
                f"Каталог AITUNNEL устарел/недоступен; модель {model} не подтверждена. "
                "Обновите каталог или включите allow_stale_catalog осознанно.")
        return info

    # ------------------------------------------------------------------ budget / ledger
    def spent_this_month_rub(self) -> float:
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_rub),0) FROM ai_spend WHERE substr(created_at,1,7)=?", (period,)
        ).fetchone()
        return round(float(row[0] or 0), 4)

    def spent_today_rub(self) -> float:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_rub),0) FROM ai_spend WHERE substr(created_at,1,10)=?", (day,)
        ).fetchone()
        return round(float(row[0] or 0), 4)

    def _budget_ok(self, estimated_rub: float) -> None:
        if self.max_monthly_rub <= 0:
            raise AIBudgetExceeded("Месячный бюджет AITUNNEL равен 0")
        spent = self.spent_this_month_rub()
        if spent + estimated_rub > self.max_monthly_rub:
            raise AIBudgetExceeded(
                f"Бюджет AITUNNEL исчерпан: потрачено {spent:.2f} ₽ из {self.max_monthly_rub:.2f} ₽")

    def _estimate(self, info: dict, messages: list[dict], max_tokens: int) -> float:
        input_tokens = max(1, sum(len(str(m.get("content", ""))) for m in messages) // 4)
        return expected_api_cost(input_tokens, max_tokens,
                                 float(info["prompt_cost"]), float(info["completion_cost"]))

    def _record(self, task: str, model: str, input_tokens: int, output_tokens: int,
                cost: float, estimated: bool, latency_ms: int, prompt_hash: str,
                cache_hit: bool = False) -> None:
        self.db.execute(
            """INSERT INTO ai_spend(created_at,task,model,input_tokens,output_tokens,cost_rub,
                 estimated,latency_ms,cache_hit,prompt_hash,version) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (dbm.now(), task, model, input_tokens, output_tokens, round(cost, 6),
             int(estimated), latency_ms, int(cache_hit), prompt_hash, "aitunnel-v1"))
        self.db.commit()

    def ledger(self, limit: int = 20) -> dict:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        total = self.spent_this_month_rub()
        by_task = [dict(r) for r in self.db.execute(
            """SELECT task, COUNT(*) requests, SUM(input_tokens) input_tokens,
                      SUM(output_tokens) output_tokens, ROUND(SUM(cost_rub),4) cost_rub,
                      SUM(estimated) estimated_count, SUM(cache_hit) cache_hits
               FROM ai_spend WHERE substr(created_at,1,7)=? GROUP BY task ORDER BY cost_rub DESC""",
            (month,))]
        recent = [dict(r) for r in self.db.execute(
            """SELECT created_at,task,provider,model,input_tokens,output_tokens,cost_rub,estimated,
                      latency_ms,cache_hit FROM ai_spend ORDER BY id DESC LIMIT ?""", (limit,))]
        return {"month": month, "spent_rub": total, "budget_rub": self.max_monthly_rub,
                "by_task": by_task, "recent": recent,
                "catalog_age_hours": self.catalog_age_hours(),
                "models_in_catalog": self._catalog_count()}

    # ------------------------------------------------------------------ calls
    def complete(self, task: str, messages: list[dict], *, max_tokens: int = 700,
                 temperature: float = 0.2, json_mode: bool = False,
                 facts_version: str = "", use_cache: bool = True,
                 model: str | None = None) -> Completion:
        if not self.api_key:
            raise AIUnavailable("AITUNNEL_API_KEY не задан")
        if not messages or any(m.get("role") not in {"system", "user", "assistant"} for m in messages):
            raise ValueError("Unsupported chat messages")
        assert_no_secrets(json.dumps(messages, ensure_ascii=False))
        primary = str(model or self.task_model(task))
        if not primary:
            raise AIModelUnavailable(f"Для задачи {task} не выбрана модель (local task?)")
        models = [primary]
        # An explicit model request is never silently escalated to another model.
        fallback = self.conf.get("fallback_model")
        if model is None and self.conf.get("fallback_enabled") and fallback and fallback not in models:
            models.append(str(fallback))
        errors: list[str] = []
        for index, model in enumerate(models):
            try:
                return self._complete_model(task, model, messages, max_tokens=max_tokens,
                                            temperature=temperature, json_mode=json_mode,
                                            facts_version=facts_version, use_cache=use_cache)
            except AIUnavailable as exc:
                errors.append(f"{model}: {exc}")
                if index == 0 and len(models) > 1:
                    # Explicit, cost-capped fallback only. Never escalate silently.
                    continue
                raise
        raise AIUnavailable("; ".join(errors) or "AITUNNEL call failed")

    def _failure(self, model: str) -> None:
        _BREAKER_FAILURES[model] = _BREAKER_FAILURES.get(model, 0) + 1
        if _BREAKER_FAILURES[model] >= BREAKER_THRESHOLD:
            _BREAKER[model] = time.time() + BREAKER_SECONDS

    def _complete_model(self, task: str, model: str, messages: list[dict], *,
                        max_tokens: int, temperature: float, json_mode: bool,
                        facts_version: str, use_cache: bool) -> Completion:
        info = self.ensure_model(model)
        # Reasoning models spend tokens before the visible answer; enforce a
        # task floor so the JSON/letter is not truncated.
        max_tokens = max(int(max_tokens), int(TASK_MIN_OUTPUT.get(task, 1200)))
        estimated_cost = self._estimate(info, messages, max_tokens)
        if estimated_cost > self.max_call_rub:
            raise AIBudgetExceeded(
                f"Оценка одного запроса {estimated_cost:.2f} ₽ выше лимита {self.max_call_rub:.2f} ₽")
        self._budget_ok(estimated_cost)

        cache_key = hashlib.sha256(
            f"{model}|{task}|{json_mode}|{temperature}|{max_tokens}|{facts_version}|"
            f"{json.dumps(messages, ensure_ascii=False, sort_keys=True)}".encode("utf-8")
        ).hexdigest()
        if use_cache:
            cached = self.db.execute("SELECT content FROM ai_cache WHERE key=?", (cache_key,)).fetchone()
            if cached:
                self.db.execute("UPDATE ai_cache SET hits=hits+1 WHERE key=?", (cache_key,))
                self._record(task, model, 0, 0, 0.0, False, 0, cache_key, cache_hit=True)
                return Completion(cached["content"], model, 0.0, False, 0, 0, 0, True, task)

        if time.time() < _BREAKER.get(model, 0):
            raise AIUnavailable(f"{model}: временная блокировка после повторных ошибок")

        capability = self.db.execute(
            "SELECT supports_json FROM ai_capability WHERE model=?", (model,)).fetchone()
        supports_json = not (capability and capability["supports_json"] == 0)

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
        }
        if json_mode and supports_json:
            payload["response_format"] = {"type": "json_object"}
        empty_attempts = 0
        while True:
            started = time.time()
            try:
                result = self._post(payload)
            except AIUnavailable:
                self._failure(model)
                raise
            if result is None and json_mode and supports_json:
                # Probe/fallback: the gateway rejects structured output.
                self.db.execute(
                    "INSERT OR REPLACE INTO ai_capability(model,supports_json,checked_at) VALUES (?,?,?)",
                    (model, 0, dbm.now()))
                self.db.commit()
                payload.pop("response_format", None)
                supports_json = False
                try:
                    result = self._post(payload)
                except AIUnavailable:
                    self._failure(model)
                    raise
            if result is None:
                self._failure(model)
                raise AIUnavailable(f"AITUNNEL не ответил корректно ({model})")
            latency_ms = int((time.time() - started) * 1000)
            usage = result.get("usage") or {}
            choice = (result.get("choices") or [{}])[0].get("message") or {}
            content = choice.get("content") or ""
            input_tokens = int(usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or 0)
            cost = usage.get("cost_rub")
            estimated = cost is None
            if estimated:
                cost = expected_api_cost(input_tokens, output_tokens,
                                         float(info["prompt_cost"]), float(info["completion_cost"]))
            cost = float(cost)
            if isinstance(content, str) and content.strip():
                break
            # Reasoning models may return only reasoning and no content. Record
            # the paid attempt, then retry once with a larger output budget.
            self._record(task, model, input_tokens, output_tokens, cost, estimated,
                         latency_ms, cache_key)
            empty_attempts += 1
            if empty_attempts >= 2:
                self._failure(model)
                raise AIUnavailable("Пустой ответ AITUNNEL после повторной попытки")
            payload["max_tokens"] = min(8000, int(payload["max_tokens"]) * 2)
        _BREAKER_FAILURES[model] = 0
        self._record(task, model, input_tokens, output_tokens, cost, estimated, latency_ms, cache_key)
        if use_cache:
            self.db.execute(
                "INSERT OR REPLACE INTO ai_cache(key,task,model,content,cost_rub,created_at,hits) "
                "VALUES (?,?,?,?,?,?,1)",
                (cache_key, task, model, content, cost, dbm.now()))
            self.db.commit()
        return Completion(content, model, cost, estimated, input_tokens, output_tokens,
                          latency_ms, False, task)

    def _post(self, payload: dict) -> dict | None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}/chat/completions", data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST")
        last_error = ""
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code == 400 and "response_format" in payload:
                    try:
                        detail = exc.read(2000).decode("utf-8", "replace").casefold()
                    except Exception:
                        detail = ""
                    if "response_format" in detail or "json" in detail:
                        return None
                if exc.code == 429 and attempt < 2:
                    time.sleep(8 * (attempt + 1))
                    last_error = "HTTP 429"
                    continue
                if exc.code >= 500 and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    last_error = f"HTTP {exc.code}"
                    continue
                raise AIUnavailable(f"AITUNNEL HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = type(exc).__name__
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise AIUnavailable(f"AITUNNEL недоступен: {last_error}") from None
        return None


def task_for_kind(kind: str) -> str:
    mapping = {
        "vacancy_analysis": "deep_analysis",
        "cover_letter": "cover_letter",
        "cover_letter_dedup": "cover_letter",
        "cover_letter_fix": "cover_letter",
        "letter_critique": "critic",
        "reply_draft": "draft_reply",
        "classify": "classify",
        "extract_requirements": "extract_requirements",
        "resume_improvement": "resume_improvement",
    }
    return mapping.get(kind, "generic")


def refresh_on_start(db: sqlite3.Connection, settings: dict) -> dict:
    """One catalog refresh per process, only when stale and enabled."""
    global _CATALOG_CHECKED
    conf = (settings.get("llm") or {}).get("aitunnel") or {}
    if _CATALOG_CHECKED or not conf.get("catalog_refresh_on_start", True):
        return {"refreshed": False, "reason": "disabled or already checked"}
    _CATALOG_CHECKED = True
    router = AITunnelRouter(db, settings)
    return router.refresh_catalog(force=False)
