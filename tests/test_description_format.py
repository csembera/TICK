"""
Tests for the evaluator's description-format report (tick_common.format_report,
prune_format): what the evaluator reads to learn a board's layout, and what the
gate re-runs to check it.

    python3 -m unittest discover tests

Synthetic boards: every posting shares an intro and an equal-opportunity
paragraph, and the board labels its duties with a heading the generic patterns
misfile — the case a learned alias exists for.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".claude", "skills", "tick-evaluator", "scripts"))
import tick_common  # noqa: E402

INTRO = "Dunder Mifflin is a regional paper company serving Scranton and beyond for over fifty years."
EEO = "Dunder Mifflin is an equal opportunity employer and considers applicants without regard to race."


def posting(duty: str) -> str:
    return (f"<p>{INTRO}</p>"
            "<p><b>SKILLS</b></p>"
            f"<ul><li>{duty}</li><li>Answer client calls.</li></ul>"
            "<p><b>Qualifications</b></p><ul><li>Two years of sales experience.</li></ul>"
            "<p>The salary range for this role is $50,000 - $60,000.</p>"
            f"<p>{EEO}</p>")


BOARD = [posting(d) for d in ("Sell paper.", "Deliver paper.", "Audit paper stock.", "Train new staff.")]


class Inventory(unittest.TestCase):
    def setUp(self):
        self.rep = tick_common.format_report(BOARD)

    def test_headings_counted_per_posting(self):
        heads = {h["text"]: h for h in self.rep["headings"]}
        self.assertEqual(heads["SKILLS"]["postings"], 4)
        self.assertEqual(heads["SKILLS"]["generic"], "qualifications")   # the misfile an alias fixes

    def test_repeated_blocks_are_boilerplate_candidates(self):
        texts = {b["text"]: b for b in self.rep["repeated_blocks"]}
        self.assertEqual(texts[INTRO]["postings"], 4)
        self.assertEqual(texts[INTRO]["position"], "first")
        self.assertEqual(texts[EEO]["generic_kind"], "eeo")
        self.assertFalse(any("Sell paper" in t for t in texts))   # role text is not repeated

    def test_pay_and_sections(self):
        self.assertEqual(self.rep["pay_found"], 4)
        self.assertEqual(self.rep["role_sections"]["responsibilities"], 0.0)

    def test_plain_template_headings_are_candidates(self):
        board = [f"<p>Job Summary</p><p>{d}</p>" for d in ("Sell paper.", "Deliver paper.", "Count paper.")]
        cands = {c["text"]: c["postings"] for c in tick_common.format_report(board)["heading_candidates"]}
        self.assertEqual(cands.get("Job Summary"), 3)
        fmt = {"aliases": {"summary": ["Job Summary"]}}
        rep = tick_common.format_report(board, fmt)
        self.assertEqual(rep["alias_hits"]["summary"]["Job Summary"], 3)   # counted once it is aliased
        self.assertNotIn("Job Summary", {c["text"] for c in rep["heading_candidates"]})

    def test_empty_sample(self):
        self.assertEqual(tick_common.format_report([])["sampled"], 0)


class FormatCheck(unittest.TestCase):
    FMT = {"aliases": {"responsibilities": ["SKILLS", "Never Used Heading"]},
           "boilerplate": [{"kind": "company_intro", "starts_with": "Dunder Mifflin is a regional paper company"},
                           {"kind": "benefits", "starts_with": "This sentence appears on no posting"}]}

    def setUp(self):
        self.rep = tick_common.format_report(BOARD, self.FMT)

    def test_alias_moves_duties(self):
        self.assertEqual(self.rep["role_sections"]["responsibilities"], 1.0)
        self.assertEqual(self.rep["alias_hits"]["responsibilities"]["SKILLS"], 4)
        self.assertEqual(self.rep["boilerplate_found"].get("company_intro"), 4)

    def test_rule_hits_and_findings(self):
        hits = {r["kind"]: r["postings"] for r in self.rep["rule_hits"]}
        self.assertEqual(hits, {"company_intro": 4, "benefits": 0})
        self.assertTrue(any("matched no sampled posting" in f for f in self.rep["findings"]))
        self.assertTrue(any("'Never Used Heading'" in f for f in self.rep["findings"]))

    def test_prune_drops_only_dead_rules(self):
        pruned = tick_common.prune_format(self.FMT, self.rep)
        self.assertEqual([r["kind"] for r in pruned["boilerplate"]], ["company_intro"])
        self.assertEqual(pruned["aliases"], self.FMT["aliases"])

    def test_rare_rule_is_flagged_as_role_text(self):
        # a paragraph on one posting in four is that role's text, not the board's boilerplate
        board = BOARD[:3] + [BOARD[3] + "<p>This role joins the Stamford branch sales floor.</p>"]
        fmt = {"boilerplate": [{"kind": "benefits", "contains": "joins the Stamford branch"}]}
        rep = tick_common.format_report(board, fmt)
        self.assertTrue(any("likely role text" in f for f in rep["findings"]))


class CollectorHeaders(unittest.TestCase):
    def test_gate_sends_what_the_collector_sends(self):
        import types
        sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *a, **k: None))
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "infra", "src", "collector"))
        import app
        h = tick_common.collector_headers({"request_headers": {"orgoid": "X1", "Accept": "text/plain"}})
        self.assertEqual(h["User-Agent"], app._UA)          # drift here breaks the mirror
        self.assertEqual(h["orgoid"], "X1")                  # config headers win, as in _http_json
        self.assertEqual(h["Accept"], "text/plain")
        # a browser Accept-Language empties some boards' posting lists
        self.assertNotIn("Accept-Language", h)


class Sampling(unittest.TestCase):
    def test_spread_spans_the_list(self):
        self.assertEqual(tick_common.spread(list(range(100)), 4), [0, 25, 50, 75])
        self.assertEqual(tick_common.spread([1, 2], 5), [1, 2])

    def test_description_html_inline_and_adp(self):
        self.assertEqual(tick_common.description_html(
            {"description_field": "data.description"}, {"data": {"description": "<p>x</p>"}}, None), "<p>x</p>")
        detail = {"jobRequisitions": [{"jobDescription": "<p>adp</p>"}]}
        got = tick_common.description_html(
            {"ats": "adp", "detail_endpoint": "https://x/{posting_id}", "schema_map": {"posting_id": "reqId"}},
            {"reqId": "42"}, lambda url, *a, **k: detail if url == "https://x/42" else {})
        self.assertEqual(got, "<p>adp</p>")

    def test_description_html_workday(self):
        got = tick_common.description_html(
            {"ats": "workday", "endpoint": "https://t.wd1.myworkdayjobs.com/wday/cxs/t/site/jobs"},
            {"externalPath": "/job/x/Role_DM-1"},
            lambda url, *a, **k: {"jobPostingInfo": {"jobDescription": "<p>wd</p>"}}
            if url.endswith("/site/job/x/Role_DM-1") else {})
        self.assertEqual(got, "<p>wd</p>")


if __name__ == "__main__":
    unittest.main()
