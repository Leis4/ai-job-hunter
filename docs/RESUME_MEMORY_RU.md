# Resume memory and continuous skills

`core/resume_memory.py` extends `CandidateMemory` and the existing SQLite RAG.
No second profile, database or vector service is used.

## Import

Supported: PDF, DOCX, TXT/MD, JSON.

1. File hash is computed and stored in `resume_imports`.
2. Text is extracted by the existing `onboarding.extract_text()`.
3. Local Qwen (`QwenEngine.analyze_resume`) extracts structured facts when
   available; the deterministic local parser is used as a fallback.
4. The extractor compares facts with the current resume/profile and produces
   `new`, `changed`, `conflict` and `same` items.
5. Nothing is written until the user confirms selected items.
6. Confirmation merges skills/projects/education/experience/contacts without
   deleting existing data, then updates RAG.

Re-importing the same hash returns the existing proposal and never duplicates
skills. Unknown skill level remains `unknown`.

## Skills

A phrase such as «Начал изучать PyTorch» is parsed into a proposed skill and
one of:

- `learning`
- `basic`
- `practical`
- `confident`
- `unknown`

The user confirms the proposal. An existing skill is updated, never duplicated.
`learning` and `basic` reduce the matchmaking skill strength; they do not make a
candidate an experienced specialist.

## Learning journal

`learning_history` stores date, description, source, related skills and status.
It is a fact source, not an automatic proof of professional seniority.

## Resume refresh

`preview_resume_update()` compares confirmed memory with resume.json and returns
new skills/projects. `apply_resume_update()` adds approved items and exports a
new PDF/DOCX through the existing `export_variant()`.

## Incremental RAG

`CandidateMemory.set_fact()` deletes the previous document with the same ref,
updates `candidate_fact`, FTS and vectors, and adds the new one. `forget_fact()`
removes the document from `memory_documents`, `memory_fts` and `memory_vectors`.
The existing `bge-m3` model and 1024-dimensional vectors are unchanged.

## Endpoints

- `POST /api/memory/resume/import` (multipart)
- `POST /api/memory/resume/confirm`
- `POST /api/memory/skill`
- `POST /api/memory/project`
- `POST /api/memory/learning`
- `GET /api/memory/learning`
- `POST /api/memory/resume/preview`
- `POST /api/memory/resume/update`

## Tests

```powershell
py -3 tests/resume_memory_smoke.py
py -3 tests/candidate_memory_smoke.py
py -3 tests/memory_smoke.py
```

The resume-memory test uses an isolated temporary config/data directory and a
generated text PDF. It never writes real personal data.
