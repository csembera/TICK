"""
Tests for the Apple mode: the session its search needs, paging the search,
grouping its rows (one per position per location) into one posting per
position with excluded teams dropped, turning a detail payload into HTML,
fetching descriptions under a time budget, and the collector reusing an
open posting's description instead of fetching it again.

    python3 -m unittest discover tests

Drives apple.py, and the collector's collect_company, against a fake
jobs.apple.com for the fictional Dunder Mifflin. No network.
"""

import io
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
import app  # noqa: E402
import apple  # noqa: E402

HOST = "https://jobs.dundermifflin.example"
SLUG = "dunder-mifflin"
CONFIG = {
    "company_name": "Dunder Mifflin", "company_slug": SLUG, "ats": "apple",
    "endpoint": f"{HOST}/api/v1/search", "http_method": "POST",
    "request_body": {"query": "", "filters": {"locations": ["postLocation-USA"]},
                     "locale": "en-us", "sort": "newest", "format": {"longDate": "MMMM D, YYYY"}},
    "detail_endpoint": f"{HOST}/api/v1/jobDetails/{{posting_id}}",
    "exclude_teams": ["STORE"], "reuse_descriptions": True, "date_trust": "high",
    "schema_map": {"posting_id": "positionId", "title": "postingTitle", "location": "Location",
                   "url": f"{HOST}/en-us/details/{{positionId}}/{{transformedPostingTitle}}",
                   "team": "Team", "posted_on": "PostedOn"},
}
SALES = {"teamName": "Sales", "teamCode": "SALES"}
STORE = {"teamName": "Paper Store", "teamCode": "STORE"}


def row(pos, suffix, place, team=SALES, title="Sales Representative ", posted="2026-10-02T10:00:00.000Z"):
    return {"id": f"{pos}-{suffix}", "positionId": pos, "reqId": f"{pos}-{suffix}",
            "postingTitle": title, "transformedPostingTitle": "sales-representative",
            "postDateInGMT": posted, "team": team, "type": "REQ",
            "locations": [{"name": place, "stateProvince": "", "countryName": "United States"}]}


DETAIL = {"res": {
    "jobSummary": "Sell paper to the businesses of Scranton.",
    "description": "Call on clients.\n\nGrow the territory.",
    "minimumQualifications": "Two years selling <anything>\nA driver's license",
    "preferredQualifications": "",
    "postingFooters": [{"localizations": {"en_US": [
        {"name": "Pay & Benefits", "content": "<p>The base pay range is $45,000 - $60,000.</p>"}]}}],
}}


class FakeSite:
    """jobs.apple.com as the session sees it: the search answers only after
    the search page set a cookie and with the CSRF token sent back."""

    def __init__(self, rows, details=None):
        self.rows, self.details = rows, details or {}
        self.cookie = self.token_seen = False
        self.detail_calls = []

    def open(self, req, timeout=None):
        url = req.full_url
        if url.endswith("/en-us/search"):
            self.cookie = True
            return self._resp("<html></html>")
        if url.endswith("/api/v1/CSRFToken"):
            return self._resp("", {"X-Apple-CSRF-Token": "tok"})
        if url.endswith("/api/v1/search"):
            body = json.loads(req.data)
            ok = self.cookie and req.get_header("X-apple-csrf-token") == "tok" and "format" in body
            rows = self.rows if ok else []
            page = rows[(body["page"] - 1) * 20: body["page"] * 20]
            return self._resp(json.dumps({"res": {"searchResults": page, "totalRecords": len(rows)}}))
        if "/api/v1/jobDetails/" in url:
            pid = url.rsplit("/", 1)[1]
            self.detail_calls.append(pid)
            if pid not in self.details:
                raise OSError("detail down")
            return self._resp(json.dumps(self.details[pid]))
        raise AssertionError(f"unexpected request {url}")

    @staticmethod
    def _resp(body, headers=None):
        r = io.BytesIO(body.encode())
        r.headers = headers or {}
        return r

    def session(self):
        return apple.Session(HOST, "en-us", opener=self)


