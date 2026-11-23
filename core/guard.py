"""Secret hygiene shared by LLM clients. Kept dependency-free to avoid cycles."""
from __future__ import annotations

import os

SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE")


class SecretLeakBlocked(RuntimeError):
    """Raised when a prompt contains a value copied from the environment."""


def secret_values() -> list[str]:
    values = []
    for name, value in os.environ.items():
        if any(hint in name.upper() for hint in SECRET_HINTS) and len(value) >= 8:
            values.append(value)
    return values


def assert_no_secrets(text: str) -> None:
    for value in secret_values():
        if value and value in text:
            raise SecretLeakBlocked("Запрос заблокирован: в промпте обнаружен секрет.")


def redact(text: str) -> str:
    if not text:
        return text
    for value in secret_values():
        text = text.replace(value, "***")
    return text
