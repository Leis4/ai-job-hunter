"""Settings and profile loading with deep defaults and safe migration.

Old config/settings.json keys (``trudvsem``, ``weights``) keep working; new
sections are merged in. Nothing here touches secrets: API keys stay in
environment variables and are never written to disk.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from .paths import CONFIG, ensure_dirs

DEFAULT_SETTINGS: dict[str, Any] = {
    "schema": 3,
    "threshold": 65,
    "max_drafts_per_cycle": 10,
    "automation": {
        "enabled": True,
        "light_minutes": 30,
        "full_hours": 6,
        "discovery_hours": 24,
        "validate_limit": 40,
        "auto_enable_channels": False,
        "auto_enable_limit": 100,
        "prepare_best": True,
    },
    "startups": {
        "enabled": True,
        "hours": 24,
        "max_pages": 4,
        "max_details": 40,
        "prepare_drafts": True,
        "draft_limit": 6,
        "draft_min_fit": 60,
    },
    "search": {
        "strategy": True,
        "max_queries": {"trudvsem": 7, "habr": 10, "vk_career": 7, "mts": 8, "hh_api": 4},
    },
    "experience_tolerance_years": 1,
    "send_applications": False,
    "browser_automation": False,
    "autopilot": {
        "mode": "observer",
        "enabled": False,
        "auto_apply": False,
        "auto_replies": False,
        "min_match": 75,
        "daily_limit": 5,
        "directions": ["python", "backend", "data", "ml", "ai", "llm", "rag"],
        "work_modes": ["remote", "hybrid"],
        "locations": ["Москва"],
        "excluded_employers": [],
        "allowed_resumes": [],
        "reply_categories": ["acknowledgement"],
        "poll_minutes": 30,
    },
    "submission": {
        "channels": {
            "official_api": {"enabled": True, "auto": True},
            "ats_form": {"enabled": False, "auto": False, "domains": []},
            "career_site_form": {
                "enabled": False, "auto": False,
                "domains": ["job.mts.ru", "career.habr.com", "team.vk.company"],
            },
            "email": {
                "enabled": False, "auto": False, "smtp_configured": False,
                "smtp_host": "", "smtp_port": 587, "smtp_security": "starttls",
                "smtp_username": "", "from_email": "", "from_name": "",
            },
            "telegram_contact": {"enabled": False, "auto": False, "sender_configured": False},
            "manual_assisted": {"enabled": True, "auto": False},
        },
    },
    "contact_note": "",
    "weights": {
        "role": 0.20,
        "skills": 0.28,
        "text": 0.14,
        "experience": 0.13,
        "work_mode": 0.07,
        "location": 0.06,
        "salary": 0.05,
        "freshness": 0.07,
    },
    "scoring": {
        "bm25_k1": 1.5,
        "bm25_b": 0.75,
        "shortlist_limit": 150,
        "uncertainty_z": 1.28,
        "skill_prior": 0.25,
        "required_skill_weight": 2.0,
        "nice_skill_weight": 0.9,
        "listed_skill_weight": 1.5,
        "hard_experience": True,
        "hard_seniority": True,
        "hard_salary": False,
        "min_skill_coverage": 0.0,
        "fresh_days": 21,
    },
    "sources": {
        "trudvsem": {
            "enabled": True,
            "queries": ["python", "аналитик данных", "data engineer", "стажер", "практикант"],
            "max_pages": 2,
            "per_page": 100,
            "region": "7700000000000",
            "min_interval_hours": 3,
            "fetch_details": 0,
        },
        "hh_api": {
            "enabled": False,
            "queries": ["python", "аналитик данных", "стажёр python"],
            "area": 1,
            "per_page": 50,
            "max_pages": 1,
            "experience": None,
            "schedule": None,
            "period": 7,
            "min_interval_hours": 6,
            "fetch_details": 20,
            "note": "Официальный api.hh.ru. Анонимный доступ зависит от IP; 403/429 останавливают источник, обхода нет.",
        },
        "feed": {
            "enabled": False,
            "urls": [],
            "min_interval_hours": 12,
            "note": "RSS/Atom/JSON Feed карьерных сайтов, которые вы вправе читать.",
        },
        "mts": {
            "enabled": False,
            "queries": ["python", "аналитик данных", "стажер", "data scientist", "ml"],
            "max_pages": 1,
            "max_queries": 8,
            "use_strategy": True,
            "min_interval_hours": 8,
            "fetch_details": 40,
            "note": "Публичный поиск job.mts.ru; включается в config/settings.json.",
        },
        "telegram": {
            "enabled": False,
            "channels": [],
            "max_pages": 1,
            "max_channels_per_run": 30,
            "min_interval_hours": 4,
            "fetch_details": 0,
            "enrich_limit": 8,
            "note": "Публичные веб-превью t.me/s/<channel>; каналы добавляются в настройках.",
        },
        "ats": {
            "enabled": False,
            "boards": [],
            "min_interval_hours": 12,
            "fetch_details": 60,
            "note": "Публичные job boards Greenhouse, Ashby, Lever.",
        },
        "tbank": {
            "enabled": False,
            "queries": ["data", "analyst", "ml", "python"],
            "max_queries": 6,
            "use_strategy": True,
            "min_interval_hours": 12,
            "fetch_details": 20,
            "note": "Публичная страница IT-вакансий Т-Банка.",
        },
    },
    "llm": {
        "enabled": True,
        "provider": "auto",
        "base_env": "JOB_HUNTER_API_BASE",
        "key_env": "JOB_HUNTER_API_KEY",
        "model_env": "JOB_HUNTER_MODEL",
        "fallback_base": "https://api.deepseek.com/v1",
        "fallback_model": "deepseek-chat",
        "temperature": 0.3,
        "max_calls_per_cycle": 8,
        "monthly_budget_usd": 3.0,
        "timeout_seconds": 60,
        "max_job_chars": 6000,
        "critique": True,
        "price_input_per_mtok": 0.27,
        "price_output_per_mtok": 1.1,
        "aitunnel": {
            "base_env": "AITUNNEL_API_BASE",
            "key_env": "AITUNNEL_API_KEY",
            "base": "https://api.aitunnel.ru/v1",
            "catalog_url": "https://api.aitunnel.ru/public/aitunnel/models",
            "catalog_refresh_on_start": True,
            "allow_stale_catalog": False,
            "models": {
                "cheap": "glm-5.3-flash",
                "quality": "glm-5.3-flash",
                "critic": "glm-5.3-flash",
            },
            "task_models": {},
            "usd_rub_rate": 90.0,
            "max_call_rub": 5.0,
            "fallback_enabled": False,
            "fallback_model": "deepseek-v4.1-flash-1017",
        },
    },
    "queue": {
        "enabled": True,
        "daily_limit": 8,
        "daily_minutes": 90,
        "minutes_per_application": 12,
        "diversity_lambda": 0.72,
        "min_score": 65,
        "require_manual_approval": True,
        "duplicate_window_days": 180,
        "delay_seconds": [25, 90],
        "channels": {
            "hh_ui": {
                "enabled": False,
                "note": "Отклик через вашу видимую сессию Chromium. Только при явном включении; CAPTCHA/403/429 => остановка.",
            },
            "manual_export": {"enabled": True},
        },
    },
    "browser": {
        "headless": False,
        "timeout_ms": 45000,
        "slow_mo_ms": 60,
        "locale": "ru-RU",
        "timezone": "Europe/Moscow",
        "hh_base": "https://hh.ru",
    },
    "memory": {
        "enabled": True,
        "embed_enabled": True,
        "embedding_model": "bge-m3",
        "ollama_url": "http://127.0.0.1:11434",
        "unload_after_batch": True,
        "max_docs_per_cycle": 40,
        "retrieval_limit": 6,
    },
    "qwen": {
        "enabled": True,
        "provider": "auto",
        "url": "http://127.0.0.1:11434",
        "model": "qwen3.5:4b",
        "timeout_seconds": 180,
        "num_predict": 1200,
        "keep_alive": "30s",
        "unload_after": False,
        "think": False,
        "auto_start_ollama": True,
        "analyze_with_model": False,
        "cloud": {
            "enabled": True,
            "allow_fallback": True,
            "models": {
                "cheap": "qwen3.7-flash",
                "quality": "qwen3.8-flash",
            },
            "max_letter_rub": 1.5,
            "max_daily_rub": 30.0,
            "complex_chars": 3500,
            "high_priority_score": 85,
        },
    },
    "calibration": {
        "min_samples": 60,
        "min_positives": 12,
        "min_negatives": 12,
        "min_test": 15,
    },
    "monitor": {
        "enabled": True,
        "check_messages_hours": 3,
        "check_statuses_hours": 6,
    },
    "notifications": {
        "telegram": {"enabled": False, "token_env": "JOB_HUNTER_TELEGRAM_TOKEN", "chat_env": "JOB_HUNTER_TELEGRAM_CHAT"}
    },
    "privacy": {
        "strip_contacts": True,
        "send_profile_to_llm": "minimal",
        "allow_cloud_llm": True,
        "retention_days": 365,
    },
}

DEFAULT_PROFILE: dict[str, Any] = {
    "schema": 3,
    "education_confirmed": "",
    "professional_experience_years": 0,
    "verified_skills": [],
    "verified_projects": [],
    "learning_interests": [],
    "skill_aliases": {},
    "preferences": {
        "role_keywords": [],
        "excluded_titles": ["курьер", "продавец", "колл-центр", "оператор продаж", "бариста", "официант",
                            "агент по недвижимости", "риэлтор", "грузчик", "водитель", "уборщик", "охранник"],
        "work_modes": ["гибрид", "удал", "remote", "гибкий", "офис"],
        "locations": ["Москва", "Moscow"],
        "availability": "",
        "salary_target_rub": None,
        "relocation": False,
    },
}

_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE")


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Не удалось прочитать {path.name}: {exc}") from exc


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_settings() -> dict:
    ensure_dirs()
    raw = _read_json(CONFIG / "settings.json")
    merged = _deep_merge(DEFAULT_SETTINGS, raw)
    # Backwards compatibility: old top-level trudvsem block.
    if isinstance(raw.get("trudvsem"), dict) and "sources" not in raw:
        merged["sources"]["trudvsem"] = _deep_merge(DEFAULT_SETTINGS["sources"]["trudvsem"], raw["trudvsem"])
    if raw.get("send_applications") is True:
        merged["queue"]["enabled"] = True
    return merged


def save_settings(settings: dict) -> None:
    _write_json(CONFIG / "settings.json", settings)


def load_profile() -> dict:
    ensure_dirs()
    path = CONFIG / "profile.json"
    merged = _deep_merge(DEFAULT_PROFILE, _read_json(path))
    # A field left empty on purpose must not be silently invented.
    return merged


def save_profile(profile: dict) -> None:
    _write_json(CONFIG / "profile.json", profile)


def llm_config(settings: dict) -> dict:
    cfg = dict(settings.get("llm") or {})
    base = os.environ.get(cfg.get("base_env", ""), "").strip()
    key = os.environ.get(cfg.get("key_env", ""), "").strip()
    model = os.environ.get(cfg.get("model_env", ""), "").strip()
    if not base and os.environ.get("DEEPSEEK_API_KEY"):
        base = "https://api.deepseek.com/v1"
    if not key:
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not model:
        model = "deepseek-chat" if os.environ.get("DEEPSEEK_API_KEY") else cfg.get("fallback_model", "")
    return {
        "base": (base or cfg.get("fallback_base", "")).rstrip("/"),
        "key": key,
        "model": model or cfg.get("fallback_model", ""),
        "temperature": float(cfg.get("temperature", 0.3)),
        "timeout": int(cfg.get("timeout_seconds", 60)),
        "max_job_chars": int(cfg.get("max_job_chars", 6000)),
    }


def redact(text: str) -> str:
    """Belt-and-braces: never print env values that look secret."""
    if not text:
        return text
    for name, value in os.environ.items():
        if any(h in name.upper() for h in _SECRET_HINTS) and len(value) >= 8:
            text = text.replace(value, "***")
    return text
