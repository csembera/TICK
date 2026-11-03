"""
Tests for the persona editor: the join Lambda's /api/profile, and the pages
that call it (the account menu on every app page, the editor on /profile,
My terms on the dashboard).

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
        self.assertEqual(body["email"], "pam@example.com")   # the account menu's name

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


ACCOUNT = {"slug": "initech", "name": "Initech", "ats": "greenhouse", "tier": None, "board": None,
           "has_pages": False, "open_roles": None, "captured_at": None, "careers_url": None,
           "status": "Not collected", "confidence": None}


def load_publisher():
    """The publisher Lambda, under its own module name, rendering through its
    report.py symlink: the renderer exactly as the Lambda runs it."""
    here = os.path.join(ROOT, "infra", "src", "publisher")
    fake_boto3 = types.SimpleNamespace(client=lambda *a, **k: None)
    spec = importlib.util.spec_from_file_location("publisher_app", os.path.join(here, "app.py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"boto3": fake_boto3}), \
            mock.patch.dict(os.environ, {"DATA_BUCKET": "d", "SITE_BUCKET": "s"}), \
            mock.patch.object(sys, "path", [here, *sys.path]):
        spec.loader.exec_module(module)
    return module


class Pages(unittest.TestCase):
    def signed_in_pages(self):
        return {"accounts": report.render_home([ACCOUNT]), "profile": report.render_profile(),
                "brief": report.render_no_brief("initech", "Initech", "today")}

    def test_the_profile_page_has_the_editor_hidden_until_the_api_answers(self):
        page = report.render_profile()
        self.assertIn('<section class="persona" id="persona" aria-labelledby="persona-h" hidden>', page)
        for key, label in persona.ROLES.items():
            self.assertIn(f'<option value="{key}">{label}</option>', page)
        for key in persona.BUYERS:
            self.assertIn(f'name="buyers" value="{key}"', page)
        self.assertIn(f'maxlength="{persona.OFFER_CHARS}"', page)
        self.assertIn("tickProfile().then", page)
        self.assertIn("tickApi('POST', '/api/profile', {persona: p})", page)
        self.assertIn('id="profile-lede" role="status"', page)   # says so if the profile cannot load

    def test_the_accounts_page_no_longer_holds_the_editor(self):
        page = report.render_home([ACCOUNT])
        for gone in ('id="persona"', 'id="persona-form"', 'href="#persona"', "_persona"):
            self.assertNotIn(gone, page)

    def test_every_signed_in_page_has_the_account_menu(self):
        for name, page in self.signed_in_pages().items():
            with self.subTest(name):
                self.assertIn('<details class="nav-menu" id="nav-menu">', page)
                self.assertIn('href="/profile"', page)
                self.assertIn("<span>Edit profile</span>", page)
                self.assertIn('href="/auth/logout" id="nav-signout"', page)
                self.assertNotIn('class="nav-login"', page)
                # defined once, in the menu bar, before any script that calls them
                self.assertEqual(page.count("function tickApi"), 1)
                self.assertLess(page.index("window.tickProfile"), page.index('<main id="main">'))

    def test_the_menu_marks_the_profile_page_only_on_it(self):
        self.assertIn('href="/profile" aria-current="page"', report.render_profile())
        self.assertNotIn('href="/profile" aria-current="page"', report.render_home([ACCOUNT]))

    def test_the_404_page_offers_log_in_and_no_menu(self):
        page = report.render_404()
        self.assertIn('<a class="nav-login" href="/login">Log in</a>', page)
        self.assertNotIn("nav-menu", page.split("<style>")[0] + page.split("</style>")[1])
        self.assertNotIn("tickApi", page)

    def test_the_dashboard_saves_my_terms_to_the_persona(self):
        script = report._tables_script()
        self.assertNotIn("function tickApi", script)   # the menu bar's script defines it
        self.assertIn("tickProfile().then", script)
        self.assertIn("tickApi('POST', '/api/profile', {persona: {focus_terms: mine}})", script)
        self.assertIn(f"MAX_TERMS = {persona.MAX_FOCUS}", script)

    def test_the_publisher_renders_one_profile_page_beside_the_homes(self):
        publisher = load_publisher()
        lake_io = {"keys": [], "get": None}
        with mock.patch.object(publisher.report, "open_lake", return_value=lake_io), \
                mock.patch.object(publisher.report, "load_configs", return_value=[]), \
                mock.patch.object(publisher.report, "build_accounts", return_value=[ACCOUNT]):
            pages = publisher.render_site("s3://d", scopes={"*", "initech"})
        self.assertEqual(publisher.PROFILE_KEY, "home/profile/index.html")
        self.assertIn("Your profile", pages[publisher.PROFILE_KEY])
        homes = [k for k in pages if k.startswith("home/") and k != publisher.PROFILE_KEY]
        self.assertEqual(sorted(homes), sorted(f"home/{publisher.scope_key(s)}/index.html"
                                               for s in ("*", "initech")))
        self.assertEqual(len(pages), 3)


if __name__ == "__main__":
    unittest.main()
