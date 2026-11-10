"""
Tests for the welcome email in infra/template.yaml (InviteMessageTemplate).

    python3 -m unittest discover tests

Cognito only checks the message when the stack deploys, and a bad one fails the
whole deploy, so these catch it first: both placeholders are there, the body
and subject fit Cognito's limits, and the HTML's tables close. The template is
read as text, since its CloudFormation tags need a YAML loader the suite
does not have.
"""

import os
import re
import unittest

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "infra", "template.yaml")


def invite_template() -> tuple[str, str]:
    """The invite's subject and body, as Cognito receives them."""
    with open(TEMPLATE) as f:
        lines = f.read().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "InviteMessageTemplate:")
    subject = body = None
    i = start + 1
    while i < len(lines):
        key = lines[i].strip()
        if key.startswith("EmailSubject:"):
            subject = key.split(":", 1)[1].strip().strip("'\"")
        elif key == "EmailMessage: |-":
            indent = len(lines[i]) - len(lines[i].lstrip())
            block = []
            i += 1
            # a literal block runs until the first non-blank line indented no
            # deeper than its key
            while i < len(lines) and (not lines[i].strip()
                                      or len(lines[i]) - len(lines[i].lstrip()) > indent):
                block.append(lines[i])
                i += 1
            depth = min(len(b) - len(b.lstrip()) for b in block if b.strip())
            body = "\n".join(b[depth:] for b in block).rstrip("\n")
            continue
        elif not lines[i].startswith(" " * 10) and key and not key.startswith("#"):
            break
        i += 1
    return subject, body


class InviteEmailTest(unittest.TestCase):
    def setUp(self):
        self.subject, self.body = invite_template()

    def test_has_each_placeholder_once(self):
        self.assertEqual(self.body.count("{username}"), 1)
        self.assertEqual(self.body.count("{####}"), 1)

    def test_fits_cognito_limits(self):
        self.assertLessEqual(len(self.body), 20000)
        self.assertTrue(0 < len(self.subject) <= 140)

    def test_links_to_sign_in(self):
        self.assertIn('href="https://tick.sembasolutions.com/login"', self.body)

    def test_tables_close(self):
        for tag in ("table", "tr", "td"):
            opened = len(re.findall(rf"<{tag}[\s>]", self.body))
            self.assertEqual(opened, self.body.count(f"</{tag}>"), tag)

    def test_no_style_block(self):
        # Gmail drops <style>, so every rule has to be inline
        self.assertNotIn("<style", self.body)


if __name__ == "__main__":
    unittest.main()
