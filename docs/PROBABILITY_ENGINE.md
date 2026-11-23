# Personal application probability

This module learns only from confirmed application outcomes. It does not create
examples from prepared drafts, does not use LLM-generated labels and does not
call a paid API.

## Import

| Method | Behaviour |
|---|---|
| hh.ru official API | `GET https://api.hh.ru/negotiations` with `HH_ACCESS_TOKEN`; 403/429 is recorded, not bypassed |
| JSON | array or `items`/`negotiations`/`rows`, including nested HH vacancy objects |
| CSV | aliases title/company/url/sent_at/status/... |
| saved HTML | tolerant parser for saved negotiation blocks; unknown fields left empty |
| pasted email | explicit paste of an email notification; no mailbox credentials requested |
| manual | title, company, date, status, link |

Passwords, cookies and confirmation codes are never requested or stored. Tokens
stay in the environment and are never written to SQLite or logs.

Duplicate imports are matched by a stable external key (HH negotiation id;
otherwise a hash of source, job/url, employer, title and sent date). Re-importing
updates the same row and appends an event only when the status changes.

## Stored separately

`application_outcomes` stores the current aggregate state and
`application_outcome_events` stores every observed transition. The aggregate
contains employer, title, sent/last event dates, resume, skills, required
experience, seniority, salary, location, schedule, source, Match Score, response
type, status, status source, confidence and a feature snapshot.

`no_response_yet`, `submitted`, `viewed`, `responded`, `interview` and `unknown`
are pending, **not** failures. Only `rejected` is a confirmed failure; only
`invited` and `offer` are confirmed invitations.

## Bulk history import

The Statistics page accepts multiple `.html`/`.htm` files and ZIP archives. ZIP
contents are expanded in memory (uncompressed limit 40 MB), all history pages are
merged, duplicate negotiation IDs are removed, and vacancy IDs are extracted from
`/vacancy/<id>` links. A saved vacancy page supplied in the same batch is matched
by vacancy ID and its description, skills and conditions are attached to the
outcome. The preview lists every row before confirmation.

After confirmation, `POST /api/outcomes/enrich` processes descriptions in chunks.
It checks existing SQLite jobs, previously saved publications, supplied vacancy
HTML, and only then the official hh.ru API when `HH_ACCESS_TOKEN` is present. It
stores `job_description`, `description_source`, `description_fetched_at`,
`details_json` and a completeness score. Missing or deleted vacancies stay empty;
no text is invented.

`application_outcomes.job_description` is the original publicly available text;
feature snapshots derived from an import are marked `at_submission=false` and are
excluded from ML training.

## Status crawl and checkpointed enrichment

`POST /api/outcomes/enqueue` schedules one status task per imported application
and one detail task per unique vacancy. `POST /api/outcomes/process` processes
the queue in bounded chunks; state, attempts, errors and `next_attempt_at` live
in `enrichment_queue`, so a restarted process continues. A 429 sets a 90-second
backoff and stops the batch; 401/403 is recorded and never bypassed.

Status checks use `GET /negotiations/{id}` and `/messages` with the user's OAuth
token. Raw hh state, normalized state, last event, viewed/invitation/rejection
dates, message count and evidence URL are stored. Without a token, the status
already present in the user's saved HTML remains the evidence and is marked as
such; no status is invented.

Vacancy detail tasks are deduplicated by `vacancy_id`; one description is applied
to all responses for that vacancy. The lookup order is SQLite → saved
publications → user HTML → official `GET /vacancies/{id}`. A vacancy has its own
`active/archived/removed/unavailable/unknown` status; archiving never changes an
application to rejected.

## Beta-Binomial

For a segment `(seniority, direction)`:

```text
posterior = Beta(1 + s, 1 + f)
mean      = (1 + s) / (2 + s + f)
```

The prior `Beta(1,1)` is deliberately weakly informative and is not tuned to
produce attractive numbers. The 90% credible interval is computed with
`scipy.stats.beta.ppf` (deterministic fallback sampling if SciPy is unavailable).

Fallback is strictly `(seniority, direction)` → same `seniority` → prior. A
Middle rejection is never used to lower a Junior estimate. A displayed personal
probability requires at least 20 resolved outcomes in the selected segment, at
least 5 invitations, at least 5 refusals and a 90% interval no wider than 0.30.
Otherwise the UI says «Недостаточно истории для достоверного прогноза» and shows
the expert realism score separately.

## ML gate

Logistic regression is trained only when at least 60 resolved outcomes, 15
invitations and 15 refusals exist, and only on rows whose feature snapshot was
captured at or before submission. Historical imports without a snapshot are not
used for ML. Features: Skill Match, Experience Gap, Career Fit, required
experience, seniority, direction, schedule, geography, education requirement,
company/vacancy type and freshness at submission.

Validation is a chronological 70/30 split. Metrics: Brier, Log Loss, PR-AUC and
calibration bins, compared with the constant base rate. The candidate is
activated only if its Brier score improves on the holdout by at least 0.005 over
the baseline and the active model. Otherwise Beta-Binomial remains active.

Model versions and parameters are saved in `probability_models`; active
coefficients live in `data/probability_model.json` (local, not committed).

## Current state

The working SQLite contains **0 confirmed application outcomes** and **0 sent
applications**, so:

- ML is disabled;
- no personal probability is published for any vacancy;
- only the explicit «Недостаточно истории» message and the separate expert
  realism score are shown;
- no synthetic outcomes were inserted into the working database.

Automation calls `maybe_retrain` after each cycle, but it does nothing until new
confirmed outcomes appear.