class Group(unittest.TestCase):
    def test_a_position_listed_once_per_location_is_one_posting(self):
        [p] = apple.group([row("101", "0836", "Scranton"), row("101", "0157", "Nashua"),
                           row("101", "3543", "Stamford")])
        self.assertEqual(p["positionId"], "101")
        self.assertEqual(p[apple.LOCATION_KEY], "Nashua; Scranton; Stamford")
        self.assertEqual(p[apple.REQ_IDS_KEY], ["101-0157", "101-0836", "101-3543"])
        self.assertEqual(len(p[apple.ROWS_KEY]), 3)
        self.assertEqual(p[apple.TEAM_KEY], "Sales")
        self.assertEqual(p[apple.POSTED_KEY], "2026-10-02")
        self.assertEqual(p["postingTitle"], "Sales Representative")

    def test_the_search_order_does_not_change_the_posting(self):
        rows = [row("101", "0836", "Scranton"), row("101", "0157", "Nashua")]
        self.assertEqual(apple.group(rows), apple.group(rows[::-1]))

    def test_excluded_teams_are_dropped_and_positions_keep_search_order(self):
        got = apple.group([row("102", "1", "Scranton"), row("900", "1", "Mall", team=STORE),
                           row("101", "1", "Nashua")], exclude_teams=["STORE"])
        self.assertEqual([p["positionId"] for p in got], ["102", "101"])

    def test_a_state_the_name_lacks_is_added(self):
        r = row("101", "1", "Austin")
        r["locations"][0]["stateProvince"] = "Texas"
        self.assertEqual(apple.group([r])[0][apple.LOCATION_KEY], "Austin, Texas")


class DetailHtml(unittest.TestCase):
    def test_fields_under_the_sites_headings_and_footers_after(self):
        h = apple.detail_html(DETAIL["res"])
        self.assertIn("<h3>Summary</h3><p>Sell paper to the businesses of Scranton.</p>", h)
        self.assertIn("<h3>Description</h3><p>Call on clients.</p><p>Grow the territory.</p>", h)
        self.assertIn("<h3>Minimum Qualifications</h3><ul><li>Two years selling &lt;anything&gt;</li>", h)
        self.assertNotIn("Preferred Qualifications", h)   # empty fields are left out
        self.assertTrue(h.endswith("<h3>Pay &amp; Benefits</h3><p>The base pay range is $45,000 - $60,000.</p>"))

    def test_an_empty_detail_is_none(self):
        self.assertIsNone(apple.detail_html({}))


class FetchAll(unittest.TestCase):
    def test_pages_the_search_inside_a_session(self):
        rows = [row(str(100 + i), "1", "Scranton") for i in range(45)]
        site = FakeSite(rows)
        postings, total = apple.fetch_all(CONFIG, site.session())
        self.assertEqual(total, 45)
        self.assertEqual(len(postings), 45)

    def test_a_search_without_the_session_fails_the_run(self):
        site = FakeSite([row("101", "1", "Scranton")])
        site.open_real = site.open
        site.open = lambda req, timeout=None: (site._resp("{}", {}) if req.full_url.endswith("CSRFToken")
                                                else site.open_real(req, timeout))
        with self.assertRaises(RuntimeError):
            apple.fetch_all(CONFIG, site.session())

    def test_paging_short_of_the_total_fails_the_run(self):
        site = FakeSite([row(str(100 + i), "1", "Scranton") for i in range(45)])
        real = site.open

        def lossy(req, timeout=None):
            resp = real(req, timeout)
            if req.full_url.endswith("/api/v1/search") and json.loads(req.data)["page"] == 2:
                return site._resp(json.dumps({"res": {"searchResults": [], "totalRecords": 45}}))
            return resp
        site.open = lossy
        with self.assertRaises(RuntimeError):
            apple.fetch_all(CONFIG, site.session())


    def test_a_page_that_times_out_once_is_retried(self):
        site = FakeSite([row(str(100 + i), "1", "Scranton") for i in range(25)])
        real, failed = site.open, []

        def flaky(req, timeout=None):
            if req.full_url.endswith("/api/v1/search") and json.loads(req.data)["page"] == 2 and not failed:
                failed.append(1)
                raise TimeoutError("The read operation timed out")
            return real(req, timeout)
        site.open = flaky
        with mock.patch.object(apple.time, "sleep"):
            postings, _ = apple.fetch_all(CONFIG, site.session())
        self.assertEqual((len(postings), failed), (25, [1]))


