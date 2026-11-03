"""
Tests for tools/tick.py, the operations CLI.

    python3 -m unittest discover tests

The AWS CLI is replaced by a fake that answers from a fixed account and
records every call, so these check what the CLI would ask AWS to do: that a
grant keeps the rest of a viewer's accounts, that a single-company crawl sends
the dispatcher's own message, that a dry run changes nothing. No network.
The accounts are the fictional Dunder Mifflin and Initech.
"""

import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import tick  # noqa: E402

OUTPUTS = {"DataBucketName": "data", "SiteBucketName": "site", "SiteDistributionId": "DIST",
           "UserPoolId": "pool", "ViewerGroup": "tick-viewers", "AdminGroup": "tick-admins",
           "CollectQueueUrl": "https://sqs/collect", "CollectDLQUrl": "https://sqs/dlq",
           "PublisherFunctionName": "tick-collector-publisher",
           "DispatcherFunctionName": "tick-collector-dispatcher",
           "JoinRequestsTableName": "joins", "AccountRequestsTableName": "account-requests",
           "SiteUrl": "https://tick.example", "SiteLoginDomain": "https://login.example"}
DM = {"company_name": "Dunder Mifflin", "company_slug": "dunder-mifflin", "ats": "workday",
      "schema_map": {"posting_id": "id"}}
INITECH = {"company_name": "Initech", "company_slug": "initech", "ats": "greenhouse",
           "schema_map": {"posting_id": "id"}}
# the bucket names a config otherwise than its slug, as it does for real
CONFIGS = {"configs/dm.json": DM, "configs/initech.json": INITECH}


class FakeAws:
    """Answers AWS CLI calls from a small fake account and records them."""

    def __init__(self, user_accounts="dunder-mifflin", groups=("tick-viewers",), homes=(),
                 configs=None, invoke_result=None, function_error=None):
        self.calls = []
        self.user_accounts, self.groups, self.homes = user_accounts, list(groups), set(homes)
        self.configs = dict(CONFIGS if configs is None else configs)
        self.invoke_result = invoke_result if invoke_result is not None else {"pages": 3}
        self.function_error = function_error

    def __call__(self, cmd):
        self.calls.append(cmd)
        op = cmd[2] if cmd[1] != "s3" else f"s3 {cmd[2]}"
        if op == "describe-stacks":
            return json.dumps({"Stacks": [{"Outputs": [{"OutputKey": k, "OutputValue": v}
                                                        for k, v in OUTPUTS.items()]}]})
        if op == "list-objects-v2":
            prefix = cmd[cmd.index("--prefix") + 1]
            keys = list(self.configs) if prefix == "configs/" else []
            return json.dumps({"Contents": [{"Key": k, "LastModified": "2026-11-02T03:01:00+00:00"}
                                            for k in keys]})
        if op == "s3 cp" and cmd[3].startswith("s3://"):      # reading a config
            key = cmd[3].split("/", 3)[3]
            return json.dumps(self.configs[key])
        if op == "admin-get-user":
            if cmd[cmd.index("--username") + 1] == "nobody@example.com":
                raise tick.AwsError("An error occurred (UserNotFoundException)")
            return json.dumps({"UserStatus": "CONFIRMED", "Enabled": True, "UserAttributes": [
                {"Name": "email", "Value": "pam@example.com"},
                {"Name": "custom:accounts", "Value": self.user_accounts}]})
        if op == "admin-list-groups-for-user":
            return json.dumps({"Groups": [{"GroupName": g} for g in self.groups]})
        if op == "head-object":
            key = cmd[cmd.index("--key") + 1]
            if key not in self.homes:
                raise tick.AwsError("An error occurred (404) when calling the HeadObject operation")
            return "{}"
        if op == "invoke":
            out = next(a for a in cmd if a.endswith("result.json"))
            with open(out, "w") as f:
                f.write(json.dumps(self.invoke_result))
            meta = {"StatusCode": 200}
            if self.function_error:
                meta["FunctionError"] = self.function_error
            return json.dumps(meta)
        if op == "get-item":
            return "{}"
        if op == "get-queue-attributes":
            return json.dumps({"Attributes": {"ApproximateNumberOfMessages": "0",
                                              "ApproximateNumberOfMessagesNotVisible": "0",
                                              "QueueArn": "arn:aws:sqs:us-east-1:1:dlq"}})
        # every call that changes something just succeeds
        return "{}"

    def ops(self, name):
        return [c for c in self.calls if name in c]


