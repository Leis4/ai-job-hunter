"""Official hh.ru API client for the authorized applicant account.

Only documented endpoints are used. No cookies, CAPTCHA bypass or HTML scraping.
Tokens come from the encrypted OAuth store.
"""
from __future__ import annotations

import time
from urllib.parse import urlencode

import requests

from . import hh_oauth

API = "https://api.hh.ru"
UA = "AI-Job-Hunter/5.0 (personal local job search)"
HH_UA = "AI-Job-Hunter/5.0"
APPLY_OK = (200, 201, 204)
ERROR_KIND = {
    "already_applied": "already_applied",
    "test_required": "test_required",
    "limit_exceeded": "limit_exceeded",
    "resume_not_found": "resume_not_found",
    "invalid_vacancy": "invalid_vacancy",
    "application_denied": "application_denied",
    "resume_visibility_conflict": "resume_visibility",
    "no_invitation": "no_invitation",
    "in_a_row_limit": "message_limit",
    "overall_limit": "message_limit",
    "disabled_by_employer": "messaging_disabled",
    "chat_archived": "chat_archived",
    "archived": "vacancy_archived",
    "wrong_state": "wrong_state",
    "forbidden": "forbidden",
}


class HHAPIError(RuntimeError):
    def __init__(self, status: int, payload: dict | None = None, reason: str = ""):
        super().__init__(reason or f"HTTP {status}")
        self.status = status
        self.payload = payload or {}
        self.reason = reason
        self.values = self.error_values
        self.kind = next((ERROR_KIND[v] for v in self.values if v in ERROR_KIND), "")
        if not self.kind:
            self.kind = {401: "auth_required", 403: "forbidden", 404: "not_found",
                         429: "rate_limit"}.get(status, "api_error")

    @property
    def error_values(self) -> list[str]:
        return [str(x.get("value") or x.get("type") or "") for x in
                (self.payload.get("errors") or []) if isinstance(x, dict)]


class HHClient:
    def __init__(self, token: str | None = None, *, min_interval: float = 0.25):
        self.token = (token or hh_oauth.access_token() or "").strip()
        self.min_interval = min_interval
        self._last = 0.0

    def _headers(self) -> dict:
        if not self.token:
            raise HHAPIError(401, reason="Нет OAuth-токена hh.ru")
        return {"Authorization": f"Bearer {self.token}", "User-Agent": UA,
                "HH-User-Agent": HH_UA, "Accept": "application/json"}

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()

    def request(self, method: str, path: str, *, params: dict | None = None,
                data: dict | None = None, retry_auth: bool = True) -> dict:
        self._wait()
        response = requests.request(method, API + path, params=params,
                                    data=data, headers=self._headers(), timeout=25)
        if response.status_code == 401 and retry_auth:
            refreshed = hh_oauth.refresh()
            if refreshed.get("access_token"):
                self.token = refreshed["access_token"]
                return self.request(method, path, params=params, data=data, retry_auth=False)
        if response.status_code == 429:
            raise HHAPIError(429, reason="hh.ru: превышен лимит запросов")
        if response.status_code not in APPLY_OK:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise HHAPIError(response.status_code, payload, f"HTTP {response.status_code}")
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def me(self) -> dict:
        return self.request("GET", "/me")

    def resumes(self) -> list[dict]:
        payload = self.request("GET", "/resumes/mine")
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return payload.get("items") or []
        return []

    def vacancy(self, vacancy_id: str) -> dict:
        return self.request("GET", f"/vacancies/{vacancy_id}")

    def search_vacancies(self, text: str, *, area: int | None = 1, per_page: int = 100,
                         max_pages: int = 10, extra: dict | None = None) -> list[dict]:
        params = {"text": text, "per_page": per_page, "page": 0}
        if area:
            params["area"] = area
        if extra:
            params.update(extra)
        return self.paginate("/vacancies", params=params, max_pages=max_pages)

    def negotiations(self, *, status: str = "all", max_pages: int = 500) -> list[dict]:
        return self.paginate("/negotiations", params={"per_page": 100, "status": status},
                             max_pages=max_pages)

    def negotiation(self, negotiation_id: str) -> dict:
        return self.request("GET", f"/negotiations/{negotiation_id}")

    def messages(self, negotiation_id: str) -> list[dict]:
        return self.paginate(f"/negotiations/{negotiation_id}/messages",
                             params={"per_page": 100}, max_pages=20)

    def send_message(self, negotiation_id: str, text: str) -> dict:
        return self.request("POST", f"/negotiations/{negotiation_id}/messages",
                            data={"message": text})

    def apply(self, vacancy_id: str, resume_id: str, message: str) -> dict:
        """Official POST /negotiations. Result is confirmed only by HTTP success."""
        try:
            payload = self.request("POST", "/negotiations", data={
                "vacancy_id": str(vacancy_id), "resume_id": str(resume_id),
                "message": message,
            })
            negotiation_id = ""
            if isinstance(payload, dict):
                negotiation_id = str(payload.get("id") or payload.get("negotiation_id") or "")
            return {"ok": True, "status": "submitted", "negotiation_id": negotiation_id,
                    "raw": payload}
        except HHAPIError as exc:
            values = exc.error_values
            status = {
                "already_applied": "already_applied",
                "test_required": "test_required",
                "limit_exceeded": "limit_exceeded",
                "resume_not_found": "resume_not_found",
                "invalid_vacancy": "invalid_vacancy",
                "application_denied": "application_denied",
                "resume_visibility_conflict": "resume_visibility",
                "auth_required": "auth_required",
                "rate_limit": "rate_limit",
                "forbidden": "forbidden",
            }.get(exc.kind, "failed")
            return {"ok": False, "status": status, "http_status": exc.status,
                    "errors": values, "kind": exc.kind, "reason": exc.reason}

    def diagnostics(self) -> dict:
        steps = []
        for name, method, path, params in (
            ("me", "GET", "/me", None),
            ("resumes", "GET", "/resumes/mine", None),
            ("history", "GET", "/negotiations", {"per_page": 1, "status": "all"}),
            ("search", "GET", "/vacancies", {"text": "Python", "per_page": 1}),
        ):
            try:
                payload = self.request(method, path, params=params)
                steps.append({"step": name, "ok": True,
                              "items": len(payload.get("items") or []) if isinstance(payload, dict) else
                              (len(payload) if isinstance(payload, list) else 0)})
            except HHAPIError as exc:
                steps.append({"step": name, "ok": False, "http_status": exc.status,
                              "kind": exc.kind, "reason": exc.reason,
                              "errors": exc.error_values})
            except Exception as exc:
                steps.append({"step": name, "ok": False, "kind": "network",
                              "reason": f"{type(exc).__name__}: {exc}"})
        return {"ok": all(x["ok"] for x in steps), "steps": steps}

    def paginate(self, path: str, *, params: dict | None = None, max_pages: int = 100) -> list[dict]:
        items: list[dict] = []
        page = 0
        while page < max_pages:
            query = dict(params or {})
            query["page"] = page
            payload = self.request("GET", path, params=query)
            if isinstance(payload, list):
                return payload
            batch = payload.get("items") if isinstance(payload, dict) else None
            if batch is None:
                return [payload] if isinstance(payload, dict) else items
            items.extend(batch)
            pages = int(payload.get("pages") or 0)
            if not batch or page + 1 >= pages:
                break
            page += 1
        return items


def diagnostics() -> dict:
    status = hh_oauth.status()
    return {"oauth": status, "api": "https://api.hh.ru",
            "apply_endpoint": "POST /negotiations",
            "messages_endpoint": "POST /negotiations/{id}/messages"}
