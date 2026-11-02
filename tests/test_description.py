"""
Tests for infra/src/collector/description.py.

    python3 -m unittest discover tests

Fixtures are synthetic (the fictional Dunder Mifflin) but copy the layouts
real boards use: bold paragraph headings, colon headings, all-caps headings,
<li> bullets and "•" bullets run together in one bold block, a company intro
and an equal-opportunity statement on every posting, a pay range in prose.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "infra", "src", "collector"))
import description  # noqa: E402

BOLD_PARAGRAPHS = """
<p><b>Join Dunder Mifflin, a regional leader in paper with over a century of service.
We empower our people to build lasting client relationships.</b></p>
<p><b>Key Responsibilities</b></p>
<p><b>• Reconcile ceded paper balances.• Analyze aged balances and resolve reconciling items.</b></p>
<p><b>• Lead process improvements and automation initiatives.</b></p>
<p><b>Qualifications</b></p>
<p>Education: Bachelor's degree in Accounting or related field.</p>
<p>The salary range for this role is $85,000 - $110,000. Actual pay depends on experience.</p>
<p>Dunder Mifflin is an equal opportunity employer, which means we will consider all applicants
without regard to race, age or religion.</p>
"""

COLON_AND_CAPS = """
<div>What you'll be doing:</div>
<ul><li>Sell paper to regional accounts.</li><li>Grow an existing book of business.</li></ul>
<div>Here's what we expect:</div>
<ul><li>Two years of sales experience.</li></ul>
<div><strong>PREFERRED QUALIFICATIONS</strong></div>
<ul><li>A valid driver's license.</li></ul>
<div><strong>WORK ENVIRONMENT</strong></div>
<p>Hybrid, three days a week in Scranton.</p>
<div><strong>How we value you</strong></div>
<p>Medical, dental and a generous paper allowance.</p>
"""

SUBHEADINGS = """
<p><b>Duties and Responsibilities</b></p>
<p><b>Credentialing</b></p>
<ul><li>Verify vendor credentials.</li></ul>
<p><b>Start Your Career in Sales</b></p>
<p>Not a heading this parser knows.</p>
"""


class Headings(unittest.TestCase):
    def test_generic_families(self):
        cases = {
            "Key Responsibilities": "responsibilities",
            "Summary of Responsibilities": "responsibilities",
            "What you'll be doing": "responsibilities",
            "On any given day, you'll": "responsibilities",
            "Your role and impact": "responsibilities",
            "Qualifications": "qualifications",
            "Knowledge, Skills, and Abilities": "qualifications",
            "Skills and Experience Required": "qualifications",
            "Here's what we expect": "qualifications",
            "Preferred Education and Training": "preferred_qualifications",
            "Compensation & Benefits": "compensation",
            "How we value you": "benefits",
            "What's in it for you": "benefits",
            "Company Overview": "company_intro",
            "Why Dunder Mifflin?": "company_intro",
            "Work-at-home requirements": "work_arrangement",
            "Job Description": "summary",
            "Position Summary": "summary",
        }
        for heading, kind in cases.items():
            with self.subTest(heading=heading):
                self.assertEqual(description.classify_heading(heading), kind)

    def test_config_aliases_win(self):
        fmt = {"aliases": {"summary": ["The Opportunity at Hand"], "qualifications": ["Skills"]}}
        self.assertEqual(description.classify_heading("the opportunity at hand", fmt), "summary")
        self.assertEqual(description.classify_heading("Start Your Career in Sales"), None)

    def test_unknown_kind_in_config_is_ignored(self):
        fmt = {"aliases": {"not_a_section": ["Key Responsibilities"]}}
        self.assertEqual(description.classify_heading("Key Responsibilities", fmt), "responsibilities")


class Parse(unittest.TestCase):
    def test_bold_paragraph_layout(self):
        fmt = {"boilerplate": [{"kind": "company_intro", "starts_with": "Join Dunder Mifflin, a regional leader"}]}
        d = description.parse(BOLD_PARAGRAPHS, fmt)
        s = d["sections"]
        self.assertEqual(s["responsibilities"], [
            "Reconcile ceded paper balances.",
            "Analyze aged balances and resolve reconciling items.",
            "Lead process improvements and automation initiatives."])
        self.assertIn("Education: Bachelor's degree in Accounting or related field.", s["qualifications"])
        self.assertEqual(len(s["company_intro"]), 1)
        self.assertEqual(len(s["eeo"]), 1)
        self.assertEqual(d["boilerplate"], ["company_intro", "eeo"])
        self.assertEqual(d["format"], "config")
        self.assertNotIn("Join Dunder Mifflin", description.role_text(d))
        self.assertNotIn("equal opportunity", description.role_text(d))

    def test_compensation(self):
        d = description.parse(BOLD_PARAGRAPHS)
        self.assertEqual(d["compensation"]["min"], 85000)
        self.assertEqual(d["compensation"]["max"], 110000)
        self.assertEqual(d["compensation"]["period"], "year")
        self.assertTrue(d["compensation"]["text"].startswith("The salary range"))
        hourly = description.compensation("Pay is $18.50 to $22 per hour.")
        self.assertEqual((hourly["min"], hourly["max"], hourly["period"]), (18.5, 22, "hour"))
        self.assertEqual(description.compensation("$85K–$110K")["max"], 110000)
        self.assertIsNone(description.compensation("Competitive pay."))

    def test_colon_caps_and_list_layout(self):
        s = description.parse(COLON_AND_CAPS)["sections"]
        self.assertEqual(s["responsibilities"], ["Sell paper to regional accounts.",
                                                 "Grow an existing book of business."])
        self.assertEqual(s["qualifications"], ["Two years of sales experience."])
        self.assertEqual(s["preferred_qualifications"], ["A valid driver's license."])
        self.assertEqual(s["work_arrangement"], ["Hybrid, three days a week in Scranton."])
        self.assertEqual(s["benefits"], ["Medical, dental and a generous paper allowance."])

    def test_nothing_is_dropped(self):
        d = description.parse(SUBHEADINGS)
        s = d["sections"]
        # an unknown heading inside a role section stays there as a line
        self.assertEqual(s["responsibilities"][:2], ["Credentialing", "Verify vendor credentials."])
        self.assertIn("Credentialing", d["unmatched_headings"])
        # a role section absorbs what follows until the next known heading
        self.assertIn("Not a heading this parser knows.", s["responsibilities"])

    def test_other_block_outside_role_sections(self):
        d = description.parse("<p><b>Start Your Career in Sales</b></p><p>Some text.</p>")
        self.assertEqual(d["sections"]["other"], [{"heading": "Start Your Career in Sales",
                                                   "lines": ["Some text."]}])
        self.assertEqual(description.coverage(d), 0.0)

    def test_empty(self):
        self.assertIsNone(description.parse(None))
        self.assertIsNone(description.parse("<p> </p>"))
        self.assertEqual(description.role_text(None), "")

    def test_recruiter_tags_are_not_headings(self):
        d = description.parse("<p><b>Key Responsibilities</b></p><p>Sell paper.</p><p><b>#LI-DM1</b></p>")
        self.assertEqual(d["unmatched_headings"], [])
        self.assertEqual(d["sections"]["responsibilities"], ["Sell paper.", "#LI-DM1"])

    def test_plain_line_alias_is_a_heading(self):
        # a board whose template headings are ordinary paragraphs
        page = "<p>Job Summary</p><p>Sell paper to regional accounts.</p><p>Work Location</p><p>Scranton.</p>"
        fmt = {"aliases": {"summary": ["Job Summary"], "work_arrangement": ["Work Location"]}}
        s = description.parse(page, fmt)["sections"]
        self.assertEqual(s["summary"], ["Sell paper to regional accounts."])
        self.assertEqual(s["work_arrangement"], ["Scranton."])
        # without the board's format they are just text
        self.assertIn("Work Location", description.parse(page)["sections"]["summary"])

    def test_why_this_role_is_not_the_company_intro(self):
        self.assertEqual(description.classify_heading("Why Dunder Mifflin?"), "company_intro")
        self.assertNotEqual(description.classify_heading("Why This Role Matters"), "company_intro")

    def test_bare_required_heading(self):
        self.assertEqual(description.classify_heading("Required"), "qualifications")

    def test_long_bold_paragraph_is_not_a_heading(self):
        d = description.parse("<p><b>" + "word " * 30 + "</b></p>")
        self.assertIn("summary", d["sections"])
        self.assertEqual(d["unmatched_headings"], [])


if __name__ == "__main__":
    unittest.main()