def run(argv, fake, stdin_tty=True):
    """main(argv) against the fake; returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    stdin = sys.stdin if stdin_tty else io.StringIO()      # a StringIO is not a terminal
    with mock.patch.object(tick, "run", fake), mock.patch.object(sys, "stdin", stdin), \
            redirect_stdout(out), redirect_stderr(err):
        try:
            code = tick.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def attr_value(call):
    return json.loads(call[call.index("--user-attributes") + 1])[0]["Value"]


class Users(unittest.TestCase):
    def test_grant_keeps_the_rest_of_the_list(self):
        fake = FakeAws(user_accounts="dunder-mifflin")
        code, out, _ = run(["users", "grant", "pam@example.com", "initech", "--yes"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(attr_value(fake.ops("admin-update-user-attributes")[0]), "dunder-mifflin,initech")

    def test_grant_refuses_a_company_that_is_not_tracked(self):
        fake = FakeAws()
        code, _, err = run(["users", "grant", "pam@example.com", "initech", "vance-refrigeration", "--yes"], fake)
        self.assertEqual(code, 1)
        self.assertIn("vance-refrigeration", err)
        self.assertFalse(fake.ops("admin-update-user-attributes"))

    def test_revoke_removes_only_what_it_names(self):
        fake = FakeAws(user_accounts="dunder-mifflin,initech")
        code, out, _ = run(["users", "revoke", "pam@example.com", "initech", "--yes"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(attr_value(fake.ops("admin-update-user-attributes")[0]), "dunder-mifflin")
        self.assertIn("12 hours", out)

    def test_revoking_the_last_account_writes_an_empty_list(self):
        fake = FakeAws(user_accounts="initech")
        run(["users", "revoke", "pam@example.com", "initech", "--yes"], fake)
        self.assertEqual(attr_value(fake.ops("admin-update-user-attributes")[0]), "")

    def test_a_change_republishes_only_when_its_home_is_missing(self):
        home = f"home/{tick.scope_key('dunder-mifflin,initech')}/index.html"
        fake = FakeAws(homes={home})
        run(["users", "grant", "pam@example.com", "initech", "--yes"], fake)
        self.assertFalse(fake.ops("invoke"))
        fake = FakeAws()
        run(["users", "grant", "pam@example.com", "initech", "--yes"], fake)
        self.assertEqual(len(fake.ops("invoke")), 1)

    def test_invite_creates_adds_to_group_and_sets_accounts(self):
        fake = FakeAws()
        code, _, _ = run(["users", "invite", "nobody@example.com", "--accounts", "initech", "--yes"], fake)
        self.assertEqual(code, 0)
        create = fake.ops("admin-create-user")[0]
        attrs = {a["Name"]: a["Value"] for a in json.loads(create[create.index("--user-attributes") + 1])}
        self.assertEqual(attrs["custom:accounts"], "initech")
        self.assertEqual([c[c.index("--group-name") + 1] for c in fake.ops("admin-add-user-to-group")],
                         ["tick-viewers"])

    def test_invite_refuses_an_existing_user(self):
        fake = FakeAws()
        code, _, err = run(["users", "invite", "pam@example.com", "--yes"], fake)
        self.assertEqual(code, 1)
        self.assertIn("already has an account", err)
        self.assertFalse(fake.ops("admin-create-user"))


class Collect(unittest.TestCase):
    def test_one_company_gets_the_dispatchers_own_message(self):
        fake = FakeAws()
        code, _, _ = run(["collect", "dunder-mifflin", "--yes"], fake)
        self.assertEqual(code, 0)
        send = fake.ops("send-message")[0]
        body = send[send.index("--message-body") + 1]
        # byte for byte what infra/src/dispatcher/app.py sends, found by slug
        self.assertEqual(body, json.dumps({"config_key": "configs/dm.json", "config": DM}))
        self.assertEqual(send[send.index("--queue-url") + 1], "https://sqs/collect")

    def test_a_config_waiting_for_review_is_not_crawled(self):
        fake = FakeAws(configs={"configs/dm.json": {**DM, "needs_human_review": True}})
        code, _, err = run(["collect", "dunder-mifflin", "--yes"], fake)
        self.assertEqual(code, 1)
        self.assertIn("needs_human_review", err)
        self.assertFalse(fake.ops("send-message"))

    def test_two_configs_for_one_slug_are_refused(self):
        fake = FakeAws(configs={**CONFIGS, "configs/dunder-mifflin.json": DM})
        code, _, err = run(["collect", "dunder-mifflin", "--yes"], fake)
        self.assertEqual(code, 1)
        self.assertIn("crawled twice", err)


class Publish(unittest.TestCase):
    def test_a_publisher_error_fails_the_command(self):
        fake = FakeAws(invoke_result={"errorMessage": "no tech_stack.json"}, function_error="Unhandled")
        code, _, err = run(["publish", "pages", "--yes"], fake)
        self.assertEqual(code, 1)
        self.assertIn("no tech_stack.json", err)

    def test_static_deletes_only_under_docs_and_demo(self):
        fake = FakeAws()
        code, _, _ = run(["publish", "static", "--yes"], fake)
        self.assertEqual(code, 0)
        deleting = [c for c in fake.calls if "--delete" in c]
        self.assertEqual(len(deleting), 4)
        for c in deleting:
            self.assertRegex(c[4], r"^s3://site/(docs|demo)/$")
        self.assertEqual(len(fake.ops("create-invalidation")), 1)


class Companies(unittest.TestCase):
    def push(self, cfg, fake):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        return run(["companies", "push", path, "--yes"], fake)

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def uploads(self, fake):
        return [c for c in fake.ops("cp") if c[1] == "s3" and c[4].startswith("s3://data/")]

    def test_a_changed_config_replaces_the_key_that_holds_its_slug(self):
        fake = FakeAws()
        code, out, _ = self.push({**DM, "ats": "greenhouse"}, fake)
        self.assertEqual(code, 0)
        self.assertIn('- "ats": "workday"', out)
        self.assertEqual(self.uploads(fake)[0][4], "s3://data/configs/dm.json")

    def test_a_new_company_is_named_by_its_slug(self):
        fake = FakeAws()
        self.push({**DM, "company_slug": "vance-refrigeration"}, fake)
        self.assertEqual(self.uploads(fake)[0][4], "s3://data/configs/vance-refrigeration.json")

    def test_an_unchanged_config_is_not_uploaded(self):
        fake = FakeAws()
        code, out, _ = self.push(DM, fake)
        self.assertEqual(code, 0)
        self.assertFalse(self.uploads(fake))

    def test_reserved_and_malformed_slugs_are_refused(self):
        # /docs/ is public at the edge: a company there would be served without a session
        for slug in ("docs", "accounts", "Dunder Mifflin"):
            fake = FakeAws()
            code, _, err = self.push({**DM, "company_slug": slug}, fake)
            self.assertEqual(code, 1, slug)
            self.assertFalse(self.uploads(fake), slug)


class Safety(unittest.TestCase):
    def test_a_dry_run_changes_nothing(self):
        fake = FakeAws()
        code, out, _ = run(["users", "grant", "pam@example.com", "initech", "--dry-run"], fake)
        self.assertEqual(code, 0)
        self.assertIn("would run: aws cognito-idp admin-update-user-attributes", out)
        self.assertFalse(fake.ops("admin-update-user-attributes"))

    def test_no_terminal_and_no_yes_refuses(self):
        fake = FakeAws()
        code, _, err = run(["collect", "dunder-mifflin"], fake, stdin_tty=False)
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertFalse(fake.ops("send-message"))

    def test_options_work_before_or_after_the_command(self):
        fake = FakeAws()
        run(["--dry-run", "collect", "dunder-mifflin"], fake)
        self.assertFalse(fake.ops("send-message"))
        run(["collect", "dunder-mifflin", "--dry-run"], fake)
        self.assertFalse(fake.ops("send-message"))


class Verify(unittest.TestCase):
    def test_a_guarded_page_served_without_a_session_fails(self):
        answers = {p: (200, "") for p in tick.PUBLIC_PATHS}
        answers.update({p: (302, "https://tick.example/auth/login?next=x") for p in tick.GUARDED_PATHS})
        answers["/accounts"] = (200, "")
        fetch = lambda url: answers[url.replace("https://tick.example", "") or "/"]
        with mock.patch.object(tick, "fetch", fetch):
            code, _, err = run(["verify"], FakeAws())
        self.assertEqual(code, 1)
        self.assertIn("/accounts returned 200", err)


class Scope(unittest.TestCase):
    def test_scope_matches_the_publisher(self):
        """The CLI checks for a viewer's home by the key the publisher writes."""
        import importlib.util
        sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *a, **k: None))
        here = os.path.join(ROOT, "infra", "src", "publisher")
        sys.path.insert(0, here)       # for its report.py and description.py
        # under its own name: other tests have already imported the collector as `app`
        spec = importlib.util.spec_from_file_location("publisher_app", os.path.join(here, "app.py"))
        publisher = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"DATA_BUCKET": "d", "SITE_BUCKET": "s"}):
            spec.loader.exec_module(publisher)
        sys.path.remove(here)
        for groups, accounts in ((["tick-admins"], "x"), ([], "Initech, dunder-mifflin,initech"),
                                 ([], ""), ([], "bad slug,ok")):
            ours = tick.account_scope(groups, accounts, "tick-admins")
            self.assertEqual(ours, publisher.account_scope(groups, accounts))
            self.assertEqual(tick.scope_key(ours), publisher.scope_key(ours))


if __name__ == "__main__":
    unittest.main()
