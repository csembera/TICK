"""
Tests for the persona editor: the join Lambda's /api/profile, and the pages
that call it (the editor on /accounts, My terms on the dashboard).

    python3 -m unittest discover tests

The Lambda is loaded with fake S3 and Cognito clients and driven with
function-URL events carrying a real session cookie, signed with a test
secret. No network.
"""

import hashlib
import hmac
import importlib.util
import io
import json
import os
import sys
import time
import types
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
JOIN = os.path.join(ROOT, "infra", "src", "join")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import persona  # noqa: E402
import tick_insights_report as report  # noqa: E402

SECRET = "test-secret-" + "0" * 40
PAM = "11111111-2222-3333-4444-555555555555"
READER = {"role": "account_executive", "sells_for": "Initech",
          "offer": "Hardened container images", "buyers": ["security"],
          "focus_terms": ["sbom", "kubernetes"], "competitor_terms": ["snyk"]}


class ClientError(Exception):
    def __init__(self, response, operation):
        super().__init__(operation)
        self.response = response


class FakeS3:
    def __init__(self):
        self.objects = {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            # S3's answer to a missing key for a caller that may not list the bucket
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body


class FakeCognito:
    def list_users(self, UserPoolId, Filter, Limit):
        return {"Users": [{"Attributes": [{"Name": "email", "Value": "pam@example.com"},
                                          {"Name": "custom:accounts", "Value": "initech,dunder-mifflin"}]}]}


def load_join(s3):
    """The join Lambda, under its own module name (the collector is `app`
    elsewhere in the suite), with fake AWS clients."""
    fake_boto3 = types.SimpleNamespace(
        resource=lambda *a, **k: types.SimpleNamespace(Table=lambda name: None),
        client=lambda name, *a, **k: {"s3": s3, "cognito-idp": FakeCognito()}.get(name))
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = ClientError
    env = {"TABLE_NAME": "joins", "SESSION_SECRET": SECRET, "USER_POOL_ID": "pool", "DATA_BUCKET": "data"}
    spec = importlib.util.spec_from_file_location("join_app", os.path.join(JOIN, "app.py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"boto3": fake_boto3, "botocore": types.ModuleType("botocore"),
                                       "botocore.exceptions": exceptions}), \
            mock.patch.dict(os.environ, env), mock.patch.object(sys, "path", [JOIN, *sys.path]):
        spec.loader.exec_module(module)
    return module


def cookie(sub=PAM):
    payload = f"{sub}:initech:{int(time.time()) + 3600}"
    sig = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"tick_session={payload}.{sig}"


def event(method, body=None, signed_in=True, path="/api/profile"):
    return {"rawPath": path, "requestContext": {"http": {"method": method}},
            "cookies": [cookie()] if signed_in else [], "body": json.dumps(body) if body is not None else None}


class ProfileApi(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.join = load_join(self.s3)

    def call(self, *args, **kwargs):
        r = self.join.handler(event(*args, **kwargs), None)
        return r["statusCode"], json.loads(r["body"])

    def saved(self):
        return json.loads(self.s3.objects[f"profiles/{PAM}.json"])

    def test_without_a_session_nothing_is_read_or_written(self):
        self.assertEqual(self.call("GET", signed_in=False)[0], 401)
        self.assertEqual(self.call("POST", {"persona": READER}, signed_in=False)[0], 401)
        self.assertEqual(self.s3.objects, {})

    def test_a_new_viewer_has_no_persona_and_sees_what_they_track(self):
        code, body = self.call("GET")
        self.assertEqual(code, 200)
        self.assertIsNone(body["persona"])
        self.assertEqual(body["tracks"], ["dunder-mifflin", "initech"])

    def test_a_saved_persona_is_cleaned_and_kept_under_the_cookies_sub(self):
        code, body = self.call("POST", {"persona": {**READER, "focus_terms": ["sbom", "SBOM"], "extra": 1},
                                        "sub": "someone-else"})
        self.assertEqual(code, 200)
        self.assertEqual(list(self.s3.objects), [f"profiles/{PAM}.json"])   # the body cannot pick the key
        self.assertEqual(self.saved()["persona"]["focus_terms"], ["sbom"])
        self.assertNotIn("extra", self.saved()["persona"])
        self.assertEqual(self.call("GET")[1]["persona"], persona.clean({**READER, "focus_terms": ["sbom"]})[0])

    def test_a_partial_save_keeps_the_other_fields(self):
        self.call("POST", {"persona": READER})
        code, body = self.call("POST", {"persona": {"focus_terms": ["terraform"]}})   # what My terms sends
        self.assertEqual(code, 200)
        self.assertEqual(body["persona"]["focus_terms"], ["terraform"])
        self.assertEqual(body["persona"]["sells_for"], "Initech")

    def test_a_bad_field_is_refused_and_nothing_changes(self):
        self.call("POST", {"persona": READER})
        before = dict(self.s3.objects)
        code, body = self.call("POST", {"persona": {"focus_terms": ["<script>"], "role": "wizard"}})
        self.assertEqual(code, 400)
        self.assertEqual(len(body["problems"]), 2)
        self.assertEqual(self.s3.objects, before)

    def test_malformed_and_oversized_bodies_are_refused(self):
        self.assertEqual(self.call("POST", {"persona": ["not", "an", "object"]})[0], 400)
        self.assertEqual(self.call("POST", {"persona": {"offer": "x" * 9000}})[0], 400)
        self.assertEqual(self.s3.objects, {})

    def test_the_other_routes_are_unchanged(self):
        self.assertEqual(self.call("GET", path="/api/join")[0], 405)


class Pages(unittest.TestCase):
    def test_the_accounts_page_has_the_editor_hidden_until_the_api_answers(self):
        page = report.render_home([])
        self.assertIn('<section class="persona" id="persona" aria-labelledby="persona-h" hidden>', page)
        self.assertIn('href="#persona">Your persona</a>', page)
        for key, label in persona.ROLES.items():
            self.assertIn(f'<option value="{key}">{label}</option>', page)
        for key in persona.BUYERS:
            self.assertIn(f'name="buyers" value="{key}"', page)
        self.assertIn(f'maxlength="{persona.OFFER_CHARS}"', page)
        self.assertIn("tickApi('GET', '/api/profile')", page)

    def test_the_dashboard_saves_my_terms_to_the_persona(self):
        script = report._tables_script()
        self.assertIn("function tickApi", script)
        self.assertIn("tickApi('POST', '/api/profile', {persona: {focus_terms: mine}})", script)
        self.assertIn(f"MAX_TERMS = {persona.MAX_FOCUS}", script)


if __name__ == "__main__":
    unittest.main()
