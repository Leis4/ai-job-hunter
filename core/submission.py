"""Application submission engine.

One router selects the channel; adapters report honest capabilities and only
submit when the channel is explicitly allowed by the user's autopilot policy.
No adapter bypasses CAPTCHA, logins or platform restrictions. Uncertain results
are recorded as ``submission_unverified`` and are never retried blindly.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import sqlite3
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import db as dbm
from .browser import BrowserBlocked, BrowserUnavailable, detect_block, is_hh_url, session
from .paths import LOGS, ensure_dirs
from .resume import export_variant, select_variant

CHANNELS = {
    "official_api": "Официальный API",
    "ats_form": "Форма ATS",
    "career_site_form": "Форма карьерного сайта",
    "email": "Email",
    "telegram_contact": "Telegram HR",
    "manual_assisted": "Подготовить вручную",
}
ATS_HOSTS = (
    "greenhouse.io", "greenhouse.com", "ashbyhq.com", "lever.co", "workable.com",
    "smartrecruiters.com", "recruitee.com", "teamtailor.com", "breezy.hr",
    "jobvite.com", "recruiterbox.com",
)
CAREER_ADAPTERS = {
    "career.habr.com": "habr",
    "job.mts.ru": "mts",
    "team.vk.company": "vk",
}
CAPTCHA_ADAPTERS = {"habr", "vk"}
EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
TELEGRAM_RE = re.compile(r"(?:t\.me/|telegram\.me/)([A-Za-z0-9_]{4,32})", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS submission_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  job_id TEXT NOT NULL,
  application_id INTEGER,
  channel TEXT NOT NULL,
  host TEXT,
  status TEXT NOT NULL,
  reason TEXT,
  external_id TEXT,
  evidence TEXT,
  screenshot TEXT,
  duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS submission_job_idx ON submission_attempts(job_id, id DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return ""


def _detail(job) -> dict:
    try:
        return dbm.job_detail(job)
    except Exception:
        return {}


def _cfg(settings: dict, channel: str) -> dict:
    channels = ((settings.get("submission") or {}).get("channels") or {})
    return dict(channels.get(channel) or {})


def _contact(resume: dict) -> dict:
    return dict((resume or {}).get("contact") or {})


def _missing_contact(contact: dict) -> list[str]:
    labels = {"first_name": "имя", "last_name": "фамилия", "email": "email", "phone": "телефон"}
    return [label for key, label in labels.items() if not str(contact.get(key) or "").strip()]


def _telegram_contact(text: str) -> str:
    """Only a contact explicitly offered for applications, not a channel source."""
    low = (text or "").casefold()
    for match in TELEGRAM_RE.finditer(text or ""):
        start = max(0, match.start() - 160)
        window = low[start:match.end() + 80]
        if re.search(r"отклик|резюме|связат|напиш|контакт|hr|рекрутер", window):
            return match.group(1)
    return ""


def _email_contact(text: str) -> str:
    for match in EMAIL_RE.finditer(text or ""):
        value = match.group(1).casefold()
        if any(bad in value for bad in ("example.", "noreply", "no-reply", "support@", "info@")):
            continue
        window = (text or "")[max(0, match.start() - 120):match.end() + 80].casefold()
        if re.search(r"резюме|отклик|cv|ваканс|связат|направ", window):
            return value
    return ""


EMAIL_SECRET = "smtp_password.bin"


def _email_password() -> str:
    from .secure_store import load_secret
    return (os.environ.get("AJH_SMTP_PASSWORD") or os.environ.get("SMTP_PASSWORD")
            or load_secret(EMAIL_SECRET)).strip()


def _email_ready(settings: dict) -> tuple[bool, str]:
    cfg = _cfg(settings, "email")
    if not str(cfg.get("smtp_host") or "").strip():
        return False, "SMTP-сервер не указан"
    if not str(cfg.get("from_email") or cfg.get("smtp_username") or "").strip():
        return False, "адрес отправителя не указан"
    if str(cfg.get("smtp_username") or "").strip() and not _email_password():
        return False, "пароль SMTP не сохранён"
    return True, ""


def capabilities(settings: dict) -> dict:
    """Honest, static channel matrix for UI and route decisions."""
    from . import hh_oauth
    api_ready = bool(hh_oauth.access_token())
    out = {}
    for channel, label in CHANNELS.items():
        cfg = _cfg(settings, channel)
        enabled = bool(cfg.get("enabled", channel in ("official_api", "manual_assisted")))
        auto = bool(cfg.get("auto", channel == "official_api"))
        if channel == "official_api":
            available, reason = api_ready, "" if api_ready else "OAuth hh.ru не подключён"
            auth, attachments, verification = True, True, "ответ API + история откликов"
        elif channel == "ats_form":
            available = True
            reason = "" if cfg.get("domains") else "домены ATS не добавлены"
            auth, attachments, verification = False, True, "страница подтверждения"
        elif channel == "career_site_form":
            available = True
            reason = "" if cfg.get("domains") else "домены карьерных сайтов не добавлены"
            auth, attachments, verification = False, True, "страница подтверждения"
        elif channel == "email":
            available, reason = _email_ready(settings)
            auth, attachments, verification = bool(cfg.get("smtp_username")), True, "Message-ID + ответ SMTP"
        elif channel == "telegram_contact":
            available = bool(cfg.get("sender_configured"))
            reason = "" if available else "разрешённый отправитель Telegram не подключён"
            auth, attachments, verification = True, False, "подтверждение HR"
        else:
            available, reason = True, ""
            auth, attachments, verification = False, True, "ручная отправка"
        out[channel] = {
            "label": label, "enabled": enabled, "auto": auto, "available": available,
            "auto_allowed": enabled and auto and available and channel != "manual_assisted",
            "auth_required": auth, "attachments": attachments, "verification": verification,
            "reason": reason, "domains": cfg.get("domains") or [],
        }
    return out


def route(job, settings: dict, *, resume: dict | None = None, detail: dict | None = None) -> dict:
    detail = detail if detail is not None else _detail(job)
    url = str(detail.get("apply_url") or job["url"] or "")
    host = _host(url)
    text = " ".join([str(job["description"] or ""), json.dumps(detail, ensure_ascii=False)])
    resume = resume or {}
    contact = _contact(resume)
    caps = capabilities(settings)

    channel, adapter, auto_possible, reason = "manual_assisted", "generic", False, ""
    if host.endswith("hh.ru") or job["source"] == "hh_api":
        channel = "official_api" if caps["official_api"]["available"] else "manual_assisted"
        adapter = "hh_api"
        auto_possible = channel == "official_api"
        reason = "" if auto_possible else "официальный API hh.ru не подключён"
    elif any(host == h or host.endswith("." + h) for h in ATS_HOSTS):
        channel, adapter, auto_possible = "ats_form", "ats", True
    elif host in CAREER_ADAPTERS:
        channel, adapter = "career_site_form", CAREER_ADAPTERS[host]
        auto_possible = adapter not in CAPTCHA_ADAPTERS
        reason = "форма защищена CAPTCHA" if adapter in CAPTCHA_ADAPTERS else ""
    elif host in [str(x).casefold() for x in (caps["career_site_form"].get("domains") or [])]:
        channel, adapter, auto_possible = "career_site_form", "generic", True
    elif _email_contact(text):
        channel, adapter = "email", "email"
        auto_possible = True
        ready, why = _email_ready(settings)
        reason = "" if ready else why
    elif _telegram_contact(text):
        channel, adapter = "telegram_contact", "telegram"
        reason = "нужно подключить разрешённый Telegram-отправитель"
    else:
        channel, adapter = "manual_assisted", "generic"
        reason = "автоматическая форма не подтверждена"

    cap = caps[channel]
    configured = bool(cap["enabled"])
    domain_ok = True
    if channel in ("ats_form", "career_site_form"):
        domains = [str(x).casefold() for x in cap.get("domains") or []]
        domain_ok = any(host == d or host.endswith("." + d) for d in domains)
        if not domain_ok:
            reason = reason or "домен не добавлен в разрешённые"
    auto_allowed = bool(cap["auto_allowed"] and auto_possible and domain_ok)
    missing = _missing_contact(contact) if channel in ("ats_form", "career_site_form") else []
    verification = cap["verification"]
    return {
        "channel": channel, "label": CHANNELS[channel], "adapter": adapter,
        "host": host, "apply_url": url, "enabled": configured,
        "available": bool(cap["available"]),
        "auto_possible": auto_possible, "auto_allowed": auto_allowed,
        "requires_auth": cap["auth_required"], "attachments": cap["attachments"],
        "missing_fields": missing, "requires_confirmation": not auto_allowed,
        "verification": verification, "reason": reason,
        "captcha_expected": adapter in CAPTCHA_ADAPTERS,
        "email": _email_contact(text) if channel == "email" else "",
        "telegram": _telegram_contact(text) if channel == "telegram_contact" else "",
        "telegram_url": (f"https://t.me/{_telegram_contact(text)}"
                         if channel == "telegram_contact" and _telegram_contact(text) else ""),
    }


def policy_allows(settings: dict, route_info: dict) -> bool:
    policy = settings.get("autopilot") or {}
    if policy.get("mode") != "autopilot" or not policy.get("enabled") or not policy.get("auto_apply"):
        return False
    cfg = _cfg(settings, route_info["channel"])
    return bool(route_info["auto_allowed"] and cfg.get("enabled") and cfg.get("auto"))


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()


def _record(db: sqlite3.Connection, *, job_id: str, app_id: int | None, channel: str,
            host: str, status: str, reason: str = "", external_id: str = "",
            evidence: dict | None = None, screenshot: str = "", duration_ms: int = 0) -> None:
    db.execute(
        """INSERT INTO submission_attempts(created_at,job_id,application_id,channel,host,status,
             reason,external_id,evidence,screenshot,duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (_now(), job_id, app_id, channel, host, status, reason[:1000], external_id,
         json.dumps(evidence or {}, ensure_ascii=False)[:20000], screenshot, duration_ms))
    db.commit()


