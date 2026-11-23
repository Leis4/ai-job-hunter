# Autopilot

Autopilot is integrated into the existing Python backend, SQLite database, React UI
and Windows Task Scheduler. It does not create a second scheduler or queue.

## OAuth

`core/hh_oauth.py` implements the official Authorization Code Flow:

1. `POST /api/hh/oauth/start` creates a state and opens the system browser.
2. hh.ru redirects to `http://127.0.0.1:8765/api/hh/oauth/callback`.
3. The callback validates `state` and exchanges the code at `api.hh.ru/token`.
4. Tokens are stored locally in `data/hh_oauth.json`, encrypted with Windows DPAPI.
5. Access tokens are refreshed when expired; refresh tokens are kept only there.
6. Tokens are never written to Git, logs, LLM prompts or the React API.

Required environment variables:

```text
HH_CLIENT_ID=...
HH_CLIENT_SECRET=...
HH_REDIRECT_URI=http://127.0.0.1:8765/api/hh/oauth/callback
```

Register the application at https://dev.hh.ru. Until that exists, the UI shows the
exact instruction and no OAuth request is attempted.

## Official API methods

| Operation | Method |
|---|---|
| profile | `GET /me` |
| resumes | `GET /resumes/mine` |
| search | `GET /vacancies` (pagination, no artificial 20-page cap in the client) |
| apply | `POST /negotiations` with `vacancy_id`, `resume_id`, `message` |
| negotiations | `GET /negotiations` with pagination |
| status | `GET /negotiations/{id}` |
| messages | `GET /negotiations/{id}/messages` |
| send reply | `POST /negotiations/{id}/messages` |

Every request sends both `User-Agent` and `HH-User-Agent`. After a successful
apply the client checks the accessible negotiation history by vacancy/resume; the
result is recorded as `history_confirmed` without resending. A missing history
record does not cause a retry when the API response was already successful.

The API can reject any operation with 400/401/403/404/429/5xx. Those responses
are classified and shown; CAPTCHA/auth/anti-bot protection is never bypassed.

## Modes

- `observer` — search, score, draft, monitor; nothing is sent.
- `assisted` — prepares applications/replies for the user's explicit approval.
- `autopilot` — sends only when the user separately enables `enabled` and
  `auto_apply` / `auto_replies` and a matching policy exists.

Default: `observer`, `enabled=false`, `auto_apply=false`, `auto_replies=false`.
The default daily limit is 5. Excluded employers, allowed resumes, Match Score,
directions, cities and work formats are configurable.

## Browser session (primary connection path)

`core/browser.py` provides a dedicated persistent Chromium profile at:

```text
%LOCALAPPDATA%\AI-Job-Hunter\hh-browser-profile
```

`POST /api/hh/browser/login` opens a visible Chromium at hh.ru. The user logs in
manually; the app only checks the authenticated page state and never reads or
exports cookies. The profile is outside the project and Git. A non-blocking file
lock prevents two app processes from opening the same profile.

`POST /api/hh/browser/check` verifies the saved session without exporting
cookies. `POST /api/hh/browser/capture-history` opens the history page and saves
HTML pages to `data/hh_history_capture/` for the existing bulk importer.
`POST /api/hh/browser/clear` removes only this dedicated profile.

The browser session is **not** an OAuth token and is reported separately in the
Autopilot UI/status. If hh.ru requires login again, the status says so and the
user repeats the manual browser login.

## Safety

- every apply checks local applications and the policy before the API call;
- a local application/vacancy/resume triple is unique, and the API
  `already_applied` response is handled without retry;
- HTTP 403 is classified as authorization, forbidden, already applied, test
  required, resume visibility, vacancy archived, messaging disabled, chat
  archived or a limit — not as one generic failure;
- a timeout or unknown result creates an `autopilot_decisions` item and does not
  resend blindly;
- no action is marked submitted without a successful HTTP response;
- after success the app checks the accessible history for the vacancy/resume and
  records `history_confirmed`;
- salary, schedule, start date, relocation, interview time, test tasks and
  offers are escalated to the user;
- rejection is never inferred from silence or vacancy archival;
- stop clears `enabled`, `auto_apply` and `auto_replies` immediately.

## Local credentials and DPAPI

`client_id` and `client_secret` can be saved from the Autopilot UI into
`data/hh_app_credentials.json`, encrypted with Windows DPAPI. This keeps Task
Scheduler runs independent of an interactive terminal. Environment variables
`HH_CLIENT_ID`/`HH_CLIENT_SECRET` remain an override. Secrets are never returned
by the API, logged, or committed.

DPAPI is user-scoped: the Windows Task Scheduler task must run under the same
Windows user that completed OAuth. The diagnostic panel reports
`dpapi_roundtrip`, token readability and credentials readability.

## UI

`/autopilot` contains OAuth state, mode, policy, search/sync/run/stop, decision
queue, action journal, message and result counters. `Settings → HH` links to it.

## Current limitation

No HH_CLIENT_ID / HH_CLIENT_SECRET are configured in the current environment.
The OAuth flow, encrypted storage, URL/state generation, API client, apply
classifier, policy engine, message classification, decisions and scheduler hook
are implemented and tested with mock HTTP responses, but a real account
connection and real send have not been performed. No real applications or
messages were sent during development.

The persistent Playwright profile path is prepared, but no real hh.ru browser
login was performed during automated verification. The local persistence
mechanism and profile lock were verified with a separate test page; the actual
hh.ru session is created only when the user presses «Подключить hh.ru через
браузер» and logs in manually.
