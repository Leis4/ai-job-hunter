"""Unit tests for deterministic parsing and adapters. Run: py -3 -m unittest tests.unit_core -v"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.sources.feed import parse_feed  # noqa: E402
from core.sources.hh_api import normalize as hh_normalize  # noqa: E402
from core.sources.trudvsem import normalize as trudvsem_normalize  # noqa: E402
from core.text import (  # noqa: E402
    analyze_job_text, canonical_mode, canonical_skill, detect_work_mode, extract_skills,
    jaccard, parse_experience_years, parse_salary, scrub_contacts, seniority_level,
    token_overlap,
)
from core.db import fingerprint_for, normalize_url  # noqa: E402
from core.letters import unsupported_claims  # noqa: E402
from core.resume import parse_resume_text  # noqa: E402


class TestExperience(unittest.TestCase):
    def test_numeric(self):
        self.assertEqual(parse_experience_years({"experience_years": 3}), 3.0)

    def test_no_experience(self):
        self.assertEqual(parse_experience_years({"description": "Опыт не требуется"}), 0.0)
        self.assertEqual(parse_experience_years({"description": "Можно без опыта"}), 0.0)

    def test_from_text(self):
        self.assertEqual(parse_experience_years({"description": "Требуется опыт от 2 лет"}), 2.0)
        self.assertEqual(parse_experience_years({"experience_text": "3+ года опыта"}), 3.0)

    def test_unknown_is_none(self):
        self.assertIsNone(parse_experience_years({"description": "Интересные задачи"}))
        self.assertIsNone(parse_experience_years({"description": "Опыт работы с данными будет полезен"}))


class TestSalary(unittest.TestCase):
    def test_range(self):
        low, high, cur = parse_salary("от 100 000 до 150 000 ₽")
        self.assertEqual((low, high, cur), (100000, 150000, "RUB"))

    def test_single(self):
        low, high, _ = parse_salary("от 90000")
        self.assertEqual((low, high), (90000, None))

    def test_currency(self):
        _, _, cur = parse_salary("$2000")
        self.assertEqual(cur, "USD")

    def test_structured(self):
        low, high, _ = parse_salary("", min_value="55000", max_value="75000")
        self.assertEqual((low, high), (55000, 75000))


class TestSkills(unittest.TestCase):
    def test_required_vs_nice(self):
        text = "Требуется Python и SQL. Будет плюсом Docker. Желательно знание Kubernetes."
        signals = analyze_job_text(text, [])
        by = {s.name: s.requirement for s in signals.skills}
        self.assertEqual(by["Python"], "required")
        self.assertEqual(by["SQL"], "required")
        self.assertEqual(by["Docker"], "nice")
        self.assertEqual(by["Kubernetes"], "nice")

    def test_word_boundaries(self):
        found = extract_skills("Мы пишем на Django, а не на Go. Django ORM.")
        self.assertIn("Django", found)
        self.assertNotIn("Go", found)

    def test_canonical_alias(self):
        self.assertEqual(canonical_skill("postgres"), "PostgreSQL")
        self.assertEqual(canonical_skill("машинное обучение"), "ML")

    def test_fuzzy_and_morphology(self):
        self.assertEqual(canonical_skill("Kubernetis"), "Kubernetes")
        self.assertEqual(canonical_skill("Postgress"), "PostgreSQL")
        self.assertEqual(canonical_skill("Java"), "Java")
        self.assertEqual(seniority_level("Стажёр в команду разработки"), "intern")
        self.assertEqual(seniority_level("Ведущий Python-разработчик"), "lead")
        self.assertEqual(seniority_level("Senior Python Developer"), "senior")
        self.assertEqual(seniority_level("Python-разработчик"), "unspecified")
        self.assertTrue(token_overlap({"разработ"}, {"разработк"}))
        self.assertFalse(token_overlap({"go"}, {"django"}))


class TestModes(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(detect_work_mode("удалённая работа"), "remote")
        self.assertEqual(detect_work_mode("гибрид 3/2"), "hybrid")
        self.assertEqual(canonical_mode("Удалённо"), "remote")
        self.assertEqual(canonical_mode("Hybrid"), "hybrid")


class TestPrivacy(unittest.TestCase):
    def test_scrub(self):
        text = "Пишите на hr@example.com или +7 (999) 123-45-67, telegram @recruiter_hr"
        cleaned = scrub_contacts(text)
        self.assertNotIn("hr@example.com", cleaned)
        self.assertNotIn("999", cleaned)
        self.assertNotIn("recruiter_hr", cleaned)


class TestDedupe(unittest.TestCase):
    def test_url(self):
        self.assertEqual(normalize_url("https://hh.ru/vacancy/1/?utm_source=x&from=yyy"),
                         "https://hh.ru/vacancy/1")

    def test_fingerprint_ignores_legal_form(self):
        a = fingerprint_for("Python-разработчик", 'ООО "Ромашка"')
        b = fingerprint_for("Python-разработчик", "Ромашка")
        self.assertEqual(a, b)


class TestAdapters(unittest.TestCase):
    def test_hh(self):
        card = hh_normalize({
            "id": "123", "name": "Python-разработчик", "alternate_url": "https://hh.ru/vacancy/123",
            "employer": {"name": "Demo"}, "area": {"name": "Москва"},
            "salary": {"from": 100000, "to": 150000, "currency": "RUR"},
            "experience": {"id": "between1And3", "name": "От 1 года до 3 лет"},
            "schedule": {"name": "Удалённая работа"},
            "snippet": {"requirement": "Python, SQL", "responsibility": "Разработка"},
            "key_skills": [{"name": "Python"}, {"name": "SQL"}],
        })
        self.assertEqual(card.experience_years, 1.0)
        self.assertEqual(card.work_mode, "remote")
        self.assertEqual(card.currency, "RUR")
        self.assertIn("Python", card.skills)

    def test_trudvsem_strips_contacts(self):
        card = trudvsem_normalize({"vacancy": {
            "id": "v1", "job-name": "Аналитик", "vac_url": "https://trudvsem.ru/vacancy/card/1",
            "company": {"name": "Demo", "email": "hr@example.com"},
            "region": {"name": "Город Москва"},
            "duty": "Писать SQL. Контакт: hr@example.com",
            "requirements": "Опыт от 1 года",
            "requirement": {"experience": 3},
            "salary_min": "90000", "salary_max": "120000", "skills": "['SQL', 'Python']",
            "creation-date": "2026-11-01",
        }})
        self.assertEqual(card.experience_years, 3.0)
        self.assertNotIn("hr@example.com", card.description)
        self.assertEqual(card.skills, ["SQL", "Python"])
        self.assertEqual((card.salary_min, card.salary_max), (90000, 120000))

    def test_feed_rss(self):
        xml = b"""<?xml version='1.0'?><rss><channel><item>
        <title>Junior Python</title><link>https://example.org/1</link>
        <description>Python, SQL, remote</description><pubDate>Mon, 10 Nov 2026 10:00:00 +0000</pubDate>
        <guid>1</guid></item></channel></rss>"""
        cards = parse_feed(xml, "https://example.org/feed")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].title, "Junior Python")
        self.assertEqual(cards[0].work_mode, "remote")
        self.assertEqual(cards[0].published_at, "2026-11-10")

    def test_feed_json(self):
        payload = json.dumps({"items": [{"id": "a", "title": "Data Intern", "url": "https://example.org/a",
                                         "content_text": "SQL and Python"}]}).encode()
        cards = parse_feed(payload, "https://example.org/feed.json")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].source_id, "https://example.org/feed.json#a")


class TestResumeImport(unittest.TestCase):
    def setUp(self):
        text = (ROOT / "tests" / "fixtures" / "demo_resume.txt").read_text(encoding="utf-8")
        self.resume = parse_resume_text(text, source_name="unit")

    def test_everything_unverified(self):
        self.assertTrue(self.resume["skills"])
        self.assertTrue(all(not s["verified"] for s in self.resume["skills"]))
        self.assertTrue(all(not e["verified"] for e in self.resume["experience"]))

    def test_experience_not_inflated_by_education(self):
        self.assertEqual(self.resume["total_experience_months"], 12)
        self.assertEqual(len(self.resume["experience"]), 1)
        self.assertIn("Разрабатывал внутренние скрипты", self.resume["experience"][0]["description"])

    def test_projects_grouped(self):
        projects = self.resume["projects"]
        self.assertEqual(len(projects), 2, str(projects))
        self.assertEqual(projects[0]["name"], "Учебный ETL-пайплайн")
        self.assertIn("Pandas", projects[0]["stack"])
        self.assertIn("SQLite", projects[0]["description"])

    def test_education_field(self):
        self.assertEqual(len(self.resume["education"]), 1)
        self.assertIn("Прикладная информатика", self.resume["education"][0]["field"])


class TestClaims(unittest.TestCase):
    facts = {"verified_skills": ["Python", "SQL"], "total_experience_months": 12, "projects": []}

    def test_supported(self):
        self.assertEqual(unsupported_claims("Работал с Python и SQL.", self.facts), [])

    def test_skill_invention(self):
        problems = unsupported_claims("Отлично знаю Kubernetes и Rust.", self.facts)
        self.assertTrue(any("Kubernetes" in p for p in problems))

    def test_years_invention(self):
        problems = unsupported_claims("У меня 5 лет опыта.", self.facts)
        self.assertTrue(problems)

    def test_similarity(self):
        self.assertGreater(jaccard("Python SQL данные проект", "Python SQL данные проект"), 0.99)
        self.assertLess(jaccard("Python SQL", "маркетинг продажи курьер"), 0.1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