def _duplicate(db: sqlite3.Connection, job_id: str, app_id: int | None) -> dict | None:
    row = db.execute(
        """SELECT id,state,sent_at,channel FROM applications
           WHERE job_id=? AND id<>COALESCE(?, -1)
             AND (sent_at IS NOT NULL OR state IN ('submitted','submitting','submission_unverified'))
           ORDER BY id DESC LIMIT 1""", (job_id, app_id)).fetchone()
    if row:
        return dict(row)
    job = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job and job["status"] == "submitted":
        return {"id": None, "state": "submitted", "sent_at": "", "channel": "job"}
    return None


def _set(db: sqlite3.Connection, app_id: int, state: str, **fields) -> None:
    values = {"state": state, "updated_at": dbm.now(), **fields}
    columns = ", ".join(f"{key}=?" for key in values)
    db.execute(f"UPDATE applications SET {columns} WHERE id=?", (*values.values(), app_id))
    db.commit()


def _screenshot(page, app_id: int, suffix: str) -> str:
    ensure_dirs()
    path = LOGS / f"submission-{app_id}-{suffix}-{datetime.now():%Y%m%d-%H%M%S}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:
        return ""


def _resume_path(resume: dict, variant: str, job) -> str:
    try:
        outputs = export_variant(resume, variant, formats=["pdf"], job=dict(job))
    except Exception:
        return ""
    path = outputs.get("pdf") or ""
    return path if path and Path(path).is_file() else ""


