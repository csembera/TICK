"""
Tests for the Dayforce mode: grouping the job feed's rows (one per posting
per location) into one posting per requisition, rewriting its plain-text
descriptions as HTML, and the collector running a Dayforce config.

    python3 -m unittest discover tests

Drives dayforce.py, and the collector's fetch_all/normalize/enrich, against a
fake job feed for the fictional Dunder Mifflin. No network.
"""

import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *a, **k: None))
sys.path.insert(0, os.path.join(ROOT, "infra", "src", "collector"))
import app  # noqa: E402
import dayforce  # noqa: E402

BOARD = "https://jobs.dayforce.example/en-US/dundermifflin/CANDIDATEPORTAL/jobs/"
FEED = "https://www.dayforce.example/Api/dundermifflin/V1/JobFeeds"
CONFIG = {
    "company_name": "Dunder Mifflin", "company_slug": "dunder-mifflin", "ats": "dayforce",
    "endpoint": FEED, "http_method": "GET", "date_trust": "high",
    "schema_map": {"posting_id": "ParentRequisitionCode", "title": "Title",
                   "location": "Location", "url": "JobDetailsUrl", "team": "JobFamily",
                   "posted_on": "DatePosted"},
}

DESC = ("About the Role\n"
        "Sell paper to the businesses of Scranton & its suburbs.\n\n"
        "What we are looking for\n"
        "   • Two years selling <anything> to anyone\n"
        "   • A valid driver's license\n\n"
        "Pay range: $45,000 - $60,000 per year\n")


def row(ref, req, city="Scranton", state="PA", posted="2026-10-02T00:00:00",
        title="Sales Representative ", virtual=False, desc=DESC):
    r = {"Title": title, "Description": desc, "JobFamily": "Sales",
         "JobDetailsUrl": f"{BOARD}{ref}", "DatePosted": posted,
         "LastUpdated": "2026-10-19T09:34:13.803", "ReferenceNumber": ref,
         "ParentRequisitionCode": req, "IsVirtualLocation": virtual}
    if city or state:
        r.update({"City": city, "State": state, "Country": "US"})
    return r


class Group(unittest.TestCase):
    def test_a_requisition_posted_in_several_offices_is_one_posting(self):
        # opened in Scranton, then posted again in two more offices weeks later
        feed = [row(7065, 1317),
                row(7105, 1317, "Stamford", "CT", "2026-10-19T00:00:00"),
                row(7103, 1317, "Nashua", "NH", "2026-10-19T00:00:00")]
        [p] = dayforce.group(feed)
        self.assertEqual(p["ParentRequisitionCode"], 1317)
        self.assertEqual(p["Location"], "Nashua, NH; Scranton, PA; Stamford, CT")
        self.assertEqual(p["DatePosted"], "2026-10-02")
        self.assertEqual(p["JobDetailsUrl"], f"{BOARD}7065")
        self.assertEqual(p[dayforce.POSTINGS_KEY], [7065, 7103, 7105])
        self.assertEqual(p[dayforce.ROWS_KEY], feed)

    def test_one_posting_listed_once_per_location_is_one_posting(self):
        [p] = dayforce.group([row(7136, 1338, "Stamford", "CT"), row(7136, 1338)])
        self.assertEqual(p["Location"], "Scranton, PA; Stamford, CT")
        self.assertEqual(p[dayforce.POSTINGS_KEY], [7136])

    def test_the_feed_order_does_not_change_the_posting(self):
        feed = [row(7065, 1317), row(7105, 1317, "Stamford", "CT", "2026-10-19T00:00:00"),
                row(7109, 1317, None, None, "2026-10-19T00:00:00", virtual=True)]
        self.assertEqual(dayforce.group(feed), [
            {**p, dayforce.ROWS_KEY: feed} for p in dayforce.group(list(reversed(feed)))])

    def test_a_virtual_location_is_remote_and_listed_last(self):
        [p] = dayforce.group([row(6781, 1271, None, None, virtual=True),
                              row(6781, 1271, "Stamford", "CT")])
        self.assertEqual(p["Location"], "Stamford, CT; Remote")

    def test_a_state_without_a_city(self):
        [p] = dayforce.group([row(6980, 1312, "", "TN")])
        self.assertEqual(p["Location"], "TN")

    def test_requisitions_keep_the_feed_order_and_the_title_is_trimmed(self):
        postings = dayforce.group([row(1, 20), row(2, 10), row(3, 20)])
        self.assertEqual([p["ParentRequisitionCode"] for p in postings], [20, 10])
        self.assertEqual(postings[0]["Title"], "Sales Representative")

    def test_a_row_without_a_requisition_stays_its_own_posting(self):
        postings = dayforce.group([row(7001, None), row(7002, ""), row(7003, 7001)])
        self.assertEqual([dayforce._key(p) for p in postings],
                         ["posting-7001", "posting-7002", "7001"])


