"""Playwright Chromium with a separate, user-owned session.

Security model
--------------
* The browser profile lives outside the repository at
  `%LOCALAPPDATA%\\AI-Job-Hunter\\hh-browser-profile` (legacy helpers use
  ``data/browser_profile``). Cookies/2FA state are never copied, logged, uploaded
  or sent to an LLM.
* Login is manual, in a visible window. This module never types a password and
  never solves a CAPTCHA.
* Any CAPTCHA/403/429/anti-bot page stops the current operation and is reported
  to the caller. There is deliberately no stealth/anti-detect code.
"""
from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .paths import BROWSER_PROFILE, HH_BROWSER_PROFILE, HH_HISTORY_CAPTURE, LOGS, ensure_dirs

BLOCK_MARKERS = (
    "подтвердите, что вы не робот", "доступ ограничен", "слишком много запросов",
    "captcha", "проверка безопасности", "access denied", "are you a robot",
)
CAPTCHA_SELECTORS = (
    '[data-qa="captcha"]', '.h-captcha', '.g-recaptcha', 'iframe[src*="captcha"]',
    'iframe[src*="recaptcha"]', '#captcha',
)


class BrowserUnavailable(RuntimeError):
    pass


class BrowserBlocked(RuntimeError):
    """Platform asked for human verification or rate-limited us."""


def _launch(playwright, settings: dict, headless: bool | None, profile: Path | None = None):
    cfg = settings.get("browser") or {}
    ensure_dirs()
    profile_path = profile or BROWSER_PROFILE
    profile_path.mkdir(parents=True, exist_ok=True)
    options = {
        "user_data_dir": str(profile_path),
        "headless": bool(cfg.get("headless", False)) if headless is None else headless,
        "locale": cfg.get("locale", "ru-RU"),
        "timezone_id": cfg.get("timezone", "Europe/Moscow"),
        "viewport": {"width": 1440, "height": 900},
        "args": ["--disable-dev-shm-usage"],
    }
    return playwright.chromium.launch_persistent_context(**options)


@contextmanager
def session(settings: dict, *, headless: bool | None = None) -> Iterator:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "Playwright не установлен: py -3 -m pip install playwright && py -3 -m playwright install chromium"
        ) from exc
    with sync_playwright() as playwright:
        context = _launch(playwright, settings, headless)
        context.set_default_timeout(int((settings.get("browser") or {}).get("timeout_ms", 45000)))
        try:
            yield context
        finally:
            try:
                context.close()
            except Exception:
                pass


def _base(settings: dict) -> str:
    return ((settings.get("browser") or {}).get("hh_base") or "https://hh.ru").rstrip("/")


def is_hh_url(url: str, settings: dict) -> bool:
    host = re.sub(r"^https?://", "", (url or "").casefold()).split("/")[0]
    return host.endswith("hh.ru") or host.endswith("headhunter.ru")


def detect_block(page) -> str | None:
    for selector in CAPTCHA_SELECTORS:
        try:
            if page.locator(selector).count() > 0:
                return "CAPTCHA / проверка безопасности"
        except Exception:
            continue
    try:
        body = page.locator("body").inner_text(timeout=4000).casefold()[:6000]
    except Exception:
        body = ""
    for marker in BLOCK_MARKERS:
        if marker in body:
            return marker
    return None


def _logged_in(page) -> bool:
    selectors = (
        '[data-qa="mainmenu_applicantProfile"]', '[data-qa="user-account__menu"]',
        '[data-qa="header-user-name"]', '[data-qa="mainmenu_profileAndResumes"]',
        'a[href*="/applicant/resumes"]',
    )
    for selector in selectors:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    return "/applicant/" in page.url


def auth_status(settings: dict, *, headless: bool | None = None) -> dict:
    """Open hh.ru in the persistent profile and report login/block state."""
    with session(settings, headless=headless) as context:
        page = context.new_page()
        response = page.goto(_base(settings) + "/", wait_until="domcontentloaded")
        block = detect_block(page)
        logged = _logged_in(page) if not block else False
        return {
            "logged_in": logged,
            "blocked": block,
            "status": response.status if response else None,
            "url": page.url,
            "profile_dir": str(BROWSER_PROFILE),
        }


