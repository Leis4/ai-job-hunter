"""LLM plumbing test against a local mock OpenAI-compatible server.

Verifies: request shape, JSON mode, caching, usage accounting, budget guard,
prompt-secret blocking, and the two-stage letter + critique path.
No external API and no cost. Run: py -3 tests/llm_mock.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ajh-llm-"))
CONFIG, DATA = TMP / "config", TMP / "data"
CONFIG.mkdir(parents=True)
DATA.mkdir(parents=True)
shutil.copy(ROOT / "config" / "settings.json", CONFIG / "settings.json")
shutil.copy(ROOT / "config" / "profile.json", CONFIG / "profile.json")
os.environ["JOB_HUNTER_CONFIG_DIR"] = str(CONFIG)
os.environ["JOB_HUNTER_DATA_DIR"] = str(DATA)
os.environ["JOB_HUNTER_INBOX_DIR"] = str(TMP / "inbox")
os.environ["JOB_HUNTER_API_BASE"] = "http://127.0.0.1:18999/v1"
os.environ["JOB_HUNTER_API_KEY"] = "test-key-not-a-real-secret-123"
os.environ["JOB_HUNTER_MODEL"] = "mock-deepseek"
sys.path.insert(0, str(ROOT))

from core import db as dbm  # noqa: E402
from core.letters import generate_letter  # noqa: E402
from core.memory import Memory  # noqa: E402
from core.llm import BudgetExceeded, LLM, SecretLeakBlocked  # noqa: E402
from core.resume import load_resume, parse_resume_text, save_resume, sync_from_profile  # noqa: E402
from core.scoring import rank_pending  # noqa: E402
from core.settings import load_profile, load_settings, save_profile, save_settings  # noqa: E402
from core.sources.importer import normalize, read_file  # noqa: E402

CALLS = {"count": 0, "bodies": []}
CURRENT = {"title": "", "company": ""}
PASS = []


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAIL: {name} {detail}")
    PASS.append(name)
    print(f"  ok  {name}{(' — ' + detail) if detail else ''}")


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        CALLS["count"] += 1
        CALLS["bodies"].append(body)
        prompt = json.dumps(body.get("messages"), ensure_ascii=False)
        target = (f"«{CURRENT['title']}»" if CURRENT.get("title") else "вакансию")
        company = (f" в компании {CURRENT['company']}" if CURRENT.get("company") else "")
        if "проверяешь сопроводительные" in prompt:
            content = json.dumps({
                "verdict": "revise",
                "unsupported_claims": ["5 лет опыта"],
                "revised_letter": f"Здравствуйте! Откликаюсь на {target}{company}. "
                                  "Работал с Python и SQL на учебном проекте. Буду рад обсудить задачи.",
            }, ensure_ascii=False)
        elif "Проанализируй вакансию" in prompt:
            content = json.dumps({
                "role_level": "junior", "company_type": "продукт", "main_tasks": ["автоматизация"],
                "tech_requirements": ["Python", "SQL"], "tone": "нейтральный",
                "instructions_in_letter": None, "red_flags": [],
                "letter_angle": "учебный ETL-проект",
            }, ensure_ascii=False)
        else:
            content = (f"Здравствуйте! Откликаюсь на {target}{company}. "
                       "В учебном ETL-проекте я использовал Python, SQL и Pandas. "
                       "Буду рад обсудить задачи команды.")
        payload = {"choices": [{"message": {"content": content}}],
                   "usage": {"prompt_tokens": 200, "completion_tokens": 80}}
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 18999), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = load_settings()
    db = dbm.init_db()
    llm = LLM(db, settings)
    check("llm: configured from env", llm.available)

    first = llm.chat([{"role": "user", "content": "Тестовый запрос"}], kind="test")
    second = llm.chat([{"role": "user", "content": "Тестовый запрос"}], kind="test")
    check("llm: response returned", bool(first))
    check("llm: cache hit avoids second request", first == second and CALLS["count"] == 1, f"calls={CALLS['count']}")
    usage = db.execute("SELECT COUNT(*), SUM(est_cost_usd) FROM llm_usage").fetchone()
    check("llm: usage recorded", usage[0] == 1 and usage[1] > 0, str(dict(zip(("n", "usd"), usage))))

    try:
        llm.chat([{"role": "user", "content": "leak test-key-not-a-real-secret-123"}], kind="leak")
        check("llm: secret blocked", False)
    except SecretLeakBlocked:
        check("llm: secret blocked", True)

    settings["llm"]["monthly_budget_usd"] = 0.0000001
    save_settings(settings)
    tight = LLM(db, load_settings())
    try:
        tight.chat([{"role": "user", "content": "новый уникальный запрос"}], kind="budget")
        check("llm: budget enforced", False)
    except BudgetExceeded:
        check("llm: budget enforced", True)
    settings["llm"]["monthly_budget_usd"] = 3.0
    save_settings(settings)

    # Prepare a verified resume and one vacancy, then run the full letter pipeline.
    resume = sync_from_profile(parse_resume_text(
        (ROOT / "tests" / "fixtures" / "demo_resume.txt").read_text(encoding="utf-8")))
    for skill in resume["skills"]:
        skill["verified"] = True
    for project in resume["projects"]:
        project["verified"] = True
    resume["skills"].append({"name": "Kubernetes", "level": "", "evidence": "", "verified": False})
    save_resume(resume)
    profile = load_profile()
    profile["verified_skills"] = ["Python", "SQL", "Pandas", "Git", "Linux"]
    profile["professional_experience_years"] = 1
    save_profile(profile)
    for item in read_file(ROOT / "tests" / "fixtures" / "demo_jobs.json"):
        dbm.upsert_job(db, normalize(item, source="demo"))
    rank_pending(db, settings, profile, resume)
    job = db.execute("SELECT * FROM jobs WHERE eligible=1 ORDER BY score DESC LIMIT 1").fetchone()
    check("llm: eligible job selected", job is not None)
    memory = Memory(db, {"memory": {"enabled": True, "embed_enabled": False}})
    memory.sync_facts(resume, profile, "test-facts")
    memory.index_job(dict(job))
    context = memory.context("Python SQL ETL")
    check("memory: facts retrieved for letter", bool(context["facts"]), str(len(context["facts"])))
    kinds_before = CALLS["count"]
    CURRENT["title"], CURRENT["company"] = job["title"], job["company"]
    result = generate_letter(db, settings, profile, resume, job, llm=LLM(db, load_settings()),
                             force=True, memory=memory)
    check("llm: letter via DeepSeek path", result["source"] == "deepseek", result["source"])
    prompt_blob = json.dumps(CALLS["bodies"][-3:], ensure_ascii=False)
    check("memory: prompt contains provenance block", "РЕЛЕВАНТНАЯ ПАМЯТЬ" in prompt_blob or "[F1]" in prompt_blob)
    check("memory: unverified fact not sent", "kubernetes" not in prompt_blob.casefold())
    check("llm: critique ran", "critique" in result["checks"])
    check("llm: assistant text stored", "Здравствуйте" in job["letter"] or "Здравствуйте" in result["letter"])
    check("llm: analysis stored in result", bool(result["analysis"]))
    check("llm: at least 3 calls (analysis/letter/critique)", CALLS["count"] - kinds_before >= 3,
          f"calls={CALLS['count'] - kinds_before}")
    jd = json.loads(json.dumps(result["analysis"], ensure_ascii=False))
    check("llm: analysis JSON parsed", jd.get("role_level") == "junior")

    # Fallback path must still produce a local letter when the key is removed.
    os.environ.pop("JOB_HUNTER_API_KEY", None)
    os.environ.pop("JOB_HUNTER_API_BASE", None)
    os.environ.pop("JOB_HUNTER_MODEL", None)
    llm2 = LLM(db, load_settings())
    check("llm: unavailable without key", not llm2.available)
    fallback = generate_letter(db, settings, profile, resume, job, llm=llm2, force=True)
    check("llm: factual fallback is used", fallback["source"] == "factual_fallback", fallback["source"])
    check("llm: fallback has no invented years", "лет опыта" not in fallback["letter"])
    server.shutdown()
    print(f"\nLLM MOCK: {len(PASS)} проверок пройдено")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\n{exc}")
        sys.exit(1)
