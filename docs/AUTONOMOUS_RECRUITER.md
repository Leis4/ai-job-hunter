# Autonomous recruiter: scheduler, hh.ru, channel intelligence

## Auto mode

`jobhunter.py auto` is the single entry point used by `run_cycle.cmd` and
Windows Task Scheduler (`schedule_task.ps1`, every 30 minutes by default).

- light update every `automation.light_minutes` (Telegram only, 30 channels per
  pass, rotating by last run);
- full source update every `automation.full_hours` (6 h);
- discovery + validation every `automation.discovery_hours` (24 h);
- `data/cycle.lock` prevents concurrent cycles;
- offline check postpones the run for 5 minutes;
- `automation_state` stores last/next run and errors;
- no browser is used; playwrite only for the explicit application draft.

Windows task:

```powershell
powershell -File .\schedule_task.ps1 -IntervalHours 0.5 -RunNow
powershell -File .\schedule_task.ps1 -Status
```

Task Scheduler runs `run_cycle.cmd` → `jobhunter.py auto`, independent of the
React UI. If the PC is off/asleep, the task runs at the next opportunity.

## Telegram scaling

- catalogs: lovach gist, firstmessage, telemetr visible page, Ebazhanov GitHub;
- candidate statuses: discovered → validating → qualified/irrelevant/unavailable/inactive → enabled;
- per-channel quality: posts_30d, vacancies, internships, junior, ads ratio,
  relevance, quality, last post, auto-pause after 45 days without posts;
- `max_channels_per_run` limits one pass to 30 channels; oldest-run channels go first;
- bulk add, CSV import, validate all, enable recommended, disable irrelevant.

## hh.ru

`api.hh.ru` responds HTTP 403 `forbidden` with a request id from this network.The application records the real response in `core/hh_client.py` and does not
bypass it. Direct access requires a registered hh application and OAuth token in
`HH_ACCESS_TOKEN`; the token stays in the environment and is never sent to LLM.
Without it the UI shows `blocked`, plus indirect hh vacancies found through
Telegram/other sources, their descriptions and enrichment status.

## Startups

Startup discovery runs in the same `jobhunter.py auto` cycle once per
`settings.startups.hours`. It stores company leads separately from vacancies and
prepares outreach drafts only for qualified technical roles with a public contact.
See `docs/STARTUP_DISCOVERY.md` for sources, categories, scores and limits.
The application submission center and its verification rules are described in
`docs/APPLICATION_CENTER.md`; it never sends automatically.

## Probability

Personal outcome history, Beta-Binomial estimates and the gated ML model are
described in `docs/PROBABILITY_ENGINE.md`. With no confirmed outcomes the UI
shows «Недостаточно истории для достоверного прогноза» and never fabricates a
percentage.
