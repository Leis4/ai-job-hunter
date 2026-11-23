# Persistent candidate memory

`core/candidate_memory.py` is the single application-facing access layer. It
does not create a second profile or database.

## Storage

| Data | Authority |
| --- | --- |
| contact, education, verified skills, projects, experience | `config/resume.json` |
| work preferences | `config/profile.json` |
| provenance and confirmed free-form facts | `candidate_facts` in `data/jobs.sqlite3` |
| RAG documents, FTS5, vectors | existing `memory_documents`, `memory_fts`, `memory_vectors` |
| applications, messages, decisions | existing tables |
| AITUNNEL / SMTP secrets | DPAPI files under `data/` |
| resume versions | existing `output/resume_versions` and resume files |

`CandidateMemory.resume()` overlays the newest confirmed `contact.*` fact over
the structured file, so Settings, Submission Engine and resume export see the
same email/phone. `update_contact()` writes both the fact record and the
structured file.

RAG stores verified skills, projects, experience, education, preferences and
confirmed decisions. It never stores passwords, tokens, cookies or API keys.
Contacts are deliberately not indexed in RAG.

## API

```text
get_fact(key)
set_fact(key, value, source, confirmed, kind, index)
update_fact(key, value)
forget_fact(key)
search_memory(query)
get_candidate_context(job_id)
get_application_context(application_id)
get_integration_status()
get_secret(name)   # trusted server code only
set_secret(name, value)
forget_secret(name)
```

HTTP endpoints are local and token-guarded like the rest of the API:

- `GET /api/memory/status`
- `GET /api/memory/facts`
- `POST /api/memory/sync`
- `POST /api/memory/fact` with `{key,value,kind}` or `{key,delete:true}`
- `POST /api/memory/secret/forget` with `{name}`

No endpoint returns a saved secret value.

## CLI

```powershell
py -3 jobhunter.py memory setup    # asks only for missing contact fields
py -3 jobhunter.py memory status   # flags/counts, no secret values
py -3 jobhunter.py memory rebuild  # FTS + local bge-m3 vectors
```

Secrets remain:

```powershell
py -3 jobhunter.py secret aitunnel set
```

## Qwen and submission

`letters.generate_letter()` passes verified `candidate_fact` memory to
`QwenEngine.generate_letter(memory_facts=...)`. The prompt includes only
verified facts and never contact fields. `SubmissionEngine` resolves the
candidate through `CandidateMemory.resume()`, so a changed email is used by the
next application, while already-sent rows keep their historical external IDs
and evidence.

## Tests

```powershell
py -3 tests/candidate_memory_smoke.py
py -3 tests/memory_smoke.py
```

The first test uses an isolated temporary config/data directory, tests
contact persistence across a new instance, confirmed facts, deletion, RAG,
Qwen prompt isolation and submission contact visibility. It never touches real
personal data.
