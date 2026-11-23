# AI Job Hunter 4.0 — React UI

Новый интерфейс: React 18 + TypeScript + Vite + Tailwind + shadcn-style компоненты
(Radix) + Lucide. Python-бэкенд остался прежним (`web/server.py`), SPA общается с ним
через JSON API из `web/api.py`.

## Запуск

```powershell
py -3 jobhunter.py serve --port 8765
```

Открыть http://127.0.0.1:8765/ — сервер отдаёт `frontend/dist` и API.

Сборка фронтенда (после изменений в `frontend/src`):

```powershell
cd frontend
npm install      # один раз
npm run build
```

`frontend/dist` закоммичен, поэтому обычный запуск не требует Node.

## Откат на старый SSR-интерфейс

```powershell
$env:JOB_HUNTER_LEGACY_UI = '1'
py -3 jobhunter.py serve --port 8765
```

Старые маршруты (`/jobs`, `/queue`, `/resume`, `/settings`, `/onboarding`,
`/actions/*`, `/export/*`) сохранены без изменений и работают при включённом флаге.

## API

Чтение (GET, без токена, только localhost + Host/Origin-guard):

| Путь | Назначение |
| --- | --- |
| `/api/session` | CSRF-токен для записи |
| `/api/dashboard` | сводка, воронка, события, топ-вакансии, статус агента и LLM |
| `/api/jobs?…` | поиск и фильтры (те же параметры, что у `list_jobs`) |
| `/api/jobs/{id}` | детали, факторы скоринга, письмо, отклики, переписка |
| `/api/resume` | Resume Studio, варианты, версии, полнота |
| `/api/onboarding` | черновик, файлы, статус Qwen/AITUNNEL |
| `/api/applications` | очередь и история откликов |
| `/api/messages` | ответы работодателей |
| `/api/settings` | настройки, профиль, LLM, память, калибровка |
| `/api/export/{jobs.csv,queue.csv,analytics.json}` | выгрузки |

Запись (POST JSON, обязателен `X-JobHunter-Token` из `/api/session`):

`/api/cycle/start|stop`, `/api/jobs/favorite|prepare|letter|generate-letter`,
`/api/onboarding/upload|qwen|aitunnel|cancel|discard|confirm|import`,
`/api/queue/build`, `/api/queue/{id}/approve|send|cancel`,
`/api/resume/save|confirm|project|import|restore|export`,
`/api/settings`, `/api/profile`, `/api/sources/run`, `/api/model/refresh`,
`/api/calibrate`, `/api/memory/rebuild`, `/api/messages/import|reply|sync`.

Запись из UI никогда не отправляет отклик на площадку: `confirm: false`, канал
`manual_export` только выгружает файлы. Автоотправка выключена.

## Страницы

- **Дашборд** — статус агента, реальные счётчики, очередь, причины пропуска,
  воронка, динамика, последние события, топ-вакансии с действиями.
- **Job Explorer** — фильтры по источнику, навыку, опыту, зарплате, формату,
  Match Score, избранному; пагинация; карточка вакансии с факторами скоринга.
- **Resume Studio** — загрузка PDF/DOCX/TXT/MD, разбор Qwen/AITUNNEL, подтверждение
  фактов, версии и экспорт; черновик явно отделён от подтверждённых данных.
- **Отклики** — очередь, одобрение/отмена, ручная выгрузка, история, импорт ответов
  и черновики ответов HR.
- **Настройки** — профиль и предпочтения, источники, модель/расходы/память,
  калибровка, JSON настроек, экспорт.

## Проверки

```powershell
py -3 tests\web_smoke.py        # SSR + JSON API (48 проверок)
py -3 tests\api_onboarding.py   # SPA-подтверждение профиля (13 проверок)
py -3 tests\onboarding_smoke.py
py -3 tests\onboarding_http.py
py -3 tests\unit_core.py
py -3 tests\math_smoke.py
py -3 tests\smoke_e2e.py
```

`tests/web_smoke.py` и `tests/onboarding_http.py` выставляют
`JOB_HUNTER_LEGACY_UI=1`, чтобы покрывать старый SSR независимо от наличия `dist`.
