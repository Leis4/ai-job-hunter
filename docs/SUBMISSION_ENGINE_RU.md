# Submission Engine

`core/submission.py` routes one application to a real channel and stores
evidence. It does not replace the queue, autopilot or Qwen engine.

## Channels

| Channel | Auto | What is verified |
| --- | --- | --- |
| `official_api` | only with hh.ru OAuth | HTTP response + negotiations history |
| `ats_form` | only for allowlisted ATS domains | confirmation page/marker |
| `career_site_form` | only for allowlisted domains without CAPTCHA | confirmation page/marker |
| `email` | after explicit SMTP setup and channel enable | Message-ID + SMTP acceptance |
| `telegram_contact` | not implemented until a sender is connected | HR reply (future adapter) |
| `manual_assisted` | never | user sends; no submission claimed |

Email uses SMTP with STARTTLS/SSL and a password stored by Windows DPAPI.
SMTP acceptance proves only that the mail server accepted the message, not that
it reached the employer's inbox. A message without a confirmed Message-ID is
never treated as delivered.

Every adapter reports availability, automatic permission, authorization need,
attachments, missing fields, confirmation requirement and verification method.

## Safety rules

- Only `autopilot` + `enabled` + `auto_apply` + enabled channel + allowlisted
  domain can submit without a click.
- CAPTCHA, login walls, archived vacancies and unknown required fields stop the
  attempt and create `requires_user_action`.
- The application row is moved to `submitting` atomically; one job cannot be
  submitted twice concurrently.
- `submitted` requires a portal marker, API response/history or mail id.
- Unknown result becomes `submission_unverified`; it is never retried blindly.
- A submitted/unverified application on the same job blocks every channel.
- Evidence is stored in `submission_attempts` and in application events.

## Real pages checked

- `career.habr.com` — guest form exists, but reCAPTCHA is present. Manual only.
- `team.vk.company` — feedback form exists, but VK ID CAPTCHA is present. Manual only.
- `job.mts.ru` — live vacancy form accepts name, phone, email, city, PDF and a
  cover letter; no CAPTCHA was detected. A dry run filled the form and left the
  final «Отправить» click to the user. No application was sent.
- `careers.datadoghq.com`, `job-boards.greenhouse.io`, `jobs.ashbyhq.com` —
  generic ATS adapter exists, but these hosts time out from the current network,
  so live ATS submission is not verified.
- `hh.ru` — archived example and login/OAuth requirement; official API remains
  the only unattended hh.ru path.
- Current database has no eligible ATS or email vacancies.

## Configuration

Settings → Подключения → «Каналы отправки». Only explicitly enabled channels
are used. The domain list enables ATS/career forms, for example:

```
job.mts.ru, boards.greenhouse.io
```

For email fill SMTP host, port, security, login, sender address and an app
password. The password is stored in DPAPI (`data/smtp_password.bin`) and is
never returned to the browser. Prefer an app password rather than the main
mailbox password. Habr and VK still stop on CAPTCHA even if their domains are
added.

## Commands

```powershell
py -3 tests/submission_smoke.py
py -3 tests/submission_email_smoke.py
py -3 tests/submission_live_inspect.py
```

The smoke tests use one local HTTP form and one local SMTP server only. They
verify successful delivery, duplicate blocking, unknown-result blocking, CAPTCHA
stop and observer policy. No external application or email is sent.
