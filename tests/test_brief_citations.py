"""
Tests that every role a brief cites links somewhere: an open role to its
posting on the board, a closed one to its archive page. The links come from
the lake at render time, not the evidence pack, whose standing lists carry
no URL and which cannot know a role closed after the brief was written.

    python3 -m unittest discover tests

Synthetic pack and lake for the fictional Dunder Mifflin. No network.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import tick_insights_report as report  # noqa: E402

SLUG = "dunder-mifflin"
BOARD_URL = "https://jobs.example.com/dm/"


def evidence():
    standing = {"title": "Regional Manager", "days_open": 40, "is_leadership": True}
    return {
        "company": {"slug": SLUG, "name": "Dunder Mifflin"},
        "window": {"start": "2026-10-25", "end": "2026-11-01", "crawl_days": 7},
        "board": {"open_at_start": 3, "open_at_end": 3, "opened": 1, "closed": 1, "reopened": 0,
                  "net_change": 0, "weekly_open_totals": [],
                  # the pack's standing lists hold no url
                  "open_leadership_roles": [{"id": "DM-1", **standing}],
                  "longest_open": [{"id": "DM-2", **standing, "title": "Sales Lead"}]},
        "opened": [{"id": "DM-3", "title": "Accountant", "opened_on": "2026-10-30",
                    "url": BOARD_URL + "DM-3"}],
        "closed": [{"id": "DM-4", "title": "Receptionist", "closed_on": "2026-10-28", "days_open": 12,
                    "url": BOARD_URL + "DM-4"}],
        "reopened": [],
    }


def lake():
    return {"state": {"DM-1": {"first_seen": "2026-09-22"},
                      "DM-2": {"first_seen": "2026-09-22", "closed_on": "2026-11-02"},   # closed since
                      "DM-3": {"first_seen": "2026-10-30"},
                      "DM-4": {"first_seen": "2026-10-16", "closed_on": "2026-10-28"}},
            "curated": {pid: {"posting_id": pid, "url": BOARD_URL + pid} for pid in ("DM-1", "DM-3")}}


BRIEF = {"paragraphs": ["Hiring [[DM-1]], [[DM-2]], [[DM-3]] and [[DM-4]] on the [[BOARD]]."],
         "action_items": []}


def sources(page):
    """{posting id: href or None} from the brief's numbered source list."""
    out = {}
    for body in re.findall(r'<span class="cite"[^>]*>\d+</span><span>(.*?)</span></div>', page):
        m = re.match(r'(?:<a href="([^"]+)"[^>]*>)?([\w-]+) —', body)
        if m:
            out[m.group(2)] = m.group(1)
    return out


class Citations(unittest.TestCase):
    def test_every_cited_role_links_open_to_the_board_closed_to_the_archive(self):
        page = report.render_ai_brief(BRIEF, evidence(), lake())
        self.assertEqual(sources(page), {
            "DM-1": BOARD_URL + "DM-1",                  # standing: url from the lake's curated board
            "DM-2": report.archive_href(SLUG, "DM-2"),   # open in the pack, closed since
            "DM-3": BOARD_URL + "DM-3",
            "DM-4": report.archive_href(SLUG, "DM-4"),
        })
        self.assertIn("closed since, on 2026-11-02", page)

    def test_a_role_reopened_since_links_to_the_board_again(self):
        now = lake()
        now["state"]["DM-4"] = {"first_seen": "2026-10-16"}
        now["curated"]["DM-4"] = {"posting_id": "DM-4", "url": BOARD_URL + "DM-4"}
        self.assertEqual(sources(report.render_ai_brief(BRIEF, evidence(), now))["DM-4"], BOARD_URL + "DM-4")

    def test_without_a_lake_the_pack_alone_decides(self):
        links = sources(report.render_ai_brief(BRIEF, evidence()))
        self.assertEqual(links["DM-4"], report.archive_href(SLUG, "DM-4"))
        self.assertEqual(links["DM-3"], BOARD_URL + "DM-3")


if __name__ == "__main__":
    unittest.main()
