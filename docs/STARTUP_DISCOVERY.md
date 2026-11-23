# Startup discovery and outreach

The engine extends the existing AI Job Hunter 4.0. It does not create a second
application queue and does not send messages.

## Connected public sources

| Source | What is read | Limits |
|---|---|---|
| Product Radar | category pages `seeking-interns`, `hiring`, product pages with published statuses | 2 listing pages per category, detail page cached for 7 days, 20 s timeout, 1 request/s |
| StartupFellows | `/vacancies?page=N`, vacancy detail pages | up to 6 listing pages/day, at most 40 detail pages/day, 1 request/s |

Seed checks: Aiesa.ru, Virton AI, Upgraide / vasilisa.ai, Webinest, Sniper Search.
DotSolution Startup Studio has no public Product Radar page and its domain did not
resolve during the check, so it was not parsed or invented.

`github.com/alekseevpavel04/ru-it-jobs-radar`, `github.com/orthlus/telegram-list`
and Wellfound were examined as catalog ideas. They are not connected as vacancy
sources: the GitHub lists duplicate the already connected Telegram catalog, and
Wellfound is not a Russian public board with usable legal access from this host.
No parser was written for an unavailable page.

## Lead model

`company_leads` stores one published opportunity per URL. A company can have
several roles; this is deliberate, so a product-manager post cannot overwrite a
Python post. Before an outreach draft the UI warns if another draft or sent
message already exists for the same company.

Opportunity kinds:

- `verified_vacancy` — vacancy with explicitly confirmed pay;
- `paid_internship` — internship with salary/pay explicitly stated;
- `paid_project` — project/part-time with confirmed pay;
- `company_hiring` — public hiring statement without confirmed pay;
- `potential_employer` — product profile, no public hiring signal;
- `unpaid_opportunity` — explicitly no pay / equity-only;
- `cofounder_search` — co-founder or CTO-partner search;
- `unknown` — public post without enough information.

`potential_employer` is never counted as a vacancy. Unpaid and co-founder posts
never become paid opportunities. "Возможность продолжить на оплачиваемой основе"
is treated as payment unknown, not paid.

## Scores

Three independent expert estimates are stored, never presented as offer odds:

- **Startup Fit** (0–100): confirmed skill overlap, role/internship fit,
  schedule, freshness, public contact;
- **Hiring Evidence** (0–100): how explicit and recent the public hiring signal is;
- **Compensation Confidence** (0–100): how precisely pay is stated.

Hard caps lower fit for non-technical roles, C#/.NET/mobile-only roles and
conflicting schedules. Unknown mandatory conditions are shown as unknown.

## Outreach

Drafts are local templates built only from:

- the published product description and hiring text;
- verified resume skills;
- one real educational project;
- an explicit statement that commercial experience is absent.

No LLM is called for drafts by default, so this feature does not increase
AITUNNEL spend. Every draft waits for manual confirmation; `contacted` is set only
after the user confirms a real external send.

## Schedule

`jobhunter.py auto` checks startups once per `settings.startups.hours` (24 by
default). The existing Windows task `AI-Job-Hunter-Cycle` remains the only
scheduler. `automation_state.last_startups_at` prevents repeated daily runs.
Settings → «Поиск и источники» exposes the interval.

## Verification snapshot

At the verification run: 220 Telegram candidates and 118 active channels were
untouched; the startup engine held 44 usable company/opportunity cards from two
sources, including 26 with a public hiring signal; 8 personalized drafts were
prepared; 0 messages were sent. The real counts are visible in the Startups tab
and in `/api/startups`.
