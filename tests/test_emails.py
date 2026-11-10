"""
Tests for the emails TICK sends to people, all built by tools/email_layout.py.

    python3 -m unittest discover tests

Cognito only checks its two bodies when the stack deploys, and a bad one fails
the whole deploy, so these catch it first: the copies in infra/template.yaml
match the generator, each placeholder is there once, the bodies and subjects
fit Cognito's limits. Then, for every email: the HTML's tables close, there is
no <style> (Gmail drops it), every button is the Outlook-safe kind, and each
ends with who sent it. The template is read as text, since its CloudFormation
tags need a YAML loader the suite does not have.
"""

import os
import re
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import email_layout as mail  # noqa: E402

with open(mail.TEMPLATE) as f:
    TEMPLATE = f.read()

ACCOUNTS = [{"name": "Initech", "slug": "initech", "open_roles": 12, "tech_roles": 1, "crawled_on": "2026-11-09"},
            {"name": "Dunder Mifflin & Co", "slug": "dunder-mifflin", "open_roles": None, "tech_roles": None,
             "crawled_on": None}]


def every_email() -> dict[str, str]:
    return {"invite": mail.invite(), "verification": mail.verification(),
            "waitlist": mail.waitlist_confirmation()[1], "new account": mail.new_accounts(ACCOUNTS[:1])[1],
            "new accounts": mail.new_accounts(ACCOUNTS)[1]}


class CognitoTest(unittest.TestCase):
    def test_template_matches_the_generator(self):
        for which, body in mail.cognito_bodies().items():
            self.assertEqual(mail.template_body(TEMPLATE, which), body,
                             f"{which} drifted: run `python3 tools/email_layout.py cognito --write`")

    def test_has_each_placeholder_once(self):
        invite = mail.template_body(TEMPLATE, "invite")
        self.assertEqual(invite.count("{username}"), 1)
        self.assertEqual(invite.count("{####}"), 1)
        self.assertEqual(mail.template_body(TEMPLATE, "verification").count("{####}"), 1)

    def test_fits_cognito_limits(self):
        for which in ("invite", "verification"):
            self.assertLessEqual(len(mail.template_body(TEMPLATE, which)), 20000, which)
            self.assertTrue(0 < len(mail.template_subject(TEMPLATE, which)) <= 140, which)

    def test_password_days_match_the_pool(self):
        days = re.search(r"TemporaryPasswordValidityDays:\s*(\d+)", TEMPLATE).group(1)
        self.assertEqual(int(days), mail.PASSWORD_DAYS)

    def test_invite_links_to_sign_in(self):
        self.assertIn('href="https://tick.sembasolutions.com/login"', mail.template_body(TEMPLATE, "invite"))


class EveryEmailTest(unittest.TestCase):
    def test_tables_close(self):
        for name, body in every_email().items():
            for tag in ("table", "tr", "td"):
                opened = len(re.findall(rf"<{tag}[\s>]", body))
                self.assertEqual(opened, body.count(f"</{tag}>"), f"{name}: {tag}")

    def test_no_style_block_or_svg(self):
        for name, body in every_email().items():
            self.assertNotIn("<style", body, name)
            self.assertNotIn("<svg", body, name)

    def test_buttons_paint_the_cell(self):
        # Outlook ignores background on <a>: a coloured link would show bare
        for name, body in every_email().items():
            self.assertNotRegex(body, r"<a [^>]*background:", name)

    def test_says_who_sent_it(self):
        for name, body in every_email().items():
            self.assertIn(mail.SENDER_LINE, body, name)
            self.assertIn("display:none", body, f"{name}: no preheader")


class WaitlistTest(unittest.TestCase):
    def test_has_a_text_part_and_says_why(self):
        subject, body, text = mail.waitlist_confirmation()
        self.assertEqual(subject, "You're on the TICK list")
        self.assertIn("/demo", text)
        self.assertIn("If that wasn't you", text)
        self.assertIn("If that wasn&#x27;t you", body)


class NewAccountsTest(unittest.TestCase):
    def test_one_account(self):
        subject, body, text = mail.new_accounts(ACCOUNTS[:1], "https://tick.example")
        self.assertEqual(subject, "Initech is now in your TICK")
        self.assertIn("12 roles open", body)
        self.assertIn("1 tech role among them", body)
        self.assertIn("https://tick.example/initech/dashboard.html", text)
        self.assertIn("Sign out and back in to see it.", text)

    def test_several_and_names_are_escaped(self):
        subject, body, _ = mail.new_accounts(ACCOUNTS)
        self.assertEqual(subject, "2 accounts added to your TICK")
        self.assertIn("Dunder Mifflin &amp; Co", body)
        self.assertNotIn("Dunder Mifflin & Co", body)
        self.assertIn("first crawl runs tonight", body)
        self.assertNotIn("/dunder-mifflin/dashboard.html", body)


if __name__ == "__main__":
    unittest.main()