def _check_consents(scope) -> list[str]:
    clicked = []
    for box in scope.locator('input[type="checkbox"]').all():
        try:
            if box.is_checked():
                continue
            label = ""
            box_id = box.get_attribute("id") or ""
            if box_id:
                try:
                    label = scope.page.locator(f'label[for="{box_id}"]').first.inner_text(timeout=800)
                except Exception:
                    label = ""
            if not label:
                for xpath in ("xpath=ancestor::label[1]", "xpath=ancestor::div[1]",
                              "xpath=ancestor::*[self::div or self::p][1]"):
                    try:
                        label = box.locator(xpath).first.inner_text(timeout=800)
                        if label:
                            break
                    except Exception:
                        continue
            if not label:
                label = box.get_attribute("aria-label") or box.get_attribute("name") or ""
            if re.search(r"соглас|принима|персональн|privacy|consent|terms", label, re.I) and \
                    not re.search(r"рассыл|получение информац|актуальн|маркет|newsletter|promo", label, re.I):
                try:
                    box.check(force=True)
                except Exception:
                    try:
                        if box_id:
                            scope.page.locator(f'label[for="{box_id}"] .h-checkbox__inner').first.click(force=True)
                    except Exception:
                        pass
                if not box.is_checked() and box_id:
                    try:
                        scope.page.locator(f'label[for="{box_id}"]').first.click(force=True)
                    except Exception:
                        pass
                if not box.is_checked():
                    try:
                        box.evaluate("e => { e.checked = true; e.dispatchEvent(new Event('input', {bubbles:true})); e.dispatchEvent(new Event('change', {bubbles:true})); }")
                    except Exception:
                        pass
                scope.page.wait_for_timeout(300)
                if box.is_checked():
                    clicked.append(label[:80])
        except Exception:
            continue
    return clicked


def _application_scope(page):
    for form in page.locator("form").all():
        try:
            if form.locator('input[type="file"]').count():
                return form
        except Exception:
            continue
    return page


def _unresolved_city(scope) -> bool:
    for element in scope.locator('input[role="combobox"]').all():
        try:
            if not element.is_visible() or (element.input_value() or "").strip():
                continue
            container = element.locator("xpath=ancestor::*[@data-button-aria-label][1]")
            label = container.first.get_attribute("data-button-aria-label") if container.count() else ""
            label = (label or element.get_attribute("aria-label") or "").casefold()
            if re.search(r"город|city|населённ|населенн", label):
                return True
        except Exception:
            continue
    return False


def _fill_city(scope, contact: dict) -> list[str]:
    city = str(contact.get("city") or "").strip()
    if not city:
        return []
    from .apply_browser import _field_label
    filled = []
    for element in scope.locator("input").all():
        try:
            if not element.is_visible() or (element.input_value() or "").strip():
                continue
            label = _field_label(element).casefold()
            if re.search(r"город|city|населённ|населенн", label):
                element.fill(city)
                filled.append("city")
        except Exception:
            continue
    for element in scope.locator('input[role="combobox"]').all():
        try:
            if not element.is_visible() or (element.input_value() or "").strip():
                continue
            container = element.locator("xpath=ancestor::*[@data-button-aria-label][1]")
            label = container.first.get_attribute("data-button-aria-label") if container.count() else ""
            label = (label or element.get_attribute("aria-label") or "").casefold()
            if not re.search(r"город|city|населённ|населенн", label):
                continue
            element.click()
            element.fill(city)
            scope.page.wait_for_timeout(1800)
            chosen = False
            for option in scope.page.locator('[role="option"]').all():
                try:
                    text = option.inner_text(timeout=800)
                    if text.strip().casefold() == city.casefold() or city.casefold() in text.casefold():
                        option.click()
                        chosen = True
                        break
                except Exception:
                    continue
            if not chosen:
                element.press("Enter")
            scope.page.wait_for_timeout(300)
            if (element.input_value() or "").strip():
                filled.append("city")
        except Exception:
            continue
    return filled


