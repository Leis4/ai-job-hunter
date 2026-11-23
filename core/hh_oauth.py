"""Official hh.ru OAuth 2.0 Authorization Code Flow and encrypted token store.

Tokens are stored under data/ and protected with Windows DPAPI when available.
They are never written to logs, Git, prompts or the React payload.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
import time
import urllib.parse
import webbrowser
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .paths import DATA, ensure_dirs

AUTH_URL = "https://hh.ru/oauth/authorize"
TOKEN_URL = "https://api.hh.ru/token"
API = "https://api.hh.ru"
TOKEN_PATH = DATA / "hh_oauth.json"
CREDENTIALS_PATH = DATA / "hh_app_credentials.json"
STATE_PATH = DATA / "hh_oauth_state.json"
DEFAULT_REDIRECT = "http://127.0.0.1:8765/api/hh/oauth/callback"
USER_AGENT = "AI-Job-Hunter/5.0 (personal job search; local app)"
HH_USER_AGENT = "AI-Job-Hunter/5.0"


class OAuthError(RuntimeError):
    pass


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        return b"PLAIN:" + data
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buffer = ctypes.create_string_buffer(data)
    src = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    dst = Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), None, None, None, None, 0,
                                                  ctypes.byref(dst)):
        raise OAuthError("Windows DPAPI: не удалось защитить токен")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def _unprotect(data: bytes) -> bytes:
    if data.startswith(b"PLAIN:"):
        return data[6:]
    if os.name != "nt":
        raise OAuthError("Защищённое хранилище доступно только в Windows")
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buffer = ctypes.create_string_buffer(data)
    src = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    dst = Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0,
                                                    ctypes.byref(dst)):
        raise OAuthError("Windows DPAPI: не удалось прочитать токен")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def redirect_uri() -> str:
    return (os.environ.get("HH_REDIRECT_URI") or DEFAULT_REDIRECT).strip()


def credentials() -> tuple[str, str]:
    env_id = (os.environ.get("HH_CLIENT_ID") or "").strip()
    env_secret = (os.environ.get("HH_CLIENT_SECRET") or "").strip()
    if env_id and env_secret:
        return env_id, env_secret
    saved = _load_credentials()
    return (saved.get("client_id", "").strip(), saved.get("client_secret", "").strip())


def _load_credentials() -> dict:
    if not CREDENTIALS_PATH.exists():
        return {}
    try:
        return json.loads(_unprotect(CREDENTIALS_PATH.read_bytes()).decode("utf-8"))
    except (OSError, ValueError, OAuthError):
        return {}


def save_credentials(client_id: str, client_secret: str) -> dict:
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id and not client_secret:
        clear_credentials()
        return {"ok": True, "cleared": True}
    if not client_id or not client_secret:
        return {"ok": False, "reason": "Нужны client_id и client_secret"}
    ensure_dirs()
    raw = json.dumps({"client_id": client_id, "client_secret": client_secret,
                      "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                     ensure_ascii=False).encode("utf-8")
    CREDENTIALS_PATH.write_bytes(_protect(raw))
    return {"ok": True, "stored": True, "client_id_tail": client_id[-4:]}


def clear_credentials() -> None:
    try:
        CREDENTIALS_PATH.unlink()
    except FileNotFoundError:
        pass


def configured() -> bool:
    client_id, client_secret = credentials()
    return bool(client_id and client_secret)


def storage_status() -> dict:
    """Report DPAPI readability without exposing any secret."""
    probe_ok = False
    try:
        probe = b"ai-job-hunter-dpapi-probe"
        probe_ok = _unprotect(_protect(probe)) == probe
    except Exception:
        probe_ok = False
    tokens = load_tokens()
    saved_credentials = _load_credentials()
    return {
        "dpapi_roundtrip": probe_ok,
        "token_file": str(TOKEN_PATH),
        "token_file_exists": TOKEN_PATH.exists(),
        "token_decryptable": bool(tokens.get("access_token")),
        "credentials_file": str(CREDENTIALS_PATH),
        "credentials_file_exists": CREDENTIALS_PATH.exists(),
        "credentials_decryptable": bool(saved_credentials.get("client_id") and
                                      saved_credentials.get("client_secret")),
        "scheduler_user_note": ("Task Scheduler должен запускаться под тем же Windows-пользователем, "
                                 "который выполнял OAuth: DPAPI использует область текущего пользователя."),
    }


def save_tokens(payload: dict) -> None:
    ensure_dirs()
    payload = dict(payload)
    payload["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if payload.get("expires_in"):
        payload["expires_at"] = (datetime.now(timezone.utc) +
                                 timedelta(seconds=int(payload["expires_in"]))).isoformat(timespec="seconds")
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    TOKEN_PATH.write_bytes(_protect(raw))


def load_tokens() -> dict:
    if not TOKEN_PATH.exists():
        return {}
    try:
        return json.loads(_unprotect(TOKEN_PATH.read_bytes()).decode("utf-8"))
    except (OSError, ValueError, OAuthError):
        return {}


def clear_tokens() -> None:
    try:
        TOKEN_PATH.unlink()
    except FileNotFoundError:
        pass


def access_token() -> str:
    data = load_tokens()
    token = str(data.get("access_token") or "").strip()
    expires = data.get("expires_at") or ""
    if token and expires:
        try:
            if datetime.fromisoformat(expires) <= datetime.now(timezone.utc) + timedelta(seconds=60):
                refreshed = refresh()
                return refreshed.get("access_token", "")
        except ValueError:
            pass
    return token


def refresh() -> dict:
    data = load_tokens()
    refresh_token = str(data.get("refresh_token") or "").strip()
    client_id, client_secret = credentials()
    if not refresh_token or not client_id or not client_secret:
        return {}
    response = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client_id, "client_secret": client_secret,
    }, headers={"User-Agent": USER_AGENT, "HH-User-Agent": HH_USER_AGENT}, timeout=25)
    if response.status_code != 200:
        return {}
    payload = response.json()
    payload.setdefault("refresh_token", refresh_token)
    save_tokens(payload)
    return payload


def start() -> dict:
    client_id, client_secret = credentials()
    if not client_id or not client_secret:
        return {"ok": False, "status": "not_configured",
                "reason": "Нужны HH_CLIENT_ID и HH_CLIENT_SECRET приложения hh.ru.",
                "instructions": (
                    "Зарегистрируйте приложение на https://dev.hh.ru, укажите redirect URI "
                    f"{redirect_uri()}, затем задайте HH_CLIENT_ID и HH_CLIENT_SECRET в окружении Windows."
                )}
    state = secrets.token_urlsafe(24)
    ensure_dirs()
    STATE_PATH.write_text(json.dumps({"state": state, "created_at": time.time(),
                                      "redirect_uri": redirect_uri()}), encoding="utf-8")
    params = {"response_type": "code", "client_id": client_id,
              "redirect_uri": redirect_uri(), "state": state}
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return {"ok": True, "auth_url": url, "redirect_uri": redirect_uri(), "state": state,
            "storage": storage_status(),
            "credentials_source": "env" if os.environ.get("HH_CLIENT_ID") else "dpapi"}


def exchange(code: str, state: str) -> dict:
    if not STATE_PATH.exists():
        raise OAuthError("OAuth state не найден; начните подключение заново")
    saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if not secrets.compare_digest(str(saved.get("state") or ""), str(state or "")):
        raise OAuthError("OAuth state не совпал")
    if time.time() - float(saved.get("created_at") or 0) > 900:
        raise OAuthError("OAuth state истёк")
    client_id, client_secret = credentials()
    if not client_id or not client_secret:
        raise OAuthError("HH_CLIENT_ID или HH_CLIENT_SECRET не заданы")
    response = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code", "code": code,
        "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": saved.get("redirect_uri") or redirect_uri(),
    }, headers={"User-Agent": USER_AGENT, "HH-User-Agent": HH_USER_AGENT}, timeout=25)
    if response.status_code != 200:
        try:
            detail = response.json()
        except ValueError:
            detail = {}
        hint = ""
        text = json.dumps(detail, ensure_ascii=False)
        if "redirect" in text.casefold() or response.status_code == 400:
            hint = (" Проверьте, что Redirect URI в приложении dev.hh.ru полностью совпадает с "
                    f"{saved.get('redirect_uri') or redirect_uri()} (включая http/https, порт и путь).")
        raise OAuthError(f"Обмен code на токен: HTTP {response.status_code}.{hint} {text[:300]}")
    payload = response.json()
    if not payload.get("access_token"):
        raise OAuthError("hh.ru не вернул access_token")
    save_tokens(payload)
    try:
        me = requests.get(API + "/me", headers={"Authorization": f"Bearer {payload['access_token']}",
                                                "User-Agent": USER_AGENT,
                                                "HH-User-Agent": HH_USER_AGENT}, timeout=20)
        if me.status_code == 200:
            payload["me"] = me.json()
    except requests.RequestException:
        pass
    try:
        STATE_PATH.unlink()
    except FileNotFoundError:
        pass
    return payload


def status() -> dict:
    data = load_tokens()
    token = access_token()
    expires = data.get("expires_at") or ""
    me = None
    if token:
        try:
            response = requests.get(API + "/me", headers={
                "Authorization": f"Bearer {token}", "User-Agent": USER_AGENT,
                "HH-User-Agent": HH_USER_AGENT}, timeout=15)
            if response.status_code == 200:
                me = response.json()
        except requests.RequestException:
            pass
    saved = _load_credentials()
    source = "environment" if (os.environ.get("HH_CLIENT_ID") and
                               os.environ.get("HH_CLIENT_SECRET")) else (
        "dpapi" if saved else "none")
    return {"configured": configured(), "connected": bool(token) and bool(me),
            "has_refresh_token": bool(data.get("refresh_token")),
            "expires_at": expires, "me": me,
            "redirect_uri": redirect_uri(),
            "credentials_source": source,
            "storage": storage_status(),
            "redirect_diagnostics": {
                "expected": DEFAULT_REDIRECT,
                "current": redirect_uri(),
                "matches_default": redirect_uri().rstrip("/") == DEFAULT_REDIRECT.rstrip("/"),
                "hint": "Redirect URI в dev.hh.ru должен совпадать посимвольно, включая порт и /api/hh/oauth/callback.",
            },
            "instructions": ("Зарегистрируйте приложение на https://dev.hh.ru и сохраните "
                             "HH_CLIENT_ID/HH_CLIENT_SECRET через мастер автопилота. Redirect URI: "
                             + redirect_uri())
            if not configured() else ""}


def callback_html(ok: bool, message: str) -> str:
    color = "#16a34a" if ok else "#dc2626"
    return f"""<!doctype html><meta charset="utf-8"><title>hh.ru OAuth</title>
<body style="font-family:Segoe UI,Arial;padding:40px"><h2 style="color:{color}">{message}</h2>
<p>Можно закрыть эту вкладку и вернуться в AI Job Hunter.</p></body>"""
