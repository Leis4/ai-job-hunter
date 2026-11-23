"""Local dashboard: quality UI, actions and exports. Binds to 127.0.0.1 only."""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import threading
import time
import traceback
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from core import db as dbm
from core import onboarding as onboarding_io
from core import resume as resume_io
from core.analytics import calibration_status, dashboard_data, export_all
from core.letters import generate_letter
from core.llm import LLM
from core.messages import (CATEGORY_LABELS, draft_reply, list_messages, message_stats,
                           save_incoming, sync_from_browser)
from core.paths import DATA, EXPORTS, ROOT, TOKEN_PATH, ensure_dirs
from core.pipeline import run_cycle
from core.queue import (approve, cancel, export_queue_csv, list_applications,
                        queue_stats, send_application, build_queue)
from core.resume import (VARIANTS, confirm_items, diff_variants, export_variant, facts_pack,
                         import_resume, list_resume_versions, load_resume, restore_version,
                         save_resume, select_variant, sync_from_profile, variant_summary,
                         variant_view, verified_skills)
from core.scoring import calibrated_probability, train_calibration
from core.aitunnel import AITunnelRouter
from core.memory import Memory
from core.settings import load_profile, load_settings, redact, save_settings
from core.sources import SOURCES
from core.text import clean_text
from . import api as api_web
from .theme import CSS, NAV, layout  # AFO visual system

FRONTEND_DIST = ROOT / "frontend" / "dist"
LEGACY_UI = os.environ.get("JOB_HUNTER_LEGACY_UI", "") == "1"

STATUS_LABELS = {
    "new": "Новая", "draft": "Черновик", "queued": "В очереди", "approved": "Одобрена",
    "submitted": "Отправлена", "viewed": "Просмотрена", "replied": "Ответ", "invited": "Приглашение",
    "offer": "Оффер", "rejected": "Отказ", "archived": "Архив", "skipped": "Пропущена",
}
QUEUE_LABELS = {
    "queued": "В очереди", "approved": "Одобрено", "sending": "Отправляется", "submitted": "Отправлено",
    "exported": "Выгружено", "failed": "Ошибка", "blocked": "Заблокировано", "cancelled": "Отменено",
}
FACTOR_LABELS = {
    "role": "Роль", "skills": "Навыки", "text": "Текст вакансии", "experience": "Опыт",
    "work_mode": "Формат работы", "location": "География", "salary": "Зарплата",
    "freshness": "Свежесть",
}
MODE_LABELS = {"remote": "удалённо", "hybrid": "гибрид", "office": "офис"}
CHANNEL_LABELS = {"manual_export": "Ручная выгрузка", "hh_ui": "hh.ru (сессия)"}
EVENT_LABELS = {
    "job_imported": "Импорт вакансии", "letter_drafted": "Письмо подготовлено",
    "letter_edited": "Письмо отредактировано", "queued": "В очереди",
    "application_approved": "Отклик одобрен", "application_cancelled": "Отклик отменён",
    "application_submitted": "Отклик отправлен", "application_exported": "Письмо выгружено",
    "application_blocked": "Отклик остановлен", "message_received": "Сообщение работодателя",
    "favorite_toggled": "Избранное", "cycle_error": "Ошибка цикла",
}

def e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def safe_url(url: str) -> str:
    text = str(url or "").strip()
    parts = urlsplit(text)
    return e(text) if parts.scheme in ("http", "https") and parts.netloc else "#"


def chip(text: str, kind: str = "") -> str:
    return f'<span class="chip {kind}">{e(text)}</span>'


def status_chip(status: str) -> str:
    kind = {"invited": "good", "offer": "good", "rejected": "bad", "submitted": "info",
            "approved": "warn", "queued": "warn"}.get(status, "")
    return chip(STATUS_LABELS.get(status, status or "—"), kind)


def score_color(score: float) -> str:
    if score >= 85:
        return "good"
    if score >= 70:
        return "info"
    if score >= 50:
        return "warn"
    return "bad"


def score_cell(score, low=None, high=None) -> str:
    value = float(score or 0)
    head = chip(f"{value:.0f}", score_color(value))
    if low is not None and high is not None:
        return f'{head}<br><small>{float(low):.0f}–{float(high):.0f}</small>'
    return head


def num(value, digits: int = 0, default: str = "—") -> str:
    return default if value is None else f"{float(value):.{digits}f}"


def fmt_pct(value, digits: int = 0) -> str:
    return "—" if value is None else f"{float(value) * 100:.{digits}f}%"


def fmt_money(low, high, currency="RUB") -> str:
    if not low and not high:
        return "—"
    symbol = {"RUB": "₽", "USD": "$", "EUR": "€", "KZT": "₸"}.get((currency or "RUB").upper(), currency or "")
    if low and high and low != high:
        return f"{low:,} – {high:,} {symbol}".replace(",", " ")
    return f"{low or high:,} {symbol}".replace(",", " ")


def fmt_dt(value) -> str:
    if not value:
        return "—"
    return str(value)[:16].replace("T", " ")


def token() -> str:
    ensure_dirs()
    if not TOKEN_PATH.exists():
        TOKEN_PATH.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