def _open_form(page, url: str, adapter: str) -> dict:
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    blocked = detect_block(page)
    if blocked:
        return {"ok": False, "status": "blocked", "reason": blocked}
    body = page.locator("body").inner_text(timeout=6000).casefold()
    if re.search(r"вакансия в архиве|вакансия закрыта|not found|страница не найдена", body):
        return {"ok": False, "status": "closed", "reason": "вакансия закрыта или недоступна"}
    if adapter == "habr":
        try:
            link = page.locator('a[href="#guest-response"]').first
            if link.count():
                link.click()
                page.wait_for_timeout(800)
        except Exception:
            pass
    elif page.locator("form").count() == 0:
        for pattern in (r"откликнуться", r"apply", r"отправить резюме", r"заполнить"):
            try:
                button = page.get_by_role("button", name=re.compile(pattern, re.I))
                if not button.count():
                    button = page.get_by_role("link", name=re.compile(pattern, re.I))
                if button.count():
                    button.first.click()
                    page.wait_for_timeout(1200)
                    break
            except Exception:
                continue
    blocked = detect_block(page)
    if blocked:
        return {"ok": False, "status": "blocked", "reason": blocked}
    has_form = bool(page.locator("form, [role='dialog']").count())
    login = bool(re.search(r"войд|войт|sign in|log in|авториз", page.locator("body").inner_text(timeout=5000).casefold())) \
        and not has_form
    if login:
        return {"ok": False, "status": "auth_required", "reason": "нужна авторизация на площадке"}
    if not has_form:
        return {"ok": False, "status": "manual", "reason": "форма отклика не найдена"}
    return {"ok": True, "status": "form"}


def _submit_button(scope):
    for pattern in (r"^Отправить$", r"^Откликнуться", r"^Submit", r"^Apply", r"^Send"):
        try:
            button = scope.get_by_role("button", name=re.compile(pattern, re.I))
            for item in button.all():
                if item.is_visible() and item.is_enabled():
                    return item
        except Exception:
            continue
    try:
        buttons = scope.locator('button[type="submit"], input[type="submit"]').all()
        for item in buttons:
            if item.is_visible():
                return item
    except Exception:
        pass
    return None


def _markers(body: str, url: str) -> bool:
    text = (body or "").casefold()
    if any(marker in text for marker in (
            "отклик отправлен", "заявка отправлена", "спасибо за отклик", "спасибо, заявка",
            "application submitted", "application received", "thank you for applying",
            "успешно отправлен")):
        return True
    return any(part in (url or "").casefold() for part in ("/thanks", "/success", "/applied"))


