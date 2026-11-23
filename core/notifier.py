"""Optional Telegram notifications. Disabled by default; no secrets in logs."""
from __future__ import annotations

import json
import os
import urllib.request

from .settings import redact


def enabled(settings: dict) -> bool:
    cfg = settings.get("notifications", {}).get("telegram", {})
    return bool(cfg.get("enabled") and os.environ.get(cfg.get("token_env", "")) and
                os.environ.get(cfg.get("chat_env", "")))


def send(settings: dict, text: str) -> dict:
    cfg = settings.get("notifications", {}).get("telegram", {})
    if not enabled(settings):
        return {"sent": False, "reason": "Telegram выключен или нет токена в окружении"}
    token = os.environ.get(cfg.get("token_env", ""), "")
    chat = os.environ.get(cfg.get("chat_env", ""), "")
    body = json.dumps({"chat_id": chat, "text": text[:3800], "disable_web_page_preview": True}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return {"sent": 200 <= response.status < 300}
    except Exception as exc:
        return {"sent": False, "reason": redact(f"{type(exc).__name__}: {exc}")}
