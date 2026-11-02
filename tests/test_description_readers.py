"""
Tests for how descriptions are read: the approximate rebuild of flattened
text (description.from_text), and the renderer's accessors every page and the
brief evidence go through.

    python3 -m unittest discover tests

Synthetic rows: one the collector parsed (`description`, no flat copy) and one
from before descriptions kept their structure (`description_raw` only).
"""

import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "infra", "src", "collector"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import description  # noqa: E402
import tick_insights_report as report  # noqa: E402

INTRO = "Join Dunder Mifflin, a regional leader in paper. We empower our people."
FLAT = (f"{INTRO} Key Responsibilities • Sell paper to regional accounts. "
        "• Work with the Benefits Administration team on renewals. "
        "Qualifications Education: Bachelor's degree. Two years of sales experience. "
        "The salary range for this role is $50,000 - $60,000. "
        "Dunder Mifflin is an equal opportunity employer without regard to race.")

HTML = ("<p>Join Dunder Mifflin, a regional leader in paper.</p>"
        "<p><b>Key Responsibilities</b></p><ul><li>Sell paper to regional accounts.</li></ul>"
        "<p><b>Qualifications</b></p><ul><li>Two years of sales experience.</li>"
        "<li>A valid driver's license.</li></ul>"
        "<p>The hourly pay range for this position is $18.50-$22.00/hr.</p>"
        "<p>Dunder Mifflin is an equal opportunity employer without regard to race.</p>")
FMT = {"boilerplate": [{"kind": "company_intro", "starts_with": "Join Dunder Mifflin, a regional leader"}]}

OLD_ROW = {"posting_id": "DM-1", "title": "Sales Rep", "description_raw": FLAT}
NEW_ROW = {"posting_id": "DM-2", "title": "Sales Rep", "description_raw": None,
           "description": description.parse(HTML, FMT)}


class FromText(unittest.TestCase):
    def setUp(self):
        self.d = description.from_text(FLAT, skip_sentences=2)

    def test_sections_found_in_flat_text(self):
        s = self.d["sections"]
        self.assertEqual(s["responsibilities"], ["Sell paper to regional accounts.",
                                                 "Work with the Benefits Administration team on renewals."])
        self.assertIn("Two years of sales experience.", s["qualifications"])
        self.assertTrue(self.d["approximate"])

    def test_mid_sentence_heading_words_do_not_split(self):
        # "Benefits" opens no section inside "the Benefits Administration team"
        self.assertNotIn("benefits", self.d["sections"])

    def test_intro_eeo_and_pay(self):
        s = self.d["sections"]
        self.assertEqual(s["company_intro"], ["Join Dunder Mifflin, a regional leader in paper.",
                                              "We empower our people."])
        self.assertEqual(len(s["eeo"]), 1)
        self.assertEqual(self.d["compensation"]["max"], 60000)
        role = description.role_text(self.d)
        self.assertNotIn("Join Dunder Mifflin", role)
        self.assertNotIn("equal opportunity", role)

    def test_alias_from_the_board_format(self):
        d = description.from_text("SKILLS • Sell paper. • Count paper.", {"aliases": {"responsibilities": ["SKILLS"]}})
        self.assertEqual(d["sections"]["responsibilities"], ["Sell paper.", "Count paper."])

    def test_empty(self):
        self.assertIsNone(description.from_text(None))
        self.assertEqual(description.full_text(None), "")

    def test_full_text_keeps_everything(self):
        full = description.full_text(NEW_ROW["description"])
        for part in ("Join Dunder Mifflin", "Sell paper", "equal opportunity"):
            self.assertIn(part, full)


class Accessors(unittest.TestCase):
    def test_parsed_prefers_the_collector(self):
        self.assertIs(report.parsed_description(NEW_ROW), NEW_ROW["description"])
        self.assertTrue(report.parsed_description(OLD_ROW)["approximate"])
        self.assertIsNone(report.parsed_description(None))

    def test_role_and_full_text(self):
        self.assertNotIn("Join Dunder Mifflin", report.role_text_of(NEW_ROW))
        self.assertIn("Join Dunder Mifflin", report.full_text_of(NEW_ROW))
        self.assertEqual(report.full_text_of(OLD_ROW), FLAT)
        self.assertNotIn("Join Dunder Mifflin", report.role_text_of(OLD_ROW, boilerplate_n=2))

    def test_structured_row_without_intro_rule_is_trimmed(self):
        row = {"description": description.parse(HTML)}   # no format: the intro sits in summary
        self.assertIn("Join Dunder Mifflin", report.role_text_of(row))
        self.assertNotIn("Join Dunder Mifflin", report.role_text_of(row, boilerplate_n=1))
        # a row whose intro rule already matched is not trimmed again
        self.assertTrue(report.role_text_of(NEW_ROW, boilerplate_n=1).startswith("Sell paper"))

    def test_pay(self):
        self.assertEqual(report.pay_of(NEW_ROW)["period"], "hour")
        self.assertEqual(report.pay_label(report.pay_of(NEW_ROW)), "$18.50–$22 an hour")
        yearly = report.pay_of(OLD_ROW)
        self.assertEqual(report.pay_label(yearly), "$50,000–$60,000 a year")
        self.assertEqual(report.pay_label(yearly, short=True), "$50K–$60K")
        self.assertIsNone(report.pay_label(None))

    def test_hiring_profile_uses_qualifications(self):
        self.assertEqual(report.hiring_profile(NEW_ROW),
                         "Two years of sales experience; A valid driver's license")

    def test_detect_boilerplate_prefix_reads_structured_rows(self):
        board = {f"DM-{i}": {"description": description.parse(HTML)} for i in range(4)}
        self.assertGreaterEqual(report.detect_boilerplate_prefix(board), 1)


class ArchiveSections(unittest.TestCase):
    def test_sections_render_without_boilerplate(self):
        out = report._description_sections(NEW_ROW["description"])
        self.assertIn("<h4 class=\"arch-sec\">Responsibilities</h4>", out)
        self.assertIn("Sell paper to regional accounts.", out)
        self.assertNotIn("regional leader in paper", out)
        self.assertNotIn("equal opportunity employer", out)
        self.assertIn("left out", out)
        self.assertNotIn("rebuilt", out)

    def test_approximate_rebuild_says_so(self):
        out = report._description_sections(report.parsed_description(OLD_ROW, 2))
        self.assertIn("rebuilt from the posting", out)


if __name__ == "__main__":
    unittest.main()
