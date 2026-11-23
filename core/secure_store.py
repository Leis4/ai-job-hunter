"""DPAPI-protected local secret storage.

The Windows Data Protection API ties the ciphertext to the current Windows
user, so a copied file is useless on another account or machine. Values are
never written to SQLite, config, logs or the frontend.

Only two public operations expose a secret: :func:`store_secret` (write) and
:func:`load_secret` (read into memory for an API client). Status helpers return
the source label, never the value.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from .paths import DATA, ensure_dirs

AITUNNEL_SECRET = "aitunnel_api_key.bin"


class SecretStoreError(RuntimeError):
    pass


def _blob(data: bytes):
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data)
    return Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def protect(data: bytes) -> bytes:
    if os.name != "nt":
        # Local development outside Windows keeps the same file format, but the
        # secret is not encrypted. The app targets Windows; status reports it.
        return b"PLAIN:" + data
    src = _blob(data)
    dst = _blob(b"")
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(src), "AI Job Hunter local secret", None, None, None, 0,
            ctypes.byref(dst)):
        raise SecretStoreError("Windows DPAPI: не удалось зашифровать значение")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def unprotect(data: bytes) -> bytes:
    if data.startswith(b"PLAIN:"):
        return data[6:]
    if os.name != "nt":
        raise SecretStoreError("Защищённое хранилище доступно только в Windows")
    src = _blob(data)
    dst = _blob(b"")
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)):
        raise SecretStoreError("Windows DPAPI: не удалось прочитать значение")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def _path(name: str):
    if "/" in name or "\\" in name or not name:
        raise ValueError("Invalid secret name")
    return DATA / name


def store_secret(name: str, value: str) -> None:
    value = (value or "").strip()
    if len(value) < 8:
        raise ValueError("Секрет слишком короткий")
    ensure_dirs()
    path = _path(name)
    path.write_bytes(protect(value.encode("utf-8")))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_secret(name: str) -> str:
    path = _path(name)
    if not path.exists():
        return ""
    try:
        return unprotect(path.read_bytes()).decode("utf-8").strip()
    except (OSError, SecretStoreError, UnicodeDecodeError):
        return ""


def delete_secret(name: str) -> bool:
    path = _path(name)
    if path.exists():
        path.unlink()
        return True
    return False


def has_secret(name: str) -> bool:
    return bool(load_secret(name))


def aitunnel_key(settings: dict | None = None) -> str:
    """Environment wins, then the DPAPI file. Never written by this function."""
    conf = ((settings or {}).get("llm") or {}).get("aitunnel") or {}
    for name in ("AITUNNEL_API_KEY", str(conf.get("key_env") or "")):
        if name:
            value = (os.environ.get(name) or "").strip()
            if value:
                return value
    return load_secret(AITUNNEL_SECRET)


def aitunnel_key_source(settings: dict | None = None) -> str:
    conf = ((settings or {}).get("llm") or {}).get("aitunnel") or {}
    for name in ("AITUNNEL_API_KEY", str(conf.get("key_env") or "")):
        if name and (os.environ.get(name) or "").strip():
            return "env"
    if load_secret(AITUNNEL_SECRET):
        return "dpapi"
    return "none"
