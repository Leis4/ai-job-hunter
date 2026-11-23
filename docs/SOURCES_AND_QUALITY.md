# Vacancy quality: sources, duplicates, eligibility, realism

## Sources

| Source | Status | How it is read |
| --- | --- | --- |
| `trudvsem` | works, keyless | official open-data API `opendata.trudvsem.ru` |
| `habr` | works, keyless | public vacancy cards and detail pages on `career.habr.com` |
| `vk_career` | works, keyless | public search on `team.vk.company` (`__NEXT_DATA__` + schema.org description) |
| `hh_api` | disabled | official `api.hh.ru` returns HTTP 403 from this network; no bypass is attempted |
| `superjob` | unavailable | official API needs a personal `SUPERJOB_API_KEY`; without it the source reports the real reason |
| `feed` | opt-in | RSS/Atom/JSON feeds explicitly configured by the owner |

Source state is stored in `source_state` and exposed to the UI as
`/api/settings → source_status`: configured / available / last status / last error /
cards received. The UI never shows a disabled or key-less source as working.

## Near-duplicate clustering

`core/dedup.py` runs after each collection (and can be re-run safely):

1. exact fingerprint from the importer (cross-source copies);
2. near-duplicate pass by company block using description cosine + 4-word shingles,
   title/company/skills similarity, salary and location agreement;
3. a copy is linked only when the title is compatible — a generic internship text
   from the same company no longer merges Frontend and Backend roles.

The canonical row is chosen by applications, description richness and fetch time.
Every original keeps its own row, URL and source. `job_publications` stores
`copy_id → canonical_id` with similarity; the job page lists all publications and
links to each original posting.

Queued applications of a merged copy are moved to the canonical job; if the same
channel already exists, the stale one is cancelled with an audit event.

## Eligibility and Match Score

`Match Score` stays a 0–100 compatibility estimate, never a probability.

Hard/quality gates (`core/scoring.py`, settings `scoring.*`):

- hard constraints: excluded category, experience gap, senior/lead without
  confirmed years, strict education, licence;
- `commercial_experience`: explicit production/ML/NLP requirements
  ("production-ready", "промышленная разработка", "внедрение моделей",
  "опыт в DS/NLP", "чат-боты"…) when the candidate has no confirmed commercial
  months → rejection with the matched phrases in the reason;
- `min_skill_match` (50): lower skill coverage → status `weak`, not green;
- `min_invite_realism` (35): implausible invitations → `weak`;
- `threshold` (65): below it the vacancy is not queued.

Statuses shown in the UI: `Проходит требования` (green), `Низкое соответствие`
(amber, known mismatch), `Нужна проверка` (amber, requirements unknown),
`Не проходит` (red, hard constraint), `Ниже порога` (amber, score below threshold).

## Invite realism

`scores.invite_realism` is a separate 0–100 estimate for *this* candidate, not a
probability: skill match, experience gap, seniority level, commercial experience,
requirement coverage and profile completeness. Reasons are shown next to the
number ("нет коммерческого опыта", "требуют 1 г. опыта сверх подтверждённого").
It is displayed on cards and in the vacancy page, and gates the queue together
with eligibility.

## Letters and the queue

`prepare` selects a resume variant by vacancy title + matched skills, generates a
letter, runs the deterministic fact check and the LLM critique, and queues a
`manual_export` application. If the check finds unsupported claims, or the letter
does not name the real vacancy/company, the letter is replaced by the local
factual template (`letter_source = factual_factcheck`). Nothing is sent to a job
board from the UI: `confirm=false`, export only.