def _submit_form(db: sqlite3.Connection, settings: dict, app, job, route_info: dict, *,
                 resume: dict, profile: dict, confirm: bool, headless: bool | None,
                 dry_run: bool) -> dict:
    variant = app["resume_variant"] or select_variant(resume, dict(job), _detail(job))
    contact = _contact(resume)
    missing = _missing_contact(contact)
    if missing:
        return {"status": "requires_user", "reason": "Не хватает данных: " + ", ".join(missing)}
    resume_path = _resume_path(resume, variant, job)
    if route_info["attachments"] and not resume_path:
        return {"status": "requires_user", "reason": "Не удалось подготовить PDF-резюме"}

    started = time.time()
    try:
        from .apply_browser import _fill_form  # shared, tested field matcher
        with session(settings, headless=headless) as context:
            page = context.new_page()
            opened = _open_form(page, route_info["apply_url"], route_info["adapter"])
            if not opened["ok"]:
                status = "blocked" if opened["status"] in ("blocked", "auth_required") else "requires_user"
                return {"status": status, "reason": opened["reason"],
                        "url": page.url, "screenshot": _screenshot(page, app["id"], "open")}
            scope = _application_scope(page)
            form_state = _fill_form(scope, contact, job["letter"], resume_path)
            form_state["filled"] += _fill_city(scope, contact)
            consents = _check_consents(scope)
            remaining = [x for x in form_state["empty_required"]
                         if not re.search(r"соглас|персональн|consent|terms|принима", x, re.I)]
            if remaining:
                return {"status": "requires_user",
                        "reason": "Заполните вручную: " + ", ".join(remaining[:4]),
                        "filled": form_state["filled"], "url": page.url,
                        "screenshot": _screenshot(page, app["id"], "form")}
            if _unresolved_city(scope):
                return {"status": "requires_user",
                        "reason": "Выберите город в форме",
                        "filled": form_state["filled"], "consents": consents, "url": page.url,
                        "screenshot": _screenshot(page, app["id"], "form")}
            if dry_run or not confirm:
                page.wait_for_timeout(600)
                try:
                    page.locator("form").first.scroll_into_view_if_needed()
                    page.wait_for_timeout(300)
                except Exception:
                    pass
                return {"status": "ready_to_review",
                        "reason": "Форма заполнена, отправка не выполнялась",
                        "filled": form_state["filled"], "consents": consents,
                        "url": page.url, "screenshot": _screenshot(page, app["id"], "ready")}
            button = _submit_button(scope)
            if button is None:
                return {"status": "requires_user", "reason": "Кнопка отправки не найдена",
                        "url": page.url, "screenshot": _screenshot(page, app["id"], "no-submit")}
            try:
                button.scroll_into_view_if_needed()
            except Exception:
                pass
            before = _screenshot(page, app["id"], "before-submit")
            try:
                with page.expect_navigation(timeout=15000):
                    button.click()
            except Exception:
                page.wait_for_timeout(2500)
            page.wait_for_timeout(1500)
            blocked = detect_block(page)
            if blocked:
                return {"status": "blocked", "reason": blocked, "url": page.url,
                        "screenshot": _screenshot(page, app["id"], "blocked")}
            try:
                body = page.locator("body").inner_text(timeout=6000)
            except Exception:
                body = ""
            screenshot = _screenshot(page, app["id"], "after-submit")
            evidence = {"url": page.url, "body_sha256": hashlib.sha256(body.encode()).hexdigest()[:24],
                        "screenshot_before": before, "screenshot_after": screenshot,
                        "filled": form_state["filled"], "consents": consents}
            if _markers(body, page.url):
                return {"status": "submitted", "url": page.url, "screenshot": screenshot,
                        "evidence": evidence, "duration_ms": int((time.time() - started) * 1000),
                        "external_id": f"portal:{hashlib.sha256((page.url + body[:200]).encode()).hexdigest()[:16]}"}
            return {"status": "unverified",
                    "reason": "Страница не подтвердила получение заявки",
                    "url": page.url, "screenshot": screenshot, "evidence": evidence,
                    "duration_ms": int((time.time() - started) * 1000)}
    except BrowserBlocked as exc:
        return {"status": "blocked", "reason": str(exc)}
    except BrowserUnavailable as exc:
        return {"status": "unavailable", "reason": str(exc)}
    except Exception as exc:
        return {"status": "unverified", "reason": f"{type(exc).__name__}: {exc}"[:400],
                "duration_ms": int((time.time() - started) * 1000)}


def test_email_connection(settings: dict) -> dict:
    """SMTP login/NOOP check without sending a message."""
    ready, reason = _email_ready(settings)
    if not ready:
        return {"ok": False, "reason": reason}
    cfg = _cfg(settings, "email")
    host = str(cfg.get("smtp_host") or "").strip()
    port = int(cfg.get("smtp_port") or 587)
    security = str(cfg.get("smtp_security") or "starttls").casefold()
    username = str(cfg.get("smtp_username") or "").strip()
    password = _email_password()
    smtp = None
    try:
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(host, port, timeout=15)
            smtp.ehlo()
            if security == "starttls":
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
        if username and password:
            smtp.login(username, password)
        code, message = smtp.noop()
        return {"ok": 200 <= int(code) < 300, "host": host, "port": port,
                "security": security, "auth": bool(username),
                "reason": "" if 200 <= int(code) < 300 else f"SMTP NOOP: {code}"}
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "reason": "SMTP: неверный логин или пароль приложения"}
    except (smtplib.SMTPException, TimeoutError, OSError) as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        try:
            if smtp is not None:
                smtp.quit()
        except Exception:
            pass


