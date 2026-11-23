"""Local Qwen resume parser through Ollama.

The failure this fixes: Qwen3 spends the whole ``num_predict`` budget on the
hidden thinking stream and returns ``content=""`` with ``done_reason="length"``.
We disable thinking for Qwen3 (``think: false``), raise the output budget, and
fall back to a plain retry for older Qwen models. The draft is never saved as
verified; the user confirms every fact.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .onboarding import merge_model_payload
from .settings import load_settings
from .text import scrub_contacts


class QwenUnavailable(RuntimeError):
    pass


class QwenParser:
    def __init__(self, settings: dict | None = None):
        self.settings = settings or load_settings()
        conf = self.settings.get("qwen") or {}
        self.url = str(conf.get("url", "http://127.0.0.1:11434")).rstrip("/")
        self.model = str(conf.get("model", "qwen3:8b"))
        self.timeout = int(conf.get("timeout_seconds", 180))
        self.num_predict = int(conf.get("num_predict", 3000))
        self.think = bool(conf.get("think", False))
        self.unload_after = bool(conf.get("unload_after", True))

    def available(self) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=8) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return False, f"Ollama недоступен: {type(exc).__name__}"
        names = {m.get("name", "") for m in payload.get("models", [])}
        if self.model in names or f"{self.model}:latest" in names:
            return True, ""
        return False, f"модель {self.model} не установлена (скачивание не выполняется)"

    def _chat(self, prompt: str, *, think: bool | None, num_predict: int) -> dict:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "Ты извлекаешь только явные факты. Возвращай только JSON."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {"num_predict": int(num_predict), "temperature": 0.1},
            "keep_alive": "2m",
        }
        if think is not None:
            body["think"] = bool(think)
        request = urllib.request.Request(
            self.url + "/api/chat", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(300).decode("utf-8", "replace")
            except Exception:
                pass
            if think is not None and exc.code == 400:
                return self._chat(prompt, think=None, num_predict=max(num_predict, 4500))
            raise QwenUnavailable(f"Qwen HTTP {exc.code}: {detail[:160]}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise QwenUnavailable(f"Qwen недоступен: {type(exc).__name__}") from None
        except json.JSONDecodeError as exc:
            raise QwenUnavailable("Qwen вернул не-JSON HTTP-ответ") from None

    def parse_resume(self, text: str, *, source_name: str = "") -> dict:
        available, reason = self.available()
        if not available:
            raise QwenUnavailable(reason)
        ai_text = scrub_contacts(text)
        prompt = (
            "Извлеки из резюме ТОЛЬКО явно написанные факты. Не придумывай опыт, годы, компании, "
            "навыки и достижения. Верни строго JSON:\n"
            '{"headline":"", "summary":"", "contact":{"email":"","phone":"","city":""},'
            '"skills":[{"name":"","evidence":"точная цитата"}],'
            '"education":[{"institution":"","degree":"","field":"","start":"","end":"","evidence":""}],'
            '"experience":[{"company":"","position":"","start":"","end":"","description":"","evidence":""}],'
            '"projects":[{"name":"","role":"","stack":[],"description":"","evidence":""}],'
            '"languages":[{"name":"","level":"","evidence":""}],'
            '"directions":["..."], "constraints":{"locations":[],"work_modes":[],"employment":[],'
            '"relocation":false,"salary_target_rub":null,"availability":""}}\n'
            "Если данных нет — пустая строка/массив/null. evidence должна быть дословной цитатой."
            "\n\nРЕЗЮМЕ:\n" + ai_text[:12000]
        )
        # Qwen3: thinking consumes the token budget and leaves content empty.
        result = self._chat(prompt, think=False if self.think is False else None,
                            num_predict=self.num_predict)
        message = result.get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            thinking = (message.get("thinking") or "").strip()
            reason = result.get("done_reason") or "unknown"
            if thinking and reason == "length":
                raise QwenUnavailable(
                    "Qwen израсходовал лимит на reasoning и не вернул JSON "
                    f"(done_reason=length, thinking {len(thinking)} символов). "
                    "Повторите с увеличенным лимитом или AITUNNEL."
                )
            raise QwenUnavailable(f"Qwen вернул пустой content (done_reason={reason})")
        from .llm import extract_json
        payload = extract_json(content)
        if not payload:
            raise QwenUnavailable("Qwen вернул текст, но не валидный JSON")
        draft = merge_model_payload(text, payload, source_name=source_name,
                                    method=f"qwen:{self.model} + local check",
                                    cost_rub=0.0)
        if self.unload_after:
            self.unload()
        return draft

    def unload(self) -> None:
        try:
            body = json.dumps({"model": self.model, "prompt": "", "stream": False,
                               "keep_alive": 0, "options": {"num_predict": 1}}).encode()
            request = urllib.request.Request(
                self.url + "/api/generate", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=20):
                pass
        except Exception:
            pass
