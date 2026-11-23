"""SSRF-safe HTTP fetch for links found in untrusted Telegram posts."""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import requests

MAX_BYTES = 2_500_000
ALLOWED_TYPES = ("text/html", "application/xhtml+xml", "application/json",
                 "text/plain", "application/xml", "text/xml")


class UnsafeURL(ValueError):
    pass


@dataclass
class Page:
    url: str
    status: int
    content_type: str
    text: str


def _check_host(host: str) -> None:
    host = (host or "").strip().strip("[]").casefold()
    if not host:
        raise UnsafeURL("пустой host")
    if host in ("localhost", "metadata.google.internal") or host.endswith((".local", ".internal")):
        raise UnsafeURL("локальный host запрещён")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except OSError as exc:
        raise UnsafeURL(f"DNS не разрешён: {exc}") from exc
    if not addresses:
        raise UnsafeURL("DNS не вернул адрес")
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise UnsafeURL(f"некорректный IP: {raw}")
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified or not ip.is_global):
            raise UnsafeURL(f"внутренний/непубличный адрес: {raw}")


def validate_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("разрешены только http/https")
    if parts.username or parts.password:
        raise UnsafeURL("учётные данные в URL запрещены")
    if parts.port and parts.port not in (80, 443):
        raise UnsafeURL("разрешены только порты 80/443")
    _check_host(parts.hostname or "")
    return parts.geturl()


def fetch(url: str, *, timeout: int = 20, proxy: str = "") -> Page:
    current = validate_url(url)
    session = requests.Session()
    session.trust_env = False
    session.cookies.clear()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        for _ in range(4):
            response = session.get(current, timeout=timeout, allow_redirects=False,
                                   headers={"User-Agent": "AI-Job-Hunter/4.0 (safe enrichment)",
                                            "Accept": "text/html,application/xhtml+xml,application/json;q=0.8"},
                                   proxies=proxies, stream=True)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location") or ""
                response.close()
                if not location:
                    raise UnsafeURL("редирект без Location")
                current = validate_url(urljoin(current, location))
                continue
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().casefold()
            if content_type and not any(content_type.startswith(x) for x in ALLOWED_TYPES):
                response.close()
                raise UnsafeURL(f"тип ответа запрещён: {content_type}")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_BYTES:
                response.close()
                raise UnsafeURL("ответ слишком большой")
            chunks, total = [], 0
            for chunk in response.iter_content(65536):
                total += len(chunk)
                if total > MAX_BYTES:
                    response.close()
                    raise UnsafeURL("ответ превысил лимит")
                chunks.append(chunk)
            response.close()
            raw = b"".join(chunks)
            return Page(url=current, status=response.status_code, content_type=content_type,
                        text=raw.decode("utf-8", "replace"))
        raise UnsafeURL("слишком много редиректов")
    finally:
        session.close()