def _submit_email(db: sqlite3.Connection, settings: dict, app, job, route_info: dict, *,
                  resume: dict, confirm: bool) -> dict:
    cfg = _cfg(settings, "email")
    to_addr = str(route_info.get("email") or "").strip()
    if not to_addr or not EMAIL_RE.fullmatch(to_addr):
        return {"status": "requires_user", "reason": "Не подтверждён email работодателя"}
    from_email = str(cfg.get("from_email") or cfg.get("smtp_username") or "").strip()
    if not from_email or not EMAIL_RE.fullmatch(from_email):
        return {"status": "requires_user", "reason": "Не указан email отправителя"}
    password = _email_password()
    username = str(cfg.get("smtp_username") or "").strip()
    if username and not password:
        return {"status": "requires_user", "reason": "Пароль SMTP не сохранён"}
    already = db.execute(
        """SELECT 1 FROM submission_attempts
           WHERE channel='email' AND status='submitted' AND evidence LIKE ? LIMIT 1""",
        (f'%"to": "{to_addr}"%',)).fetchone()
    if already:
        return {"status": "duplicate", "reason": "Этому адресу уже отправляли отклик"}
    resume_path = _resume_path(resume, app["resume_variant"] or "python", job)
    if not resume_path:
        return {"status": "requires_user", "reason": "Не удалось подготовить PDF-резюме"}
    contact = _contact(resume)
    name = " ".join(x for x in (contact.get("first_name"), contact.get("last_name")) if x).strip()
    subject = f"Отклик на вакансию «{str(job['title'])[:90]}»" + (f" — {name}" if name else "")
    body = (str(job["letter"] or "").strip() + "\n\n"
            f"Резюме во вложении. Контакт: {from_email}\n")
    msg = EmailMessage()
    msg["From"] = formataddr((str(cfg.get("from_name") or name or ""), from_email))
    msg["To"] = to_addr
    msg["Subject"] = subject[:180]
    msg["Message-ID"] = make_msgid(domain=from_email.split("@")[-1] or "ai-job-hunter.local")
    msg.set_content(body)
    try:
        data = Path(resume_path).read_bytes()
        msg.add_attachment(data, maintype="application", subtype="pdf",
                           filename=Path(resume_path).name)
    except OSError as exc:
        return {"status": "requires_user", "reason": f"Резюме не читается: {exc}"}
    host = str(cfg.get("smtp_host") or "").strip()
    port = int(cfg.get("smtp_port") or 587)
    security = str(cfg.get("smtp_security") or "starttls").casefold()
    started = time.time()
    smtp = None
    try:
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(host, port, timeout=30)
            smtp.ehlo()
            if security == "starttls":
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
        if username and password:
            smtp.login(username, password)
        refused = smtp.send_message(msg, from_addr=from_email, to_addrs=[to_addr])
        if refused:
            return {"status": "failed", "reason": f"SMTP отклонил получателя: {', '.join(refused)}"}
    except smtplib.SMTPAuthenticationError:
        return {"status": "blocked", "reason": "SMTP: неверный логин или пароль приложения"}
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
        return {"status": "failed", "reason": f"SMTP отклонил адрес: {str(exc)[:200]}"}
    except (smtplib.SMTPException, TimeoutError, OSError) as exc:
        return {"status": "unverified", "reason": f"SMTP не подтвердил отправку: {type(exc).__name__}"}
    finally:
        try:
            if smtp is not None:
                smtp.quit()
        except Exception:
            pass
    return {"status": "submitted", "external_id": msg["Message-ID"],
            "verification": "smtp_accepted",
            "evidence": {"to": to_addr, "from": from_email, "subject": subject,
                         "message_id": msg["Message-ID"], "accepted": True},
            "duration_ms": int((time.time() - started) * 1000)}


def _submit_api(db: sqlite3.Connection, settings: dict, app, job, route_info: dict, *,
                confirm: bool) -> dict:
    from . import hh_oauth
    from .hh_api_client import HHAPIError, HHClient
    token = hh_oauth.access_token()
    if not token:
        return {"status": "requires_user", "reason": "OAuth hh.ru не подключён"}
    policy = settings.get("autopilot") or {}
    allowed = [str(x) for x in policy.get("allowed_resumes") or [] if str(x).strip()]
    if not allowed:
        return {"status": "requires_user", "reason": "Не выбрано разрешённое резюме hh.ru"}
    resume_id = allowed[0]
    if not job["source_id"]:
        return {"status": "requires_user", "reason": "Нет ID вакансии hh.ru"}
    try:
        client = HHClient()
        result = client.apply(str(job["source_id"]), resume_id, job["letter"] or "")
    except HHAPIError as exc:
        if exc.kind in ("already_applied",):
            return {"status": "already", "reason": "Отклик уже существует"}
        if exc.kind in ("test_required",):
            return {"status": "requires_user", "reason": "Для отклика требуется тест"}
        if exc.status in (401, 403, 429):
            return {"status": "blocked", "reason": f"API hh.ru: {exc.reason}"}
        return {"status": "failed", "reason": f"API hh.ru: {exc.reason}"}
    if not result.get("ok"):
        return {"status": "failed", "reason": result.get("reason") or "API отказал"}
    negotiation = result.get("negotiation_id") or ""
    verified = False
    try:
        for item in client.negotiations(max_pages=5):
            vacancy = item.get("vacancy") or {}
            if str(vacancy.get("id") or "") == str(job["source_id"]):
                negotiation = negotiation or str(item.get("id") or "")
                verified = True
                break
    except Exception:
        pass
    return {"status": "submitted", "external_id": negotiation,
            "verification": "history" if verified else "api_response",
            "reason": "Подтверждено историей hh.ru" if verified else "HTTP-ответ API hh.ru"}


def _candidate_resume(db: sqlite3.Connection, settings: dict) -> dict:
    from .candidate_memory import CandidateMemory
    return CandidateMemory(db, settings).resume()


