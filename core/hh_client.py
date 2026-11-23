"""HeadHunter diagnostics and honest direct/indirect status.

The official API requires a registered application for OAuth. Anonymous access
can be restricted by IP; this module records the real response instead of
guessing. No cookies, CAPTCHA bypass or HTML scraping are used.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

import requests

API = "https://api.hh.ru"
UA = "AI-Job-Hunter/4.0 (personal local job search)"


def _diag_payload(response: requests.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        body = {}
    errors = body.get("errors") if isinstance(body, dict) else None
    return {
        "status": response.status_code,
        "error_type": (errors or [{}])[0].get("type") if isinstance(errors, list) and errors else "",
        "request_id": response.headers.get("X-Request-Id") or
        (body.get("request_id") if isinstance(body, dict) else ""),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def diagnose() -> dict:
    result: dict = {"direct": "unknown", "diagnostic": {}, "oauth_configured": bool(os.environ.get("HH_ACCESS_TOKEN"))}
    try:
        response = requests.get(f"{API}/vacancies", params={"text": "python", "area": 1, "per_page": 2},
                                headers={"User-Agent": UA, "HH-User-Agent": "AI-Job-Hunter/5.0"}, timeout=15)
        result["diagnostic"] = _diag_payload(response)
        if response.status_code == 200:
            result["direct"] = "working"
        elif response.status_code == 403:
            result["direct"] = "blocked"
            result["reason"] = "HTTP 403 forbidden: доступ ограничен для этого IP/приложения"
        elif response.status_code == 429:
            result["direct"] = "rate_limited"
            result["reason"] = "HTTP 429: превышен лимит запросов"
        else:
            result["direct"] = "error"
            result["reason"] = f"HTTP {response.status_code}"
    except requests.RequestException as exc:
        result["direct"] = "unavailable"
        result["reason"] = f"{type(exc).__name__}: {exc}"[:200]
    token = (os.environ.get("HH_ACCESS_TOKEN") or "").strip()
    if token:
        try:
            me = requests.get(f"{API}/me", headers={"User-Agent": UA,
                                                    "HH-User-Agent": "AI-Job-Hunter/5.0",
                                                    "Authorization": f"Bearer {token}"}, timeout=15)
            result["oauth_status"] = me.status_code
            result["oauth_error"] = _diag_payload(me).get("error_type", "")
        except requests.RequestException as exc:
            result["oauth_status"] = 0
            result["oauth_error"] = type(exc).__name__
    return result


def status(db: sqlite3.Connection) -> dict:
    diag = diagnose()
    counts = db.execute(
        """SELECT COUNT(*) total,
                  SUM(CASE WHEN eligible=1 AND url LIKE '%hh.ru%' THEN 1 ELSE 0 END) eligible,
                  SUM(CASE WHEN url LIKE '%hh.ru%' AND length(description)>500 THEN 1 ELSE 0 END) detailed,
                  SUM(CASE WHEN url LIKE '%hh.ru%' THEN 1 ELSE 0 END) with_hh_url
           FROM jobs WHERE duplicate_of IS NULL"""
    ).fetchone()
    enriched = db.execute(
        """SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NULL AND url LIKE '%hh.ru%'
           AND json_extract(detail,'$.enrichment') IS NOT NULL"""
    ).fetchone()[0]
    return {
        **diag,
        "tokens": {"HH_ACCESS_TOKEN": bool(os.environ.get("HH_ACCESS_TOKEN"))},
        "indirect": {
            "with_hh_url": int(counts["with_hh_url"] or 0),
            "detailed": int(counts["detailed"] or 0),
            "enriched": int(enriched or 0),
            "eligible": int(counts["eligible"] or 0),
        },
        "oauth_instructions": (
            "Зарегистрируйте приложение на dev.hh.ru, получите client_id/secret, "
            "выполните OAuth и сохраните access token в переменной HH_ACCESS_TOKEN. "
            "Пароль и 2FA вводятся только на сайте hh.ru."
        ),
    }
