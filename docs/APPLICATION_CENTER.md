# Application execution center

The center turns prepared drafts into reviewable applications without ever
sending anything automatically. It reuses `applications`, `core/queue.py`,
`core/apply_browser.py`, the existing browser profile and the existing Windows
scheduler.

## Readiness states

`core/readiness.py` recomputes one blocking reason per application and startup
lead:

| State | Meaning |
|---|---|
| `needs_resume` | no suitable resume variant selected |
| `needs_letter` | cover letter missing |
| `needs_contact_info` | name / surname / email / phone required by the form is missing in the profile |
| `needs_verification` | letter changed after approval and must be reviewed again |
| `ready_to_review` | documents and contact data are ready; user must approve |
| `ready_to_submit` | approved and prepared; only the final manual action remains |
| `requires_user_action` | Telegram/HR, aggregator, hh authorization or an unknown form question |
| `submitted` | user or platform explicitly confirmed submission |
| `submission_unverified` | attempt happened but no confirmation |
| `blocked` | duplicate, ineligible, cancelled or CAPTCHA/403 |

The readiness engine is deterministic and local. It does not invent a name,
email, answer or employer.

## What the browser can do

`core/apply_browser.py` in `draft_only` mode:

1. opens the original public page;
2. opens the response form if a visible button/link exists;
3. detects CAPTCHA, login walls and blocks;
4. fills known fields: first/last name, email, phone, cover letter;
5. selects a known city/relocation option when it exactly matches the profile;
6. attaches a generated PDF (DOCX fallback);
7. records filled and unknown required fields;
8. saves a screenshot and a JSON snapshot;
9. **never clicks the final submit button**.

Unknown required questions stop the preparation and are reported in the UI.

Verified on the live queue:

| Case | Result |
|---|---|
| Ozon / changellenge.com form | form opened, phone filled, email required, screenshot saved, no submit |
| MTS / job.mts.ru form | letter, phone and PDF resume filled; final button detected; name/surname/email required |
| Habr Career | blocked by CAPTCHA; no bypass attempted |
| hh.ru | form is authorization-gated; kept as `requires_user_action` |

## Queue and verification

`applications.state` remains the operational state. `readiness` is separate, so
an unfinished form cannot hide a previously approved application.

Explicit confirmation is required:

- `POST /api/queue/{id}/confirm-submitted` with `confirm: true`;
- saves `sent_at`, `verified_at`, `verification_method`,
  `verification_source`, `external_id`, `outcome='unknown'` and a follow-up date;
- opening a page or filling a form never sets `submitted`;
- `POST /api/queue/{id}/unverified` records an uncertain result;
- `POST /api/queue/{id}/outcome` tracks `viewed`, `replied`, `interview`,
  `offer`, `rejected`, `withdrawn`, `unknown`.

The follow-up tracker never treats silence as rejection and never resends.

## Startup outreach

The eight prepared startup drafts appear in the same center. They keep their
kind (`company_hiring`, `paid_internship`, `cofounder_search`, ...), published
contact, payment status and personalized text. `contacted` requires an explicit
confirmation and is stored as user attestation. There is no bulk sending.

## Current working-data snapshot

- 37 application records preserved, 0 deleted;
- 5 direct-form applications need name/surname/email, 24 require manual action
  (Telegram/HR, aggregator, hh authorization), 8 are blocked/cancelled;
- 0 applications are `ready_to_submit` until the user fills the contact block;
- 6 startup drafts are `ready_to_review` (public Telegram contact + letter);
- 2 startup drafts need a contact on the company website;
- 2 forms were actually opened and partially filled (Ozon, MTS);
- 1 CAPTCHA block (Habr), 1 authorization-gated hh page;
- 0 applications or messages were sent.

The contact block in the submission center saves only user-entered values to
`config/resume.json`, which is deliberately excluded from commits, and immediately
recomputes readiness.
