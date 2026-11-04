"""
Tests for overview briefs and the brief archive: the overview evidence pack
(evidence.py --overview), check.py's overview rules, which brief an account's
brief.html shows, the archive pages under /<slug>/briefs, publish.py's choice
of draft, and accounts.py --overview.

    python3 -m unittest discover tests

Builds a two-crawl lake a week apart with the real collector (its date
patched) for the fictional Dunder Mifflin: a platform role and a data
director stand, an accountant's role closes, and a data engineer who also
asks for Kafka opens. The briefs in it are hand-written fixtures, not model
output. No network.
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import date
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *a, **k: None))
sys.path.insert(0, os.path.join(ROOT, "infra", "src", "collector"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, ".claude", "skills", "tick-brief", "scripts"))
import accounts  # noqa: E402
import app  # noqa: E402
import check  # noqa: E402
import evidence  # noqa: E402
import publish  # noqa: E402
import tick_insights_report as report  # noqa: E402

SLUG = "dunder-mifflin"
CONFIG = {"company_name": "Dunder Mifflin", "company_slug": SLUG, "date_trust": "high",
          "description_field": "desc",
          "schema_map": {"posting_id": "id", "title": "title", "location": "loc", "url": "url", "team": "team"}}
ROLES = {
    "A": ("Platform Engineer", "IT", "Run Kubernetes and Terraform on AWS with Python."),
    "B": ("Director of Data Engineering", "IT", "Lead the Snowflake and dbt platform; Python and Airflow."),
    "C": ("Accountant", "Finance", "Reconcile ledgers every month."),
    "D": ("Data Engineer", "IT", "Build Airflow pipelines from Kafka into Snowflake with Python and dbt."),
}
READER = {"role": "account_executive", "sells_for": "Initech", "offer": "Managed Snowflake",
          "focus_terms": ["snowflake"]}
FIRST, SECOND = "2026-10-19", "2026-10-26"


def crawl(day: date, pids: str) -> None:
    class Today(date):
        @classmethod
        def today(cls):
            return day
    app.fetch_all = lambda config: ([{"id": p, "title": ROLES[p][0], "team": ROLES[p][1], "loc": "Scranton, PA",
                                      "url": f"https://dm.example/{p}",
                                      "desc": f"<p><b>Responsibilities</b></p><ul><li>{ROLES[p][2]}</li></ul>"}
                                     for p in pids], len(pids))
    with mock.patch.object(app, "date", Today):
        app.collect_company(CONFIG)


def overview_brief(ev: dict, **over) -> dict:
    b = {"prompt_version": 1, "kind": "overview", "company_slug": SLUG,
         "persona_ref": (ev.get("persona") or {}).get("ref"), "as_of": ev["as_of"],
         "paragraphs": ["The data platform runs on Snowflake and dbt [[STACK]], owned by a director seat [[B]].",
                        "Platform work sits on Kubernetes and Terraform in AWS [[A]].",
                        "Finance hires alongside [[BOARD]]."],
         "action_items": [{"kind": "outreach", "title": "Approach the data director",
                           "why": "One leadership seat owns the data platform [[B]].",
                           "quote": {"text": "Lead the Snowflake and dbt platform", "source": "B"},
                           "steps": ["Map the Snowflake estate [[B]]", "Ask about Airflow ownership [[B]]"]}]}
    b.update(over)
    return b


def weekly_brief(ev: dict, **over) -> dict:
    b = {"prompt_version": 9, "company_slug": SLUG, "persona_ref": (ev.get("persona") or {}).get("ref"),
         "week": {"start": ev["window"]["start"], "end": ev["window"]["end"]}, "quiet_week": False,
         "paragraphs": ["A data engineer opened [[D]].", "An accountant's posting came down [[C]]."],
         "action_items": []}
    b.update(over)
    return b


def put(lake: str, part: str, brief: dict, ev: dict) -> None:
    d = os.path.join(lake, "briefs", f"company={SLUG}", part)
    os.makedirs(d, exist_ok=True)
    for name, body in (("brief.json", brief), ("evidence.json", ev)):
        with open(os.path.join(d, name), "w") as f:
            json.dump(body, f)


class LakeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lake = tempfile.mkdtemp(prefix="tick-test-lake-")
        cls._saved = (app.LOCAL_DIR, app.fetch_all)
        app.LOCAL_DIR = cls.lake
        crawl(date(2026, 10, 19), "ABC")
        crawl(date(2026, 10, 26), "ABD")

    @classmethod
    def tearDownClass(cls):
        app.LOCAL_DIR, app.fetch_all = cls._saved
        shutil.rmtree(cls.lake, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(os.path.join(self.lake, "briefs"), ignore_errors=True)

    def pages(self) -> dict[str, str]:
        io = report.open_lake(self.lake)
        return report.render_brief_pages(io, report.load_lake(self.lake, SLUG, io), SLUG, "Dunder Mifflin")


class OverviewPack(LakeCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ev = evidence.build_overview(cls.lake, SLUG)
        cls.first = evidence.build_overview(cls.lake, SLUG, as_of=FIRST, reader=READER)

    def terms(self, ev):
        return {r["term"]: r for rows in ev["stack"]["by_category"].values() for r in rows}

    def test_it_describes_one_crawl(self):
        self.assertEqual((self.ev["kind"], self.ev["as_of"]), ("overview", SECOND))
        self.assertEqual(self.ev["tracking"], {"first_crawl": FIRST, "crawls": 2, "date_trust": "high"})
        self.assertEqual(self.first["as_of"], FIRST)
        self.assertEqual([p["id"] for p in self.ev["postings"]], ["B", "D", "A"])  # leadership, then stack-heaviest

    def test_the_stack_is_counted_in_code(self):
        t = self.terms(self.ev)
        self.assertEqual((t["snowflake"]["postings"], t["snowflake"]["roles"]), (2, ["B", "D"]))
        self.assertEqual(t["snowflake"]["teams"], [{"team": "IT", "roles": 2}])
        self.assertFalse(t["python"]["baseline"])   # 3 of 3, but too few roles for a share to mean much
        self.assertIn({"terms": ["dbt", "snowflake"], "postings": 2}, self.ev["stack"]["co_occurring"])
        self.assertIn({"term": "kafka", "roles": ["D"]}, self.ev["stack"]["rare"])
        self.assertEqual([r["term"] for r in self.ev["stack"]["since_first_crawl"]], ["kafka"])
        self.assertEqual([r["id"] for r in self.ev["stack"]["leadership_stack"]], ["B"])

    def test_only_tech_and_leadership_roles_keep_their_text(self):
        first = {p["id"]: p for p in self.first["postings"]}
        self.assertIn("Snowflake", first["B"]["description"])
        self.assertIsNone(first["C"]["description"])
        self.assertEqual(first["C"]["team"], "Finance")

    def test_one_crawl_compares_nothing(self):
        self.assertEqual(self.first["stack"]["since_first_crawl"], [])
        self.assertIn("One crawl so far", " ".join(self.first["data_quality_flags"]))
        self.assertIsNone(self.first["persona"]["focus_terms"][0]["new_to_board"])
        self.assertEqual(self.first["persona"]["focus_terms"][0]["open_roles"], 1)

    def test_check_passes_a_good_overview(self):
        fails, _, _ = check.check(json.dumps(overview_brief(self.ev)), self.ev)
        self.assertEqual(fails, [])

    def test_check_refuses_what_the_overview_prompt_forbids(self):
        def fails(**over):
            return " | ".join(check.check(json.dumps(overview_brief(self.ev, **over)), self.ev)[0])
        self.assertIn("as_of", fails(as_of=FIRST))
        self.assertIn("paragraphs", fails(paragraphs=["Only one [[BOARD]]."]))
        self.assertIn("not in the evidence pack", fails(paragraphs=["x [[C]].", "y [[A]].", "z [[B]]."]))
        bad_quote = overview_brief(self.ev)["action_items"]
        bad_quote[0]["quote"] = {"text": "Lead the Oracle platform", "source": "B"}
        self.assertIn("not verbatim", fails(action_items=bad_quote))
        self.assertIn("kind", fails(kind="weekly"))
        # a weekly brief is no overview, and an overview is no weekly brief
        weekly_ev = evidence.build(self.lake, SLUG)
        self.assertTrue(check.check(json.dumps(weekly_brief(weekly_ev)), self.ev)[0])
        self.assertTrue(check.check(json.dumps(overview_brief(self.ev)), weekly_ev)[0])

    def test_publish_and_render_agree_on_where_each_kind_lives(self):
        weekly_ev = evidence.build(self.lake, SLUG)
        self.assertEqual(check.s3_prefix(self.ev), f"briefs/company={SLUG}/overview={SECOND}")
        self.assertEqual(check.s3_prefix(weekly_ev), f"briefs/company={SLUG}/week={SECOND}")
        for ev in (self.ev, weekly_ev):
            m = report._BRIEF_RE.search(check.s3_prefix(ev) + "/brief.json")
            self.assertEqual((m.group(1), m.group(3)), (SLUG, SECOND))


class Archive(LakeCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ov = evidence.build_overview(cls.lake, SLUG, as_of=FIRST, reader=READER)
        cls.wk = evidence.build(cls.lake, SLUG)

    def test_no_brief_means_no_archive(self):
        self.assertEqual(list(self.pages()), [f"{SLUG}/brief.html"])
        self.assertIn("No brief yet", self.pages()[f"{SLUG}/brief.html"])

    def test_a_new_account_shows_its_overview(self):
        put(self.lake, f"overview={FIRST}", overview_brief(self.ov), self.ov)
        page = self.pages()[f"{SLUG}/brief.html"]
        self.assertIn("Account overview", page)
        self.assertIn("written for Account executive, Initech", page)
        self.assertIn(f'href="/{SLUG}/briefs">Brief archive (1)', page)

    def test_the_weekly_brief_takes_over_and_both_are_archived(self):
        put(self.lake, f"overview={FIRST}", overview_brief(self.ov), self.ov)
        put(self.lake, f"week={SECOND}", weekly_brief(self.wk), self.wk)
        pages = self.pages()
        self.assertEqual(sorted(pages), sorted([
            f"{SLUG}/brief.html", f"{SLUG}/briefs/index.html",
            f"{SLUG}/briefs/weekly-{SECOND}/index.html", f"{SLUG}/briefs/overview-{FIRST}/index.html"]))
        # the publisher's half of the site bucket: nothing outside <slug>/
        self.assertTrue(all(k.startswith(f"{SLUG}/") for k in pages))
        self.assertIn("Account brief", pages[f"{SLUG}/brief.html"])
        index = pages[f"{SLUG}/briefs/index.html"]
        self.assertLess(index.index(f"weekly-{SECOND}"), index.index(f"overview-{FIRST}"))
        self.assertIn("A data engineer opened.", index)
        old = pages[f"{SLUG}/briefs/overview-{FIRST}/index.html"]
        self.assertIn("An earlier brief", old)
        self.assertIn(f'href="/{SLUG}/briefs/weekly-{SECOND}">Week to {SECOND} &rarr;', old)
        self.assertIn("This is the account's current brief", pages[f"{SLUG}/briefs/weekly-{SECOND}/index.html"])

    def test_an_archived_brief_for_another_reader_does_not_name_them(self):
        put(self.lake, f"overview={FIRST}", overview_brief(self.ov), self.ov)
        put(self.lake, f"week={SECOND}", weekly_brief(self.wk), self.wk)    # default reader now
        pages = self.pages()
        self.assertNotIn("Initech", pages[f"{SLUG}/briefs/overview-{FIRST}/index.html"])
        self.assertNotIn("Initech", pages[f"{SLUG}/briefs/index.html"])

    def test_an_archived_brief_for_the_same_reader_still_does(self):
        wk = evidence.build(self.lake, SLUG, reader=READER)
        put(self.lake, f"overview={FIRST}", overview_brief(self.ov), self.ov)
        put(self.lake, f"week={SECOND}", weekly_brief(wk), wk)
        self.assertIn("written for Account executive, Initech",
                      self.pages()[f"{SLUG}/briefs/overview-{FIRST}/index.html"])

    def test_an_old_brief_links_a_role_closed_since_to_its_archive(self):
        put(self.lake, f"overview={FIRST}", overview_brief(
            self.ov, paragraphs=["Finance hires [[C]].", "Data [[B]].", "Platform [[A]]."]), self.ov)
        put(self.lake, f"week={SECOND}", weekly_brief(self.wk), self.wk)
        old = self.pages()[f"{SLUG}/briefs/overview-{FIRST}/index.html"]
        self.assertIn(f'href="{report.archive_href(SLUG, "C")}"', old)
        self.assertIn("closed since, on 2026-10-26", old)

    def test_on_one_date_the_weekly_brief_is_current(self):
        ov = evidence.build_overview(self.lake, SLUG)
        put(self.lake, f"overview={SECOND}", overview_brief(ov), ov)
        put(self.lake, f"week={SECOND}", weekly_brief(self.wk), self.wk)
        entries = report.list_briefs(report.open_lake(self.lake), SLUG)
        self.assertEqual([e["id"] for e in entries], [f"weekly-{SECOND}", f"overview-{SECOND}"])
        self.assertEqual(report.current_brief(entries)["id"], f"weekly-{SECOND}")

    def test_accounts_lists_new_accounts_for_an_overview_until_one_is_published(self):
        self.assertEqual(accounts.accounts(self.lake, overview=True), [SLUG])
        put(self.lake, f"overview={FIRST}", overview_brief(self.ov), self.ov)
        self.assertEqual(accounts.accounts(self.lake, overview=True), [])
        self.assertEqual(accounts.accounts(self.lake), [SLUG])


class PublishPicksTheKind(unittest.TestCase):
    def test_weekly_and_overview_drafts_are_told_apart(self):
        d = tempfile.mkdtemp()
        try:
            for name in (FIRST, SECOND, f"overview-{FIRST}"):
                os.makedirs(os.path.join(d, name))
                for f in ("brief.json", "evidence.json"):
                    open(os.path.join(d, name, f), "w").close()
            self.assertEqual(publish.pick_week(d, None), SECOND)
            self.assertEqual(publish.pick_week(d, None, overview=True), f"overview-{FIRST}")
            self.assertEqual(publish.pick_week(d, FIRST, overview=True), f"overview-{FIRST}")
            self.assertIsNone(publish.pick_week(d, SECOND, overview=True))
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