def submit_application(db: sqlite3.Connection, settings: dict, app_id: int, *,
                       confirm: bool = False, headless: bool | None = None,
                       dry_run: bool = False, resume: dict | None = None,
                       profile: dict | None = None) -> dict:
    _ensure_schema(db)
    app = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not app:
        return {"ok": False, "status": "missing", "reason": "Отклик не найден"}
    job = dbm.get_job(db, app["job_id"])
    if not job:
        return {"ok": False, "status": "missing", "reason": "Вакансия не найдена"}
    resume = resume or _candidate_resume(db, settings)
    profile = profile or settings.get("_profile") or {}
    route_info = route(job, settings, resume=resume)
    duplicate = _duplicate(db, job["id"], app["id"])
    if duplicate:
        return {"ok": False, "status": "duplicate", "reason": "По этой вакансии уже есть отклик",
                "existing": duplicate, "route": route_info}
    if app["state"] == "submitting":
        _set(db, app["id"], "submission_unverified", last_error="Прерванная отправка: результат неизвестен")
        _record(db, job_id=job["id"], app_id=app["id"], channel=route_info["channel"],
                host=route_info["host"], status="unverified", reason="stale submitting state")
        return {"ok": False, "status": "submission_unverified",
                "reason": "Предыдущая отправка не завершилась; повтор запрещён"}
    if app["state"] == "submission_unverified":
        return {"ok": False, "status": "submission_unverified",
                "reason": "Результат прошлой отправки не подтверждён; повтор запрещён",
                "route": route_info}
    if app["state"] not in ("approved", "queued", "ready_to_review", "requires_user_action"):
        return {"ok": False, "status": "blocked",
                "reason": f"Отклик в состоянии {app['state']}; отправка запрещена"}
    if not (job["letter"] or "").strip():
        return {"ok": False, "status": "blocked", "reason": "Письмо не подготовлено"}
    if str(job["letter_source"] or "").startswith(("factual_", "qwen_unavailable")):
        return {"ok": False, "status": "blocked", "reason": "Черновик требует ручной проверки"}
    if route_info["missing_fields"] and route_info["channel"] in ("ats_form", "career_site_form"):
        reason = "Не хватает данных: " + ", ".join(route_info["missing_fields"])
        _set(db, app["id"], "requires_user_action",
             note=json.dumps({"submission": route_info, "reason": reason}, ensure_ascii=False))
        _record(db, job_id=job["id"], app_id=app["id"], channel=route_info["channel"],
                host=route_info["host"], status="requires_user", reason=reason)
        return {"ok": False, "status": "requires_user", "reason": reason, "route": route_info}
    auto = policy_allows(settings, route_info)
    if not auto and not confirm and not dry_run:
        _set(db, app["id"], "requires_user_action",
             note=json.dumps({"submission": route_info, "reason": route_info["reason"] or "нужно подтверждение"},
                             ensure_ascii=False))
        _record(db, job_id=job["id"], app_id=app["id"], channel=route_info["channel"],
                host=route_info["host"], status="requires_user", reason=route_info["reason"])
        return {"ok": False, "status": "requires_user", "reason": route_info["reason"] or "Нужно подтверждение",
                "route": route_info}
    if route_info["channel"] == "telegram_contact" and not dry_run:
        _set(db, app["id"], "requires_user_action",
             note=json.dumps({"submission": route_info, "reason": route_info["reason"]},
                             ensure_ascii=False))
        _record(db, job_id=job["id"], app_id=app["id"], channel=route_info["channel"],
                host=route_info["host"], status="requires_user", reason=route_info["reason"])
        return {"ok": False, "status": "requires_user", "reason": route_info["reason"],
                "route": route_info}
    if route_info["channel"] == "manual_assisted" and not dry_run:
        _set(db, app["id"], "requires_user_action",
             note=json.dumps({"submission": route_info, "reason": route_info["reason"] or "нужно подтверждение"},
                             ensure_ascii=False))
        _record(db, job_id=job["id"], app_id=app["id"], channel=route_info["channel"],
                host=route_info["host"], status="requires_user", reason=route_info["reason"])
        return {"ok": False, "status": "requires_user", "reason": route_info["reason"] or "Нужно подтверждение",
                "route": route_info}

    cur = db.execute("""UPDATE applications SET state='submitting', attempts=attempts+1,
                        updated_at=? WHERE id=? AND state IN
                        ('approved','queued','ready_to_review','requires_user_action')""",
                     (dbm.now(), app["id"]))
    db.commit()
    if cur.rowcount != 1:
        return {"ok": False, "status": "blocked", "reason": "Отклик уже отправляется"}
    try:
        if route_info["channel"] == "email":
            if dry_run:
                cfg = _cfg(settings, "email")
                result = {"status": "ready_to_review",
                          "reason": "Письмо подготовлено; отправка не выполнялась",
                          "evidence": {"to": route_info.get("email"),
                                       "from": cfg.get("from_email") or cfg.get("smtp_username"),
                                       "subject": f"Отклик на вакансию «{job['title']}»"}}
            else:
                result = _submit_email(db, settings, app, job, route_info, resume=resume, confirm=confirm)
        elif route_info["channel"] == "official_api":
            result = _submit_api(db, settings, app, job, route_info, confirm=confirm)
        else:
            result = _submit_form(db, settings, app, job, route_info, resume=resume,
                                  profile=profile, confirm=(confirm or auto), headless=headless,
                                  dry_run=dry_run)
    except Exception as exc:
        result = {"status": "unverified", "reason": f"{type(exc).__name__}: {exc}"[:400]}

    status = result.get("status", "unverified")
    evidence = result.get("evidence") or {}
    screenshot = result.get("screenshot") or ""
    if status == "submitted":
        ts = dbm.now()
        _set(db, app["id"], "submitted", sent_at=ts, external_id=result.get("external_id") or "",
             verification_method=result.get("verification") or "portal_confirm",
             verification_source=route_info["host"], verified_at=ts,
             last_error=None, note=json.dumps({"submission": route_info, "evidence": evidence},
                                              ensure_ascii=False))
        db.execute("UPDATE jobs SET status='submitted', updated_at=? WHERE id=?", (ts, job["id"]))
        dbm.save_outcome(db, job, "applied", app["id"])
        dbm.log_event(db, job["id"], "application_submitted", actor="submission_engine",
                      payload={"channel": route_info["channel"], "host": route_info["host"],
                               "external_id": result.get("external_id"), "evidence": evidence},
                      application_id=app["id"])
    elif status in ("unverified", "unknown"):
        _set(db, app["id"], "submission_unverified", last_error=result.get("reason") or "Результат неизвестен",
             note=json.dumps({"submission": route_info, "evidence": evidence}, ensure_ascii=False))
        dbm.log_event(db, job["id"], "application_unverified", actor="submission_engine",
                      payload={"channel": route_info["channel"], "reason": result.get("reason")},
                      application_id=app["id"])
        status = "submission_unverified"
    elif status == "ready_to_review":
        _set(db, app["id"], "ready_to_review", last_error=None,
             note=json.dumps({"submission": route_info, "evidence": evidence}, ensure_ascii=False))
        status = "ready_to_review"
    else:
        mapped = "blocked" if status in ("blocked", "unavailable", "closed", "duplicate") else "requires_user_action"
        _set(db, app["id"], mapped, last_error=result.get("reason") or status,
             note=json.dumps({"submission": route_info, "evidence": evidence}, ensure_ascii=False))
        status = mapped
    _record(db, job_id=job["id"], app_id=app["id"], channel=route_info["channel"],
            host=route_info["host"], status=status, reason=result.get("reason", ""),
            external_id=result.get("external_id", ""), evidence=evidence, screenshot=screenshot,
            duration_ms=int(result.get("duration_ms") or 0))
    return {"ok": status in ("submitted", "ready_to_review"), "status": status,
            **{k: v for k, v in result.items() if k not in ("evidence", "status")}, "route": route_info}