class FetchDescriptions(unittest.TestCase):
    def test_stops_when_the_budget_runs_out_and_skips_failed_details(self):
        postings = apple.group([row(str(100 + i), "1", "Scranton") for i in range(6)])
        site = FakeSite([], {str(100 + i): DETAIL for i in range(6) if i != 1})
        ticks = iter([0, 0, 0, 100])   # start, batch 1, batch 2, then past the deadline
        cfg = {**CONFIG, "detail_workers": 2, "detail_budget_s": 50}
        got = apple.fetch_descriptions(cfg, postings, site.session(), clock=lambda: next(ticks))
        self.assertEqual(got, 3)   # 100, 102, 103: 101 failed, 104 and 105 past the budget
        self.assertEqual([apple.DESCRIPTION_KEY in p for p in postings],
                         [True, False, True, True, False, False])


    def test_the_runs_deadline_wins_over_a_longer_budget(self):
        postings = apple.group([row(str(100 + i), "1", "Scranton") for i in range(6)])
        site = FakeSite([], {str(100 + i): DETAIL for i in range(6)})
        now = [0.0]

        def clock():
            now[0] += 10   # each check is ten seconds later
            return now[0]
        cfg = {**CONFIG, "detail_workers": 2, "detail_budget_s": 600}
        # the run must stop by t=50; less one detail timeout (15 s), fetching ends at t=35
        got = apple.fetch_descriptions(cfg, postings, site.session(), clock=clock, stop_at=50)
        self.assertEqual(got, 4)   # checks at t=20 and t=30 pass, t=40 is past it


class CollectorReuse(unittest.TestCase):
    """The collector fetches a description once, and reuses it while the
    position stays open."""

    def setUp(self):
        self.lake = tempfile.mkdtemp(prefix="tick-test-lake-")
        self._saved = app.LOCAL_DIR
        app.LOCAL_DIR = self.lake
        self.site = FakeSite([], {"101": DETAIL, "102": DETAIL})
        real = apple.session_for
        self.patch = mock.patch.object(apple, "session_for", lambda cfg, opener=None: real(cfg, self.site))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        app.LOCAL_DIR = self._saved
        shutil.rmtree(self.lake, ignore_errors=True)

    def crawl(self, rows):
        self.site.rows = rows
        self.site.detail_calls = []
        return app.collect_company(CONFIG)

    def curated(self):
        path = os.path.join(self.lake, f"curated/company={SLUG}/dt={date.today().isoformat()}/postings.jsonl")
        with open(path) as f:
            return {r["posting_id"]: r for r in map(json.loads, f.read().splitlines())}

    def test_an_open_position_is_not_fetched_again(self):
        first = self.crawl([row("101", "1", "Scranton"), row("900", "1", "Mall", team=STORE)])
        self.assertEqual((first["collected"], first["descriptions_fetched"]), (1, 1))
        self.assertEqual(self.site.detail_calls, ["101"])

        second = self.crawl([row("102", "1", "Nashua"), row("101", "1", "Scranton")])
        self.assertEqual(self.site.detail_calls, ["102"])
        self.assertEqual((second["descriptions_fetched"], second["descriptions_reused"]), (1, 1))
        rows = self.curated()
        self.assertEqual(rows["101"]["description"], rows["102"]["description"])
        self.assertIn("compensation", json.dumps(rows["101"]["description"]))
        self.assertEqual(rows["101"]["url"], f"{HOST}/en-us/details/101/sales-representative")
        self.assertEqual(rows["101"]["team"], "Sales")

    def test_a_position_left_without_a_description_is_fetched_next_run(self):
        self.site.details = {}
        first = self.crawl([row("101", "1", "Scranton")])
        self.assertEqual(first["without_description"], 1)
        self.site.details = {"101": DETAIL}
        second = self.crawl([row("101", "1", "Scranton")])
        self.assertEqual((second["descriptions_fetched"], second["descriptions_reused"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