def login(settings: dict, *, timeout_minutes: int = 6) -> dict:
    """Open a visible browser and wait for the user to sign in manually."""
    with session(settings, headless=False) as context:
        page = context.new_page()
        page.goto(_base(settings) + "/login", wait_until="domcontentloaded")
        print("Открыт Chromium. Войдите в hh.ru вручную (пароль/2FA вводятся только вами).")
        print("После входа нажмите Enter в этом окне терминала; окно не закроется само.")
        try:
            input()
        except EOFError:
            page.wait_for_timeout(min(60_000, timeout_minutes * 60_000))
        page.goto(_base(settings) + "/", wait_until="domcontentloaded")
        return {"logged_in": _logged_in(page), "blocked": detect_block(page), "url": page.url}


@contextmanager
def hh_profile_lock():
    """Prevent two processes from using the same Chromium profile."""
    HH_BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    lock_path = HH_BROWSER_PROFILE / ".session.lock"
    handle = open(lock_path, "a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError as exc:
                raise BrowserUnavailable("Профиль hh.ru уже используется другим процессом") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                raise BrowserUnavailable("Профиль hh.ru уже используется другим процессом") from exc
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


@contextmanager
def hh_session(settings: dict, *, headless: bool | None = None) -> Iterator:
    """Persistent user profile at %LOCALAPPDATA%\\AI-Job-Hunter\\hh-browser-profile."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "Playwright не установлен: py -3 -m pip install playwright && py -3 -m playwright install chromium"
        ) from exc
    with hh_profile_lock():
        with sync_playwright() as playwright:
            context = _launch(playwright, settings, headless, profile=HH_BROWSER_PROFILE)
            context.set_default_timeout(int((settings.get("browser") or {}).get("timeout_ms", 45000)))
            try:
                yield context
            finally:
                try:
                    context.close()
                except Exception:
                    pass


def hh_login(settings: dict, *, timeout_minutes: int = 8) -> dict:
    """Open hh.ru visibly and wait for the user to authenticate manually."""
    deadline = time.monotonic() + max(60, timeout_minutes * 60)
    try:
        with hh_session(settings, headless=False) as context:
            page = context.new_page()
            response = page.goto("https://hh.ru/", wait_until="domcontentloaded")
            block = detect_block(page)
            if block:
                return {"ok": False, "status": "blocked", "reason": block,
                        "url": page.url, "profile_dir": str(HH_BROWSER_PROFILE)}
            while time.monotonic() < deadline:
                try:
                    if page.is_closed():
                        return {"ok": False, "status": "closed",
                                "reason": "Окно браузера закрыто до входа",
                                "profile_dir": str(HH_BROWSER_PROFILE)}
                    if _logged_in(page):
                        page.wait_for_timeout(1200)
                        return {"ok": True, "status": "connected", "logged_in": True,
                                "url": page.url, "profile_dir": str(HH_BROWSER_PROFILE),
                                "message": "Сессия сохранена в локальном профиле."}
                    page.wait_for_timeout(2000)
                except Exception:
                    break
            return {"ok": False, "status": "timeout",
                    "reason": "Вход не обнаружен. Повторите авторизацию в открытом окне.",
                    "url": page.url, "profile_dir": str(HH_BROWSER_PROFILE)}
    except BrowserUnavailable as exc:
        return {"ok": False, "status": "unavailable", "reason": str(exc)}
    except Exception as exc:
        return {"ok": False, "status": "error",
                "reason": f"{type(exc).__name__}: {exc}"}


def hh_profile_status() -> dict:
    return {"profile_exists": HH_BROWSER_PROFILE.exists(),
            "profile_dir": str(HH_BROWSER_PROFILE),
            "lock_file_present": (HH_BROWSER_PROFILE / ".session.lock").exists(),
            "note": "Проверка входа выполняется кнопкой «Проверить сессию»."}


def hh_session_status(settings: dict) -> dict:
    """Check login without exporting cookies; the profile is never serialized."""
    if not HH_BROWSER_PROFILE.exists():
        return {"ok": False, "profile_exists": False, "logged_in": False,
                "profile_dir": str(HH_BROWSER_PROFILE), "reason": "Профиль ещё не создан"}
    try:
        with hh_session(settings, headless=True) as context:
            page = context.new_page()
            response = page.goto("https://hh.ru/", wait_until="domcontentloaded")
            block = detect_block(page)
            return {"ok": True, "profile_exists": True,
                    "logged_in": bool(_logged_in(page)) if not block else False,
                    "blocked": block or "", "http_status": response.status if response else None,
                    "url": page.url, "profile_dir": str(HH_BROWSER_PROFILE),
                    "cookies_exported": False}
    except BrowserUnavailable as exc:
        return {"ok": False, "profile_exists": True, "logged_in": False,
                "profile_dir": str(HH_BROWSER_PROFILE), "reason": str(exc)}


def capture_history(settings: dict, *, pages: int = 5) -> dict:
    """Open the user's history and save HTML pages for the existing importer."""
    HH_HISTORY_CAPTURE.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    try:
        with hh_session(settings, headless=False) as context:
            page = context.new_page()
            page.goto("https://hh.ru/applicant/negotiations", wait_until="domcontentloaded")
            block = detect_block(page)
            if block:
                return {"ok": False, "status": "blocked", "reason": block}
            if not _logged_in(page):
                return {"ok": False, "status": "auth_required",
                        "reason": "Войдите в hh.ru в открытом окне и повторите сохранение."}
            for index in range(max(1, min(20, pages))):
                if index:
                    page.goto(f"https://hh.ru/applicant/negotiations?page={index}",
                              wait_until="domcontentloaded")
                    page.wait_for_timeout(800)
                if detect_block(page):
                    break
                html = page.content()
                if len(html) < 500:
                    break
                target = HH_HISTORY_CAPTURE / f"history-{time.strftime('%Y%m%d-%H%M%S')}-{index}.html"
                target.write_text(html, encoding="utf-8")
                saved.append(str(target))
                if "negotiations-item" not in html:
                    break
            return {"ok": bool(saved), "status": "saved" if saved else "empty",
                    "pages": len(saved), "files": saved, "profile_dir": str(HH_BROWSER_PROFILE),
                    "cookies_exported": False,
                    "note": "Файлы можно загрузить в Статистика → Массовый импорт HTML / ZIP."}
    except BrowserUnavailable as exc:
        return {"ok": False, "status": "unavailable", "reason": str(exc)}
    except Exception as exc:
        return {"ok": False, "status": "error", "reason": f"{type(exc).__name__}: {exc}"}


def clear_hh_session() -> dict:
    """Remove the dedicated hh profile (never the user's main browser profile)."""
    import shutil
    try:
        if HH_BROWSER_PROFILE.exists():
            shutil.rmtree(HH_BROWSER_PROFILE, ignore_errors=False)
        return {"ok": True, "profile_dir": str(HH_BROWSER_PROFILE), "cleared": True}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _screenshot(page, name: str) -> str:
    ensure_dirs()
    path = LOGS / f"browser-{name}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:
        return ""


def apply_hh(settings: dict, job, letter: str, *, dry_run: bool = True,
             headless: bool | None = None) -> dict:
    """Apply on hh.ru through the user's own visible session.

    dry_run only inspects the page: it never clicks the response button.
    """
    if job["source"] != "hh_api" and not is_hh_url(job["url"], settings):
        return {"status": "unsupported", "reason": "Канал hh_ui принимает только вакансии hh.ru"}
    if not is_hh_url(job["url"], settings):
        return {"status": "unsupported", "reason": "Нет ссылки на вакансию hh.ru"}
    if not letter.strip():
        return {"status": "failed", "reason": "Пустое сопроводительное письмо"}
    with session(settings, headless=headless) as context:
        page = context.new_page()
        response = page.goto(job["url"], wait_until="domcontentloaded")
        if response and response.status in (403, 429):
            raise BrowserBlocked(f"HTTP {response.status}: площадка ограничила доступ")
        block = detect_block(page)
        if block:
            raise BrowserBlocked(block)
        if not _logged_in(page):
            return {"status": "auth_required", "reason": "Сессия не авторизована. Запустите login."}
        body = page.locator("body").inner_text(timeout=8000).casefold()
        if "вы откликнулись" in body or "отклик отправлен" in body:
            return {"status": "already", "reason": "Похоже, отклик уже отправлен"}
        if "внешняя форма" in body or "откликнуться на сайте" in body:
            return {"status": "needs_manual", "reason": "Вакансия использует внешнюю форму отклика"}
        button = None
        for selector in ('[data-qa="vacancy-response-link-top"]', '[data-qa="vacancy-response-link-bottom"]',
                         '[data-qa*="vacancy-response"]', 'a:has-text("Откликнуться")',
                         'button:has-text("Откликнуться")'):
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    button = locator
                    break
            except Exception:
                continue
        if button is None:
            shot = _screenshot(page, "no-apply-button")
            return {"status": "unsupported", "reason": "Кнопка отклика не найдена (возможно, требуется анкета)", "screenshot": shot}
        if dry_run:
            return {"status": "dry_run", "reason": "Кнопка отклика доступна; в dry-run клик не выполняется"}
        button.click()
        page.wait_for_timeout(1500)
        block = detect_block(page)
        if block:
            raise BrowserBlocked(block)
        filled = False
        for selector in ('textarea[data-qa="vacancy-response-popup-form-letter-input"]',
                         'textarea[data-qa="vacancy-cover-letter-textarea"]',
                         'textarea[name="cover_letter"]', 'textarea'):
            try:
                area = page.locator(selector).first
                if area.count() > 0 and area.is_visible():
                    area.fill(letter)
                    filled = True
                    break
            except Exception:
                continue
        questions = page.locator('[data-qa*="task-question"], [data-qa*="vacancy-response-question"]')
        if questions.count() > 0:
            return {"status": "needs_manual", "reason": "Вакансия содержит вопросы — заполните вручную",
                    "screenshot": _screenshot(page, "questions")}
        submitted = False
        for selector in ('[data-qa="vacancy-response-submit-popup"]',
                         '[data-qa="vacancy-response-popup-form-submit"]',
                         'button:has-text("Отправить")', 'button:has-text("Откликнуться")'):
            try:
                submit = page.locator(selector).first
                if submit.count() > 0 and submit.is_visible():
                    submit.click()
                    submitted = True
                    break
            except Exception:
                continue
        if not submitted:
            return {"status": "needs_manual", "reason": "Не найдена кнопка отправки",
                    "screenshot": _screenshot(page, "no-submit")}
        page.wait_for_timeout(2000)
        block = detect_block(page)
        if block:
            raise BrowserBlocked(block)
        after = page.locator("body").inner_text(timeout=8000).casefold()
        success = any(marker in after for marker in ("отклик отправлен", "вы откликнулись", "отклик успешно"))
        return {"status": "submitted" if success else "unknown",
                "reason": "" if success else "Отправка не подтверждена текстом страницы",
                "letter_attached": filled,
                "screenshot": "" if success else _screenshot(page, "apply-unknown")}


def fetch_negotiations(settings: dict, *, limit: int = 30, headless: bool | None = None) -> list[dict]:
    """Best-effort read of the negotiations list. No clicks that change state."""
    with session(settings, headless=True if headless is None else headless) as context:
        page = context.new_page()
        response = page.goto(_base(settings) + "/applicant/negotiations", wait_until="domcontentloaded")
        if response and response.status in (403, 429):
            raise BrowserBlocked(f"HTTP {response.status}")
        block = detect_block(page)
        if block:
            raise BrowserBlocked(block)
        if not _logged_in(page):
            raise BrowserBlocked("Сессия не авторизована")
        items = page.locator('[data-qa="negotiation-item"]')
        count = min(items.count(), limit)
        out = []
        for index in range(count):
            item = items.nth(index)
            def text_of(*selectors: str) -> str:
                for selector in selectors:
                    try:
                        locator = item.locator(selector).first
                        if locator.count() > 0:
                            return locator.inner_text().strip()
                    except Exception:
                        continue
                return ""
            link = ""
            try:
                anchor = item.locator('[data-qa="negotiation-vacancy"] a').first
                if anchor.count() > 0:
                    link = anchor.get_attribute("href") or ""
            except Exception:
                pass
            if link.startswith("/"):
                link = _base(settings) + link
            out.append({
                "title": text_of('[data-qa="negotiation-vacancy"]'),
                "company": text_of('[data-qa="negotiation-employer"]'),
                "status": text_of('[data-qa="negotiation-status"]'),
                "date": text_of('[data-qa="negotiation-date"]'),
                "url": link,
                "unread": item.locator('[data-qa*="unread"]').count() > 0,
            })
        return out


def export_session_fingerprint(settings: dict) -> dict:
    """Non-secret diagnostics: cookie count and domains, never cookie values."""
    with session(settings, headless=True) as context:
        cookies = context.cookies()
    domains = sorted({c.get("domain", "") for c in cookies})
    return {"cookie_count": len(cookies), "domains": domains[:20]}
