"""Browser-assisted application drafting (draft_only).

The adapter opens the public vacancy page in the isolated Playwright profile,
tries to open the application form, fills only verified contact data and the
prepared letter, attaches the exported resume, takes a screenshot and STOPS.
It never clicks the final submit button and never reports a submission.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

from . import db as dbm
from .browser import BrowserBlocked, BrowserUnavailable, detect_block, session
from .paths import LOGS, ensure_dirs
from .resume import export_variant, load_resume, sync_from_profile

FIELD_HINTS = {
    "first_name": ("имя", "first name", "firstname", "given name"),
    "last_name": ("фамилия", "last name", "lastname", "surname", "family"),
    "email": ("email", "e-mail", "почта", "электрон"),
    "phone": ("телефон", "phone", "mobile", "мобильн", "+7", "тел."),
    "letter": ("письм", "сообщен", "cover", "motivation", "comment", "комментар", "сопровод"),
}


def _log(db: sqlite3.Connection, job_id: str, app_id: int, kind: str, payload: dict) -> None:
    dbm.log_event(db, job_id, kind, payload=payload, application_id=app_id)
    db.commit()


def _screenshot(page, app_id: int) -> str:
    ensure_dirs()
    path = LOGS / f"apply-form-{app_id}-{date.today():%Y%m%d}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:
        return ""


def _match(name: str) -> str:
    low = (name or "").casefold()
    for field, hints in FIELD_HINTS.items():
        if any(hint in low for hint in hints):
            return field
    return ""


def _resume_file(resume: dict, variant: str, job: dict) -> str:
    try:
        outputs = export_variant(resume, variant, formats=["pdf", "docx"], job=job)
    except Exception:
        return ""
    for key in ("pdf", "docx"):
        path = outputs.get(key)
        if path and Path(path).is_file():
            return str(path)
    return ""


def _label_for(element) -> str:
    try:
        element_id = element.get_attribute("id") or ""
        if element_id:
            label = element.page.locator(f'label[for="{element_id}"]')
            if label.count():
                return label.first.inner_text(timeout=1500)
    except Exception:
        pass
    try:
        parent = element.locator("xpath=ancestor::*[self::label or self::div][1]")
        if parent.count():
            return parent.first.inner_text(timeout=1200)[:180]
    except Exception:
        pass
    return ""


def _field_label(element) -> str:
    return " ".join(filter(None, [
        element.get_attribute("name"), element.get_attribute("placeholder"),
        element.get_attribute("aria-label"), element.get_attribute("id"),
        _label_for(element),
    ]))


def _fill_form(page, contact: dict, letter: str, resume_path: str) -> dict:
    filled: list[str] = []
    empty_required: list[str] = []
    unknown_required: list[str] = []
    for element in page.locator("input, textarea").all():
        try:
            if not element.is_visible():
                continue
            kind = (element.get_attribute("type") or "text").casefold()
            if kind in ("hidden", "submit", "button", "checkbox", "radio", "file"):
                if kind in ("checkbox", "radio") and element.get_attribute("required"):
                    unknown_required.append(_field_label(element)[:60])
                continue
            label = _field_label(element)
            field = _match(label)
            value = ""
            if field == "first_name":
                value = contact.get("first_name") or ""
            elif field == "last_name":
                value = contact.get("last_name") or ""
            elif field == "email":
                value = contact.get("email") or ""
            elif field == "phone":
                value = contact.get("phone") or ""
            elif field == "letter":
                value = letter
            if not value:
                if element.get_attribute("required"):
                    unknown_required.append(label[:60] or "обязательное поле")
                continue
            element.fill(value[:4000])
            filled.append(field)
        except Exception:
            continue
    for element in page.locator("select").all():
        try:
            if not element.is_visible():
                continue
            label = _field_label(element).casefold()
            options = [o.strip() for o in element.locator("option").all_inner_texts() if o.strip()]
            choice = ""
            if any(word in label for word in ("город", "city", "населённ", "населенн")) and contact.get("city"):
                choice = next((o for o in options if contact["city"].casefold() in o.casefold()), "")
            elif any(word in label for word in ("переезд", "relocation", "релокац")):
                choice = next((o for o in options if o.casefold() in ("нет", "no")), "")
            elif any(word in label for word in ("гражданств", "citizenship")) and contact.get("citizenship"):
                choice = next((o for o in options if contact["citizenship"].casefold() in o.casefold()), "")
            if choice:
                element.select_option(label=choice)
                filled.append(label[:50] or "select")
            elif element.get_attribute("required"):
                unknown_required.append(label[:60] or "обязательный список")
        except Exception:
            continue
    if resume_path:
        try:
            file_input = page.locator('input[type="file"]')
            if file_input.count():
                file_input.first.set_input_files(resume_path)
                filled.append("resume_file")
        except Exception:
            pass
    for element in page.locator("[required]").all():
        try:
            tag = (element.evaluate("e => e.tagName") or "").casefold()
            if tag == "select":
                continue
            if element.is_visible() and not (element.input_value() or "").strip():
                name = (_field_label(element) or "поле")[:60]
                if name not in empty_required:
                    empty_required.append(name)
        except Exception:
            continue
    for name in unknown_required:
        if name and name not in empty_required:
            empty_required.append(name)
    return {"filled": sorted(set(filled)), "empty_required": empty_required[:10]}



def prepare_form(db: sqlite3.Connection, settings: dict, app_id: int, *,
                 headed: bool = False) -> dict:
    app = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not app:
        return {"ok": False, "status": "missing", "reason": "Отклик не найден"}
    job = dbm.get_job(db, app["job_id"])
    if not job:
        return {"ok": False, "status": "missing", "reason": "Вакансия не найдена"}
    if not (job["letter"] or "").strip():
        return {"ok": False, "status": "blocked", "reason": "Письмо не подготовлено"}
    variant = app["resume_variant"] or "python"
    approved = app["state"] == "approved"
    resume = sync_from_profile(load_resume())
    contact = resume.get("contact") or {}
    db.execute("UPDATE applications SET state='opening_application', updated_at=? WHERE id=?",
               (dbm.now(), app_id))
    _log(db, job["id"], app_id, "application_open_started",
         {"url": job["url"], "headed": headed})
    try:
        with session(settings, headless=not headed) as context:
            page = context.new_page()
            page.goto(job["url"], wait_until="domcontentloaded")
            db.execute("UPDATE applications SET state='form_filling', updated_at=? WHERE id=?",
                       (dbm.now(), app_id))
            _log(db, job["id"], app_id, "application_form_filling", {"url": page.url})
            page.wait_for_timeout(1200)
            blocked = detect_block(page)
            if blocked:
                db.execute("""UPDATE applications SET state='blocked', last_error=?, updated_at=?
                              WHERE id=?""", (blocked[:400], dbm.now(), app_id))
                _log(db, job["id"], app_id, "application_form_blocked", {"reason": blocked})
                return {"ok": False, "status": "blocked", "reason": blocked, "url": job["url"]}
            opened = False
            for pattern in (r"откликнуться", r"отклик", r"apply", r"send resume",
                            r"отправить резюме"):
                try:
                    button = page.get_by_role("button", name=re.compile(pattern, re.I))
                    if button.count() == 0:
                        button = page.get_by_role("link", name=re.compile(pattern, re.I))
                    if button.count():
                        button.first.click()
                        opened = True
                        page.wait_for_timeout(1200)
                        break
                except Exception:
                    continue
            body = ""
            try:
                body = page.locator("body").inner_text(timeout=4000).casefold()
            except Exception:
                pass
            login_required = bool(re.search(r"войд|войт|sign in|log in|авториз", body)) and \
                not page.locator("form").count()
            resume_path = _resume_file(resume, variant, dict(job))
            form_state = _fill_form(page, contact, job["letter"], resume_path) if not login_required else \
                {"filled": [], "empty_required": []}
            if resume_path:
                db.execute("UPDATE applications SET resume_path=?, resume_variant=?, updated_at=? WHERE id=?",
                           (resume_path, variant, dbm.now(), app_id))
                db.commit()
            shot = _screenshot(page, app_id)
            has_form = bool(page.locator("form, [role='dialog']").count())
            final_button = False
            try:
                final_button = bool(page.get_by_role(
                    "button", name=re.compile(r"отправ|submit|send", re.I)).count())
            except Exception:
                final_button = False
            status = "requires_user_action"
            if login_required:
                note_text = "Нужна авторизация на площадке"
            elif not has_form:
                note_text = "Форма отклика не найдена — откройте вакансию вручную"
            elif form_state["empty_required"]:
                note_text = "Форма открыта и частично заполнена; заполните вручную: " + \
                    ", ".join(form_state["empty_required"][:4])
            elif form_state["filled"]:
                status = "ready_to_review"
                note_text = "Форма заполнена, финальная кнопка не нажата"
            else:
                note_text = "Форма найдена, но подтверждённых данных для заполнения нет"
            if approved and not login_required:
                status = "approved"
            note = {
                "mode": "draft_only",
                "opened_form": opened,
                "has_form": has_form,
                "login_required": login_required,
                "filled": form_state["filled"],
                "empty_required": form_state["empty_required"],
                "final_button_present": final_button,
                "screenshot": shot,
                "url": page.url,
                "submitted": False,
                "note": note_text,
            }
            db.execute("""UPDATE applications SET state=?, note=?, updated_at=? WHERE id=?""",
                       (status, json.dumps(note, ensure_ascii=False), dbm.now(), app_id))
            _log(db, job["id"], app_id, "application_form_prepared", note)
            return {"ok": True, "status": status, "url": page.url, **note}
    except BrowserBlocked as exc:
        db.execute("UPDATE applications SET state='blocked', last_error=?, updated_at=? WHERE id=?",
                   (str(exc)[:400], dbm.now(), app_id))
        return {"ok": False, "status": "blocked", "reason": str(exc)}
    except BrowserUnavailable as exc:
        return {"ok": False, "status": "unavailable", "reason": str(exc)}
    except Exception as exc:
        db.execute("""UPDATE applications SET state='requires_user_action', last_error=?, updated_at=?
                      WHERE id=?""", (f"{type(exc).__name__}: {exc}"[:400], dbm.now(), app_id))
        return {"ok": False, "status": "requires_user_action",
                "reason": f"{type(exc).__name__}: {exc}"}