def submit_job(db: sqlite3.Connection, settings: dict, job, *, confirm: bool = False,
               dry_run: bool = False, headless: bool | None = None,
               resume: dict | None = None) -> dict:
    """Create/reuse one application row and run the routed adapter."""
    _ensure_schema(db)
    resume = resume or _candidate_resume(db, settings)
    route_info = route(job, settings, resume=resume)
    existing = db.execute("SELECT * FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
                          (job["id"],)).fetchone()
    if existing:
        if existing["channel"] != route_info["channel"] and existing["state"] in (
                "queued", "manual_review", "requires_user_action", "approved", "ready_to_review"):
            try:
                db.execute("UPDATE applications SET channel=?, updated_at=? WHERE id=?",
                           (route_info["channel"], dbm.now(), existing["id"]))
                db.commit()
            except sqlite3.IntegrityError:
                pass
        return submit_application(db, settings, existing["id"], confirm=confirm,
                                  dry_run=dry_run, headless=headless, resume=resume)
    variant = select_variant(resume, dict(job), _detail(job))
    ts = dbm.now()
    cur = db.execute("""INSERT INTO applications(job_id,channel,state,letter_hash,resume_variant,
                         created_at,updated_at,approved_at) VALUES (?,?,?,?,?,?,?,?)""",
                     (job["id"], route_info["channel"], "approved", job["letter_hash"], variant,
                      ts, ts, ts))
    db.commit()
    return submit_application(db, settings, int(cur.lastrowid), confirm=confirm,
                              dry_run=dry_run, headless=headless, resume=resume)


def inspect_url(url: str, settings: dict, *, headless: bool = True) -> dict:
    """Read-only page capability probe used by tests and diagnostics."""
    try:
        with session(settings, headless=headless) as context:
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            body = page.locator("body").inner_text(timeout=6000).casefold()
            return {
                "url": page.url, "title": page.title(),
                "captcha": bool(detect_block(page)),
                "forms": page.locator("form").count(),
                "file_inputs": page.locator('input[type="file"]').count(),
                "login": bool(re.search(r"войд|войт|sign in|log in|авториз", body)),
                "archived": bool(re.search(r"в архиве|закрыта|not found", body)),
                "apply_buttons": page.get_by_text(re.compile(r"отклик|apply|отправить", re.I)).count(),
            }
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:300]}
