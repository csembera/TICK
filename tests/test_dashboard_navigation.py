"""
Tests for what the dashboard's tables are searched, filtered and sorted on:
the data attributes on each row, the descriptions shipped for search, and
the stack terms that filter Open roles when clicked.

    python3 -m unittest discover tests

Drives the real collect_company() against a temporary local lake
(LOCAL_OUTPUT_DIR mode), twice on one day so one posting closes into the
archive, then renders the dashboard. The script that reads these attributes
runs only in a browser; this checks the page hands it the right data. The
company is the fictional Dunder Mifflin.
"""

import html
import json
import os
import re
import shutil
import sys
import tempfile
import types
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *a, **k: None))
sys.path.insert(0, os.path.join(ROOT, "infra", "src", "collector"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import app  # noqa: E402
import tick_insights_report as report  # noqa: E402

SLUG = "dunder-mifflin"
CONFIG = {"company_name": "Dunder Mifflin", "company_slug": SLUG, "date_trust": "high",
          "description_field": "desc",
          "schema_map": {"posting_id": "id", "title": "title", "location": "loc", "url": "url"}}
EEO = "<p><b>Equal Opportunity</b></p><p>Dunder Mifflin is an equal opportunity employer.</p>"
BOARD = {
    "A": ("Director of Sales",
          "<p><b>Responsibilities</b></p><ul><li>Grow regional accounts with Python reports.</li></ul>"
          "<p><b>Compensation</b></p><p>The pay range for this role is $90,000 - $110,000 per year.</p>"
          + EEO),
    "B": ("Warehouse Associate",
          "<p><b>Responsibilities</b></p><ul><li>Load trucks for the Scranton branch.</li></ul>"
          "<p><b>Compensation</b></p><p>Pay for this role is $20.00 per hour.</p>" + EEO),
    "C": ("Receptionist",
          "<p><b>Responsibilities</b></p><ul><li>Answer phones &lt;/script&gt;&lt;script&gt;alert(1)"
          "&lt;/script&gt; politely.</li></ul>" + EEO),
    "D": ("Head of Accounting",
          "<p><b>Responsibilities</b></p><ul><li>Reconcile ledgers in SAP.</li></ul>"
          "<p><b>Compensation</b></p><p>The pay range for this role is $120,000 - $140,000 per year.</p>"
          + EEO),
}


def rows_of(page: str, table: str) -> list[dict]:
    """Each data row of a table, as {attribute: value}."""
    body = page.split(f'id="{table}"')[1].split("</table>")[0]
    return [{k: html.unescape(v) for k, v in re.findall(r'(data-[\w-]+)="([^"]*)"', tr)}
            for tr in re.findall(r"<tr (data-[^>]*)>", body)]


class DashboardNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lake = tempfile.mkdtemp(prefix="tick-test-lake-")
        cls._saved = (app.LOCAL_DIR, app.fetch_all)
        app.LOCAL_DIR = cls.lake

        def crawl(pids):
            app.fetch_all = lambda config: ([{"id": p, "title": BOARD[p][0], "loc": "Scranton, PA",
                                              "url": f"https://dm.example/{p}", "desc": BOARD[p][1]}
                                             for p in pids], len(pids))
            app.collect_company(CONFIG)

        crawl("ABCD")
        crawl("ABC")          # a second run the same day: D has closed
        io = report.open_lake(cls.lake)
        cls.page = report.render_dashboard(report.load_lake(cls.lake, SLUG, io), SLUG)
        cls.open = {r["data-s-title"]: r for r in rows_of(cls.page, "open-roles")}
        cls.archive = {r["data-s-title"]: r for r in rows_of(cls.page, "archive-roles")}
        blob = re.search(r'<script type="application/json" id="open-roles-text">(.*?)</script>',
                         cls.page, re.S).group(1)
        cls.blob, cls.texts = blob, json.loads(blob)

    @classmethod
    def tearDownClass(cls):
        app.LOCAL_DIR, app.fetch_all = cls._saved
        shutil.rmtree(cls.lake, ignore_errors=True)

    def test_rows_carry_their_filter_flags(self):
        self.assertEqual(sorted(self.open), ["director of sales", "receptionist", "warehouse associate"])
        self.assertEqual(self.open["director of sales"]["data-lead"], "1")
        self.assertEqual(self.open["receptionist"]["data-lead"], "0")

    def test_pay_sorts_on_one_yearly_scale(self):
        self.assertEqual(self.open["director of sales"]["data-s-pay"], "100000")
        self.assertEqual(self.open["warehouse associate"]["data-s-pay"], "41600")   # $20 x 2080 hours
        self.assertEqual(self.open["receptionist"]["data-s-pay"], "")                # no pay: sinks

    def test_annual_pay(self):
        self.assertEqual(report.annual_pay({"min": None, "max": 120000, "period": "year"}), 120000)
        self.assertEqual(report.annual_pay({"min": 30, "max": 30, "period": "hour"}), 62400)
        self.assertIsNone(report.annual_pay(None))

    def test_search_text_is_the_role_only_one_per_row(self):
        self.assertEqual(len(self.texts), len(self.open))
        joined = " ".join(self.texts)
        self.assertIn("Load trucks for the Scranton branch.", joined)
        # the equal-opportunity text is on every posting: searching it would match the whole board
        self.assertNotIn("equal opportunity employer", joined)

    def test_description_cannot_close_its_script_element(self):
        self.assertNotIn("</script><script>alert", self.blob)
        self.assertIn("</script><script>alert(1)</script>", " ".join(self.texts))

    def test_stack_terms_are_buttons_that_filter(self):
        self.assertEqual(self.open["director of sales"]["data-terms"], "python")
        self.assertIn('data-stack-term="python"', self.page.split('id="open-roles"')[0])   # its card
        self.assertIn('<button type="button" class="tag tag-outline" data-stack-term="python"', self.page)

    def test_archive_rows_carry_flags_and_sort_keys(self):
        d = self.archive["head of accounting"]
        self.assertEqual(d["data-lead"], "1")
        self.assertTrue(d["data-arch"].startswith("arch-"))
        self.assertTrue(d["data-s-closed"])
        self.assertEqual(d["data-s-days"], "0")

    def test_sortable_columns_and_filters_are_declared(self):
        for key in ("title", "loc", "seen", "pay", "n", "closed", "days"):
            self.assertIn(f'data-key="{key}"', self.page)
        for flag in ("new", "tech", "lead", "mine"):
            self.assertIn(f'data-flag="{flag}"', self.page)
        self.assertNotIn('data-flag="paid"', self.page)
        self.assertIn('id="my-terms" hidden', self.page)   # revealed by the script only


if __name__ == "__main__":
    unittest.main()
