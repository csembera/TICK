"""
Tests for an account's first crawl: it reports from day one, with a
dashboard of the open board, but no brief until a second crawl gives a
week of movement to write about.

    python3 -m unittest discover tests

Drives the real collect_company() against a temporary local lake
(LOCAL_OUTPUT_DIR mode), with fetch_all replaced by a fake board. The
company is the fictional Dunder Mifflin.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *a, **k: None))
sys.path.insert(0, os.path.join(ROOT, "infra", "src", "collector"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import app  # noqa: E402
import tick_insights_report as report  # noqa: E402

SLUG = "dunder-mifflin"
CONFIG = {"company_name": "Dunder Mifflin", "company_slug": SLUG, "date_trust": "high",
          "schema_map": {"posting_id": "id", "title": "title", "location": "loc", "url": "url"}}
TODAY = date.today().isoformat()
BOARD = {"A": "Sales Rep", "B": "Accountant", "C": "Receptionist"}
ACCOUNTS_PY = os.path.join(ROOT, ".claude", "skills", "tick-brief", "scripts", "accounts.py")


class FirstCrawl(unittest.TestCase):
    def setUp(self):
        self.lake = tempfile.mkdtemp(prefix="tick-test-lake-")
        self._saved = (app.LOCAL_DIR, app.fetch_all)
        app.LOCAL_DIR = self.lake
        app.fetch_all = lambda config: ([{"id": p, "title": t, "loc": "Scranton, PA",
                                          "url": f"https://dm.example/{p}"}
                                         for p, t in BOARD.items()], len(BOARD))
        app.collect_company(CONFIG)

    def tearDown(self):
        app.LOCAL_DIR, app.fetch_all = self._saved
        shutil.rmtree(self.lake, ignore_errors=True)

    def account(self):
        accounts = report.build_accounts([], report.open_lake(self.lake))
        self.assertEqual([a["slug"] for a in accounts], [SLUG])
        return accounts[0]

    def dashboard(self):
        io = report.open_lake(self.lake)
        return report.render_dashboard(report.load_lake(self.lake, SLUG, io), SLUG)

    def briefed(self):
        out = subprocess.run([sys.executable, ACCOUNTS_PY, self.lake],
                             capture_output=True, text=True, check=True).stdout
        return out.split()

    def add_earlier_snapshot(self, days_back=14):
        """Copy today's snapshot to an earlier date, as a crawl that day would
        have left it, so the lake holds two crawls to compare."""
        then = (date.today() - timedelta(days=days_back)).isoformat()
        src = os.path.join(self.lake, f"derived/tech_stack/company={SLUG}/dt={TODAY}/tech_stack.json")
        with open(src) as f:
            snap = json.load(f)
        snap["captured_dt"] = then
        dst = src.replace(f"dt={TODAY}", f"dt={then}")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w") as f:
            json.dump(snap, f)
        return then

    def test_first_crawl_is_reporting_with_pages(self):
        a = self.account()
        self.assertEqual(a["status"], "Reporting")
        self.assertTrue(a["has_pages"])
        self.assertFalse(a["has_baseline"])
        self.assertIsNone(a["delta"])

    def test_first_crawl_dashboard_shows_the_open_board(self):
        page = self.dashboard()
        for title in BOARD.values():
            self.assertIn(title, page)
        self.assertIn(f"First crawl {TODAY}", page)
        self.assertIn("From the next crawl", page)
        self.assertIn("On the board", page)
        # nothing that is a change between two crawls
        self.assertNotIn("Adopting", page)
        self.assertNotIn(f"&ndash; {TODAY}", page)
        self.assertNotIn(f"&rarr; {TODAY}", page)

    def test_first_crawl_gets_no_brief(self):
        self.assertEqual(self.briefed(), [])

    def test_second_crawl_compares(self):
        then = self.add_earlier_snapshot()
        a = self.account()
        self.assertEqual(a["status"], "Reporting")
        self.assertTrue(a["has_baseline"])
        self.assertEqual(a["prev_dt"], then)
        page = self.dashboard()
        self.assertIn(f"{then} &ndash; {TODAY}", page)   # the window, labelled with its real dates
        self.assertIn("Adopting", page)
        self.assertNotIn("From the next crawl", page)
        self.assertEqual(self.briefed(), [SLUG])


if __name__ == "__main__":
    unittest.main()