class FetchAll(unittest.TestCase):
    def test_gets_the_endpoint_and_counts_requisitions(self):
        calls = []

        def get_json(url):
            calls.append(url)
            return [row(1, 10), row(2, 10, "Stamford", "CT"), row(3, 11)]
        postings, total = dayforce.fetch_all(CONFIG, get_json)
        self.assertEqual((calls, len(postings), total), ([FEED], 2, 2))

    def test_a_payload_that_is_not_a_list_fails_the_run(self):
        with self.assertRaises(RuntimeError):
            dayforce.fetch_all(CONFIG, lambda url: {"Message": "JOBFEED_INVALID_CLIENT_PROPERTY"})


class TextHtml(unittest.TestCase):
    def test_lines_become_paragraphs_and_bullets_a_list(self):
        self.assertEqual(
            dayforce.text_html(DESC),
            "<p>About the Role</p>"
            "<p>Sell paper to the businesses of Scranton &amp; its suburbs.</p>"
            "<p>What we are looking for</p>"
            "<ul><li>Two years selling &lt;anything&gt; to anyone</li>"
            "<li>A valid driver&#x27;s license</li></ul>"
            "<p>Pay range: $45,000 - $60,000 per year</p>")

    def test_a_list_at_the_end_is_closed(self):
        self.assertEqual(dayforce.text_html("Duties\n• Sell\n• Deliver"),
                         "<p>Duties</p><ul><li>Sell</li><li>Deliver</li></ul>")

    def test_html_and_empty_text_pass_through(self):
        self.assertEqual(dayforce.text_html("<p>Sell paper</p>"), "<p>Sell paper</p>")
        self.assertIsNone(dayforce.text_html(None))
        self.assertEqual(dayforce.text_html("  "), "  ")


class Collector(unittest.TestCase):
    """The collector's own path: fetch_all groups the feed through
    dayforce.py, normalize maps the grouped posting, enrich parses its
    description without another request."""

    def test_collects_a_dayforce_feed_end_to_end(self):
        feed = [row(7065, 1317), row(7105, 1317, "Stamford", "CT", "2026-10-19T00:00:00"),
                row(7109, 1317, None, None, "2026-10-19T00:00:00", virtual=True),
                row(6757, 1265, "Utica", "NY", title="Regional Manager")]
        calls = []

        def http_json(url, method="GET", body=None, headers=None, timeout=None):
            calls.append((url, method))
            return feed

        with mock.patch.object(app, "_http_json", http_json):
            postings, total = app.fetch_all(CONFIG)
            rows = [app.normalize(p, CONFIG, "2026-11-02") for p in postings]
            fetched = app.enrich(postings, rows, CONFIG)

        self.assertEqual(calls, [(FEED, "GET")])
        self.assertEqual((len(postings), total, fetched), (2, 2, 2))
        self.assertEqual([r["posting_id"] for r in rows], ["1317", "1265"])
        r = rows[0]
        self.assertEqual(r["source"], "dayforce")
        self.assertEqual(r["title"], "Sales Representative")
        self.assertEqual(r["location"], "Scranton, PA; Stamford, CT; Remote")
        self.assertTrue(r["remote_flag"])
        self.assertEqual(r["url"], f"{BOARD}7065")
        self.assertEqual(r["team"], "Sales")
        self.assertEqual(r["posted_on"], "2026-10-02")
        self.assertIn("A valid driver's license", app.description.full_text(r["description"]))
        self.assertEqual(r["description"]["compensation"]["min"], 45000)


if __name__ == "__main__":
    unittest.main()
