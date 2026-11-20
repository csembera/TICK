"""
Tests for tools/teaser.py, the one-page teaser of an account for a prospect.

    python3 -m unittest discover tests

A fictional account, Initech, with a brief and evidence pack shaped like the
tick-brief skill's: the brief's words come through whole and escaped, its
citations are numbered and linked, a persona's brief is refused, and the page
and the email are written from a lake on disk. No network, no Chrome.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import teaser  # noqa: E402

URL = "https://initech.example/jobs/"


def posting(pid, title, opened_on="2026-11-05", tech=False, lead=False, terms=(), still_open=True):
    return {"id": pid, "title": title, "location": "Austin, TX", "team": None, "url": URL + pid,
            "is_tech_role": tech, "is_leadership": lead, "stack_terms": list(terms),
            "opened_on": opened_on, "still_open": still_open, "pay": None, "description": None}


def weekly():
    evidence = {
        "company": {"name": "Initech", "slug": "initech"},
        "window": {"start": "2026-11-02", "end": "2026-11-08"},
        "board": {"id": "BOARD", "open_at_start": 40, "open_at_end": 44, "net_change": 4, "opened": 6,
                  "closed": 2, "reopened": 0, "tech_roles_open": 9,
                  "open_leadership_roles": [{"id": "I-9", "title": "Director, Platform", "days_open": 30,
                                             "is_leadership": True}]},
        "stack": {"id": "STACK", "new_to_board": [{"term": "kafka", "postings": 1}],
                  "rising": [{"term": "python", "from": 3, "to": 5}]},
        "opened": [posting("I-1", "Data Engineer", tech=True, terms=["kafka", "python"]),
                   posting("I-2", "Data Engineer", tech=True, terms=["python"]),
                   posting("I-3", "TPS Report Analyst", "2026-11-07"),
                   posting("I-4", "Platform Engineer", "2026-11-03", tech=True, still_open=False)],
        "closed": [posting("I-5", "Collator")],
    }
    brief = {"company_slug": "initech", "persona_ref": None, "week": evidence["window"], "quiet_week": False,
             "paragraphs": ["Initech is building a <streaming> platform [[I-1]] [[I-2]], led from the "
                            "open Director seat [[I-9]] on a growing board [[BOARD]].",
                            "TPS reports are back [[I-3]]."],
             "action_items": [{"kind": "pitch", "title": "Pitch Kafka & ops", "why": "Two engineers [[I-1]].",
                               "quote": {"text": "real-time pipelines", "source": "I-1"}, "steps": []}]}
    return brief, evidence


def overview():
    evidence = {
        "kind": "overview", "company": {"name": "Initech", "slug": "initech"}, "as_of": "2026-11-09",
        "board": {"id": "BOARD", "open_roles": 30, "tech_roles": 8, "leadership_roles": 0},
        "stack": {"id": "STACK", "since_first_crawl": [],
                  "by_category": {"Languages": [{"term": "python", "postings": 7, "baseline": False},
                                                {"term": "excel", "postings": 20, "baseline": True}]}},
        "postings": [dict(posting("I-1", "Data Engineer", tech=True, terms=["python"]), days_open=3)],
    }
    brief = {"kind": "overview", "company_slug": "initech", "persona_ref": None, "as_of": "2026-11-09",
             "paragraphs": ["Initech has a data team [[I-1]]."], "action_items": []}
    return brief, evidence


class TeaserTest(unittest.TestCase):
    def test_citations_are_numbered_merged_and_linked(self):
        brief, evidence = weekly()
        c = teaser.build(brief, evidence, 1, 1)
        text = c["cites"].html(c["excerpt"])
        self.assertIn("&lt;streaming&gt;", text)
        self.assertIn(f'platform<sup><a href="{URL}I-1">1</a>,<a href="{URL}I-2">2</a></sup>', text)
        self.assertIn("seat<sup>3</sup>", text)         # a standing role: no URL in the pack
        self.assertIn("on a growing board.", text)      # [[BOARD]] is no posting: no number, no note
        self.assertEqual(teaser.unlinked_notes(c["cites"]), "3 Director, Platform")
        self.assertIn('<a href="' + URL + 'I-1">1</a>', c["cites"].html(c["move"]["why"]))

    def test_board_fills_a_standing_roles_link(self):
        brief, evidence = weekly()
        c = teaser.build(brief, evidence, 1, 1, {"I-9": {"url": URL + "I-9", "title": "Director, Platform"},
                                                        "I-1": {}, "I-2": {}, "I-3": {}})
        self.assertIn(f'seat<sup><a href="{URL}I-9">3</a></sup>', c["cites"].html(c["excerpt"]))
        self.assertEqual(teaser.unlinked_notes(c["cites"]), "")   # the board's citation is dropped

    def test_persona_brief_is_refused(self):
        brief, evidence = weekly()
        brief["persona_ref"] = "abc123"
        with self.assertRaises(SystemExit):
            teaser.build(brief, evidence, 1, 1)

    def test_paragraph_out_of_range_is_refused(self):
        brief, evidence = weekly()
        with self.assertRaises(SystemExit):
            teaser.build(brief, evidence, 3, 1)

    def test_weekly_numbers_and_headline(self):
        brief, evidence = weekly()
        c = teaser.build(brief, evidence, 1, 1)
        self.assertEqual([(k["label"], k["value"]) for k in c["kpis"]],
                         [("Open roles", 44), ("Closed this week", 2), ("Tech roles open", 9),
                          ("Leadership seats open", 1)])
        self.assertEqual(c["kpis"][0]["delta"], "+4")
        self.assertEqual(c["headline"], "Initech opened 6 roles last week. Here's what they're building.")
        self.assertEqual(teaser.period(evidence), "Week of Nov 2 – 8, 2026")

    def test_overview_drops_zero_tiles(self):
        brief, evidence = overview()
        c = teaser.build(brief, evidence, 1, 1)
        self.assertEqual([k["label"] for k in c["kpis"]], ["Open roles", "Tech roles open"])
        self.assertEqual(c["headline"], "Initech has 30 roles open. Here's what they're hiring for.")
        self.assertEqual(c["stack"], [("python", "7 roles")])   # excel is baseline
        self.assertIsNone(c["move"])

    def test_roles_put_cited_first_group_titles_and_skip_closed(self):
        brief, evidence = weekly()
        c = teaser.build(brief, evidence, 2, 1)        # cites I-3, the move cites I-1
        rows = c["roles"]
        self.assertEqual([(r["title"], r["count"]) for r in rows],
                         [("TPS Report Analyst", 1), ("Data Engineer", 2)])
        self.assertEqual(rows[1]["terms"], [{"term": "kafka", "new": True}, {"term": "python", "new": False}])
        self.assertEqual(c["roles_label"], "New on the board")

    def test_a_cited_standing_role_makes_it_on_the_board(self):
        brief, evidence = weekly()
        c = teaser.build(brief, evidence, 1, 1)
        self.assertIn("Director, Platform", [r["title"] for r in c["roles"]])
        self.assertEqual(c["roles_label"], "On the board")

    def test_writes_page_and_email_from_a_lake(self):
        brief, evidence = weekly()
        with tempfile.TemporaryDirectory() as lake, tempfile.TemporaryDirectory() as out:
            d = os.path.join(lake, "briefs", "company=initech", "week=2026-11-08")
            os.makedirs(d)
            for name, doc in (("brief.json", brief), ("evidence.json", evidence)):
                with open(os.path.join(d, name), "w") as f:
                    json.dump(doc, f)
            with redirect_stdout(io.StringIO()) as listing:
                self.assertEqual(teaser.main(["initech", "--lake", lake, "--list"]), 0)
            self.assertIn("--paragraph 2: TPS reports are back.…", listing.getvalue())
            with redirect_stdout(io.StringIO()):
                teaser.main(["initech", "--lake", lake, "--out", out])
            with open(os.path.join(out, "teaser.html")) as f:
                page = f.read()
            with open(os.path.join(out, "email.html")) as f:
                email = f.read()
        self.assertNotIn("Prepared for", page)
        self.assertIn("Pitch Kafka &amp; ops", page)
        self.assertIn("“real-time pipelines”", page)
        self.assertNotIn("<style", email)
        self.assertNotIn("<svg", email)
        self.assertIn(f'<a href="{URL}I-1"', email)


if __name__ == "__main__":
    unittest.main()
