"""
Tests for the SuccessFactors mode: reading a career site's job feed (an RSS
document with Google's job fields) into postings, and the collector running a
SuccessFactors config.

    python3 -m unittest discover tests

Drives successfactors.py, the collector's fetch_all/normalize/enrich, and the
evaluator's description sampler against a fake feed for the fictional Dunder
Mifflin. No network.
"""

import html
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *a, **k: None))
sys.path.insert(0, os.path.join(ROOT, "infra", "src", "collector"))
sys.path.insert(0, os.path.join(ROOT, ".claude", "skills", "tick-evaluator", "scripts"))
import app  # noqa: E402
import successfactors  # noqa: E402
import tick_common  # noqa: E402

SITE = "https://jobs.dundermifflin.example"
FEED = f"{SITE}/sitemap.xml"
CONFIG = {
    "company_name": "Dunder Mifflin", "company_slug": "dunder-mifflin", "ats": "successfactors",
    "endpoint": FEED, "http_method": "GET", "date_trust": "high",
    "schema_map": {"posting_id": "id", "title": "title", "location": "location",
                   "url": "link", "team": "job_function", "posted_on": None},
}

DESC = ("<p><b>About the role</b></p><p>Sell paper to the businesses of Scranton &amp; its suburbs.</p>"
        "<p><b>Qualifications</b></p><ul><li>Two years selling to anyone</li>"
        "<li>Comfortable with SQL and Python reporting</li></ul>"
        "<p>The salary range for this role is $45,000 - $60,000.</p>")


def item(pid, title, place, desc=DESC, function="Sales"):
    fn = f"<g:job_function>{function}</g:job_function>" if function else ""
    # the feed escapes each description's HTML inside CDATA, as the real one does
    return (f"<item><title>{html.escape(f'{title} ({place})')}</title>"
            f"<description><![CDATA[{html.escape(desc)}]]></description>"
            f"<link>{SITE}/job/{pid}/</link><guid>{pid}</guid>"
            f"<g:id>{pid}</g:id><g:expiration_date>2026-12-07</g:expiration_date>"
            f"<g:employer>Dunder Mifflin</g:employer>{fn}"
            f"<g:location>{html.escape(place)}</g:location></item>")


def feed(*items):
    return ('<?xml version="1.0" encoding="UTF-8" ?>'
            '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel>'
            "<title>Careers at Dunder Mifflin</title>" + "".join(items) + "</channel></rss>")


class Items(unittest.TestCase):
    def test_reads_each_item_under_its_local_field_names(self):
        [p] = successfactors.items(feed(item("1444727800", "Sales Representative",
                                             "Scranton, PA, US, 18503")))
        self.assertEqual(p["id"], "1444727800")
        self.assertEqual(p["title"], "Sales Representative")
        self.assertEqual(p["location"], "Scranton, PA, US, 18503")
        self.assertEqual(p["link"], f"{SITE}/job/1444727800/")
        self.assertEqual(p["job_function"], "Sales")
        self.assertEqual(p["employer"], "Dunder Mifflin")

    def test_the_description_is_the_postings_html(self):
        [p] = successfactors.items(feed(item("1", "Sales Representative", "Scranton, PA, US")))
        self.assertEqual(p[successfactors.DESCRIPTION_KEY], DESC)

    def test_only_an_exact_location_suffix_leaves_the_title(self):
        postings = successfactors.items(feed(
            item("1", "Regional Manager (Interim)", "Scranton, PA, US"),
            item("2", "Sales Representative", "Stamford, CT, US"),
        ))
        self.assertEqual([p["title"] for p in postings],
                         ["Regional Manager (Interim)", "Sales Representative"])
        raw = feed(item("3", "Warehouse Lead", "Utica, NY, US")).replace(
            "Warehouse Lead (Utica, NY, US)", "Warehouse Lead (Nashua)")
        self.assertEqual(successfactors.items(raw)[0]["title"], "Warehouse Lead (Nashua)")

    def test_a_missing_field_is_absent_and_items_keep_feed_order(self):
        postings = successfactors.items(feed(item("2", "B", "Utica", function=None),
                                             item("1", "A", "Nashua")))
        self.assertEqual([p["id"] for p in postings], ["2", "1"])
        self.assertNotIn("job_function", postings[0])

    def test_a_plain_rss_field_wins_over_its_namespaced_namesake(self):
        raw = feed(item("1", "Sales Representative", "Scranton")).replace(
            "<g:id>", "<g:title>Not the title</g:title><g:id>")
        self.assertEqual(successfactors.items(raw)[0]["title"], "Sales Representative")

    def test_an_empty_feed_lists_nothing(self):
        self.assertEqual(successfactors.items(feed()), [])

    def test_a_document_that_is_not_rss_fails_the_run(self):
        with self.assertRaises(RuntimeError):
            successfactors.items('<?xml version="1.0"?><Job-Listing><Job/></Job-Listing>')

    def test_a_feed_declaring_entities_is_refused(self):
        bomb = ('<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaa">]>'
                "<rss><channel><item><title>&a;</title></item></channel></rss>")
        with self.assertRaises(RuntimeError):
            successfactors.items(bomb)


class FetchAll(unittest.TestCase):
    def test_gets_the_endpoint_and_counts_postings(self):
        calls = []

        def get_text(url):
            calls.append(url)
            return feed(item("1", "A", "Scranton"), item("2", "B", "Stamford"))
        postings, total = successfactors.fetch_all(CONFIG, get_text)
        self.assertEqual((calls, len(postings), total), ([FEED], 2, 2))


class Collector(unittest.TestCase):
    """The collector's own path: fetch_all reads the feed through
    successfactors.py, normalize maps the item, enrich parses its description
    without another request."""

    def test_collects_a_successfactors_feed_end_to_end(self):
        calls = []

        def http_text(url, headers=None, timeout=None):
            calls.append(url)
            return feed(item("1444727800", "Sales Representative", "Scranton, PA, US, 18503"),
                        item("1444207500", "Regional Manager", "Stamford, CT, US, 06901",
                             desc="<p>Run the branch.</p>", function=None))

        with mock.patch.object(app, "_http_text", http_text):
            postings, total = app.fetch_all(CONFIG)
            rows = [app.normalize(p, CONFIG, "2026-11-07") for p in postings]
            fetched = app.enrich(postings, rows, CONFIG)

        self.assertEqual(calls, [FEED])
        self.assertEqual((len(postings), total, fetched), (2, 2, 2))
        self.assertEqual([r["posting_id"] for r in rows], ["1444727800", "1444207500"])
        r = rows[0]
        self.assertEqual(r["source"], "successfactors")
        self.assertEqual(r["title"], "Sales Representative")
        self.assertEqual(r["location"], "Scranton, PA, US, 18503")
        self.assertEqual(r["url"], f"{SITE}/job/1444727800/")
        self.assertEqual(r["team"], "Sales")
        self.assertIsNone(rows[1]["team"])
        self.assertIn("Two years selling to anyone", app.description.role_text(r["description"]))
        self.assertEqual(r["description"]["compensation"]["min"], 45000)
        self.assertTrue(r["is_tech_role"])

    def test_the_evaluator_samples_the_description_the_collector_parses(self):
        [p] = successfactors.items(feed(item("1", "Sales Representative", "Scranton")))
        self.assertEqual(tick_common.description_html(CONFIG, p, get_json=None), DESC)


if __name__ == "__main__":
    unittest.main()
