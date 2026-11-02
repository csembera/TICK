"""
Tests for the Taleo mode: paging a board across its career sections, reading
a description out of a job page, and the location and date cleanup the
collector applies to a Taleo row.

    python3 -m unittest discover tests

Drives taleo.py, and the collector's fetch_all/normalize/enrich, against a
fake Taleo tenant for the fictional Dunder Mifflin. No network.
"""

import html
import os
import sys
import types
import unittest
import urllib.parse
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *a, **k: None))
sys.path.insert(0, os.path.join(ROOT, "infra", "src", "collector"))
import app  # noqa: E402
import taleo  # noqa: E402

HOST = "https://dundermifflin.taleo.example/careersection"
DETAIL = HOST + "/{careerSection}/jobdetail.ftl?job={contestNo}&lang=en"
CONFIG = {
    "company_name": "Dunder Mifflin", "company_slug": "dunder-mifflin", "ats": "taleo",
    "endpoint": HOST + "/rest/jobboard/searchjobs?lang=en", "http_method": "POST",
    "request_headers": {"tz": "GMT-05:00", "tzname": "America/New_York"},
    "portals": [{"portal": "111", "section": "2"}, {"portal": "222", "section": "stamford"}],
    "detail_endpoint": DETAIL, "date_trust": "high",
    "schema_map": {"posting_id": "contestNo", "title": "column[0]", "location": "column[1]",
                   "posted_on": "column[2]", "url": DETAIL},
}


def req(n, loc='["US-PA-Scranton"]'):
    return {"jobId": str(1000 + n), "contestNo": f"26{n:04d}",
            "column": [f"Sales Rep {n}", loc, "Oct 31, 2026"], "locationsColumns": [1]}


class FakeSearch:
    """Taleo's searchjobs: each portal's requisitions, served a page at a time.

    totals overrides the totalCount a portal reports; drop_once lists
    (portal, pageNo) pages that come back empty the first time they are asked
    for, as a page can when the board changes mid-run."""

    def __init__(self, portals, page_size=25, totals=None, drop_once=()):
        self.portals, self.page_size = portals, page_size
        self.totals, self.drop_once = totals or {}, set(drop_once)
        self.calls = []

    def __call__(self, url, body):
        portal = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["portal"][0]
        page = body["pageNo"]
        self.calls.append((portal, page))
        rows = self.portals[portal]
        start = (page - 1) * self.page_size
        batch = rows[start:start + self.page_size]
        if (portal, page) in self.drop_once:
            self.drop_once.discard((portal, page))
            batch = []
        return {"requisitionList": batch,
                "pagingData": {"currentPageNo": page, "pageSize": self.page_size,
                               "totalCount": self.totals.get(portal, len(rows))}}


def job_page(*fields, id_first=True):
    """A job page whose initialHistory holds rich-text fields the way Taleo
    writes them: each twice, '!*!'-marked, URL-encoded, ':' backslash-escaped."""
    rich = ["!*!" + urllib.parse.quote(f.replace(":", "\\:")) for f in fields for _ in (1, 2)]
    value = "!|!".join(["ftlx0", "ftlUtil_resetPage!%24!requisitionDescriptionInterface",
                        "313499", "Sales Rep", "!*!Scranton", *rich, "true"])
    attrs = (f'id="initialHistory" value="{html.escape(value)}"' if id_first
             else f'value="{html.escape(value)}" id="initialHistory"')
    return f'<html><body><input type="hidden" name="initialHistory" {attrs}></body></html>'


class FetchAll(unittest.TestCase):
    def test_pages_every_section_and_tags_each_posting(self):
        search = FakeSearch({"111": [req(n) for n in range(27)], "222": [req(n) for n in range(50, 52)]})
        postings, total = taleo.fetch_all(CONFIG, search)
        self.assertEqual(len(postings), 29)
        self.assertEqual(total, 29)
        self.assertEqual(search.calls, [("111", 1), ("111", 2), ("222", 1)])
        self.assertEqual({p["careerSection"] for p in postings[:27]}, {"2"})
        self.assertEqual({p["careerSection"] for p in postings[27:]}, {"stamford"})

    def test_a_posting_repeated_on_the_next_page_is_kept_once(self):
        rows = [req(n) for n in range(27)]
        search = FakeSearch({"111": rows, "222": []})
        # a posting added while paging pushes page 1's last row onto page 2
        search.portals["111"] = rows[:25] + rows[24:]
        search.totals["111"] = 27
        postings, _ = taleo.fetch_all(CONFIG, search)
        ids = [p["contestNo"] for p in postings]
        self.assertEqual(len(ids), 27)
        self.assertEqual(len(set(ids)), 27)

    def test_a_requisition_in_two_sections_is_one_posting(self):
        search = FakeSearch({"111": [req(1)], "222": [req(1), req(2)]})
        postings, total = taleo.fetch_all(CONFIG, search)
        self.assertEqual([(p["contestNo"], p["careerSection"]) for p in postings],
                         [("260001", "2"), ("260002", "stamford")])
        self.assertEqual(total, 3)   # what the sections reported, before the overlap

    def test_a_short_section_is_retried_once(self):
        search = FakeSearch({"111": [req(n) for n in range(27)], "222": []},
                            drop_once={("111", 2)})
        postings, _ = taleo.fetch_all(CONFIG, search)
        self.assertEqual(len(postings), 27)
        self.assertEqual(search.calls.count(("111", 1)), 2)

    def test_a_section_still_short_after_the_retry_fails_the_run(self):
        search = FakeSearch({"111": [req(n) for n in range(27)], "222": []}, totals={"111": 30})
        with self.assertRaisesRegex(RuntimeError, "reported 30 postings but 27"):
            taleo.fetch_all(CONFIG, search)

    def test_the_search_body_keeps_the_config_and_sets_the_page(self):
        bodies = []

        def post(url, body):
            bodies.append(body)
            return {"requisitionList": [], "pagingData": {"totalCount": 0}}
        taleo.fetch_all({**CONFIG, "portals": CONFIG["portals"][:1],
                         "request_body": {"fieldData": {"fields": {"KEYWORD": "paper"}}}}, post)
        self.assertEqual(bodies[0]["pageNo"], 1)
        self.assertEqual(bodies[0]["fieldData"], {"fields": {"KEYWORD": "paper"}})
        self.assertIn("sortingSelection", bodies[0])


