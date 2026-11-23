"""DeepSeek / OpenAI-compatible client with cache, budget guard and no secrets in logs.

The API key is read from the environment only. Prompt payloads are checked
against known secret values before leaving the machine.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import db as dbm
from .aitunnel import (AIBudgetExceeded, AIModelUnavailable, AIUnavailable, AITunnelRouter,
                       Completion, refresh_on_start, task_for_kind)
from .guard import SecretLeakBlocked, assert_no_secrets
from .settings import llm_config, redact

_COOLDOWN: dict[str, float] = {}
COOLDOWN_SECONDS = 90


class LLMUnavailable(RuntimeError):
    pass


class BudgetExceeded(LLMUnavailable):
    pass


def _secret_values() -> list[str]:
    from .guard import secret_values
    return secret_values()


def assert_no_secrets(text: str) -> None:
    for value in _secret_values():
        if value and value in text:
            raise SecretLeakBlocked("Запрос заблокирован: в промпте обнаружен секрет.")


def extract_json(text: str) -> dict:
    text = (text or "").strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if match:
        text = match.group(1)
    else:
        first, last = text.find("{"), text.rfind("}")
        if first >= 0 and last > first:
            text = text[first:last + 1]
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def month_spend(db: sqlite3.Connection) -> float:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    row = db.execute(
        "SELECT COALESCE(SUM(est_cost_usd),0) FROM llm_usage WHERE substr(created_at,1,7)=?", (month,)
    ).fetchone()
    return float(row[0] or 0.0)


class LLM:
    def __init__(self, db: sqlite3.Connection, settings: dict):
        self.db = db
        self.settings = settings
        self.cfg = llm_config(settings)
        self.enabled = bool(settings.get("llm", {}).get("enabled", True))
        self.provider_pref = str((settings.get("llm") or {}).get("provider", "auto"))
        self.router: AITunnelRouter | None = None
        if self.enabled and self.provider_pref in ("auto", "aitunnel"):
            try:
                router = AITunnelRouter(db, settings)
                if router.configured:
                    self.router = router
                    try:
                        refresh_on_start(db, settings)
                    except Exception:
                        pass
            except Exception:
                self.router = None
        self.direct_available = bool(self.cfg["base"] and self.cfg["key"] and self.cfg["model"])
        self.last_result: Completion | None = None

    @property
    def available(self) -> bool:
        if not self.enabled:
            return False
        if self.provider_pref == "aitunnel" and self.router is None:
            return False
        if self.router is not None and self.provider_pref in ("auto", "aitunnel"):
            return True
        return self.direct_available

    def status(self) -> dict:
        if self.router is not None:
            ledger = self.router.ledger(limit=5)
            return {
                "available": self.available, "provider": "aitunnel",
                "model": self.router.task_model("cover_letter"),
                "base": self.router.base, "configured": True,
                "month_spend_rub": ledger["spent_rub"], "budget_rub": ledger["budget_rub"],
                "month_spend_usd": round(ledger["spent_rub"] / float(
                    (self.settings.get("llm") or {}).get("aitunnel", {}).get("usd_rub_rate", 90.0)), 4),
                "budget_usd": float(self.settings["llm"].get("monthly_budget_usd", 3.0)),
                "tasks": {task: self.router.task_model(task) for task in
                          ("classify", "deep_analysis", "cover_letter", "critic")},
                "catalog_age_hours": ledger["catalog_age_hours"],
                "models_in_catalog": ledger["models_in_catalog"],
                "by_task": ledger["by_task"],
            }
        return {
            "available": self.available, "provider": "direct",
            "model": self.cfg["model"] if self.available else "",
            "base": self.cfg["base"] if self.available else "",
            "configured": bool(self.cfg["key"]),
            "month_spend_usd": round(month_spend(self.db), 4),
            "budget_usd": float(self.settings["llm"].get("monthly_budget_usd", 3.0)),
        }

    def _budget_ok(self, estimated: float = 0.0) -> bool:
        budget = float(self.settings["llm"].get("monthly_budget_usd", 3.0))
        if budget <= 0:
            return False
        return month_spend(self.db) + estimated <= budget

    def chat(self, messages: list[dict], *, kind: str = "generic", json_mode: bool = False,
             max_tokens: int = 1200, temperature: float | None = None,
             use_cache: bool = True, facts_version: str = "") -> str:
        if not self.available:
            raise LLMUnavailable("LLM не настроена: задайте AITUNNEL_API_KEY или JOB_HUNTER_API_KEY/BASE/MODEL.")
        if self.router is not None and self.provider_pref in ("auto", "aitunnel"):
            task = task_for_kind(kind)
            try:
                result = self.router.complete(
                    task, messages, max_tokens=max_tokens,
                    temperature=self.cfg["temperature"] if temperature is None else temperature,
                    json_mode=json_mode, facts_version=facts_version, use_cache=use_cache)
            except (AIUnavailable, AIBudgetExceeded, AIModelUnavailable) as exc:
                raise LLMUnavailable(str(exc)) from None
            self.last_result = result
            return result.content
        payload_text = json.dumps(messages, ensure_ascii=False)
        assert_no_secrets(payload_text)
        cache_key = hashlib.sha256(
            f"{kind}|{self.cfg['model']}|{json_mode}|{facts_version}|{payload_text}".encode("utf-8")
        ).hexdigest()
        if use_cache:
            row = self.db.execute("SELECT response FROM llm_cache WHERE key=?", (cache_key,)).fetchone()
            if row:
                return row["response"]

        breaker_key = f"{self.cfg['base']}|{self.cfg['model']}"
        if time.time() < _COOLDOWN.get(breaker_key, 0):
            raise LLMUnavailable("LLM временно недоступна после ошибки; используется локальный шаблон.")

        budget = float(self.settings["llm"].get("monthly_budget_usd", 3.0))
        if not self._budget_ok(0.0):
            raise BudgetExceeded(f"Месячный бюджет LLM исчерпан (${budget:.2f}). Черновики будут локальными.")

        body = {
            "model": self.cfg["model"],
            "messages": messages,
            "temperature": self.cfg["temperature"] if temperature is None else temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.cfg['key']}"}
        url = self.cfg["base"] + "/chat/completions"
        last_error = ""
        for attempt in range(3):
            request = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.cfg["timeout"]) as response:
                    result = json.load(response)
                content = ((result.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                content = content.strip()
                usage = result.get("usage") or {}
                self._record_usage(kind, int(usage.get("prompt_tokens") or len(payload_text) // 4),
                                   int(usage.get("completion_tokens") or len(content) // 4))
                if not content:
                    raise LLMUnavailable("Пустой ответ LLM")
                if use_cache:
                    self.db.execute(
                        "INSERT OR REPLACE INTO llm_cache(key, model, kind, response, created_at) VALUES (?,?,?,?,?)",
                        (cache_key, self.cfg["model"], kind, content, dbm.now()),
                    )
                    self.db.commit()
                _COOLDOWN.pop(breaker_key, None)
                return content
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    time.sleep(8 * (attempt + 1))
                    last_error = "HTTP 429"
                    continue
                if exc.code >= 500 and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    last_error = f"HTTP {exc.code}"
                    continue
                _COOLDOWN[breaker_key] = time.time() + COOLDOWN_SECONDS
                raise LLMUnavailable(redact(f"LLM HTTP {exc.code}")) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = type(exc).__name__
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                _COOLDOWN[breaker_key] = time.time() + COOLDOWN_SECONDS
                raise LLMUnavailable(f"LLM недоступна: {last_error}") from exc
        _COOLDOWN[breaker_key] = time.time() + COOLDOWN_SECONDS
        raise LLMUnavailable(f"LLM не ответила: {last_error}")

    def _record_usage(self, kind: str, input_tokens: int, output_tokens: int) -> None:
        llm = self.settings.get("llm", {})
        in_price = float(llm.get("price_input_per_mtok", 0.27))
        out_price = float(llm.get("price_output_per_mtok", 1.10))
        cost = input_tokens / 1_000_000 * in_price + output_tokens / 1_000_000 * out_price
        self.db.execute(
            "INSERT INTO llm_usage(kind, model, input_chars, output_chars, est_cost_usd, created_at) VALUES (?,?,?,?,?,?)",
            (kind, self.cfg["model"], input_tokens * 4, output_tokens * 4, round(cost, 6), dbm.now()),
        )
        self.db.commit()


def describe(settings: dict) -> dict:
    cfg = llm_config(settings)
    return {"configured": bool(cfg["key"]), "model": cfg["model"], "base": cfg["base"]}