def svg_line(series: list[dict], keys: list[tuple[str, str]], *, height: int = 168) -> str:
    if not series:
        return ""
    width, pad_l, pad_r, pad_t, pad_b = 760, 30, 8, 12, 24
    maximum = max(1, max(max(point.get(k, 0) for k, _ in keys) for point in series))
    step = (width - pad_l - pad_r) / max(1, len(series) - 1)
    colors = ["#6ea8ff", "#3ddc97", "#ffbf5e", "#ff7a8a"]
    parts = []
    grid = []
    for fraction in (0.0, 0.5, 1.0):
        y = height - pad_b - fraction * (height - pad_t - pad_b)
        grid.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                    f'stroke="#22304e" stroke-width="1" stroke-dasharray="3 4"/>')
        grid.append(f'<text x="2" y="{y + 3:.1f}" fill="#7186a8" font-size="9">{int(maximum * fraction)}</text>')
    for index, (key, _label) in enumerate(keys):
        coords = []
        for i, point in enumerate(series):
            x = pad_l + i * step
            y = height - pad_b - (point.get(key, 0) / maximum) * (height - pad_t - pad_b)
            coords.append(f"{x:.1f},{y:.1f}")
        parts.append(f'<polyline fill="none" stroke="{colors[index % len(colors)]}" stroke-width="2" '
                     f'stroke-linejoin="round" stroke-linecap="round" points="{" ".join(coords)}"/>')
        last_x = pad_l + (len(series) - 1) * step
        last_y = height - pad_b - (series[-1].get(key, 0) / maximum) * (height - pad_t - pad_b)
        parts.append(f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3" fill="{colors[index % len(colors)]}"/>')
    labels = [series[0]["date"], series[len(series) // 2]["date"], series[-1]["date"]]
    for i, label in enumerate(labels):
        anchor = "start" if i == 0 else ("end" if i == 2 else "middle")
        x = pad_l if i == 0 else (width - pad_r if i == 2 else pad_l + (width - pad_l - pad_r) / 2)
        parts.append(f'<text x="{x:.0f}" y="{height - 5}" fill="#7186a8" font-size="9" '
                     f'text-anchor="{anchor}">{e(label[5:])}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" '
            f'aria-label="Динамика">{"".join(grid + parts)}</svg>')


def mode_label(mode: str) -> str:
    if not mode:
        return "—"
    return MODE_LABELS.get(mode.casefold(), mode)


def score_ring(score, low=None) -> str:
    value = max(0.0, min(100.0, float(score or 0)))
    color = "#0f7a5c" if value >= 80 else ("#3157d5" if value >= 70 else ("#9a6414" if value >= 55 else "#a33245"))
    title = f"Соответствие {value:.0f} из 100. Не вероятность приглашения."
    return (f'<div class="score" style="--pct:{value:.0f};--ring:{color}" title="{e(title)}" '
            f'aria-label="{e(title)}"><span>{value:.0f}</span><small>из 100</small></div>')


def metric(label: str, value, helper: str = "", kind: str = "") -> str:
    return (f'<article class="metric {kind}"><div class="label">{e(label)}</div>'
            f'<div class="value">{e(value)}</div>'
            f'<div class="helper">{e(helper)}</div></article>')


def page_top(eyebrow: str, title: str, subtitle: str = "", actions: str = "") -> str:
    return (f'<section class="top"><div><div class="eyebrow">{e(eyebrow)}</div>'
            f'<h1>{e(title)}</h1>' + (f'<p class="sub">{e(subtitle)}</p>' if subtitle else "")
            + f'</div><div class="actions">{actions}</div></section>')


def tabs(items: list[tuple[str, str, bool]]) -> str:
    links = "".join(
        f'<a href="{href}" class="{"active" if active else ""}" {"aria-current=page" if active else ""}>{e(label)}</a>'
        for href, label, active in items)
    return f'<nav class="tabs">{links}</nav>'


def profile_state(profile: dict, resume: dict) -> dict:
    skills = len(verified_skills(resume)) or len(profile.get("verified_skills") or [])
    projects = len([p for p in resume.get("projects") or [] if p.get("verified")])
    education = len([x for x in resume.get("education") or [] if x.get("verified")])
    contact = (resume.get("contact") or {})
    has_contact = bool(contact.get("email") or contact.get("phone"))
    steps = [
        (skills >= 3, f"Подтверждено навыков: {skills}", "Добавьте минимум 3 ключевых навыка"),
        (projects >= 1, f"Подтверждено проектов: {projects}", "Добавьте хотя бы один проект"),
        (education >= 1, f"Подтверждено образование: {education}", "Подтвердите образование"),
        (has_contact, "Контакты заполнены" if has_contact else "Контакты не заполнены", "Укажите email или телефон"),
    ]
    done = sum(1 for ok, _, _ in steps if ok)
    if skills < 3:
        hint = "Добавьте 3 ключевых навыка, чтобы поиск стал точнее."
    elif projects < 1:
        hint = "Добавьте подтверждённый проект — письма станут конкретнее."
    elif not has_contact:
        hint = "Укажите контакт для откликов."
    else:
        hint = "Профиль готов: проверьте лучшие вакансии и очередь."
    return {"skills": skills, "projects": projects, "education": education,
            "has_contact": has_contact, "steps": steps, "done": done,
            "total": len(steps), "pct": round(done / len(steps) * 100), "hint": hint}


def job_highlights(job, detail: dict | None = None) -> tuple[list[tuple[str, str]], list[str]]:
    detail = detail or {}
    skills = detail.get("skills") or {}
    matched = skills.get("matched") or []
    missing = skills.get("required_missing") or []
    reasons: list[tuple[str, str]] = []
    if matched:
        reasons.append(("Совпадает: " + ", ".join(matched[:3]), "good"))
    experience = detail.get("experience") or {}
    required = experience.get("required_years")
    if required is not None and not experience.get("hard_fail"):
        if float(required) <= 0:
            reasons.append(("Опыт не требуется", "good"))
        else:
            reasons.append((f"Опыт: требуется {float(required):g} г., у вас {experience.get('candidate_years', 0):g} г.", "info"))
    salary = fmt_money(job["salary_min"], job["salary_max"], job["currency"])
    if salary != "—":
        reasons.append((f"Зарплата: {salary}", "info"))
    mode = mode_label(job["work_mode"] or "")
    if mode != "—" or job["location"]:
        reasons.append(("Формат: " + " · ".join(x for x in (mode, job["location"] or "") if x and x != "—"), "info"))
    if not reasons:
        for item in (detail.get("explanation") or [])[:2]:
            reasons.append((f"{FACTOR_LABELS.get(item['factor'], item['factor'])}: {item['score']:.0%}", "info"))
    return reasons[:2], [str(x) for x in missing[:2]]


def job_card(job, detail: dict | None = None, *, active_page: str = "/") -> str:
    reasons, missing = job_highlights(job, detail)
    reason_html = "".join(f'<span class="chip {kind}">{e(text)}</span>' for text, kind in reasons)
    if missing:
        reason_html += f'<span class="chip warn">Не хватает: {e(", ".join(missing))}</span>'
    if (detail or {}).get("needs_review"):
        reason_html += '<span class="chip warn">Проверить опыт</span>'
    if ((detail or {}).get("experience") or {}).get("seniority_fail"):
        reason_html += '<span class="chip warn">Уровень senior/lead</span>'
    if (detail or {}).get("eligibility") == "rejected":
        failed = (detail or {}).get("failed_constraints") or []
        reason_html += f'<span class="chip bad">Не подходит: {e(", ".join(failed[:2]) or "ограничения")}</span>'
    description = clean_text(job["description"] or "")[:170]
    if len(job["description"] or "") > 170:
        description += "…"
    company = " · ".join(x for x in (job["company"], job["location"], mode_label(job["work_mode"] or "")) if x and x != "—")
    if job["eligible"]:
        action = (f'<form method="post" action="/actions/prepare/{e(job["id"])}">'
                  f'<input type="hidden" name="token" value="{e(token())}">'
                  f'<button class="tiny">Подготовить</button></form>')
    else:
        action = f'<a class="tiny btn" href="/job/{e(job["id"])}">Открыть</a>'
    try:
        is_fav = bool(job["favorite"])
    except (KeyError, IndexError):
        is_fav = False
    fav = (f'<form method="post" action="/actions/favorite/{e(job["id"])}">'
           f'<input type="hidden" name="token" value="{e(token())}">'
           f'<button class="tiny" title="Избранное">{"★" if is_fav else "☆"}</button></form>')
    return f"""<article class="job"><div style="min-width:0">
<div class="company">{e(company)}</div>
<h3><a href="/job/{e(job["id"])}">{e(job["title"])}</a></h3>
<div class="description">{e(description or "Описание не загружено")}</div>
<div class="reasons">{reason_html or '<span class="chip">Причины появятся после оценки</span>'}</div>
</div><div class="jobright">{score_ring(job["score"])}{fav}{action}</div></article>"""


def queue_policy(settings: dict) -> list[str]:
    queue = settings.get("queue") or {}
    notes = []
    if not queue.get("enabled"):
        notes.append("Очередь выключена")
    if not settings.get("send_applications"):
        notes.append("Автоотправка запрещена — доступна ручная выгрузка")
    if not ((queue.get("channels") or {}).get("hh_ui") or {}).get("enabled"):
        notes.append("Канал hh.ru выключен")
    if queue.get("require_manual_approval", True):
        notes.append("Каждый отклик требует одобрения")
    notes.append(f"Лимит: {queue.get('daily_limit', 8)} откликов / {queue.get('daily_minutes', 90)} мин в день")
    return notes


class CycleRunner:
    """One background cycle at a time; the UI stays responsive and honest."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.running = False
        self.started_at = 0.0
        self.report: dict | None = None
        self.error = ""
        self.cancel_requested = False
        self.stage = ""
        self.progress: dict = {}

    def start(self, settings: dict) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.started_at = time.time()
            self.report = None
            self.error = ""
            self.cancel_requested = False
            self.stage = "starting"
            self.progress = {}
            self._thread = threading.Thread(target=self._run, args=(settings,), daemon=True)
            self._thread.start()
            return True

    def cancel(self) -> None:
        self.cancel_requested = True

    def _run(self, settings: dict) -> None:
        def progress(step: str, data: dict) -> None:
            self.stage = step
            self.progress = data if isinstance(data, dict) else {"value": data}
        try:
            self.report = run_cycle(settings=settings, stop_check=lambda: self.cancel_requested,
                                    progress=progress)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            self.stage = "completed" if not self.error else "failed"
            with self._lock:
                self.running = False

    def status(self) -> dict:
        elapsed = round(time.time() - self.started_at) if self.started_at else 0
        return {"running": self.running, "elapsed": elapsed, "report": self.report,
                "error": self.error, "cancel_requested": self.cancel_requested,
                "stage": self.stage, "progress": self.progress}


RUNNER = CycleRunner()


class ParseRunner:
    """Background resume parsing so the onboarding page stays responsive."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.running = False
        self.kind = ""
        self.started_at = 0.0
        self.error = ""
        self.method = ""
        self.cancel_requested = False

    def start(self, kind: str, path: Path, settings: dict) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.kind = kind
            self.started_at = time.time()
            self.error = ""
            self.method = ""
            self.cancel_requested = False
            self._thread = threading.Thread(target=self._run, args=(kind, path, settings), daemon=True)
            self._thread.start()
            return True

    def cancel(self) -> None:
        self.cancel_requested = True

    def _run(self, kind: str, path: Path, settings: dict) -> None:
        try:
            from core.onboarding import extract_facts, extract_text, extract_with_aitunnel, save_draft
            text = extract_text(path)
            draft = None
            errors = []
            if kind == "qwen":
                try:
                    from core.qwen import QwenParser
                    draft = QwenParser(settings).parse_resume(text, source_name=path.name)
                except Exception as exc:
                    errors.append(f"Qwen: {exc}")
            if draft is None and settings.get("llm", {}).get("aitunnel"):
                try:
                    db = dbm.init_db()
                    draft = extract_with_aitunnel(db, text, settings, source_name=path.name)
                    db.close()
                except Exception as exc:
                    errors.append(f"AITUNNEL: {exc}")
            if draft is None:
                draft = extract_facts(text, source_name=path.name,
                                      method="local fallback" + (f" after {'; '.join(errors)}" if errors else ""))
            if self.cancel_requested:
                self.error = "Операция отменена пользователем; черновик не изменён."
                return
            text_path = path.with_suffix(".txt")
            text_path.write_text(text, encoding="utf-8")
            draft["source_text_path"] = str(text_path)
            draft["source_file"] = str(path)
            if errors:
                draft["last_error"] = "; ".join(errors)[:600]
            save_draft(draft)
            self.method = str(draft.get("method", ""))
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            with self._lock:
                self.running = False

    def status(self) -> dict:
        return {"running": self.running, "kind": self.kind,
                "elapsed": round(time.time() - self.started_at) if self.started_at else 0,
                "error": self.error, "method": self.method,
                "cancel_requested": self.cancel_requested}


PARSE_RUNNER = ParseRunner()


class Handler(BaseHTTPRequestHandler):
    server_version = "AIJobHunter/0.3"
    _lock = threading.Lock()

    def log_message(self, fmt, *args):
        return

    # ------------------------------------------------------------------ helpers
    def _flash_from_query(self, query: dict) -> tuple[str, str] | None:
        return (query.get("msg", [""])[0][:400], query.get("st", [""])[0])

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                         "img-src data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, title: str, content: str, **kwargs) -> None:
        self._send(200, layout(title, content, **kwargs), "text/html; charset=utf-8")

    def _redirect(self, path: str, msg: str = "", status: str = "ok") -> None:
        base, _, existing = path.partition("?")
        parts = [p for p in (existing, f"msg={quote(msg)}" if msg else "",
                             f"st={status}" if msg else "") if p]
        target = base + ("?" + "&".join(parts) if parts else "")
        self.send_response(303)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _check_origin(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            parts = urlsplit(origin)
            if parts.hostname not in ("127.0.0.1", "localhost", "::1"):
                return False
        return True

    def _parse_multipart(self, content_type: str) -> tuple[dict, list[tuple[str, str, bytes]]]:
        from email.parser import BytesParser
        from email.policy import default
        length = int(self.headers.get("Content-Length") or 0)
        if length > 25_000_000:
            raise ValueError("Слишком большой файл")
        raw = self.rfile.read(length) if length else b""
        header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        message = BytesParser(policy=default).parsebytes(header + raw)
        fields: dict = {}
        files: list[tuple[str, str, bytes]] = []
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                files.append((str(name or ""), str(filename), payload))
            elif name:
                fields[str(name)] = payload.decode("utf-8", "replace")
        return fields, files

    def _parse_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2_000_000:
            raise ValueError("Слишком большой запрос")
        raw = self.rfile.read(length) if length else b""
        if "application/json" in (self.headers.get("Content-Type") or ""):
            return json.loads(raw.decode("utf-8"))
        return {k: v[0] if len(v) == 1 else v for k, v in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}

    def serve_spa(self, path: str) -> bool:
        index = FRONTEND_DIST / "index.html"
        if not index.exists():
            return False
        target = index
        rel = path.lstrip("/")
        if rel:
            candidate = (FRONTEND_DIST / rel).resolve()
            try:
                candidate.relative_to(FRONTEND_DIST.resolve())
            except ValueError:
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return True
            if candidate.is_file():
                target = candidate
            elif path.startswith("/assets/"):
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return True
        content_type = {
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".woff2": "font/woff2",
        }.get(target.suffix.lower(), "text/html; charset=utf-8")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if target == index else "public, max-age=31536000, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                         "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
                         "base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)
        return True

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        if not self._check_origin():
            self._send(403, b"forbidden", "text/plain; charset=utf-8"); return
        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)
        if path.startswith("/api/"):
            try:
                status, payload, ctype = api_web.get(path, query)
                if isinstance(payload, bytes):
                    self._send(status, payload, ctype)
                else:
                    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    self._send(status, body, "application/json; charset=utf-8")
            except Exception as exc:
                traceback.print_exc()
                body = json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False).encode()
                self._send(500, body, "application/json; charset=utf-8")
            return
        if not LEGACY_UI and not path.startswith(("/actions/", "/export/")):
            if self.serve_spa(path):
                return
        try:
            if path.startswith("/job/"):
                return self.page_job(path.split("/job/", 1)[1], query)
            if path.startswith("/resume/variant/"):
                return self.page_resume_variant(path.rsplit("/", 1)[1])
            if path.startswith("/resume/job/"):
                return self.page_resume_for_job(path.rsplit("/", 1)[1])
            if path == "/":
                return self.page_home(query)
            if path == "/jobs":
                return self.page_jobs(query)
            if path == "/queue":
                return self.page_queue(query)
            if path == "/applications":
                return self.page_queue(query)
            if path == "/messages":
                return self.page_messages(query)
            if path == "/resume":
                return self.page_resume(query)
            if path == "/onboarding":
                return self.page_onboarding(query)
            if path == "/analytics":
                return self.page_analytics(query)
            if path == "/sources":
                return self.page_sources(query)
            if path == "/settings":
                return self.page_settings(query)
            if path == "/today":
                return self.page_home(query)
            if path.startswith("/export/"):
                return self.download(path.split("/export/", 1)[1])
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception:
            traceback.print_exc()
            self._send(500, b"internal error", "text/plain; charset=utf-8")

    # ------------------------------------------------------------------ pages
    def page_home(self, query: dict) -> None:
        db = dbm.init_db()
        settings = load_settings()
        data = dashboard_data(db)
        s = data["summary"]
        profile, resume = load_profile(), sync_from_profile(load_resume())
        state = profile_state(profile, resume)
        threshold = float(settings.get("threshold", 70))
        best = dbm.list_jobs(db, eligible_only=True, min_score=threshold, limit=3)
        fallback = not best
        if fallback:
            best = dbm.list_jobs(db, limit=3)
        ready = db.execute(
            """SELECT COUNT(*) FROM jobs WHERE eligible=1 AND letter<>'' AND duplicate_of IS NULL
               AND status IN ('new','draft','queued','approved')"""
        ).fetchone()[0]
        runner = RUNNER.status()
        llm = LLM(db, settings)
        llm_status = llm.status()
        last_update = db.execute("SELECT MAX(updated_at) FROM jobs").fetchone()[0]
        events = db.execute(
            """SELECT e.type, e.created_at, j.title FROM events e
               LEFT JOIN jobs j ON j.id=e.job_id
               WHERE e.type NOT IN ('job_imported','favorite_toggled')
               ORDER BY e.id DESC LIMIT 7"""
        ).fetchall()
        if not events:
            events = db.execute(
                """SELECT e.type, e.created_at, j.title FROM events e
                   LEFT JOIN jobs j ON j.id=e.job_id
                   ORDER BY e.id DESC LIMIT 5"""
            ).fetchall()
        skip_counts: dict[str, int] = {}
        for row in db.execute("SELECT reason FROM jobs WHERE eligible=0 AND duplicate_of IS NULL"):
            reason = row["reason"] or ""
            if "Исключённая" in reason:
                key = "исключённая категория"
            elif "жёсткий фильтр опыта" in reason:
                key = "недостаточный опыт"
            elif "senior/lead" in reason or "уровень" in reason:
                key = "уровень senior/lead"
            elif "нужна проверка" in reason:
                key = "неизвестные требования"
            elif "ниже порога" in reason:
                key = "ниже порога"
            else:
                key = "прочее"
            skip_counts[key] = skip_counts.get(key, 0) + 1
        invited = s.get("invited", 0)
        rejected = db.execute("SELECT COUNT(*) FROM jobs WHERE status='rejected'").fetchone()[0]
        ai_spend = llm_status.get("month_spend_rub", llm_status.get("month_spend_usd", 0))
        ai_currency = "₽" if "month_spend_rub" in llm_status else "$"
        if runner["running"]:
            primary = (f'<form method="post" action="/actions/cycle/stop">'
                       f'<input type="hidden" name="token" value="{e(token())}">'
                       f'<button class="btn secondary">Остановить поиск</button></form>')
        else:
            primary = (f'<form method="post" action="/actions/cycle">'
                       f'<input type="hidden" name="token" value="{e(token())}">'
                       f'<button class="btn">Найти вакансии</button></form>')
        actions = primary + '<a class="btn secondary" href="/jobs">Job Explorer</a>'
        metrics = "".join([
            metric("Подходящие", s["eligible"], "по профилю" if not fallback else "нужен профиль"),
            metric("Готовы к отклику", ready, "письма подготовлены" if ready else "подготовьте письма", "warn" if ready == 0 else ""),
            metric("Отправлено", s["sent"], f"ответов: {s['replied']}", "muted" if s["sent"] == 0 else ""),
            metric("Приглашения", invited, f"отказов: {rejected}", "good" if invited else "muted"),
        ])
        notices = []
        draft = onboarding_io.load_draft()
        if draft and draft.get("status") == "review":
            notices.append('<div class="notice warn"><b>Черновик профиля ждёт подтверждения.</b> '
                           f'Проверьте факты из {e(draft.get("source_name", "резюме"))} и подтвердите их. '
                           '<a href="/onboarding">Открыть проверку →</a></div>')
        if state["skills"] < 3 and not (draft and draft.get("status") == "review"):
            notices.append(
                f'<div class="notice warn"><b>Профиль не заполнен.</b> {e(state["hint"])} '
                f'<a href="/onboarding">Загрузить резюме и подтвердить профиль →</a></div>')
        if runner["running"]:
            notices.append('<div class="notice">Идёт поиск и оценка вакансий. Страница обновится автоматически.</div>')
        if runner["error"]:
            notices.append(f'<div class="notice bad">Цикл завершился ошибкой: {e(runner["error"])}</div>')
        if runner["report"] and runner["report"].get("ok") and not runner["running"]:
            rep = runner["report"].get("summary") or {}
            notices.append(f'<div class="notice good">Поиск завершён: вакансий {rep.get("jobs", 0)}, '
                           f'подходящих {rep.get("eligible", 0)}, писем {rep.get("drafted", 0)}.</div>')
        if fallback and s["jobs"] > 0:
            notices.append('<div class="notice">Пока ни одна вакансия не прошла порог. '
                           'Ниже — ближайшие варианты; уточните навыки или снизьте порог в настройках.</div>')
        jobs_html = "".join(job_card(row, dbm.job_detail(row)) for row in best)
        if not jobs_html:
            jobs_html = ('<div class="empty"><b>Пока нет вакансий</b>'
                         'Запустите поиск — источники уже настроены.</div>')
        steps = "".join(
            f'<li><b>{i + 1}</b><span>{"✓ " if ok else ""}{e(label if ok else todo)}</span></li>'
            for i, (ok, label, todo) in enumerate(state["steps"]))
        posterior = data.get("posterior") or {}
        posterior_html = ""
        if posterior.get("resolved_n", 0) >= 3:
            posterior_html = (
                f'<div class="mini"><b>Историческая доля приглашений: {posterior["mean"]:.0%}</b>'
                f'<small>80% интервал {posterior["interval_80"][0]:.0%}–{posterior["interval_80"][1]:.0%} · '
                f'N={posterior["resolved_n"]} · ожидают ответа {posterior["pending_n"]}</small></div>')
        side_hint = (f'Черновик из {draft.get("source_name", "резюме")} ждёт проверки: '
                     f'{len(draft.get("skills") or [])} навыков, {len(draft.get("projects") or [])} проектов.'
                     if draft and draft.get("status") == "review" else state["hint"])
        ai_state = "работает" if runner["running"] else ("ошибка" if runner["error"] else "готов")
        ai_line = (f'<div class="row" style="margin:0 0 14px">'
                   f'<span class="chip {"good" if ai_state == "готов" else "warn" if ai_state == "работает" else "bad"}">AI-агент: {e(ai_state)}</span>'
                   f'<span class="chip">Модель: {e(llm_status.get("model") or "локальные шаблоны")}</span>'
                   f'<span class="chip">AI: {ai_spend:.2f} {ai_currency} за месяц</span>'
                   f'<span class="chip">Обновлено: {e(fmt_dt(last_update))}</span>'
                   f'<span class="spacer"></span><a class="small muted" href="/settings?tab=advanced">Диагностика →</a></div>')
        funnel_html = "".join(
            f'<div style="margin:7px 0"><div class="row"><small>{e(row["label"])}</small>'
            f'<span class="spacer"></span><b>{row["count"]}</b></div>'
            f'<div class="bar"><i style="width:{max(2, row["share"] * 100):.1f}%"></i></div></div>'
            for row in data.get("funnel", []))
        events_html = "".join(
            f'<div class="mini"><b>{e(EVENT_LABELS.get(ev["type"], ev["type"]))}</b><small>{e(fmt_dt(ev["created_at"]))}'
            f'{" · " + e(ev["title"]) if ev["title"] else ""}</small></div>'
            for ev in events) or '<div class="small muted">Событий пока нет.</div>'
        skip_html = "".join(
            f'<div class="row" style="margin:5px 0"><span class="small">{e(k)}</span>'
            f'<span class="spacer"></span><b>{v}</b></div>'
            for k, v in sorted(skip_counts.items(), key=lambda kv: -kv[1]))
        side = f"""<aside class="panel"><h2>Что сделать сейчас</h2>
<div class="hint">{e(side_hint)}</div>
<div class="progress" aria-label="Заполнение профиля"><i style="width:{state['pct']}%"></i></div>
<div class="small muted" style="margin-top:6px">{state['done']} из {state['total']} шагов</div>
<ul class="steps">{steps}</ul>
<a class="btn secondary" style="width:100%;margin-top:14px" href="/resume">Открыть резюме</a>
<div class="divider"></div><div class="list">{posterior_html}
<div class="mini"><b>Очередь: {s['queue']['queued']} в очереди</b><small>одобрено {s['queue']['approved']} · отправлено сегодня {s['queue']['sent_today']}</small></div>
<div class="mini"><b>Причины пропуска</b>{skip_html or '<small>Нет отсеянных вакансий.</small>'}</div>
</div></aside>"""
        content = (page_top("Сегодня", "Подберём работу, которая вам подходит",
                            f"Последнее обновление: {fmt_dt(last_update)} · соответствие, а не вероятность приглашения",
                            actions)
                   + "".join(notices) + ai_line + f'<section class="metrics">{metrics}</section>'
                   + f'<section class="workspace"><article class="panel"><div class="panel-head"><div>'
                     f'<h2>{"Ближайшие варианты" if fallback else "Лучшие предложения"}</h2>'
                     f'<div class="sub">Навыки, карьера и полезность показаны отдельно в карточке</div></div>'
                     f'<span class="chip info">{len(best)} лучших</span></div>{jobs_html}'
                     f'<div class="divider"></div><div class="row"><span class="small muted">'
                     f'Подобрано по навыкам, условиям и свежести</span><span class="spacer"></span>'
                     f'<a class="small" href="/jobs">Все вакансии →</a></div></article>{side}</section>'
                   + f'<section class="grid g2" style="margin-top:14px"><article class="panel"><h2>Воронка откликов</h2>{funnel_html}</article>'
                     f'<article class="panel"><div class="panel-head"><h2>Последние события</h2>'
                     f'<a class="small" href="/queue">Очередь →</a></div><div class="list">{events_html}</div></article></section>')
        self._html("Сегодня", content, active="/", flash=self._flash_from_query(query),
                   refresh=6 if runner["running"] else 0)
        db.close()

    def page_jobs(self, query: dict) -> None:
        db = dbm.init_db()
        settings = load_settings()
        profile, resume = load_profile(), sync_from_profile(load_resume())
        state = profile_state(profile, resume)
        def val(name: str, default: str = "") -> str:
            return (query.get(name, [default])[0] or default).strip()
        search = val("q")
        status = val("status")
        source = val("source")
        role = val("role")
        skill = val("skill")
        mode = val("mode")
        sort = val("sort", "score")
        exclude_company = val("exclude_company")
        exclude_words = val("exclude_words")
        only = val("eligible") == "1"
        favorite_only = val("favorite") == "1"
        all_required = val("all_required") == "1"
        try:
            min_score = float(val("min_score", "0") or 0)
        except ValueError:
            min_score = 0.0
        try:
            max_experience = float(val("max_experience")) if val("max_experience") else None
        except ValueError:
            max_experience = None
        try:
            min_salary = int(val("min_salary")) if val("min_salary") else None
        except ValueError:
            min_salary = None
        page = max(0, int(val("page", "0") or 0))
        limit, offset = 50, page * 50
        rows = dbm.list_jobs(
            db, status=status or None, source=source or None, eligible_only=only,
            min_score=min_score or None, search=search, limit=limit, offset=offset, role=role,
            max_experience=max_experience, min_salary=min_salary, work_mode=mode,
            favorite_only=favorite_only, exclude_company=exclude_company,
            exclude_words=exclude_words, all_required=all_required, skill=skill, sort=sort)
        sources = [r["source"] for r in db.execute("SELECT DISTINCT source FROM jobs ORDER BY source")]
        source_options = "".join(
            f'<option value="{e(x)}" {"selected" if x == source else ""}>{e(x)}</option>' for x in sources)
        status_options = "".join(
            f'<option value="{k}" {"selected" if k == status else ""}>{e(v)}</option>'
            for k, v in STATUS_LABELS.items())
        mode_options = "".join(
            f'<option value="{k}" {"selected" if k == mode else ""}>{e(v)}</option>'
            for k, v in (("remote", "Удалённо"), ("hybrid", "Гибрид"), ("office", "Офис")))
        sort_options = "".join(
            f'<option value="{k}" {"selected" if k == sort else ""}>{e(v)}</option>'
            for k, v in (("score", "По соответствию"), ("date", "По дате"), ("salary", "По зарплате")))
        cards = "".join(job_card(row, dbm.job_detail(row)) for row in rows)
        if not cards:
            cards = ('<div class="empty"><b>Ничего не нашлось</b>'
                     'Измените фильтры или запустите новый поиск.</div>')
        total = db.execute("SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NULL").fetchone()[0]
        base_params = {
            "q": search, "status": status, "source": source, "role": role, "skill": skill, "mode": mode,
            "min_score": int(min_score) if min_score else "", "max_experience": max_experience or "",
            "min_salary": min_salary or "", "exclude_company": exclude_company,
            "exclude_words": exclude_words, "sort": sort, "eligible": "1" if only else "",
            "favorite": "1" if favorite_only else "", "all_required": "1" if all_required else "",
        }
        def page_url(number: int) -> str:
            params = {k: v for k, v in base_params.items() if v not in ("", None)}
            params["page"] = number
            return "/jobs?" + urlencode(params)
        pager = ""
        if page > 0 or len(rows) == limit:
            pager = ('<div class="row" style="margin-top:14px">'
                     + (f'<a class="btn secondary" href="{page_url(page - 1)}">← Назад</a>' if page > 0 else "")
                     + '<span class="spacer"></span>'
                     + f'<a class="btn secondary" href="{page_url(page + 1)}">Дальше →</a></div>')
        hint = "" if state["skills"] >= 3 else (
            f'<div class="notice warn"><b>Профиль не заполнен.</b> {e(state["hint"])} '
            f'<a href="/onboarding">Заполнить профиль →</a></div>')
        content = (page_top("Вакансии", "Job Explorer", f"{total} записей · фильтры по требованиям, навыкам и полезности",
                            '<a class="btn secondary" href="/">На главную</a>')
                   + hint
                   + f'<form method="get" action="/jobs">'
                     f'<div class="toolbar"><div class="grow search"><input type="search" name="q" placeholder="Поиск по названию, компании, описанию" value="{e(search)}"></div>'
                     f'<input type="text" name="role" placeholder="Должность" value="{e(role)}" style="max-width:180px">'
                     f'<input type="text" name="skill" placeholder="Навык" value="{e(skill)}" style="max-width:150px">'
                     f'<select name="source" style="max-width:170px"><option value="">Все источники</option>{source_options}</select>'
                     f'<select name="sort" style="max-width:180px">{sort_options}</select></div>'
                     f'<div class="toolbar"><select name="max_experience" style="max-width:180px"><option value="">Опыт: любой</option>'
                     + "".join(f'<option value="{x}" {"selected" if max_experience == x else ""}>Опыт ≤ {x} г.</option>' for x in (0, 1, 2, 3, 6))
                     + f'</select><select name="min_salary" style="max-width:180px"><option value="">Зарплата: любая</option>'
                     + "".join(f'<option value="{x}" {"selected" if min_salary == x else ""}>от {x//1000} тыс. ₽</option>' for x in (50000, 80000, 100000, 150000, 200000))
                     + f'</select><select name="mode" style="max-width:160px"><option value="">Формат: любой</option>{mode_options}</select>'
                     f'<select name="min_score" style="max-width:160px"><option value="">Соответствие: любое</option>'
                     + "".join(f'<option value="{x}" {"selected" if int(min_score) == x else ""}>≥ {x}</option>' for x in (50, 60, 65, 70, 80, 90))
                     + f'</select><select name="status" style="max-width:170px"><option value="">Все статусы</option>{status_options}</select></div>'
                     f'<div class="toolbar"><label class="check"><input type="checkbox" name="eligible" value="1" {"checked" if only else ""}> только прошедшие</label>'
                     f'<label class="check"><input type="checkbox" name="all_required" value="1" {"checked" if all_required else ""}> все обязательные навыки</label>'
                     f'<label class="check"><input type="checkbox" name="favorite" value="1" {"checked" if favorite_only else ""}> избранное</label>'
                     f'<input type="text" name="exclude_company" placeholder="Исключить компании" value="{e(exclude_company)}" style="max-width:220px">'
                     f'<input type="text" name="exclude_words" placeholder="Исключить слова через запятую" value="{e(exclude_words)}" style="max-width:260px">'
                     f'<button class="btn secondary">Применить</button><a class="btn ghost" href="/jobs">Сброс</a></div></form>'
                   + f'<section class="panel">{cards}{pager}</section>')
        self._html("Вакансии", content, active="/jobs", flash=self._flash_from_query(query))
        db.close()

    def page_job(self, ident: str, query: dict) -> None:
        db = dbm.init_db()
        settings = load_settings()
        job = dbm.get_job(db, ident)
        if not job:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        detail = dbm.job_detail(job)
        profile, resume = load_profile(), sync_from_profile(load_resume())
        variant = select_variant(resume, dict(job), detail)
        apps = db.execute("SELECT * FROM applications WHERE job_id=? ORDER BY id DESC", (ident,)).fetchall()
        messages = db.execute("SELECT * FROM messages WHERE job_id=? ORDER BY id DESC", (ident,)).fetchall()
        events = db.execute("SELECT * FROM events WHERE job_id=? ORDER BY id DESC LIMIT 20", (ident,)).fetchall()
        reasons, missing = job_highlights(job, detail)
        chips = "".join(f'<span class="chip {kind}">{e(text)}</span>' for text, kind in reasons)
        missing_html = "".join(f'<span class="chip warn">{e(x)}</span>' for x in missing) or '<span class="chip good">Критичных пробелов нет</span>'
        scores = detail.get("scores") or {}
        eligibility = detail.get("eligibility") or ("rejected" if detail.get("hard_fail") else "review" if detail.get("needs_review") else "eligible")
        eligibility_html = {
            "eligible": '<span class="chip good">Проходит обязательные требования</span>',
            "review": '<span class="chip warn">Нужна проверка требований</span>',
            "rejected": '<span class="chip bad">Не проходит обязательные требования</span>',
        }.get(eligibility, "")
        score_chips = (f'<span class="chip info">Навыки {scores.get("skill_match", 0):.0f}</span>'
                       f'<span class="chip">Карьера {scores.get("career_fit", 0):.0f}</span>'
                       f'<span class="chip">Полезность {scores.get("utility", 0):.0f}</span>'
                       f'<span class="chip">Уверенность {scores.get("confidence", 0):.0%}</span>')
        notices = []
        if detail.get("eligibility") == "rejected":
            failed = detail.get("failed_constraints") or []
            notices.append(f'<div class="notice bad"><b>Не проходит обязательные требования.</b> '
                           f'Причины: {e(", ".join(failed) or "ограничения вакансии")}. '
                           f'Навыки могут совпадать, но отклик не рекомендуется.</div>')
        if detail.get("needs_review"):
            notices.append('<div class="notice warn">Часть обязательных требований не указана — проверьте вакансию вручную.</div>')
        if detail.get("red_flags"):
            notices.append('<div class="notice warn">Флаги: ' + e(", ".join(detail["red_flags"])) + '</div>')
        tk = e(token())
        actions = []
        if not apps:
            variant_options = "".join(
                f'<option value="{e(k)}" {"selected" if k == variant else ""}>{e(v["label"])}</option>'
                for k, v in VARIANTS.items())
            actions.append(f'<form method="post" action="/actions/prepare/{e(ident)}" class="row">'
                           f'<input type="hidden" name="token" value="{tk}">'
                           f'<select name="resume_variant" style="max-width:190px">{variant_options}</select>'
                           f'<button class="btn">{"Подготовить отклик" if job["eligible"] else "Подготовить письмо"}</button></form>')
        else:
            actions.append(f'<span class="chip info">{e(QUEUE_LABELS.get(apps[0]["state"], apps[0]["state"]))}</span>')
        actions.append(f'<form method="post" action="/actions/letter/{e(ident)}"><input type="hidden" name="token" value="{tk}">'
                       f'<button class="btn secondary">Перегенерировать письмо</button></form>')
        if apps and apps[0]["state"] in ("queued", "failed"):
            actions.append(f'<form method="post" action="/actions/queue/approve/{apps[0]["id"]}"><input type="hidden" name="token" value="{tk}">'
                           f'<button class="btn secondary">Одобрить отклик</button></form>')
        if apps and apps[0]["state"] == "approved":
            actions.append(f'<form method="post" action="/actions/queue/send/{apps[0]["id"]}"><input type="hidden" name="token" value="{tk}">'
                           f'<button class="btn secondary">{"Выгрузить письмо" if apps[0]["channel"] == "manual_export" else "Отправить"}</button></form>')
        actions.append(f'<form method="post" action="/actions/resume/export"><input type="hidden" name="token" value="{tk}">'
                       f'<input type="hidden" name="variant" value="{e(variant)}"><input type="hidden" name="job" value="{e(ident)}">'
                       f'<button class="btn ghost">DOCX/PDF под вакансию</button></form>')
        actions.append(f'<a class="btn ghost" target="_blank" href="/resume/job/{e(ident)}">Предпросмотр резюме ↗</a>')
        msg_html = "".join(
            f'<div class="msg {e(m["category"])}"><div class="row"><b>{e(CATEGORY_LABELS.get(m["category"], m["category"]))}</b>'
            f'<span class="spacer"></span><small class="muted">{e(fmt_dt(m["received_at"]))}</small></div>'
            f'<div style="margin-top:6px">{e(m["body"])}</div>'
            + (f'<details><summary>Черновик ответа</summary><div class="letter">{e(m["draft_reply"])}</div></details>' if m["draft_reply"] else "")
            + f'<form method="post" action="/actions/message/{m["id"]}/reply"><input type="hidden" name="token" value="{tk}">'
              f'<button class="tiny">Подготовить ответ</button></form></div>'
            for m in messages)
        app_html = "".join(
            f'<tr><td>Отклик</td><td>{e(CHANNEL_LABELS.get(a["channel"], a["channel"]))}</td>'
            f'<td>{chip(QUEUE_LABELS.get(a["state"], a["state"]))}</td><td>{e(fmt_dt(a["sent_at"] or a["updated_at"]))}</td>'
            f'<td><small>{e(a["last_error"] or "")}</small></td></tr>' for a in apps
        ) or '<tr><td colspan="5">Отклик ещё не создан</td></tr>'
        factor_rows = "".join(
            f'<div style="margin:8px 0"><div class="row"><small>{e(FACTOR_LABELS.get(name, name))}</small>'
            f'<span class="spacer"></span><small>{data["value"]:.0%} · вес {data["weight"]:.0%}</small></div>'
            f'<div class="bar"><i style="width:{max(2, data["value"] * 100):.1f}%"></i></div></div>'
            for name, data in sorted((detail.get("factors") or {}).items(), key=lambda kv: -kv[1]["value"]))
        prob = calibrated_probability(job["score"] or 0,
                                      float(((detail.get("factors") or {}).get("skills") or {}).get("value", 0)),
                                      float(((detail.get("factors") or {}).get("role") or {}).get("value", 0)),
                                      float(((detail.get("factors") or {}).get("experience") or {}).get("value", 0)),
                                      bool(job["letter"]))
        content = (f'<div class="row" style="margin-bottom:10px"><a href="/jobs">← Вакансии</a><span class="spacer"></span>'
                   f'{status_chip(job["status"])}</div>'
                   + page_top(f'{job["company"]} · {job["location"] or "город не указан"}', job["title"],
                              f'{mode_label(job["work_mode"] or "")} · {fmt_money(job["salary_min"], job["salary_max"], job["currency"])}',
                              '<a class="btn secondary" target="_blank" rel="noopener noreferrer" '
                              f'href="{safe_url(job["url"])}">Открыть оригинал ↗</a>')
                   + "".join(notices)
                   + f'<section class="grid g2"><article class="panel"><div class="row">{score_ring(job["score"])}'
                     f'<div><div class="eyebrow">Соответствие</div><h2>{num(job["score"])} из 100</h2>'
                     f'<div class="small muted">Не вероятность приглашения. Диапазон чувствительности '
                     f'{num(job["score_low"])}–{num(job["score_high"])}.</div>'
                     f'<div class="row" style="margin-top:8px">{eligibility_html}{score_chips}</div></div></div>'
                     f'<div class="divider"></div><div class="list"><div><div class="small muted">Почему подходит</div>{chips or "<span class=\"muted\">—</span>"}</div>'
                     f'<div><div class="small muted">Главный пробел</div>{missing_html}</div></div>'
                     f'<div class="divider"></div><div class="row">{actions[0]}{actions[1]}</div></article>'
                     f'<article class="panel"><h2>Отклик</h2><p class="sub">Проверьте письмо и факты перед отправкой.</p>'
                     f'<div class="letter">{e(job["letter"] or "Письмо ещё не подготовлено.")}</div>'
                     f'<details><summary>Редактировать письмо</summary>'
                     f'<form method="post" action="/actions/letter/save/{e(ident)}"><input type="hidden" name="token" value="{tk}">'
                     f'<textarea name="text" style="min-height:180px">{e(job["letter"] or "")}</textarea>'
                     f'<button class="btn secondary" style="margin-top:8px">Сохранить письмо</button>'
                     f'<div class="small muted" style="margin-top:6px">После изменения письма одобрение очереди нужно повторить.</div></form></details>'
                     f'<div class="row" style="margin-top:12px">{"".join(actions[2:])}</div>'
                     f'<div class="small muted" style="margin-top:10px">Статус: '
                     f'{e(QUEUE_LABELS.get(apps[0]["state"], apps[0]["state"]) if apps else "отклик не создан")}. '
                     f'Автоотправка не выполняется без явного разрешения.</div></article></section>'
                   + f'<section class="grid g2" style="margin-top:14px"><article class="panel">'
                     f'<div class="panel-head"><h2>Описание вакансии</h2></div>'
                     f'<div style="white-space:pre-wrap;color:#506078;font-size:13.5px;max-height:420px;overflow:auto">'
                     f'{e(job["description"] or "Описание не загружено")}</div></article>'
                     f'<article class="panel"><h2>Переписка</h2>'
                     f'{msg_html or "<div class=\"empty\"><b>Сообщений нет</b>Ответы можно добавить на странице откликов.</div>"}</article></section>'
                   + f'<section class="panel" style="margin-top:14px"><h2>История отклика</h2>'
                     f'<div class="table-wrap"><table><thead><tr><th>Отклик</th><th>Канал</th><th>Состояние</th><th>Дата</th><th>Ошибка</th></tr></thead>'
                     f'<tbody>{app_html}</tbody></table></div></section>'
                   + f'<details><summary>Детали оценки и журнал</summary>'
                     f'<div class="small muted" style="margin-bottom:8px">Метод: {e(detail.get("math_method", "weighted multi-criteria"))}. '
                     f'Модель: {e(detail.get("model", "—"))}.'
                     + (f' Калиброванная оценка: {prob:.0%} (не гарантия).' if prob is not None else ' Калибровка пока недоступна.')
                     + f'</div>{factor_rows}'
                     f'<div class="small muted" style="margin-top:10px">Обоснование: {e(job["reason"])}</div>'
                     f'<div class="divider"></div>'
                     + "".join(f'<div class="small" style="margin:5px 0"><b>{e(x["type"])}</b> · {e(fmt_dt(x["created_at"]))}</div>' for x in events)
                     + '</details>')
        self._html(job["title"], content, active="/jobs", flash=self._flash_from_query(query))
        db.close()

    def page_queue(self, query: dict) -> None:
        db = dbm.init_db()
        settings = load_settings()
        tab = (query.get("tab", ["queue"])[0] or "queue").lower()
        stats = queue_stats(db)
        apps = list_applications(db, limit=200)
        messages = list_messages(db, limit=200)
        msg_stats = message_stats(db)
        tabs_html = tabs([
            ("/queue", f"Очередь ({len(apps)})", tab != "replies"),
            ("/queue?tab=replies", f"Ответы ({sum(msg_stats['counts'].values())})", tab == "replies"),
        ])
        policy = " · ".join(e(x) for x in queue_policy(settings))
        if tab == "replies":
            body = "".join(
                f'<article class="card" style="margin-bottom:10px"><div class="row">'
                f'{chip(CATEGORY_LABELS.get(m["category"], m["category"]), {"rejection": "bad", "invitation": "good", "test_task": "warn", "offer": "good"}.get(m["category"], ""))}'
                f'<span class="spacer"></span><small class="muted">{e(fmt_dt(m["received_at"] or m["created_at"]))}</small></div>'
                f'<div style="margin:8px 0"><b>{e(m["job_title"] or m["sender"] or "Работодатель")}</b></div>'
                f'<div>{e(m["body"])}</div>'
                + (f'<details><summary>Черновик ответа</summary><div class="letter">{e(m["draft_reply"])}</div></details>' if m["draft_reply"] else "")
                + f'<form method="post" action="/actions/message/{m["id"]}/reply" style="margin-top:8px">'
                  f'<input type="hidden" name="token" value="{e(token())}"><button class="tiny">Подготовить ответ</button></form>'
                  f'</article>' for m in messages) or '<div class="empty"><b>Ответов нет</b>Вставьте письмо работодателя или синхронизируйте статусы.</div>'
            content = (page_top("Мои отклики", "Ответы работодателей", "Классификация и черновики. Ничего не отправляется автоматически.", "")
                       + tabs_html + f'<div class="notice">{policy}</div>'
                       + f'<section class="grid g2"><article class="panel"><h2>Импорт ответа</h2>'
                         f'<form method="post" action="/actions/messages/import"><input type="hidden" name="token" value="{e(token())}">'
                         f'<div class="field"><label>Компания или HR</label><input type="text" name="sender" placeholder="Например, HR компании"></div>'
                         f'<div class="field" style="margin-top:10px"><label>Текст письма</label><textarea name="body" placeholder="Вставьте письмо работодателя"></textarea></div>'
                         f'<input type="hidden" name="job_id" value=""><button class="btn" style="margin-top:10px">Разобрать</button></form></article>'
                         f'<article class="panel"><h2>Синхронизация с hh.ru</h2><p class="sub">Чтение статусов через вашу сессию Chromium. Без кликов, меняющих состояние.</p>'
                         f'<form method="post" action="/actions/messages/sync" data-confirm="Открыть Chromium и прочитать статусы откликов?">'
                         f'<input type="hidden" name="token" value="{e(token())}"><button class="btn secondary">Проверить статусы</button></form>'
                         f'<div class="small muted" style="margin-top:10px">CAPTCHA или 403 остановят синхронизацию. '
                         f'LLM: {"подключена" if LLM(db, settings).available else "не настроена — черновики по шаблону"}.</div></article></section>'
                       + f'<section class="panel" style="margin-top:14px">{body}</section>')
            self._html("Ответы", content, active="/queue", flash=self._flash_from_query(query))
            db.close()
            return

        app_cards = "".join(
            f'<article class="job"><div style="min-width:0"><label class="check"><input type="checkbox" name="ids" value="{a["id"]}">'
            f'<span><b><a href="/job/{e(a["job_id"])}">{e(a["title"])}</a></b><br>'
            f'<small class="muted">{e(a["company"])} · {e(QUEUE_LABELS.get(a["state"], a["state"]))} · '
            f'канал {e(CHANNEL_LABELS.get(a["channel"], a["channel"]))}'
            f'{" · резюме " + e(a["resume_variant"]) if a["resume_variant"] else ""}</small></span></label>'
            + (f'<details style="margin-left:27px"><summary>Письмо</summary><div class="letter">{e(a["letter"])}</div></details>' if a["letter"] else "")
            + f'</div><div class="jobright">{score_ring(a["score"])}'
              f'<a class="tiny btn" href="/job/{e(a["job_id"])}">Открыть</a></div></article>'
            for a in apps) or ('<div class="empty"><b>Очередь пуста</b>Подготовьте письма для подходящих вакансий.</div>')
        content = (page_top("Мои отклики", "Очередь откликов",
                            f"Отправлено сегодня: {stats['sent_today']} из {settings['queue'].get('daily_limit', 8)} · "
                            f"в очереди {stats['queued']} · одобрено {stats['approved']}", "")
                   + tabs_html + f'<div class="notice">{policy}</div>'
                   + f'<form method="post" action="/actions/queue/bulk">'
                     f'<input type="hidden" name="token" value="{e(token())}">'
                     f'<section class="panel"><div class="panel-head"><div><h2>Подготовленные отклики</h2>'
                     f'<div class="sub">Выберите несколько и примените действие. Отправка не происходит без разрешения.</div></div>'
                     f'<div class="row"><select name="action" style="max-width:190px">'
                     f'<option value="approve">Одобрить</option><option value="reject">Отклонить</option>'
                     f'<option value="export">Выгрузить письма</option></select>'
                     f'<button class="btn secondary">Применить</button></div></div>{app_cards}'
                     f'<div class="divider"></div><div class="row"><a class="btn secondary" href="/export/queue.csv">Экспорт CSV</a>'
                     f'<a class="btn ghost" href="/export/applications.txt">Письма для ручной отправки</a>'
                     f'<span class="spacer"></span><button class="btn ghost" formaction="/actions/queue/build">Пересобрать очередь</button></div>'
                     f'</section></form>')
        self._html("Мои отклики", content, active="/queue", flash=self._flash_from_query(query))
        db.close()

    def page_messages(self, query: dict) -> None:
        merged = dict(query)
        merged["tab"] = ["replies"]
        self.page_queue(merged)

    def page_onboarding(self, query: dict) -> None:
        db = dbm.init_db()
        settings = load_settings()
        draft = onboarding_io.load_draft()
        parse_status = PARSE_RUNNER.status()
        router = AITunnelRouter(db, settings)
        ai_ready = router.configured
        files = []
        if onboarding_io.UPLOADS.exists():
            files = sorted([p for p in onboarding_io.UPLOADS.iterdir()
                            if p.suffix.lower() in (".pdf", ".docx", ".txt", ".md")],
                           key=lambda p: p.stat().st_mtime, reverse=True)
        file_options = "".join(f'<option value="{e(p.name)}">{e(p.name)}</option>' for p in files)
        head = page_top("Профиль", "Заполнение профиля",
                        "Загрузите резюме — я покажу факты и предположения отдельно. Ничего не сохраняется без вашего подтверждения.",
                        '<a class="btn secondary" href="/">На главную</a>')
        ai_note = (f'<span class="chip good">AITUNNEL подключён</span> разбор через {e(router.task_model("extract_requirements"))}'
                   if ai_ready else
                   '<span class="chip warn">AITUNNEL не подключён</span> локальный разбор; ключ задаётся в окружении')
        if not draft or draft.get("status") == "confirmed":
            if draft and draft.get("status") == "confirmed":
                confirmed = ('<div class="notice good"><b>Профиль подтверждён.</b> '
                             'Проверьте вакансии и запустите поиск.</div>')
                extra = (f'<div class="row"><a class="btn" href="/">Открыть «Сегодня»</a>'
                         f'<a class="btn secondary" href="/resume">Проверить резюме</a>'
                         f'<form method="post" action="/actions/onboarding/discard">'
                         f'<input type="hidden" name="token" value="{e(token())}">'
                         f'<button class="btn ghost">Загрузить другое резюме</button></form></div>')
            else:
                confirmed = ""
                extra = ""
            upload = f"""<section class="grid g2"><article class="panel">
<h2>Загрузить резюме</h2>
<p class="sub">PDF, DOCX, TXT или MD. Файл обрабатывается локально; в LLM уйдёт только текст резюме, без браузерной сессии.</p>
<form method="post" action="/actions/onboarding/upload" enctype="multipart/form-data">
<input type="hidden" name="token" value="{e(token())}">
<div class="field"><label for="cvfile">Файл резюме</label>
<input id="cvfile" type="file" name="file" accept=".pdf,.docx,.txt,.md" required></div>
<button class="btn" style="margin-top:12px">Загрузить и разобрать</button></form>
<div class="divider"></div><h2>Или выбрать уже загруженный</h2>
<form method="post" action="/actions/onboarding/extract">
<input type="hidden" name="token" value="{e(token())}">
<div class="field"><label for="existing">Файл в data/uploads</label>
<select id="existing" name="filename">{file_options or '<option value="">Файлов пока нет</option>'}</select></div>
<button class="btn secondary" style="margin-top:12px" {"disabled" if not files else ""}>Разобрать выбранный</button></form>
</article><article class="panel"><h2>Что будет дальше</h2>
<ul class="steps"><li><b>1</b><span>Разбор навыков, образования, опыта и проектов</span></li>
<li><b>2</b><span>Отдельно — предположения о направлениях и ограничениях</span></li>
<li><b>3</b><span>Вы отмечаете, что подтверждаете; только это попадёт в профиль</span></li>
<li><b>4</b><span>После подтверждения можно сразу запустить поиск</span></li></ul>
<div class="notice" style="margin-top:14px">{ai_note}</div>
<div class="small muted">Опыт не выдумывается: если в резюме нет опыта работы, раздел останется пустым.</div>
</article></section>{confirmed}{extra}"""
        else:
            def checkbox(items, field, label_key="name", evidence_key="evidence"):
                rows = []
                for index, item in enumerate(items or []):
                    if not isinstance(item, dict):
                        continue
                    title = item.get(label_key) or item.get("institution") or item.get("company") or "—"
                    detail = item.get(evidence_key) or item.get("description") or ""
                    checked = "checked" if item.get("basis") == "explicit" else ""
                    rows.append(
                        f'<label class="check"><input type="checkbox" name="{field}" value="{index}" {checked}>'
                        f'<span><b>{e(title)}</b>'
                        + (f'<br><small class="muted">{e(detail[:260])}</small>' if detail else "")
                        + '</span></label>')
                return "".join(rows) or '<div class="small muted">Нет данных в резюме.</div>'

            skills_html = checkbox(draft.get("skills"), "skills")
            projects_html = checkbox(draft.get("projects"), "projects")
            education_html = checkbox(draft.get("education"), "education",
                                      label_key="institution")
            experience_html = checkbox(draft.get("experience"), "experience", label_key="company")
            languages_html = checkbox(draft.get("languages"), "languages")
            suggestions = draft.get("suggestions") or {}
            directions = suggestions.get("directions") or []
            constraints = suggestions.get("constraints") or {}
            direction_html = "".join(
                f'<label class="check"><input type="checkbox" name="directions" value="{e(x)}" checked>'
                f'<span>{e(x)} <span class="chip warn">предположение</span></span></label>'
                for x in directions) or '<div class="small muted">Направления не определены.</div>'
            modes = constraints.get("work_modes") or []
            mode_html = "".join(
                f'<label class="check"><input type="checkbox" name="mode_{key}" value="1" '
                f'{"checked" if label in modes else ""}><span>{e(label)}</span></label>'
                for key, label in (("remote", "удалённо"), ("hybrid", "гибрид"), ("office", "офис")))
            method = draft.get("method", "local")
            method_label = ("AITUNNEL: " + method.split(":", 1)[1]) if str(method).startswith("aitunnel:") else (
                "Qwen: " + method.split(":", 1)[1] if str(method).startswith("qwen:") else "локальный разбор")
            status_html = ""
            if parse_status["running"]:
                status_html = (
                    f'<div class="notice" role="status"><b>Разбор выполняется: {e(parse_status["kind"])}</b> '
                    f'{parse_status["elapsed"]} с. Страница обновится автоматически.'
                    f'<form method="post" action="/actions/onboarding/cancel" style="margin-top:8px">'
                    f'<input type="hidden" name="token" value="{e(token())}">'
                    f'<button class="btn secondary">Отменить</button></form></div>')
            elif parse_status.get("error"):
                status_html = (f'<div class="notice bad"><b>Разбор завершился ошибкой.</b> '
                               f'{e(parse_status["error"])}. Черновик сохранён — можно повторить или подтвердить вручную.</div>')
            elif draft.get("last_error"):
                status_html = (f'<div class="notice warn">Предыдущая попытка: {e(draft["last_error"])}. '
                               f'Использован резервный разбор.</div>')
            body = f"""{status_html}<div class="notice warn"><b>Ничего ещё не сохранено.</b>
Проверьте факты и снимите галочки с того, что не подтверждаете. Предположения отмечены отдельно.</div>
<div class="row" style="margin-bottom:12px"><span class="chip info">Источник: {e(draft.get("source_name", ""))}</span>
<span class="chip">{e(method_label)}</span><span class="spacer"></span>
<form method="post" action="/actions/onboarding/qwen"><input type="hidden" name="token" value="{e(token())}">
<button class="btn ghost" {"disabled" if parse_status["running"] else ""}>Разобрать Qwen</button></form>
<form method="post" action="/actions/onboarding/reextract"><input type="hidden" name="token" value="{e(token())}">
<button class="btn ghost" {"disabled" if (not ai_ready or parse_status["running"]) else ""}>Разобрать AITUNNEL</button></form>
<form method="post" action="/actions/onboarding/discard"><input type="hidden" name="token" value="{e(token())}">
<button class="btn ghost">Удалить черновик</button></form></div>
<form method="post" action="/actions/onboarding/confirm">
<input type="hidden" name="token" value="{e(token())}">
<section class="grid g2"><article class="panel"><h2>Контакты и заголовок</h2>
<div class="field"><label>Имя и роль</label><input type="text" name="headline" value="{e(draft.get("headline", ""))}"></div>
<div class="grid g2" style="margin-top:10px">
<div class="field"><label>Email</label><input type="text" name="email" value="{e((draft.get("contact") or {}).get("email", ""))}"></div>
<div class="field"><label>Телефон</label><input type="text" name="phone" value="{e((draft.get("contact") or {}).get("phone", ""))}"></div></div>
<div class="field" style="margin-top:10px"><label>Город</label><input type="text" name="city" value="{e((draft.get("contact") or {}).get("city", ""))}"></div>
<div class="field" style="margin-top:10px"><label>О себе</label><textarea name="summary">{e(draft.get("summary", ""))}</textarea></div>
</article><article class="panel"><h2>Предположения</h2>
<p class="sub">Это не факты из резюме, а выведенные направления и ограничения. Подтвердите только то, что верно.</p>
<div class="small muted">Направления</div>{direction_html}
<div class="small muted" style="margin-top:12px">Формат работы</div>{mode_html}
<div class="grid g2" style="margin-top:10px"><div class="field"><label>Города</label>
<input type="text" name="locations" value="{e(", ".join(constraints.get("locations") or []))}"></div>
<div class="field"><label>Зарплата, ₽</label>
<input type="number" name="salary_target_rub" value="{e(constraints.get("salary_target_rub") or "")}"></div></div>
<div class="field" style="margin-top:10px"><label>Доступность</label>
<input type="text" name="availability" value="{e(constraints.get("availability") or "")}"></div>
</article></section>
<section class="grid g2" style="margin-top:14px"><article class="panel"><h2>Навыки</h2>{skills_html}</article>
<article class="panel"><h2>Проекты</h2>{projects_html}
<div class="divider"></div><h2>Образование</h2>{education_html}
<div class="divider"></div><h2>Опыт работы</h2>{experience_html}</article></section>
<details><summary>Языки и источник</summary><h2>Языки</h2>{languages_html}
<div class="small muted" style="margin-top:10px">Исходный текст: {e(draft.get("source_text_path", ""))}</div></details>
<div class="row" style="margin-top:18px">
<button class="btn" name="start" value="1" {"disabled" if parse_status["running"] else ""}>Подтвердить профиль и запустить поиск</button>
<button class="btn secondary" name="start" value="0" {"disabled" if parse_status["running"] else ""}>Подтвердить без поиска</button>
</div>
<div class="small muted" style="margin-top:8px">Выбрано будет сохранено как подтверждённые факты. Неподтверждённое не попадёт в письма и LLM.</div>
</form>"""
            upload = body
        content = head + upload
        self._html("Профиль", content, active="/", flash=self._flash_from_query(query),
                   refresh=3 if parse_status["running"] else 0)
        db.close()

    def page_resume(self, query: dict) -> None:
        db = dbm.init_db()
        profile, resume = load_profile(), sync_from_profile(load_resume())
        state = profile_state(profile, resume)
        variants = variant_summary(resume)
        skills = "".join(
            f'<label class="check"><input type="checkbox" name="skills" value="{i}" {"checked" if s.get("verified") else ""}>'
            f'<span><b>{e(s.get("name"))}</b> {chip("подтверждён", "good") if s.get("verified") else chip("не подтверждён", "warn")}'
            f'<br><small class="muted">{e(s.get("evidence") or "")}</small></span></label>'
            for i, s in enumerate(resume.get("skills") or []))
        projects = "".join(
            f'<div class="mini"><div class="row"><b>{e(p.get("name"))}</b><span class="spacer"></span>'
            f'{chip("подтверждён", "good") if p.get("verified") else chip("не подтверждён", "warn")}</div>'
            f'<div class="small muted">{e(", ".join(p.get("stack") or []))}</div>'
            f'<div style="margin:5px 0">{e(p.get("description") or "")}</div>'
            f'<form method="post" action="/actions/resume/confirm-project"><input type="hidden" name="token" value="{e(token())}">'
            f'<input type="hidden" name="index" value="{i}"><button class="tiny">'
            f'{"Снять подтверждение" if p.get("verified") else "Подтвердить факт"}</button></form></div>'
            for i, p in enumerate(resume.get("projects") or []))
        variant_cards = "".join(
            f'<article class="card"><div class="eyebrow">{e(v["label"])}</div>'
            f'<div class="value" style="font-size:20px;font-weight:740;margin:6px 0">{v["skills"]} навыков · {v["projects"]} проектов</div>'
            f'<div class="row"><form method="post" action="/actions/resume/export"><input type="hidden" name="token" value="{e(token())}">'
            f'<input type="hidden" name="variant" value="{e(name)}"><button class="tiny">DOCX + PDF</button></form>'
            f'<a class="tiny btn" href="/resume/variant/{e(name)}">Предпросмотр</a></div></article>'
            for name, v in variants.items())
        versions = list_resume_versions()
        versions_html = "".join(
            f'<div class="mini"><div class="row"><b>{e(v["created"])}</b><span class="spacer"></span>'
            f'<small class="muted">{v["size"]} байт</small></div>'
            f'<form method="post" action="/actions/resume/restore"><input type="hidden" name="token" value="{e(token())}">'
            f'<input type="hidden" name="name" value="{e(v["name"])}"><button class="tiny">Восстановить</button></form></div>'
            for v in versions) or '<div class="small muted">История появится после первого сохранения.</div>'
        content = (page_top("Резюме", "Резюме и факты",
                            "Только подтверждённые факты попадают в письма. Варианты меняют акценты, не опыт.",
                            '<a class="btn secondary" href="/onboarding">Заполнить по резюме</a>')
                   + f'<div class="notice {"good" if state["pct"] == 100 else "warn"}">'
                     f'<b>{e(state["hint"])}</b> Подтверждённых навыков: {state["skills"]}, проектов: {state["projects"]}. '
                     f'<a href="/settings?tab=advanced">Диагностика памяти →</a></div>'
                   + f'<section class="grid g3">{variant_cards}</section>'
                   + f'<section class="grid g2" style="margin-top:14px"><article class="panel"><h2>Навыки</h2>'
                     f'<form method="post" action="/actions/resume/confirm"><input type="hidden" name="token" value="{e(token())}">'
                     f'{skills or "<div class=\"empty\"><b>Навыков нет</b>Импортируйте резюме ниже.</div>"}'
                     f'<button class="btn" style="margin-top:10px">Сохранить подтверждения</button></form></article>'
                     f'<article class="panel"><h2>Проекты</h2>{projects or "<div class=\"empty\"><b>Проектов нет</b>Добавьте подтверждённый проект.</div>"}'
                     f'<div class="divider"></div><h2>Импорт резюме</h2>'
                     f'<form method="post" action="/actions/resume/import"><input type="hidden" name="token" value="{e(token())}">'
                     f'<div class="field"><label>Путь к .docx/.txt/.md (или вставьте текст)</label>'
                     f'<input type="text" name="path" placeholder="C:\\путь\\к\\резюме.docx"></div>'
                     f'<div class="field" style="margin-top:8px"><label>Текст резюме</label><textarea name="text"></textarea></div>'
                     f'<button class="btn secondary" style="margin-top:8px">Импортировать (факты не подтверждены)</button></form></article></section>'
                   + f'<details><summary>Сравнить варианты и полное редактирование</summary>'
                     f'<form method="post" action="/actions/resume/diff" class="row" style="margin:10px 0">'
                     f'<input type="hidden" name="token" value="{e(token())}">'
                     f'<select name="a" style="max-width:220px">' + "".join(f'<option value="{e(k)}">{e(v["label"])}</option>' for k, v in VARIANTS.items()) + '</select>'
                     f'<select name="b" style="max-width:220px">' + "".join(f'<option value="{e(k)}">{e(v["label"])}</option>' for k, v in VARIANTS.items()) + '</select>'
                     f'<button class="btn secondary">Покивань diff</button></form>'
                     f'<form method="post" action="/actions/resume/save"><input type="hidden" name="token" value="{e(token())}">'
                     f'<textarea name="json" style="min-height:260px;font-family:Consolas,monospace">{e(json.dumps(resume, ensure_ascii=False, indent=2))}</textarea>'
                     f'<button class="btn" style="margin-top:8px">Сохранить резюме</button></form></details>'
                   + f'<details><summary>История версий ({len(versions)})</summary><div class="list">{versions_html}</div></details>')
        self._html("Резюме", content, active="/resume", flash=self._flash_from_query(query))
        db.close()

    def page_resume_variant(self, name: str) -> None:
        resume = sync_from_profile(load_resume())
        if name not in VARIANTS:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        view = variant_view(resume, name)
        markup = resume_io.render_html(view, resume.get("contact") or {})
        self._send(200, markup.encode("utf-8"), "text/html; charset=utf-8")

    def page_resume_for_job(self, ident: str) -> None:
        db = dbm.init_db()
        job = dbm.get_job(db, ident)
        if not job:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        detail = dbm.job_detail(job)
        resume = sync_from_profile(load_resume())
        variant = select_variant(resume, dict(job), detail)
        view = variant_view(resume, variant, job=dict(job), details=detail)
        markup = resume_io.render_html(view, resume.get("contact") or {})
        self._send(200, markup.encode("utf-8"), "text/html; charset=utf-8")
        db.close()

    def page_settings(self, query: dict) -> None:
        db = dbm.init_db()
        settings = load_settings()
        tab = (query.get("tab", ["basic"])[0] or "basic").lower()
        if tab not in ("basic", "sources", "advanced"):
            tab = "basic"
        profile, resume = load_profile(), sync_from_profile(load_resume())
        state = profile_state(profile, resume)
        llm = LLM(db, settings)
        status = llm.status()
        router = AITunnelRouter(db, settings)
        catalog = router.model_info(router.task_model("cover_letter"))
        memory_stats = Memory(db, settings).stats()
        tabs_html = tabs([
            ("/settings", "Основное", tab == "basic"),
            ("/settings?tab=sources", "Источники", tab == "sources"),
            ("/settings?tab=advanced", "Дополнительно", tab == "advanced"),
        ])
        head = page_top("Настройки", "Настройки", "Безопасные значения по умолчанию и прозрачные расходы.", "")
        if tab == "basic":
            llm_state = ("AITUNNEL" if status.get("provider") == "aitunnel"
                         else ("DeepSeek direct" if status.get("available") else "не подключена"))
            models = status.get("tasks") or {
                task: router.task_model(task) for task in ("classify", "deep_analysis", "cover_letter", "critic")}
            body = (f'<section class="grid g2"><article class="panel"><h2>Модель</h2>'
                    f'<div class="kv"><div class="k">Провайдер</div><div>{e(llm_state)}</div>'
                    f'<div class="k">Модель качества</div><div>{e(models.get("cover_letter", status.get("model")) or "—")}</div>'
                    f'<div class="k">Дешёвые задачи</div><div>{e(models.get("classify", "—"))}</div>'
                    f'<div class="k">Критик</div><div>{e(models.get("critic", "—"))}</div>'
                    f'<div class="k">Расход за месяц</div><div>{e(status.get("month_spend_rub", status.get("month_spend_usd", 0)))} '
                    f'{"₽" if "month_spend_rub" in status else "$"} / {e(status.get("budget_rub", status.get("budget_usd", 0)))}</div>'
                    f'<div class="k">Каталог</div><div>{e(router.task_model("cover_letter"))}: '
                    f'{e(catalog.get("prompt_cost") if catalog else "—")} ₽ вход / '
                    f'{e(catalog.get("completion_cost") if catalog else "—")} ₽ выход за 1M</div></div>'
                    f'<div class="row" style="margin-top:12px"><form method="post" action="/actions/model/refresh">'
                    f'<input type="hidden" name="token" value="{e(token())}"><button class="btn secondary">Обновить каталог</button></form>'
                    f'<form method="post" action="/actions/calibrate"><input type="hidden" name="token" value="{e(token())}">'
                    f'<button class="btn ghost">Обучить калибровку</button></form></div>'
                    f'<div class="small muted" style="margin-top:10px">Ключ AITUNNEL задаётся только в окружении. '
                    f'Без ключа письма собираются локально.</div></article>'
                    f'<article class="panel"><h2>Память</h2><div class="kv">'
                    f'<div class="k">Документов</div><div>{memory_stats["documents"]}</div>'
                    f'<div class="k">Векторов</div><div>{memory_stats["vectors"]}</div>'
                    f'<div class="k">Модель</div><div>{e((memory_stats.get("embedding") or {}).get("model", memory_stats.get("embedding_model", "—")))}</div>'
                    f'<div class="k">FTS5</div><div>{"доступен" if memory_stats["fts"] else "нет"}</div></div>'
                    f'<form method="post" action="/actions/memory/rebuild" style="margin-top:12px">'
                    f'<input type="hidden" name="token" value="{e(token())}"><button class="btn secondary">Переиндексировать</button></form></article></section>'
                    f'<section class="grid g2" style="margin-top:14px"><article class="panel"><h2>Политика откликов</h2>'
                    f'<ul class="steps">' + "".join(f'<li><b>•</b><span>{e(x)}</span></li>' for x in queue_policy(settings)) + '</ul>'
                    f'<div class="small muted">Изменить лимиты и каналы можно в Advanced → JSON настроек.</div></article>'
                    f'<article class="panel"><h2>Профиль</h2><div class="progress"><i style="width:{state["pct"]}%"></i></div>'
                    f'<div class="small muted" style="margin-top:6px">{state["done"]} из {state["total"]} шагов · {e(state["hint"])}</div>'
                    f'<a class="btn secondary" style="margin-top:12px" href="/resume">Открыть резюме</a></article></section>')
        elif tab == "sources":
            rows = []
            for name, source in SOURCES.items():
                cfg = source.config(settings)
                state_row = dbm.source_state(db, name)
                rows.append(
                    f'<article class="card"><div class="row"><b>{e(source.label)}</b><span class="spacer"></span>'
                    f'{chip("включён", "good") if cfg.get("enabled") else chip("выключен")}</div>'
                    f'<div class="small muted" style="margin:6px 0">{e(source.description)}</div>'
                    f'<div class="row">'
                    + (chip(state_row["last_status"], {"ok": "good", "blocked": "bad", "error": "bad"}.get(state_row["last_status"], "")) if state_row else chip("не запускался"))
                    + (f'<small class="muted">{e(fmt_dt(state_row["last_run_at"]))} · записей {state_row["items"]}</small>' if state_row else "")
                    + '</div>'
                    + (f'<div class="small" style="margin-top:6px;color:#8b2839">{e(state_row["last_error"] or "")}</div>' if state_row and state_row["last_error"] else "")
                    + f'<form method="post" action="/actions/collect/{e(name)}" style="margin-top:10px">'
                      f'<input type="hidden" name="token" value="{e(token())}"><button class="tiny">Запустить</button></form></article>')
            body = (f'<div class="notice">Только разрешённые способы: официальные API, открытые данные и публичные фиды. '
                    f'403/429 останавливают источник; CAPTCHA и антидетект не используются.</div>'
                    f'<section class="grid g2">{"".join(rows)}</section>'
                    f'<section class="panel" style="margin-top:14px"><h2>Браузер и сессия</h2>'
                    f'<p class="sub">Отдельный профиль Chromium: data/browser_profile. Cookie и 2FA не покидают ПК.</p>'
                    f'<div class="row"><form method="post" action="/actions/browser/check"><input type="hidden" name="token" value="{e(token())}">'
                    f'<button class="btn secondary">Проверить сессию</button></form>'
                    f'<form method="post" action="/actions/browser/fingerprint"><input type="hidden" name="token" value="{e(token())}">'
                    f'<button class="btn ghost">Диагностика cookie</button></form></div>'
                    f'<div class="small muted" style="margin-top:10px">Вход: <code>py -3 jobhunter.py login</code>. '
                    f'Пароль программа не запрашивает и не хранит.</div></section>')
        else:
            data = dashboard_data(db)
            bands = "".join(
                f'<tr><td>{e(b["band"])}</td><td>{b["jobs"]}</td><td>{b["sent"]}</td><td>{b["replied"]}</td>'
                f'<td>{fmt_pct(b["reply_rate"])}</td></tr>' for b in data["score_bands"])
            sources_table = "".join(
                f'<tr><td>{e(s["source"])}</td><td>{s["jobs"]}</td><td>{s["eligible"]}</td><td>{s["sent"]}</td>'
                f'<td>{s["replied"]}</td><td>{fmt_pct(s["reply_rate"])}</td></tr>' for s in data["by_source"])
            skills_table = "".join(
                f'<tr><td>{e(g["skill"])}</td><td>{g["demand"]}</td><td>{g["missing"]}</td><td>{fmt_pct(g["coverage"])}</td></tr>'
                for g in data["skills"][:12])
            calib = data["calibration"]
            calib_text = (f'Обучена: N={calib.get("samples")}, test N={calib.get("test_n")}, '
                          f'Brier={calib.get("brier")}, AUC={calib.get("auc")}, ECE={calib.get("ece")}'
                          if calib.get("trained") else f'Не обучена: {calib.get("reason", "")}')
            posterior = data.get("posterior") or {}
            ledger = router.ledger(limit=8)
            recent = "".join(
                f'<tr><td>{e(fmt_dt(x["created_at"]))}</td><td>{e(x["task"])}</td><td>{e(x["model"])}</td>'
                f'<td>{e(x["input_tokens"])}/{e(x["output_tokens"])}</td><td>{x["cost_rub"]:.4f} ₽</td>'
                f'<td>{"оценка" if x["estimated"] else "точно"}</td></tr>' for x in ledger["recent"]) or '<tr><td colspan="6">Запросов не было</td></tr>'
            log_tail = ""
            try:
                log_path = ROOT / "logs" / "cycle.log"
                if log_path.exists():
                    log_tail = redact("\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]))
            except OSError:
                pass
            eligible_count = int(data["summary"]["eligible"])
            cost_note = (f'Расход: {ledger["spent_rub"]:.2f} ₽ из {ledger["budget_rub"]:.2f} ₽. '
                         + (f'На прошедшую вакансию: {ledger["spent_rub"] / eligible_count:.2f} ₽. '
                            if eligible_count else 'Пока нет вакансий, прошедших порог. ')
                         + 'Оценки стоимости помечены отдельно.')
            body = (f'<div class="grid g2"><article class="panel"><h2>Воронка</h2>'
                    + "".join(f'<div class="row" style="margin:7px 0"><span>{e(r["label"])}</span><span class="spacer"></span>'
                              f'<b>{r["count"]}</b></div><div class="bar"><i style="width:{max(2, r["share"] * 100):.1f}%"></i></div>'
                              for r in data["funnel"])
                    + f'<div class="divider"></div><div class="small">'
                      + (f'Историческая доля приглашений: {posterior.get("mean", 0):.0%} '
                         f'(N={posterior.get("resolved_n", 0)}, ожидают {posterior.get("pending_n", 0)}).'
                         if posterior.get("resolved_n", 0) > 0 else
                         'Историческая доля приглашений: пока нет подтверждённых исходов.')
                      + '</div></article>'
                    f'<article class="panel"><h2>Калибровка</h2><div class="small">{e(calib_text)}</div>'
                    f'<div class="divider"></div><h2>Диагностика</h2><div class="kv">'
                    f'<div class="k">База</div><div>data/jobs.sqlite3</div><div class="k">Документов памяти</div><div>{memory_stats["documents"]}</div>'
                    f'<div class="k">Векторов</div><div>{memory_stats["vectors"]}</div><div class="k">Каталог</div><div>{e(ledger["models_in_catalog"])} моделей</div></div>'
                    f'<div class="small muted" style="margin-top:8px">{e(cost_note)}</div></article></div>'
                    f'<section class="grid g2" style="margin-top:14px"><article class="panel"><h2>Диапазоны Match и ответы</h2>'
                    f'<div class="table-wrap"><table><thead><tr><th>Диапазон</th><th>Вакансий</th><th>Отправлено</th><th>Ответы</th><th>Доля</th></tr></thead><tbody>{bands}</tbody></table></div></article>'
                    f'<article class="panel"><h2>Источники</h2><div class="table-wrap"><table><thead><tr><th>Источник</th><th>Найдено</th><th>Прошли</th><th>Отправлено</th><th>Ответы</th><th>Доля</th></tr></thead><tbody>{sources_table}</tbody></table></div></article></section>'
                    f'<section class="grid g2" style="margin-top:14px"><article class="panel"><h2>Спрос на навыки</h2>'
                    f'<div class="table-wrap"><table><thead><tr><th>Навык</th><th>Встречается</th><th>Не подтверждён</th><th>Покрытие</th></tr></thead><tbody>{skills_table}</tbody></table></div></article>'
                    f'<article class="panel"><h2>Расходы модели</h2><div class="table-wrap"><table><thead><tr><th>Когда</th><th>Задача</th><th>Модель</th><th>Токены</th><th>₽</th><th>Тип</th></tr></thead><tbody>{recent}</tbody></table></div>'
                    f'<div class="small muted" style="margin-top:8px">Кэш и точные cost_rub учитываются; оценочные значения помечены.</div></article></section>'
                    f'<details><summary>JSON настроек</summary>'
                    f'<form method="post" action="/actions/settings"><input type="hidden" name="token" value="{e(token())}">'
                    f'<textarea name="json" style="min-height:360px;font-family:Consolas,monospace">{e(json.dumps(settings, ensure_ascii=False, indent=2))}</textarea>'
                    f'<button class="btn" style="margin-top:8px">Сохранить настройки</button></form></details>'
                    f'<details><summary>Журнал последнего цикла</summary><pre style="white-space:pre-wrap">{e(log_tail or "Журнал пуст")}</pre></details>'
                    f'<div class="row" style="margin-top:14px"><a class="btn secondary" href="/export/analytics.json">Скачать аналитику JSON</a>'
                    f'<a class="btn secondary" href="/export/jobs.csv">Скачать вакансии CSV</a></div>')
        content = head + tabs_html + body
        self._html("Настройки", content, active="/settings", flash=self._flash_from_query(query))
        db.close()

    def page_analytics(self, query: dict) -> None:
        self.page_settings({"tab": ["advanced"]})

    def page_sources(self, query: dict) -> None:
        self.page_settings({"tab": ["sources"]})


    # ------------------------------------------------------------------ downloads
    def download(self, what: str) -> None:
        db = dbm.init_db()
        if what == "analytics.json":
            path = export_all(db, "json")
        elif what == "jobs.csv":
            path = export_all(db, "csv")
        elif what == "queue.csv":
            path = export_queue_csv(db)
        elif what == "applications.txt":
            rows = db.execute(
                """SELECT a.id, j.title, j.company, j.url, j.letter FROM applications a
                   JOIN jobs j ON j.id=a.job_id WHERE a.state IN ('queued','approved','exported')
                   ORDER BY j.score DESC""").fetchall()
            lines = []
            for row in rows:
                lines.append(f"#{row['id']} {row['title']} — {row['company']}\n{row['url']}\n\n{row['letter']}\n\n{'=' * 70}\n")
            path = EXPORTS / f"applications-{date.today().isoformat()}.txt"
            path.write_text("\n".join(lines), encoding="utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8"); return
        body = Path(path).read_bytes()
        ctype = "application/json; charset=utf-8" if path.suffix == ".json" else (
            "text/csv; charset=utf-8" if path.suffix == ".csv" else "text/plain; charset=utf-8")
        self._send(200, body, ctype, {"Content-Disposition": f'attachment; filename="{Path(path).name}"'})
        db.close()

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        if not self._check_origin():
            self._send(403, b"forbidden", "text/plain; charset=utf-8"); return
        path = urlsplit(self.path).path
        content_type = self.headers.get("Content-Type") or ""
        if path.startswith("/api/"):
            try:
                if content_type.startswith("multipart/form-data"):
                    form, files = self._parse_multipart(content_type)
                else:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > 5_000_000:
                        raise ValueError("Слишком большой запрос")
                    raw = self.rfile.read(length) if length else b"{}"
                    form = json.loads(raw.decode("utf-8") or "{}")
                    files = []
            except Exception as exc:
                body = json.dumps({"error": f"bad request: {exc}"}, ensure_ascii=False).encode()
                self._send(400, body, "application/json; charset=utf-8"); return
            supplied = self.headers.get("X-JobHunter-Token") or str(form.get("token") or "")
            if not secrets.compare_digest(supplied, token()):
                body = json.dumps({"error": "invalid token"}, ensure_ascii=False).encode()
                self._send(403, body, "application/json; charset=utf-8"); return
            db = dbm.init_db()
            try:
                payload, status = api_web.action(db, path, form, files)
            except Exception as exc:
                traceback.print_exc()
                status, payload = 500, {"error": f"{type(exc).__name__}: {exc}"}
            finally:
                db.close()
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")
            return
        try:
            if content_type.startswith("multipart/form-data"):
                form, files = self._parse_multipart(content_type)
                form["_files"] = files
            else:
                form = self._parse_body()
        except Exception as exc:
            self._send(400, f"bad request: {exc}".encode(), "text/plain; charset=utf-8"); return
        if not secrets.compare_digest(str(form.get("token") or ""), token()):
            self._send(403, b"invalid token", "text/plain; charset=utf-8"); return
        try:
            return self._post_action(path, form)
        except Exception as exc:
            traceback.print_exc()
            self._redirect("/", f"Ошибка: {type(exc).__name__}: {str(exc)[:300]}", "err")

    def _post_action(self, path: str, form: dict) -> None:
        back = "/"
        db = dbm.init_db()
        settings = load_settings()
        profile, resume = load_profile(), sync_from_profile(load_resume())

        if path == "/actions/onboarding/upload":
            files = form.get("_files") or []
            if not files:
                return self._redirect("/onboarding", "Файл не получен", "err")
            _, filename, data = files[0]
            suffix = Path(filename).suffix.lower()
            if suffix not in (".pdf", ".docx", ".txt", ".md"):
                return self._redirect("/onboarding", "Поддерживаются PDF, DOCX, TXT и MD", "err")
            if len(data) > 20_000_000:
                return self._redirect("/onboarding", "Файл больше 20 МБ", "err")
            onboarding_io.UPLOADS.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(filename).stem)[:60] or "resume"
            target = onboarding_io.UPLOADS / f"{datetime.now():%Y%m%d-%H%M%S}-{safe}{suffix}"
            target.write_bytes(data)
            try:
                draft = onboarding_io.extract_file(target, db=db, settings=settings)
            except Exception as exc:
                return self._redirect("/onboarding", f"Не удалось разобрать файл: {exc}", "err")
            return self._redirect("/onboarding", f"Резюме разобрано ({draft.get('method')}). Проверьте факты.")
        if path == "/actions/onboarding/extract":
            filename = Path(str(form.get("filename") or "")).name
            target = (onboarding_io.UPLOADS / filename)
            if not filename or not target.exists():
                return self._redirect("/onboarding", "Файл не найден", "err")
            try:
                draft = onboarding_io.extract_file(target, db=db, settings=settings)
            except Exception as exc:
                return self._redirect("/onboarding", f"Не удалось разобрать файл: {exc}", "err")
            return self._redirect("/onboarding", f"Резюме разобрано ({draft.get('method')}). Проверьте факты.")
        if path == "/actions/onboarding/qwen":
            draft = onboarding_io.load_draft()
            source = Path(str((draft or {}).get("source_text_path") or (draft or {}).get("source_file") or ""))
            if not source.exists():
                return self._redirect("/onboarding", "Исходный файл не найден; загрузите резюме заново", "err")
            started = PARSE_RUNNER.start("qwen", source, settings)
            return self._redirect("/onboarding", "Qwen разбирает резюме…" if started else "Разбор уже выполняется",
                                  "ok" if started else "warn")
        if path == "/actions/onboarding/cancel":
            PARSE_RUNNER.cancel()
            return self._redirect("/onboarding", "Отмена запрошена; текущий вызов завершится, черновик не изменится.", "warn")
        if path == "/actions/onboarding/reextract":
            draft = onboarding_io.load_draft()
            source = Path(str((draft or {}).get("source_text_path") or (draft or {}).get("source_file") or ""))
            if not source.exists():
                return self._redirect("/onboarding", "Исходный файл не найден; загрузите резюме заново", "err")
            router = AITunnelRouter(db, settings)
            if not router.configured:
                return self._redirect("/onboarding", "AITUNNEL не настроен: задайте AITUNNEL_API_KEY", "err")
            started = PARSE_RUNNER.start("aitunnel", source, settings)
            return self._redirect("/onboarding", "AITUNNEL разбирает резюме…" if started else "Разбор уже выполняется",
                                  "ok" if started else "warn")
        if path == "/actions/onboarding/confirm":
            draft = onboarding_io.load_draft()
            if not draft:
                return self._redirect("/onboarding", "Нет черновика", "err")
            try:
                onboarding_io.apply_confirmation(draft, form)
            except Exception as exc:
                return self._redirect("/onboarding", f"Не удалось сохранить профиль: {exc}", "err")
            if str(form.get("start")) == "1":
                started = RUNNER.start(settings)
                return self._redirect("/", "Профиль подтверждён. Поиск запущен — результаты появятся здесь."
                                      if started else "Профиль подтверждён. Поиск уже выполняется.")
            return self._redirect("/onboarding", "Профиль подтверждён. Можно запускать поиск.")
        if path == "/actions/onboarding/discard":
            onboarding_io.discard()
            return self._redirect("/onboarding", "Черновик удалён")
        if path == "/actions/cycle":
            started = RUNNER.start(settings)
            return self._redirect("/", "Поиск запущен. Страница обновится сама." if started
                                  else "Поиск уже выполняется.", "ok" if started else "warn")
        if path == "/actions/cycle/stop":
            RUNNER.cancel()
            return self._redirect("/", "Остановка запрошена: текущий шаг завершится, новые не начнутся.", "warn")
        if path.startswith("/actions/favorite/"):
            ident = path.rsplit("/", 1)[1]
            job = dbm.get_job(db, ident)
            if job:
                dbm.set_job_fields(db, ident, favorite=0 if job["favorite"] else 1)
                dbm.log_event(db, ident, "favorite_toggled", payload={"favorite": not bool(job["favorite"])}, actor="user")
            return self._redirect("/jobs", "Избранное обновлено")
        if path.startswith("/actions/prepare/"):
            ident = path.rsplit("/", 1)[1]
            job = dbm.get_job(db, ident)
            if not job:
                return self._redirect("/jobs", "Вакансия не найдена", "err")
            memory = Memory(db, settings)
            if not job["letter"]:
                generate_letter(db, settings, profile, resume, job, force=False, memory=memory)
                job = dbm.get_job(db, ident)
            variant_value = str(form.get("resume_variant") or select_variant(resume, dict(job), dbm.job_detail(job)))
            if job and job["letter"]:
                if job["eligible"]:
                    existing = db.execute("SELECT id FROM applications WHERE job_id=?", (ident,)).fetchone()
                    if not existing:
                        db.execute("""INSERT OR IGNORE INTO applications(job_id, channel, state, letter_hash,
                                      resume_variant, created_at, updated_at) VALUES (?,?,?,?,?,?,?)""",
                                   (ident, "manual_export", "queued", job["letter_hash"], variant_value,
                                    dbm.now(), dbm.now()))
                        dbm.set_job_fields(db, ident, status="queued")
                        dbm.log_event(db, ident, "queued", payload={"channel": "manual_export", "actor": "ui"})
                        memory.application_event(ident, "queued", payload={"channel": "manual_export"})
                        db.commit()
                        return self._redirect(f"/job/{ident}", "Письмо готово, отклик добавлен в очередь.")
                return self._redirect(f"/job/{ident}", "Письмо готово. Проверьте его перед отправкой.")
            return self._redirect(f"/job/{ident}", "Не удалось подготовить письмо.", "err")
        if path == "/actions/queue/bulk":
            raw_ids = form.get("ids") or []
            if isinstance(raw_ids, str):
                raw_ids = [raw_ids]
            ids = [int(x) for x in raw_ids if str(x).isdigit()]
            action = str(form.get("action") or "")
            done = 0
            for app_id in ids:
                if action == "approve":
                    done += int(approve(db, app_id).get("ok", False))
                elif action == "reject":
                    done += int(cancel(db, app_id).get("ok", False))
                elif action == "export":
                    done += int(send_application(db, settings, app_id, confirm=True).get("ok", False))
            if action == "approve-all":
                for row in db.execute("SELECT id FROM applications WHERE state IN ('queued','failed')").fetchall():
                    done += int(approve(db, row["id"]).get("ok", False))
            label = {"approve": "одобрено", "reject": "отклонено", "export": "выгружено",
                     "approve-all": "одобрено"}.get(action, "обработано")
            return self._redirect("/queue", f"{label}: {done}")
        if path.startswith("/actions/collect"):
            only = [path.rsplit("/", 1)[1]] if path.count("/") > 2 and path.rsplit("/", 1)[1] else None
            from core.sources import collect_all
            report = collect_all(db, settings, force=True, only=only)
            from core.scoring import rank_pending
            rank_pending(db, settings, profile, resume)
            summary_text = ", ".join(
                f"{k}: {v.get('status')} +{v.get('added', 0)}"
                for k, v in report.items() if not k.startswith("_"))
            return self._redirect("/sources", f"Сбор завершён — {summary_text}")
        if path.startswith("/actions/letter/save/"):
            ident = path.rsplit("/", 1)[1]
            job = dbm.get_job(db, ident)
            text = clean_text(str(form.get("text") or ""))
            if not job or not text:
                return self._redirect(f"/job/{ident}", "Пустое письмо", "err")
            digest = __import__("hashlib").sha256(text.encode("utf-8")).hexdigest()[:20]
            dbm.set_job_fields(db, ident, letter=text, letter_hash=digest, letter_source="manual",
                               status="draft" if job["status"] == "new" else job["status"])
            dbm.log_event(db, ident, "letter_edited", payload={"chars": len(text)}, actor="user")
            return self._redirect(f"/job/{ident}", "Письмо сохранено. Проверьте и одобрите отклик заново.")
        if path.startswith("/actions/letter/"):
            ident = path.rsplit("/", 1)[1]
            job = dbm.get_job(db, ident)
            if not job:
                return self._redirect("/jobs", "Вакансия не найдена", "err")
            memory = Memory(db, settings)
            result = generate_letter(db, settings, profile, resume, job, force=True, memory=memory)
            return self._redirect(f"/job/{ident}", f"Письмо: {result['source']}. {result['checks'].get('fallback_reason', '')}")
        if path == "/actions/queue/build" or path.startswith("/actions/queue/add/"):
            if path.startswith("/actions/queue/add/"):
                ident = path.rsplit("/", 1)[1]
                job = dbm.get_job(db, ident)
                if job and job["letter"]:
                    db.execute("""INSERT OR IGNORE INTO applications(job_id, channel, state, letter_hash, created_at, updated_at)
                                  VALUES (?,?,?,?,?,?)""",
                               (ident, "manual_export", "queued", job["letter_hash"], dbm.now(), dbm.now()))
                    dbm.set_job_fields(db, ident, status="queued")
                    dbm.log_event(db, ident, "queued", payload={"channel": "manual_export"})
                    db.commit()
                return self._redirect(f"/job/{ident}", "Добавлено в очередь")
            result = build_queue(db, settings)
            return self._redirect("/queue", f"Добавлено в очередь: {result.get('queued', 0)} "
                                            f"(бюджет {result.get('minutes_used', 0)}/{result.get('budget_minutes', 0)} мин)")
        if path.startswith("/actions/queue/approve/"):
            app_id = int(path.rsplit("/", 1)[1])
            result = approve(db, app_id)
            return self._redirect("/queue", result.get("reason") or "Одобрено", "ok" if result.get("ok") else "err")
        if path.startswith("/actions/queue/cancel/"):
            app_id = int(path.rsplit("/", 1)[1])
            cancel(db, app_id)
            return self._redirect("/queue", "Отменено")
        if path.startswith("/actions/queue/send/"):
            app_id = int(path.rsplit("/", 1)[1])
            result = send_application(db, settings, app_id, confirm=form.get("confirm") == "1")
            return self._redirect("/queue", f"{result.get('status')}: {result.get('reason', '')}",
                                  "ok" if result.get("ok") else "err")
        if path == "/actions/queue/send-all":
            from core.queue import send_batch
            result = send_batch(db, settings, confirm=form.get("confirm") == "1")
            sent = sum(1 for r in result["results"] if r.get("status") in ("submitted", "exported"))
            blocked = next((r for r in result["results"] if r.get("status") in ("blocked", "auth_required")), None)
            message = f"Обработано: {result['attempted']}, успешно: {sent}"
            if blocked:
                message += f". Остановка: {blocked.get('reason')}"
            return self._redirect("/queue", message, "warn" if blocked else "ok")
        if path.startswith("/actions/message/") and path.endswith("/reply"):
            mid = int(path.split("/")[3])
            result = draft_reply(db, settings, mid, resume, profile)
            return self._redirect("/queue?tab=replies", f"Черновик: {result.get('source')}. {result.get('reason','')}",
                                  "ok" if result.get("ok") else "err")
        if path == "/actions/messages/import":
            body = clean_text(str(form.get("body") or ""))
            if not body:
                return self._redirect("/queue?tab=replies", "Пустой текст", "err")
            job_id = str(form.get("job_id") or "").strip() or None
            result = save_incoming(db, job_id, body, sender=str(form.get("sender") or ""))
            try:
                Memory(db, settings).add_feedback(job_id, body, category=result.get("category", "info"),
                                                  sender=str(form.get("sender") or ""))
            except Exception:
                pass
            return self._redirect("/queue?tab=replies",
                                  f"Категория: {CATEGORY_LABELS.get(result['category'], result['category'])}")
        if path == "/actions/messages/sync":
            result = sync_from_browser(db, settings, limit=20)
            if result.get("ok"):
                return self._redirect("/queue?tab=replies",
                                      f"Прочитано откликов: {result['negotiations']}, новых: {result['added']}")
            return self._redirect("/queue?tab=replies", result.get("reason", "не удалось"), "err")
        if path == "/actions/resume/confirm":
            indexes = form.get("skills") or []
            if isinstance(indexes, str):
                indexes = [indexes]
            resume = confirm_items(resume, "skills", [int(x) for x in indexes if str(x).isdigit()])
            save_resume(resume)
            return self._redirect("/resume", "Подтверждения сохранены")
        if path == "/actions/resume/confirm-project":
            index = int(form.get("index") or 0)
            projects = resume.get("projects") or []
            if 0 <= index < len(projects):
                projects[index]["verified"] = not projects[index].get("verified")
                save_resume(resume)
            return self._redirect("/resume", "Проект обновлён")
        if path == "/actions/resume/import":
            text = str(form.get("text") or "")
            filename = str(form.get("path") or "").strip()
            if filename:
                imported = import_resume(Path(filename))
            elif text.strip():
                imported = resume_io.parse_resume_text(text, source_name="вставленный текст")
            else:
                return self._redirect("/resume", "Нет текста или пути", "err")
            save_resume(imported)
            return self._redirect("/resume", "Импортировано. Подтвердите факты вручную.")
        if path == "/actions/resume/restore":
            name = str(form.get("name") or "")
            try:
                restore_version(name)
            except Exception as exc:
                return self._redirect("/resume", f"Не удалось восстановить: {exc}", "err")
            return self._redirect("/resume", "Версия восстановлена")
        if path == "/actions/resume/save":
            try:
                payload = json.loads(str(form.get("json") or "{}"))
            except json.JSONDecodeError as exc:
                return self._redirect("/resume", f"Ошибка JSON: {exc}", "err")
            save_resume(payload)
            return self._redirect("/resume", "Резюме сохранено")
        if path == "/actions/resume/diff":
            a, b = str(form.get("a") or "python"), str(form.get("b") or "data")
            diff = diff_variants(resume, a, b)
            content = (f"<h1>Diff: {e(VARIANTS[a]['label'])} → {e(VARIANTS[b]['label'])}</h1>"
                       f"<div class='card'><h3>Только в {e(a)}</h3><p>{e(', '.join(diff['only_a']) or '—')}</p>"
                       f"<h3>Только в {e(b)}</h3><p>{e(', '.join(diff['only_b']) or '—')}</p>"
                       f"<h3>Summary</h3><p>{e(diff['a']['summary'])}</p><p>{e(diff['b']['summary'])}</p></div>"
                       f"<p><a href='/resume'>← назад</a></p>")
            return self._html("Diff", content, active="/resume")
        if path == "/actions/resume/export":
            variant = str(form.get("variant") or "python")
            job_row = dbm.get_job(db, str(form.get("job") or "")) if form.get("job") else None
            outputs = export_variant(resume, variant, job=job_row,
                                     details=dbm.job_detail(job_row) if job_row else None)
            return self._redirect("/resume", f"Экспорт: {json.dumps(outputs, ensure_ascii=False)[:300]}")
        if path == "/actions/settings":
            try:
                payload = json.loads(str(form.get("json") or "{}"))
            except json.JSONDecodeError as exc:
                return self._redirect("/settings", f"Ошибка JSON: {exc}", "err")
            save_settings(payload)
            return self._redirect("/settings", "Настройки сохранены")
        if path == "/actions/calibrate":
            result = train_calibration(db, settings)
            return self._redirect("/settings?tab=advanced", result.get("reason") or
                                  f"Обучено: N={result['samples']}, test N={result.get('test_n')}, "
                                  f"Brier={result['brier']}, AUC={result['auc']}",
                                  "ok" if result.get("trained") else "warn")
        if path == "/actions/model/refresh":
            router = AITunnelRouter(db, settings)
            result = router.refresh_catalog(force=True)
            if result.get("refreshed"):
                message = f"Каталог обновлён: {result['models']} моделей."
            else:
                message = f"Каталог не обновлён: {result.get('error', 'нет изменений')}."
            return self._redirect("/settings", message, "ok" if result.get("refreshed") else "warn")
        if path == "/actions/memory/rebuild":
            memory = Memory(db, settings)
            facts = facts_pack(resume, profile)
            from core.resume import facts_hash as facts_digest
            from core.scoring import rank_pending
            rank_pending(db, settings, profile, resume)
            memory.sync_facts(resume, profile, facts_digest(facts))
            rows = db.execute(
                """SELECT * FROM jobs WHERE duplicate_of IS NULL AND (eligible=1 OR score>=?)
                   ORDER BY score DESC LIMIT 60""", (float(settings.get("threshold", 70)) - 12,)).fetchall()
            chunks = memory.index_jobs([dict(row) for row in rows])
            embedded = memory.embed_pending(limit=60)
            memory.unload_embedder()
            return self._redirect("/settings?tab=advanced",
                                  f"Память: фактов {len(facts.get('verified_skills', []))}, чанков +{chunks}, "
                                  f"векторов +{embedded.get('embedded', 0)}.")
        if path == "/actions/browser/check":
            from core.browser import auth_status
            result = auth_status(settings, headless=True)
            message = (f"Сессия активна: {result['logged_in']}. Блокировка: {result['blocked'] or 'нет'}."
                       if not result.get("blocked") else f"Площадка требует проверки: {result['blocked']}")
            return self._redirect("/settings?tab=sources", message, "ok" if result["logged_in"] else "warn")
        if path == "/actions/browser/fingerprint":
            from core.browser import export_session_fingerprint
            info = export_session_fingerprint(settings)
            return self._redirect("/settings?tab=sources", f"Cookie: {info['cookie_count']}, домены: {', '.join(info['domains'][:5])}")
        return self._redirect("/", "Неизвестное действие", "err")


def serve(port: int = 8765, open_browser: bool = True) -> None:
    ensure_dirs()
    token()
    dbm.init_db().close()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"AI Job Hunter dashboard: {url}  (Ctrl+C — остановка)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")
    finally:
        server.server_close()