class Description(unittest.TestCase):
    DESC = "<p>Sell paper: lots of it.</p>"
    QUALS = "<p><strong>Minimum Requirements:</strong></p><ul><li>Two years in sales</li></ul>"

    def test_reads_each_rich_text_field_once_in_order(self):
        self.assertEqual(taleo.description(job_page(self.DESC, self.QUALS)), self.DESC + self.QUALS)

    def test_finds_the_input_whatever_its_attribute_order(self):
        self.assertEqual(taleo.description(job_page(self.DESC, id_first=False)), self.DESC)

    def test_a_page_without_one_has_no_description(self):
        self.assertIsNone(taleo.description("<html><body>Job not found</body></html>"))
        self.assertIsNone(taleo.description(job_page()))
        self.assertIsNone(taleo.description(None))

    def test_detail_url_fills_the_section_and_requisition(self):
        self.assertEqual(taleo.detail_url(DETAIL, {**req(7), "careerSection": "stamford"}),
                         HOST + "/stamford/jobdetail.ftl?job=260007&lang=en")


class Cleanup(unittest.TestCase):
    def test_location_text(self):
        self.assertEqual(taleo.location_text('["US-PA-Scranton"]'), "US-PA-Scranton")
        self.assertEqual(taleo.location_text('["US-PA-Scranton","US-CT-Stamford"]'),
                         "US-PA-Scranton; US-CT-Stamford")
        for passthrough in ("Multiple Locations", "[not json", "", None):
            self.assertEqual(taleo.location_text(passthrough), passthrough)

    def test_iso_date(self):
        self.assertEqual(taleo.iso_date("Oct 31, 2026"), "2026-10-31")
        self.assertEqual(taleo.iso_date("Sep 1, 2026"), "2026-09-01")
        for passthrough in ("31/10/2026", "", None):
            self.assertEqual(taleo.iso_date(passthrough), passthrough)


class Collector(unittest.TestCase):
    """The collector's own path: fetch_all pages through taleo.py, normalize
    cleans the row, enrich reads each description from its job page."""

    def test_collects_a_taleo_board_end_to_end(self):
        search = FakeSearch({"111": [req(1)], "222": [req(2, '["US-CT-Stamford","US-PA-Scranton"]')]})
        posted, pages = [], []

        def http_json(url, method="GET", body=None, headers=None, timeout=None):
            posted.append((method, headers))
            return search(url, body)

        def http_text(url, headers=None, timeout=None):
            pages.append((url, headers))
            return job_page(Description.DESC, Description.QUALS)

        with mock.patch.object(app, "_http_json", http_json), \
                mock.patch.object(app, "_http_text", http_text), \
                mock.patch.object(app, "_DETAIL_DELAY_S", 0):
            postings, total = app.fetch_all(CONFIG)
            rows = [app.normalize(p, CONFIG, "2026-11-02") for p in postings]
            fetched = app.enrich(postings, rows, CONFIG)

        self.assertEqual((len(postings), total, fetched), (2, 2, 2))
        self.assertEqual({m for m, _ in posted}, {"POST"})
        self.assertEqual(posted[0][1]["tz"], "GMT-05:00")
        self.assertEqual(pages[1], (HOST + "/stamford/jobdetail.ftl?job=260002&lang=en",
                                    CONFIG["request_headers"]))
        row = rows[1]
        self.assertEqual(row["posting_id"], "260002")
        self.assertEqual(row["source"], "taleo")
        self.assertEqual(row["location"], "US-CT-Stamford; US-PA-Scranton")
        self.assertEqual(row["posted_on"], "2026-10-31")
        self.assertEqual(row["url"], HOST + "/stamford/jobdetail.ftl?job=260002&lang=en")
        self.assertIn("Sell paper", app.description.role_text(row["description"]))
        self.assertEqual(postings[1]["jobDescription"], Description.DESC + Description.QUALS)

    def test_other_boards_keep_their_location_and_date(self):
        cfg = {**CONFIG, "ats": "other"}
        row = app.normalize(req(1), cfg, "2026-11-02")
        self.assertEqual(row["location"], '["US-PA-Scranton"]')
        self.assertEqual(row["posted_on"], "Oct 31, 2026")


if __name__ == "__main__":
    unittest.main()
