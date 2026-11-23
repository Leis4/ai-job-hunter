"""AI Job Hunter CLI.

Examples
--------
  py -3 jobhunter.py cycle
  py -3 jobhunter.py collect --source trudvsem
  py -3 jobhunter.py serve
  py -3 jobhunter.py login
  py -3 jobhunter.py queue list
  py -3 jobhunter.py messages import inbox_messages.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from core import db as dbm
from core.analytics import dashboard_data, export_all, summary
from core.letters import ensure_letters
from core.llm import LLM
from core.messages import CATEGORY_LABELS, draft_reply, import_messages, list_messages, sync_from_browser
from core.paths import INBOX, ensure_dirs
from core.pipeline import ingest_inbox, run_cycle
from core.queue import (approve, build_queue, export_queue_csv, list_applications, queue_stats,
                        send_application, send_batch)
from core.resume import (VARIANTS, confirm_items, diff_variants, export_variant, facts_pack,
                         import_resume, load_resume, save_resume, sync_from_profile,
                         variant_summary)
from core.scoring import rank_pending, train_calibration
from core.settings import load_profile, load_settings
from core.sources import SOURCES, collect_all
from core.text import clean_text


def _db():
    return dbm.init_db()


def print_cycle(report: dict) -> None:
    steps = report.get("steps", {})
    print(f"Цикл: {'OK' if report.get('ok') else 'ОШИБКА'} · {report.get('duration_sec')} с")
    inbox = steps.get("inbox") or {}
    print(f"  inbox: файлов {inbox.get('files', 0)}, новых {inbox.get('added', 0)}")
    for name, data in (steps.get("sources") or {}).items():
        line = f"  {name}: {data.get('status')}"
        if data.get("found") is not None:
            line += f", найдено {data.get('found')}, новых {data.get('added')}"
        if data.get("error"):
            line += f" — {data['error']}"
        if data.get("note"):
            line += f" ({data['note']})"
        print(line)
    cheap = steps.get("rank_cheap") or {}
    deep = steps.get("rank_deep") or {}
    print(f"  скоринг: {deep.get('scored', cheap.get('scored', 0))} вакансий, "
          f"прошли {deep.get('eligible', cheap.get('eligible', 0))}")
    letters = steps.get("letters") or {}
    if not letters.get("skipped"):
        print(f"  письма: {letters.get('drafted', 0)} (LLM {letters.get('llm', 0)}, "
              f"локально {letters.get('fallback', 0)})")
    memory = steps.get("memory") or {}
    if memory:
        stats = memory.get("stats") or {}
        print(f"  память: фактов +{memory.get('facts', 0)}, чанков +{memory.get('job_chunks', 0)}, "
              f"векторов {stats.get('vectors', 0)} (всего {stats.get('documents', 0)})")
    queue = steps.get("queue") or {}
    if queue:
        print(f"  очередь: +{queue.get('queued', 0)}")
    s = report.get("summary") or {}
    if s:
        print(f"  итог: вакансий {s.get('jobs')}, откликов {s.get('sent')}, "
              f"ответов {s.get('replied')}, приглашений {s.get('invited')}")
    if report.get("error"):
        print(f"  ОШИБКА: {report['error']}")


def cmd_cycle(args) -> int:
    settings = load_settings()
    report = run_cycle(sources=args.source or None, force_sources=args.force,
                       draft=not args.no_letters, do_queue=not args.no_queue,
                       monitor=args.monitor, do_train=args.train, settings=settings,
                       progress=lambda step, data: print(f"  · {step}"))
    print_cycle(report)
    llm = LLM(_db(), settings)
    print(f"LLM: {llm.status()}")
    return 0 if report.get("ok") else 1


def cmd_auto(args) -> int:
    from core.automation import run_auto, status as auto_status
    settings = load_settings()
    if args.status:
        print(json.dumps(auto_status(_db(), settings), ensure_ascii=False, indent=2))
        return 0
    report = run_auto(force=args.force, settings=settings)
    print(json.dumps(report, ensure_ascii=False, indent=2)[:5000])
    return 0 if report.get("ok") or report.get("skipped") else 1


def cmd_collect(args) -> int:
    settings = load_settings()
    db = _db()
    report = collect_all(db, settings, force=args.force, only=[args.source] if args.source else None)
    for name, data in report.items():
        if name.startswith("_"):
            print(f"дедупликация: {json.dumps(data, ensure_ascii=False)}")
            continue
        print(f"{name}: {json.dumps(data, ensure_ascii=False)}")
    result = rank_pending(db, settings, load_profile(), sync_from_profile(load_resume()))
    print(f"Оценено: {result['scored']}, прошли: {result['eligible']}")
    return 0


def cmd_rank(args) -> int:
    db = _db()
    result = rank_pending(db, load_settings(), load_profile(), sync_from_profile(load_resume()))
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_letters(args) -> int:
    db = _db()
    settings = load_settings()
    from core.memory import Memory
    result = ensure_letters(db, settings, load_profile(), sync_from_profile(load_resume()),
                            limit=args.limit, memory=Memory(db, settings))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_stats(args) -> int:
    db = _db()
    if args.full:
        print(json.dumps(dashboard_data(db), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(summary(db), ensure_ascii=False, indent=2))
    return 0


def cmd_export(args) -> int:
    db = _db()
    if args.kind == "queue":
        print(export_queue_csv(db))
    else:
        print(export_all(db, "json" if args.kind == "analytics" else "csv"))
    return 0


def cmd_queue(args) -> int:
    db = _db()
    settings = load_settings()
    if args.action == "build":
        print(json.dumps(build_queue(db, settings), ensure_ascii=False))
    elif args.action == "list":
        for row in list_applications(db, state=args.state):
            print(f"#{row['id']} [{row['state']}/{row['channel']}] {row['score']:.0f} "
                  f"{row['title']} — {row['company']}")
        print(json.dumps(queue_stats(db), ensure_ascii=False))
    elif args.action == "approve":
        print(json.dumps(approve(db, args.id), ensure_ascii=False))
    elif args.action == "send":
        result = send_application(db, settings, args.id, confirm=args.confirm)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    elif args.action == "send-all":
        result = send_batch(db, settings, confirm=args.confirm, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    elif args.action == "export":
        print(export_queue_csv(db))
    return 0


def cmd_messages(args) -> int:
    db = _db()
    settings = load_settings()
    if args.action == "import":
        result = import_messages(db, Path(args.file))
        try:
            from core.memory import Memory
            memory = Memory(db, settings)
            for row in db.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 50").fetchall():
                memory.add_feedback(row["job_id"], row["body"], category=row["category"] or "info",
                                    sender=row["sender"] or "", application_id=row["application_id"],
                                    received_at=row["received_at"] or "")
        except Exception:
            pass
        print(json.dumps(result, ensure_ascii=False))
    elif args.action == "sync":
        print(json.dumps(sync_from_browser(db, settings), ensure_ascii=False))
    elif args.action == "list":
        for row in list_messages(db, category=args.category):
            print(f"#{row['id']} [{CATEGORY_LABELS.get(row['category'], row['category'])}] "
                  f"{(row['job_title'] or row['sender'] or '')[:60]}: {clean_text(row['body'])[:120]}")
    elif args.action == "reply":
        result = draft_reply(db, settings, args.id, sync_from_profile(load_resume()), load_profile())
        print(result.get("draft") or result.get("reason"))
    return 0


def cmd_resume(args) -> int:
    if args.action == "import":
        resume = sync_from_profile(import_resume(Path(args.path)))
        save_resume(resume)
        print(f"Импортировано. Навыков: {len(resume.get('skills') or [])}, "
              f"проектов: {len(resume.get('projects') or [])}. Все факты требуют подтверждения.")
    elif args.action == "variants":
        resume = sync_from_profile(load_resume())
        print(json.dumps(variant_summary(resume), ensure_ascii=False, indent=2))
    elif args.action == "export":
        resume = sync_from_profile(load_resume())
        print(json.dumps(export_variant(resume, args.variant), ensure_ascii=False, indent=2))
    elif args.action == "diff":
        resume = sync_from_profile(load_resume())
        print(json.dumps(diff_variants(resume, args.a, args.b), ensure_ascii=False, indent=2))
    elif args.action == "confirm":
        resume = sync_from_profile(load_resume())
        if args.section == "skills":
            indexes = list(args.indexes)
            confirm_items(resume, "skills", indexes)
            save_resume(resume)
            print(f"Подтверждено навыков: {len(indexes)}")
        else:
            print("CLI подтверждает только навыки; проекты и образование — в dashboard.")
    return 0


def cmd_onboarding(args) -> int:
    from core import onboarding
    db = _db()
    if args.action == "extract":
        path = Path(args.file)
        if not path.exists():
            print(f"Файл не найден: {path}")
            return 1
        draft = onboarding.extract_file(path, db=db, settings=load_settings())
        print(f"Метод: {draft.get('method')}; источник: {draft.get('source_name')}")
        print(f"Навыков: {len(draft.get('skills') or [])}, проектов: {len(draft.get('projects') or [])}, "
              f"образование: {len(draft.get('education') or [])}, опыт: {len(draft.get('experience') or [])}")
        print("Проверьте и подтвердите: py -3 jobhunter.py serve -> /onboarding")
        return 0
    if args.action == "show":
        draft = onboarding.load_draft()
        if not draft:
            print("Черновик не найден")
            return 1
        print(json.dumps({k: draft.get(k) for k in
                          ("source_name", "method", "status", "headline", "skills", "education",
                           "projects", "experience", "suggestions")},
                         ensure_ascii=False, indent=2))
        return 0
    if args.action == "discard":
        onboarding.discard()
        print("Черновик удалён")
        return 0
    return 1


def cmd_login(args) -> int:
    from core.browser import login
    print(json.dumps(login(load_settings()), ensure_ascii=False, indent=2))
    return 0


def cmd_browser_check(args) -> int:
    from core.browser import auth_status
    print(json.dumps(auth_status(load_settings(), headless=args.headless), ensure_ascii=False, indent=2))
    return 0


def cmd_calibrate(args) -> int:
    print(json.dumps(train_calibration(_db(), load_settings()), ensure_ascii=False, indent=2))
    return 0


def cmd_models(args) -> int:
    from core.aitunnel import AITunnelRouter
    db = _db()
    settings = load_settings()
    router = AITunnelRouter(db, settings)
    if args.refresh:
        print("Каталог:", json.dumps(router.refresh_catalog(force=True), ensure_ascii=False))
    print("Задачи:")
    for task in ("classify", "deep_analysis", "cover_letter", "critic"):
        model = router.task_model(task)
        info = router.model_info(model) or {}
        print(f"  {task:16} → {model:28} {round(float(info.get('prompt_cost', 0)), 2)} / "
              f"{round(float(info.get('completion_cost', 0)), 2)} ₽ за 1M")
    print(json.dumps(router.ledger(), ensure_ascii=False, indent=2))
    return 0


def cmd_qwen(args) -> int:
    from core.qwen_engine import QwenEngine
    from core.resume import facts_pack, load_resume, sync_from_profile
    from core.settings import load_profile, load_settings
    db = _db()
    settings = load_settings()
    engine = QwenEngine(db, settings)
    if args.action == "status":
        print(json.dumps(engine.status(), ensure_ascii=False, indent=2))
        return 0
    facts = facts_pack(sync_from_profile(load_resume()), load_profile())
    result = engine.selftest(facts)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def cmd_secret(args) -> int:
    import getpass
    from core.secure_store import AITUNNEL_SECRET, aitunnel_key_source, delete_secret, store_secret
    if args.action == "status":
        source = aitunnel_key_source(load_settings())
        print("AITUNNEL key source:", source)
        return 0
    if args.action == "clear":
        delete_secret(AITUNNEL_SECRET)
        print("AITUNNEL key removed from DPAPI storage")
        return 0
    value = os.environ.get("AITUNNEL_API_KEY", "").strip()
    if not value:
        value = getpass.getpass("AITUNNEL API key (input hidden): ").strip()
    if not value:
        print("No key provided")
        return 1
    store_secret(AITUNNEL_SECRET, value)
    print("AITUNNEL key stored with Windows DPAPI")
    return 0


def cmd_memory(args) -> int:
    from core.candidate_memory import CandidateMemory
    from core.memory import Memory
    from core.resume import facts_hash
    from core.scoring import rank_pending
    db = _db()
    settings = load_settings()
    profile, resume = load_profile(), sync_from_profile(load_resume())
    memory_store = CandidateMemory(db, settings)
    if args.action == "status":
        print(json.dumps(memory_store.status(), ensure_ascii=False, indent=2))
        return 0
    if args.action == "setup":
        contact = memory_store.contact()
        labels = [("first_name", "Имя"), ("last_name", "Фамилия"),
                  ("email", "Email"), ("phone", "Телефон"), ("city", "Город")]
        missing = [(key, label) for key, label in labels
                   if not str(contact.get(key) or "").strip()]
        if not missing:
            print("Контактные данные уже заполнены. Используйте настройки для изменения.")
        else:
            print("Заполните только отсутствующие данные. Enter — оставить пустым.")
            for key, label in missing:
                value = input(f"{label}: ").strip()
                if value:
                    contact[key] = value
            memory_store.update_contact(contact)
            print("Контактные данные сохранены в защищённом локальном профиле.")
        print(json.dumps(memory_store.status(), ensure_ascii=False, indent=2))
        return 0
    memory = Memory(db, settings)
    if args.action == "stats":
        print(json.dumps(memory.stats(), ensure_ascii=False, indent=2))
        return 0
    facts = facts_pack(memory_store.resume(), profile)
    memory.sync_facts(memory_store.resume(), profile, facts_hash(facts))
    rank_pending(db, settings, profile, memory_store.resume())
    rows = db.execute(
        """SELECT * FROM jobs WHERE duplicate_of IS NULL AND (eligible=1 OR score>=?)
           ORDER BY score DESC LIMIT ?""",
        (float(settings.get("threshold", 70)) - 12,
         int((settings.get("memory") or {}).get("max_docs_per_cycle", 40)))).fetchall()
    chunks = memory.index_jobs([dict(row) for row in rows])
    embedded = memory.embed_pending(limit=int((settings.get("memory") or {}).get("max_docs_per_cycle", 40)))
    memory.unload_embedder()
    print(json.dumps({"job_chunks": chunks, "embed": embedded, "stats": memory.stats()},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_cost(args) -> int:
    from core.aitunnel import AITunnelRouter
    print(json.dumps(AITunnelRouter(_db(), load_settings()).ledger(), ensure_ascii=False, indent=2))
    return 0


def cmd_sources(args) -> int:
    db = _db()
    settings = load_settings()
    for name, source in SOURCES.items():
        cfg = source.config(settings)
        state = dbm.source_state(db, name)
        print(f"{name}: {'on' if cfg.get('enabled') else 'off'} · "
              f"{state['last_status'] if state else 'never'} · "
              f"{state['last_run_at'] if state else ''} · {source.label}")
    return 0


def cmd_fetch_trudvsem(args) -> int:
    settings = load_settings()
    from core.sources.trudvsem import TrudvsemSource
    from core.sources.base import Http
    ensure_dirs()
    cfg = dict(settings["sources"]["trudvsem"])
    cfg["max_pages"] = max(1, min(args.max_pages, 10))
    cards = TrudvsemSource().search(Http("trudvsem"), [args.query], cfg)
    dest = INBOX / "trudvsem_manual.json"
    dest.write_text(json.dumps([card.to_job() for card in cards], ensure_ascii=False), encoding="utf-8")
    print(f"Получено: {len(cards)} → {dest}")
    return cmd_cycle(argparse.Namespace(source=None, force=False, no_letters=False,
                                        no_queue=False, monitor=False, train=False))


def cmd_demo(args) -> int:
    from core.sources.importer import normalize, read_file
    db = _db()
    settings = load_settings()
    if args.clear:
        db.execute("DELETE FROM applications WHERE job_id IN (SELECT id FROM jobs WHERE source='demo')")
        db.execute("DELETE FROM messages WHERE job_id IN (SELECT id FROM jobs WHERE source='demo')")
        db.execute("DELETE FROM jobs WHERE source='demo'")
        db.commit()
        print("Демо-данные удалены")
        return 0
    fixture = Path(__file__).resolve().parent / "tests" / "fixtures" / "demo_jobs.json"
    added = 0
    for item in read_file(fixture):
        _, created = dbm.upsert_job(db, normalize(item, source="demo"))
        added += int(created)
    profile = load_profile()
    if not profile.get("verified_skills") and args.with_resume:
        from core.resume import parse_resume_text, save_resume as save_r
        text = (Path(__file__).resolve().parent / "tests" / "fixtures" / "demo_resume.txt").read_text(encoding="utf-8")
        demo_resume = parse_resume_text(text, source_name="demo fixture")
        for skill in demo_resume["skills"]:
            skill["verified"] = True
        for project in demo_resume["projects"]:
            project["verified"] = True
        for education in demo_resume["education"]:
            education["verified"] = True
        save_r(demo_resume)
        profile["verified_skills"] = ["Python", "SQL", "Pandas", "Git", "Linux", "Excel", "FastAPI", "Docker", "Statistics"]
        profile["professional_experience_years"] = 1
        profile["verified_projects"] = [{"name": "Учебный ETL-пайплайн", "stack": ["Python", "SQL", "Pandas"],
                                         "description": "Собирает данные из CSV, очищает и загружает в SQLite.",
                                         "verified": True}]
        from core.settings import save_profile
        save_profile(profile)
        print("Демо-профиль загружен (source=demo). Очистить: py -3 jobhunter.py demo --clear")
    resume = sync_from_profile(load_resume())
    rank_pending(db, settings, profile, resume)
    from core.memory import Memory
    from core.resume import facts_hash
    memory = Memory(db, settings)
    memory.sync_facts(resume, profile, facts_hash(facts_pack(resume, profile)))
    rows = db.execute("SELECT * FROM jobs WHERE duplicate_of IS NULL ORDER BY score DESC LIMIT 12").fetchall()
    memory.index_jobs([dict(row) for row in rows])
    if getattr(args, "embed", False):
        memory.embed_pending(limit=20)
    memory.unload_embedder()
    letters = {"drafted": 0}
    if getattr(args, "letters", False):
        letters = ensure_letters(db, settings, profile, resume, limit=6, memory=memory)
    queue = build_queue(db, settings)
    print(f"Демо: добавлено {added}, письма {letters['drafted']}, очередь {queue.get('queued', 0)}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AI Job Hunter — локальный поиск работы")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("cycle", help="полный цикл")
    p.add_argument("--source", action="append", choices=list(SOURCES))
    p.add_argument("--force", action="store_true", help="игнорировать интервалы источников")
    p.add_argument("--no-letters", action="store_true")
    p.add_argument("--no-queue", action="store_true")
    p.add_argument("--monitor", action="store_true", help="читать отклики через браузер")
    p.add_argument("--train", action="store_true", help="обучить калибровку при достаточной выборке")
    p.set_defaults(func=cmd_cycle)

    sub.add_parser("run-once", help="алиас cycle").set_defaults(func=cmd_cycle, source=None,
                                                                 force=False, no_letters=False,
                                                                 no_queue=False, monitor=False, train=False)
    p = sub.add_parser("auto", help="автономный запуск по расписанию")
    p.add_argument("--force", action="store_true", help="игнорировать интервалы")
    p.add_argument("--status", action="store_true", help="покивань состояние автопоиска")
    p.set_defaults(func=cmd_auto)

    p = sub.add_parser("collect"); p.add_argument("--source", choices=list(SOURCES))
    p.add_argument("--force", action="store_true"); p.set_defaults(func=cmd_collect)
    sub.add_parser("rank").set_defaults(func=cmd_rank)
    p = sub.add_parser("letters"); p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_letters)
    p = sub.add_parser("stats"); p.add_argument("--full", action="store_true"); p.set_defaults(func=cmd_stats)
    p = sub.add_parser("export"); p.add_argument("kind", choices=["analytics", "jobs", "queue"])
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("queue")
    p.add_argument("action", choices=["build", "list", "approve", "send", "send-all", "export"])
    p.add_argument("id", nargs="?", type=int); p.add_argument("--state")
    p.add_argument("--confirm", action="store_true", help="реально отправить (иначе dry-run)")
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("messages")
    p.add_argument("action", choices=["import", "sync", "list", "reply"])
    p.add_argument("file", nargs="?"); p.add_argument("id", nargs="?", type=int)
    p.add_argument("--category")
    p.set_defaults(func=cmd_messages)

    p = sub.add_parser("resume")
    resume_sub = p.add_subparsers(dest="action", required=True)
    ri = resume_sub.add_parser("import"); ri.add_argument("path")
    rex = resume_sub.add_parser("export"); rex.add_argument("variant", choices=list(VARIANTS))
    resume_sub.add_parser("variants")
    rd = resume_sub.add_parser("diff"); rd.add_argument("a", choices=list(VARIANTS))
    rd.add_argument("b", choices=list(VARIANTS))
    rc = resume_sub.add_parser("confirm"); rc.add_argument("section", choices=["skills"])
    rc.add_argument("indexes", nargs="+", type=int)
    p.set_defaults(func=cmd_resume)

    sub.add_parser("login").set_defaults(func=cmd_login)
    p = sub.add_parser("onboarding")
    p.add_argument("action", choices=["extract", "show", "discard"])
    p.add_argument("file", nargs="?")
    p.set_defaults(func=cmd_onboarding)
    p = sub.add_parser("browser-check"); p.add_argument("--headless", action="store_true")
    p.set_defaults(func=cmd_browser_check)
    sub.add_parser("calibrate").set_defaults(func=cmd_calibrate)
    p = sub.add_parser("models"); p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_models)
    p = sub.add_parser("qwen", help="локальная и облачная Qwen")
    p.add_argument("action", choices=["status", "test"])
    p.set_defaults(func=cmd_qwen)
    p = sub.add_parser("secret", help="DPAPI-хранилище ключей")
    p.add_argument("service", choices=["aitunnel"])
    p.add_argument("action", choices=["set", "status", "clear"])
    p.set_defaults(func=cmd_secret)
    p = sub.add_parser("memory"); p.add_argument("action", choices=["stats", "rebuild", "setup", "status"])
    p.set_defaults(func=cmd_memory)
    sub.add_parser("cost").set_defaults(func=cmd_cost)
    sub.add_parser("sources").set_defaults(func=cmd_sources)

    p = sub.add_parser("fetch-trudvsem"); p.add_argument("--query", default="python")
    p.add_argument("--max-pages", type=int, default=2); p.set_defaults(func=cmd_fetch_trudvsem)

    p = sub.add_parser("import"); p.add_argument("file"); p.set_defaults(func=_cmd_import)
    p = sub.add_parser("demo", help="загрузить синтетические демо-вакансии (source=demo)")
    p.add_argument("--clear", action="store_true"); p.add_argument("--with-resume", action="store_true", default=True)
    p.add_argument("--embed", action="store_true", help="also build embeddings (may download a model)")
    p.add_argument("--letters", action="store_true", help="also draft cover letters (may call the configured LLM)")
    p.set_defaults(func=cmd_demo)
    p = sub.add_parser("serve"); p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true"); p.set_defaults(func=_cmd_serve)
    p = sub.add_parser("daemon"); p.add_argument("--hours", type=float, default=6)
    p.set_defaults(func=_cmd_daemon)

    args = parser.parse_args(argv)
    return args.func(args)


def _cmd_import(args) -> int:
    from core.sources.importer import normalize, read_file
    db = _db()
    added = 0
    for item in read_file(Path(args.file)):
        job = normalize(item)
        if job:
            try:
                _, created = dbm.upsert_job(db, job)
                added += int(created)
            except ValueError:
                continue
    print(f"Импортировано новых: {added}")
    rank_pending(db, load_settings(), load_profile(), sync_from_profile(load_resume()))
    return 0


def _cmd_serve(args) -> int:
    from web.server import serve
    serve(args.port, open_browser=not args.no_browser)
    return 0


def _cmd_daemon(args) -> int:
    print(f"Локальный цикл каждые {args.hours:g} ч. Ctrl+C — остановка.")
    while True:
        try:
            print_cycle(run_cycle())
        except Exception as exc:
            print("Ошибка цикла:", type(exc).__name__, str(exc)[:200])
        time.sleep(max(120, args.hours * 3600))


if __name__ == "__main__":
    sys.exit(main())
