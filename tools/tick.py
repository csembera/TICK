#!/usr/bin/env python3
"""
TICK operations: every task done to the running system, in one command.

    tools/tick.py status               what ran, what is queued, what is failing
    tools/tick.py --help               every command;  tools/tick.py <command> --help

It calls the AWS CLI with your current session, so there is nothing to
install (the same choice as preview.py and the brief skill's publish.py), and
finds every resource from the tick-collector stack's outputs, so nothing here
names a bucket, pool or function.

Reading commands just run. A command that changes production lists what it
will do and asks first: --yes answers for you (the deploy pipeline passes it)
and --dry-run prints the AWS calls instead of making them. With no terminal to
ask on, a changing command refuses unless given --yes.

A company is known by its company_slug, never by its config's file name: the
bucket and the local configs/ folder both hold files named otherwise
(configs/church-mutual.json is church-mutual-insurance). Two configs with one
slug would be crawled twice a night, so every command here matches on the slug.

A user tracks the accounts in their custom:accounts. For a viewer that list is
also what they may open; an admin opens everything and the list says which
accounts they work. Each account's weekly brief is written for the persona of
the one user who tracks it (`personas list`).
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import email_layout                 # tools/email_layout.py: the layout every email shares
import persona as persona_schema   # tools/persona.py: what a persona may hold

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "infra", "site-static")
COGNITO_CSS = os.path.join(ROOT, "infra", "cognito-ui.css")
BANNER_HTML = os.path.join(STATIC, "branding", "cognito-banner.html")
BANNER_PNG = os.path.join(STATIC, "branding", "cognito-banner.png")
LOCAL_CONFIGS = os.path.join(ROOT, "configs")
# Drafts of emails to users, for review before `users send-email`. Gitignored:
# they hold a user's address and real lake numbers.
DRAFTS = os.path.join(ROOT, "local-data", "emails")
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

STACK = os.environ.get("TICK_STACK", "tick-collector")
# Not AWS_REGION: a shell set up for other work must not point TICK elsewhere.
REGION = os.environ.get("TICK_REGION", "us-east-1")

# Cache headers for the public pages: HTML is always revalidated, so a fix is
# live at once; images and other assets may be cached for a day.
NO_CACHE = "no-cache, no-store, must-revalidate"
ASSET_CACHE = "public, max-age=86400, must-revalidate"
# The only prefixes `publish static` may --delete under. The publisher owns
# home/ and every <slug>/, and a sync with --delete anywhere else could take
# the app down.
STATIC_PREFIXES = ("docs", "demo")

# What `verify` requests, with no cookie. A 200 on a guarded path is a
# client-data leak, not a cosmetic bug.
PUBLIC_PATHS = ["/", "/docs", "/demo", "/demo/dunder-mifflin/brief"]
GUARDED_PATHS = ["/accounts", "/profile", "/login", "/dunder-mifflin/dashboard.html",
                 "/dunder-mifflin/brief.html", "/dunder-mifflin/briefs"]

_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
# A company slug is the first segment of its pages' URLs, so it cannot be one
# the edge or CloudFront already routes: /accounts, /profile, /home and /login
# are app pages; /docs and /demo are public, so a company there would be
# served without a session; /auth and /api go to Lambdas.
RESERVED_SLUGS = {"accounts", "profile", "home", "login", "docs", "demo", "auth", "api"}
_EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,190}\.[^\s@]{2,63}$")

SESSION_NOTE = ("A session they already have keeps working until it expires, up to 12 hours: "
                "TICK's cookie is checked at the edge, which never asks Cognito. To end every "
                "session now, deploy a new SessionSecret, which signs everyone out: "
                "docs/operations.md, 'Cut off someone's access now'.")
STALE_AFTER = timedelta(hours=26)   # a nightly crawl older than this missed a night
QUEUE_POLL_S = 20


# ------------------------------------------------------------------ plumbing

class AwsError(Exception):
    pass


def run(cmd: list[str]) -> str:
    """Run one AWS CLI command and return what it printed. Tests replace this."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise AwsError("the AWS CLI is not installed (brew install awscli)") from None
    if r.returncode != 0:
        raise AwsError((r.stderr or r.stdout).strip())
    return r.stdout


def fail(message: str) -> None:
    # In the deploy pipeline the error becomes an annotation on the run.
    prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") == "true" else "error: "
    print(prefix + message, file=sys.stderr)
    raise SystemExit(1)


def _shown(cmd: list[str]) -> str:
    # a whole stylesheet as one argument would bury the command
    return shlex.join(a if len(a) <= 120 else f"<{len(a)} characters>" for a in cmd)


class Ops:
    """One run's settings, and the stack outputs it has looked up."""

    def __init__(self, stack: str = STACK, region: str = REGION, yes: bool = False,
                 dry_run: bool = False):
        self.stack, self.region, self.yes, self.dry_run = stack, region, yes, dry_run
        self._outputs: dict | None = None

    def aws(self, *args: str, change: bool = False, parse: bool = True):
        """One AWS CLI call. A call that changes something only prints itself
        on a dry run; reads always run, since a plan is built from them."""
        cmd = ["aws", *args, "--region", self.region] + (["--output", "json"] if parse else [])
        if change and self.dry_run:
            print("  would run: " + _shown(cmd))
            return None
        out = run(cmd)
        if not parse:
            return out
        return json.loads(out) if out.strip() else {}

    def output(self, key: str) -> str:
        if self._outputs is None:
            stack = self.aws("cloudformation", "describe-stacks", "--stack-name", self.stack)
            self._outputs = {o["OutputKey"]: o["OutputValue"]
                             for o in stack["Stacks"][0].get("Outputs", [])}
        if key not in self._outputs:
            fail(f"stack {self.stack} has no {key} output")
        return self._outputs[key]

    def confirm(self, plan: list[str]) -> bool:
        print("This will:")
        for line in plan:
            print(f"  - {line}")
        if self.dry_run:
            print("Dry run: nothing is changed. The calls it would make:")
            return True
        if self.yes:
            return True
        if not sys.stdin.isatty():
            fail("refusing to change production without --yes: there is no terminal to ask on")
        if input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return False
        return True


def plain(item: dict) -> dict:
    """A DynamoDB item as plain values."""
    out = {}
    for name, typed in item.items():
        (kind, value), = typed.items()
        out[name] = None if kind == "NULL" else value
    return out


def when(iso: str | None) -> str:
    if not iso:
        return "never"
    return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def table(rows: list[list[str]], indent: str = "  ") -> None:
    if not rows:
        return
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print(indent + "  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())


def list_objects(ops: Ops, bucket: str, prefix: str) -> list[dict]:
    # the CLI pages through the listing itself and merges Contents
    return ops.aws("s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix).get("Contents") or []


def invoke(ops: Ops, function: str) -> dict | None:
    """Invoke a Lambda and return its result, failing loudly if it raised: a
    bare `aws lambda invoke` exits 0 either way and reports the error only in
    the payload."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "result.json")
        meta = ops.aws("lambda", "invoke", "--function-name", function, "--cli-read-timeout", "0",
                       out, change=True)
        if meta is None:
            return None
        body = open(out).read() if os.path.exists(out) else ""
    if meta.get("FunctionError"):
        fail(f"{function} failed: {body}")
    return json.loads(body) if body.strip() else {}


# --------------------------------------------------------------- account scope
#
# A viewer's scope names the accounts home the edge serves them. Keep these in
# step with account_scope() and scope_key() in infra/src/publisher/app.py (and
# infra/src/auth/app.py); tests/test_tick_cli.py checks they agree.

def account_scope(groups: list[str], accounts: str, admin_group: str) -> str:
    if admin_group in groups:
        return "*"
    slugs = {s.strip().lower() for s in (accounts or "").split(",")}
    return ",".join(sorted(s for s in slugs if _SLUG_RE.match(s)))


def scope_key(scope: str) -> str:
    return hashlib.sha256(scope.encode()).hexdigest()[:16]


def home_exists(ops: Ops, scope: str) -> bool:
    try:
        ops.aws("s3api", "head-object", "--bucket", ops.output("SiteBucketName"),
                "--key", f"home/{scope_key(scope)}/index.html")
        return True
    except AwsError:
        return False


# ------------------------------------------------------------------ companies

def live_configs(ops: Ops) -> list[dict]:
    """Every SourceConfig in the bucket: [{key, config}], config None if unreadable."""
    bucket = ops.output("DataBucketName")
    keys = [o["Key"] for o in list_objects(ops, bucket, "configs/") if o["Key"].endswith(".json")]

    def read(key: str) -> dict:
        try:
            return {"key": key, "config": ops.aws("s3", "cp", f"s3://{bucket}/{key}", "-")}
        except (AwsError, ValueError) as exc:
            return {"key": key, "config": None, "error": str(exc)}

    with ThreadPoolExecutor(8) as pool:
        return sorted(pool.map(read, keys), key=lambda c: c["key"])


def slug_of(entry: dict) -> str | None:
    return (entry.get("config") or {}).get("company_slug")


def config_for(configs: list[dict], slug: str) -> dict:
    found = [c for c in configs if slug_of(c) == slug]
    if not found:
        fail(f"no config in the bucket has company_slug {slug!r}; `tools/tick.py companies list` shows them")
    if len(found) > 1:
        fail(f"{slug} has {len(found)} configs ({', '.join(c['key'] for c in found)}), so it is "
             f"crawled twice a night; delete the extra one first")
    return found[0]


def known_slugs(ops: Ops, slugs: list[str]) -> None:
    """Refuse an account that is not tracked: a typo would give a viewer an
    empty home and no error anywhere."""
    live = {slug_of(c) for c in live_configs(ops)}
    unknown = [s for s in slugs if s not in live]
    if unknown:
        fail(f"not a tracked company: {', '.join(unknown)}. `tools/tick.py companies list` shows the slugs")


def check_config(cfg: dict) -> list[str]:
    """Problems that must stop an upload; warnings are printed by the caller."""
    problems = []
    for key in ("company_name", "company_slug", "ats", "schema_map"):
        if not cfg.get(key):
            problems.append(f"missing {key}")
    slug = cfg.get("company_slug") or ""
    if slug and not _SLUG_RE.match(slug):
        problems.append(f"company_slug {slug!r} must be lowercase letters, digits and hyphens")
    if slug in RESERVED_SLUGS:
        problems.append(f"company_slug {slug!r} is reserved: /{slug}/ is already routed by the site")
    return problems


def _pretty(cfg: dict) -> list[str]:
    return json.dumps(cfg, indent=1, sort_keys=True, ensure_ascii=False).splitlines(keepends=True)


def cmd_companies_list(ops: Ops, args) -> int:
    configs = live_configs(ops)
    bucket = ops.output("DataBucketName")
    crawled = {m.group(1): o["LastModified"] for o in list_objects(ops, bucket, "derived/state/")
               if (m := re.search(r"company=([^/]+)/state\.json$", o["Key"]))}
    counts: dict[str, int] = {}
    for c in configs:
        counts[slug_of(c)] = counts.get(slug_of(c), 0) + 1
    rows = [["SLUG", "ATS", "LAST CRAWL", "CONFIG", "NOTE"]]
    for c in sorted(configs, key=lambda c: slug_of(c) or ""):
        cfg, slug = c.get("config") or {}, slug_of(c)
        note = []
        if c.get("config") is None:
            note.append("unreadable: " + c.get("error", "")[:60])
        if cfg.get("needs_human_review"):
            note.append("needs review: the nightly run skips it")
        if slug and counts[slug] > 1:
            note.append("DUPLICATE slug: crawled twice")
        rows.append([slug or "?", cfg.get("ats", "?"), when(crawled.get(slug)), c["key"], "; ".join(note)])
    table(rows)
    return 0


def cmd_companies_push(ops: Ops, args) -> int:
    try:
        with open(args.file) as f:
            cfg = json.load(f)
    except (OSError, ValueError) as exc:
        fail(f"cannot read {args.file}: {exc}")
    problems = check_config(cfg)
    if problems:
        fail(f"{args.file}: " + "; ".join(problems))
    slug = cfg["company_slug"]
    if cfg.get("needs_human_review"):
        print(f"warning: {slug} has needs_human_review set; the nightly run will skip it "
              f"and /accounts will show it as Needs review")

    configs = live_configs(ops)
    same = [c for c in configs if slug_of(c) == slug]
    if len(same) > 1:
        fail(f"{slug} already has {len(same)} configs in the bucket ({', '.join(c['key'] for c in same)}); "
             f"delete the extra one first")
    bucket = ops.output("DataBucketName")
    key = same[0]["key"] if same else f"configs/{slug}.json"
    if same:
        diff = list(difflib.unified_diff(_pretty(same[0]["config"]), _pretty(cfg),
                                         f"s3://{bucket}/{key}", args.file, n=2))
        if not diff:
            print(f"{slug}: the live config is already this one. Nothing to do.")
            return 0
        print("".join(diff[:120]) + (f"... {len(diff) - 120} more lines\n" if len(diff) > 120 else ""))
        plan = [f"replace the live config for {slug} at s3://{bucket}/{key}",
                "the next crawl uses it: tonight's, or `tools/tick.py collect " + slug + "` now"]
    else:
        plan = [f"add {slug} ({cfg.get('company_name')}) at s3://{bucket}/{key}",
                "it is listed on /accounts as Not collected after the next publish, and crawled "
                "tonight or by `tools/tick.py collect " + slug + " --publish`",
                f"no viewer sees it until it is granted: `tools/tick.py users grant <email> {slug}`"]
    if not ops.confirm(plan):
        return 1
    ops.aws("s3", "cp", args.file, f"s3://{bucket}/{key}", "--content-type", "application/json",
            "--only-show-errors", change=True, parse=False)
    if not ops.dry_run:
        print(f"Uploaded {slug}.")
    return 0


def cmd_companies_pull(ops: Ops, args) -> int:
    """Refresh the local configs/ from the bucket. A local file is matched by
    the slug inside it, so starr_config.json is updated in place rather than
    joined by a second copy; a company new here lands as <slug>_config.json.
    Local-only files (drafts, the fictional demo) are left alone."""
    os.makedirs(LOCAL_CONFIGS, exist_ok=True)
    local: dict[str, str] = {}
    for name in sorted(os.listdir(LOCAL_CONFIGS)):
        if name.endswith(".json"):
            try:
                with open(os.path.join(LOCAL_CONFIGS, name)) as f:
                    local.setdefault(json.load(f).get("company_slug"), os.path.join(LOCAL_CONFIGS, name))
            except (OSError, ValueError):
                continue
    added, updated, same = [], [], []
    live = [c for c in live_configs(ops) if c.get("config")]
    for c in live:
        slug = slug_of(c)
        path = local.get(slug) or os.path.join(LOCAL_CONFIGS, f"{slug}_config.json")
        text = json.dumps(c["config"], indent=2, ensure_ascii=False) + "\n"
        if os.path.exists(path):
            with open(path) as f:
                if json.loads(f.read() or "{}") == c["config"]:
                    same.append(slug)
                    continue
            updated.append(f"{slug} -> {os.path.relpath(path, ROOT)}")
        else:
            added.append(f"{slug} -> {os.path.relpath(path, ROOT)}")
        with open(path, "w") as f:
            f.write(text)
    print(f"configs/: {len(added)} added, {len(updated)} updated, {len(same)} unchanged")
    for line in added + updated:
        print(f"  {line}")
    only_local = sorted(s for s in local if s and s not in {slug_of(c) for c in live})
    if only_local:
        print(f"  only local (not in the bucket): {', '.join(only_local)}")
    return 0


# ------------------------------------------------------------------- collect

def queue_depth(ops: Ops, url: str) -> tuple[int, int]:
    attrs = ops.aws("sqs", "get-queue-attributes", "--queue-url", url, "--attribute-names",
                    "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible")["Attributes"]
    return int(attrs["ApproximateNumberOfMessages"]), int(attrs["ApproximateNumberOfMessagesNotVisible"])


def wait_for_queue(ops: Ops, timeout_s: int) -> bool:
    """Wait for the collect queue to empty. Its counts are approximate and lag
    a send by up to a minute, so it must read empty twice in a row."""
    url, dlq = ops.output("CollectQueueUrl"), ops.output("CollectDLQUrl")
    dead_before = queue_depth(ops, dlq)[0]
    deadline, empty_reads = time.time() + timeout_s, 0
    while empty_reads < 2:
        if time.time() > deadline:
            waiting, crawling = queue_depth(ops, url)
            print(f"Still {waiting} waiting and {crawling} crawling after {timeout_s // 60} minutes. "
                  f"A failing crawl is retried every 30 minutes; see `tools/tick.py logs collector`.")
            return False
        time.sleep(QUEUE_POLL_S)
        waiting, crawling = queue_depth(ops, url)
        empty_reads = empty_reads + 1 if waiting + crawling == 0 else 0
        if waiting + crawling:
            print(f"  {waiting} waiting, {crawling} crawling", flush=True)
    dead = queue_depth(ops, dlq)[0] - dead_before
    if dead > 0:
        print(f"{dead} crawl(s) failed three times and moved to the DLQ: `tools/tick.py dlq list`")
        return False
    print("Queue empty: every crawl finished.")
    return True


def publish_pages(ops: Ops) -> None:
    result = invoke(ops, ops.output("PublisherFunctionName"))
    if result is None:
        return
    print(f"Published {result.get('accounts_rendered', '?')} accounts, {result.get('homes_rendered', '?')} "
          f"accounts homes, {result.get('pages', '?')} pages. CDN invalidation: "
          f"{result.get('invalidation') or 'failed; pages refresh within 5 minutes anyway'}.")


def cmd_collect(ops: Ops, args) -> int:
    if bool(args.all) == bool(args.slugs):
        fail("name the companies to crawl, or pass --all")
    after = (["wait for the crawls to finish"] if args.wait or args.publish else []) + \
            (["then re-render the site"] if args.publish else [])
    if args.all:
        if not ops.confirm(["crawl every company in configs/ now, as the 03:00 UTC run does",
                            "a company crawled already today merges into that day's record"] + after):
            return 1
        result = invoke(ops, ops.output("DispatcherFunctionName"))
        if result is not None:
            print(f"Queued {result.get('dispatched')}; skipped {result.get('skipped')} needing review; "
                  f"{result.get('invalid')} unreadable.")
    else:
        configs = live_configs(ops)
        picked = []
        for slug in args.slugs:
            entry = config_for(configs, slug)
            if entry["config"].get("needs_human_review"):
                fail(f"{slug} is flagged needs_human_review, so the nightly run skips it and so does "
                     f"this; re-verify the config with the tick-evaluator skill first")
            picked.append(entry)
        if not ops.confirm([f"crawl {', '.join(args.slugs)} now: one message each on the collect queue, "
                            f"the same one the 03:00 UTC run sends",
                            "a company crawled already today merges into that day's record"] + after):
            return 1
        url = ops.output("CollectQueueUrl")
        for entry in picked:
            body = json.dumps({"config_key": entry["key"], "config": entry["config"]})
            ops.aws("sqs", "send-message", "--queue-url", url, "--message-body", body, change=True)
        if not ops.dry_run:
            print(f"Queued {len(picked)}.")
    if ops.dry_run or not (args.wait or args.publish):
        return 0
    if not wait_for_queue(ops, args.timeout * 60):
        return 1
    if args.publish:
        publish_pages(ops)
    return 0


# -------------------------------------------------------------------- publish

def cmd_publish_pages(ops: Ops, args) -> int:
    if not ops.confirm(["re-render /accounts and every account's pages from the lake",
                        "invalidate the CDN, so viewers see them within a minute"]):
        return 1
    publish_pages(ops)
    return 0


def cmd_publish_static(ops: Ops, args) -> int:
    """The public half of the site, from infra/site-static/. Static content
    goes first in any deploy: see infra/README.md § Deploying a change."""
    bucket = args.bucket or ops.output("SiteBucketName")
    dist = args.distribution or ops.output("SiteDistributionId")
    if not ops.confirm([f"upload / and /og-image.png, and sync /docs and /demo to s3://{bucket}",
                        "delete files under docs/ and demo/ that are gone from site-static/, "
                        "and nowhere else",
                        f"invalidate those paths on distribution {dist}"]):
        return 1

    def step(*args):
        out = ops.aws(*args, change=True, parse=False)
        for line in (out or "").splitlines():
            print(f"  {line}")

    step("s3", "cp", os.path.join(STATIC, "index.html"), f"s3://{bucket}/index.html",
         "--content-type", "text/html; charset=utf-8", "--cache-control", NO_CACHE)
    step("s3", "cp", os.path.join(STATIC, "og-image.png"), f"s3://{bucket}/og-image.png",
         "--content-type", "image/png", "--cache-control", ASSET_CACHE)
    for prefix in STATIC_PREFIXES:
        step("s3", "sync", os.path.join(STATIC, prefix) + "/", f"s3://{bucket}/{prefix}/",
             "--exclude", "*", "--include", "*.html", "--delete",
             "--content-type", "text/html; charset=utf-8", "--cache-control", NO_CACHE)
        try:
            step("s3", "sync", os.path.join(STATIC, prefix) + "/", f"s3://{bucket}/{prefix}/",
                 "--exclude", "*.html", "--delete", "--cache-control", ASSET_CACHE)
        except AwsError as exc:   # assets are optional; the pages above are what matter
            print(f"  warning: assets under {prefix}/ did not sync: {exc}")
    ops.aws("cloudfront", "create-invalidation", "--distribution-id", dist, "--paths",
            "/", "/index.html", "/og-image.png", "/docs/*", "/demo/*", change=True)
    if not ops.dry_run:
        print("Published. The invalidation reaches every edge within a minute or so.")
    return 0


def cmd_publish_login(ops: Ops, args) -> int:
    """The Cognito hosted UI's CSS and banner. Owned here, not by the template:
    SetUICustomization and AWS::Cognito::UserPoolUICustomizationAttachment
    write the same object, and declaring both fails the deploy with
    AlreadyExists. Idempotent; Cognito rejects the whole stylesheet if one
    property is off its allowlist; see `tools/tick.py cognito css --help`."""
    for path in (COGNITO_CSS, BANNER_PNG):
        if not os.path.exists(path):
            fail(f"{os.path.relpath(path, ROOT)} is missing"
                 + (" (render it with `tools/tick.py cognito banner`)" if path == BANNER_PNG else ""))
    pool = ops.output("UserPoolId")
    client = ops.aws("cloudformation", "describe-stack-resource", "--stack-name", ops.stack,
                     "--logical-resource-id", "SiteUserPoolClient")["StackResourceDetail"]["PhysicalResourceId"]
    with open(COGNITO_CSS) as f:
        css = f.read()
    if not ops.confirm([f"restyle the sign-in page of pool {pool}, client {client}",
                        f"{css.count(chr(10))} lines of CSS and a {os.path.getsize(BANNER_PNG)} byte banner"]):
        return 1
    ops.aws("cognito-idp", "set-ui-customization", "--user-pool-id", pool, "--client-id", client,
            "--css", css, "--image-file", f"fileb://{BANNER_PNG}", change=True)
    if not ops.dry_run:
        print(f"Applied. Check it at {ops.output('SiteLoginDomain')}.")
    return 0


# --------------------------------------------------------------------- cognito

def cmd_cognito_banner(ops: Ops, args) -> int:
    """Re-render the sign-in banner PNG from its HTML, after a copy edit. Local
    only; `tools/tick.py publish login` uploads it."""
    if not os.path.exists(CHROME):
        fail(f"Chrome not found at {CHROME}; set CHROME to its binary")
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--default-background-color=00000000", "--force-device-scale-factor=2",
                    "--window-size=420,56", "--virtual-time-budget=8000", f"--screenshot={BANNER_PNG}",
                    f"file://{BANNER_HTML}"], capture_output=True)
    size = os.path.getsize(BANNER_PNG)
    if size > 100 * 1024:
        fail(f"{BANNER_PNG} is {size} bytes; Cognito refuses a banner over 100KB")
    print(f"Rendered {os.path.relpath(BANNER_PNG, ROOT)} ({size} bytes; Cognito's limit is 100KB).")
    return 0


def cmd_cognito_css(ops: Ops, args) -> int:
    """Check the sign-in page's live CSS against infra/cognito-ui.css, and show
    the difference if someone changed one without the other. Cognito serves
    no default stylesheet through its API: the template of every class and
    property it accepts is the console's "Download CSS template", under the
    user pool's hosted UI customization."""
    client = ops.aws("cloudformation", "describe-stack-resource", "--stack-name", ops.stack,
                     "--logical-resource-id", "SiteUserPoolClient")["StackResourceDetail"]["PhysicalResourceId"]
    live = ops.aws("cognito-idp", "get-ui-customization", "--user-pool-id", ops.output("UserPoolId"),
                   "--client-id", client).get("UICustomization", {}).get("CSS") or ""
    if args.print:
        print(live)
        return 0
    with open(COGNITO_CSS) as f:
        repo = f.read()
    if live.strip() == repo.strip():
        print("The live sign-in CSS matches infra/cognito-ui.css.")
        return 0
    if not live:
        print("The sign-in page has no custom CSS: `tools/tick.py publish login` applies it.")
        return 1
    # Cognito drops the file's final newline; without this the last line always differs
    sys.stdout.writelines(difflib.unified_diff((live.rstrip() + "\n").splitlines(True),
                                               (repo.rstrip() + "\n").splitlines(True),
                                               "live sign-in CSS", "infra/cognito-ui.css"))
    print("\nThey differ: `tools/tick.py publish login` makes the live page match the repo.")
    return 1


# ----------------------------------------------------------------------- users

def parse_slugs(text: str | list[str] | None) -> list[str]:
    items = text if isinstance(text, list) else (text or "").split(",")
    return sorted({s.strip().lower() for part in items for s in part.split(",") if s.strip()})


def get_user(ops: Ops, email: str) -> dict | None:
    try:
        u = ops.aws("cognito-idp", "admin-get-user", "--user-pool-id", ops.output("UserPoolId"),
                    "--username", email)
    except AwsError as exc:
        if "UserNotFoundException" in str(exc):
            return None
        raise
    attrs = {a["Name"]: a["Value"] for a in u.get("UserAttributes", [])}
    groups = [g["GroupName"] for g in ops.aws(
        "cognito-idp", "admin-list-groups-for-user", "--user-pool-id", ops.output("UserPoolId"),
        "--username", email).get("Groups", [])]
    return {"email": attrs.get("email", email), "status": u.get("UserStatus"), "enabled": u.get("Enabled"),
            "sub": attrs.get("sub") or u.get("Username"),
            "accounts": parse_slugs(attrs.get("custom:accounts", "")), "groups": groups}


def need_user(ops: Ops, email: str) -> dict:
    user = get_user(ops, email)
    if not user:
        fail(f"no user {email}; `tools/tick.py users invite {email}` creates one")
    return user


def check_email(email: str) -> str:
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        fail(f"{email!r} is not an email address")
    return email


def _accounts_attr(slugs: list[str]) -> str:
    # JSON, not the CLI's Name=..,Value=.. shorthand, which splits on the
    # commas in a list of slugs
    return json.dumps([{"Name": "custom:accounts", "Value": ",".join(slugs)}])


def _home_plan(ops: Ops, groups: list[str], slugs: list[str]) -> tuple[list[str], bool]:
    scope = account_scope(groups, ",".join(slugs), ops.output("AdminGroup"))
    if home_exists(ops, scope):
        return [], False
    return ["re-render the site, so the accounts home for that set of accounts exists"], True


def group_members(ops: Ops, group: str) -> set[str]:
    return {u["Username"] for u in ops.aws("cognito-idp", "list-users-in-group", "--user-pool-id",
                                            ops.output("UserPoolId"), "--group-name", group).get("Users", [])}


def pool_users(ops: Ops) -> list[dict]:
    """Every enabled user who can sign in: {email, sub, accounts, admin}.
    `accounts` is what they track (and, for a viewer, what they may open)."""
    users = ops.aws("cognito-idp", "list-users", "--user-pool-id", ops.output("UserPoolId")).get("Users", [])
    viewers, admins = group_members(ops, ops.output("ViewerGroup")), group_members(ops, ops.output("AdminGroup"))
    out = []
    for u in users:
        if u["Username"] not in viewers | admins or not u.get("Enabled", True):
            continue
        attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
        out.append({"email": attrs.get("email", u["Username"]), "sub": attrs.get("sub", u["Username"]),
                    "accounts": parse_slugs(attrs.get("custom:accounts", "")),
                    "admin": u["Username"] in admins})
    return out


def cmd_users_list(ops: Ops, args) -> int:
    pool = ops.output("UserPoolId")
    viewer, admin = ops.output("ViewerGroup"), ops.output("AdminGroup")
    users = ops.aws("cognito-idp", "list-users", "--user-pool-id", pool).get("Users", [])
    viewers, admins = group_members(ops, viewer), group_members(ops, admin)
    rows = [["EMAIL", "STATUS", "ACCESS"]]
    for u in sorted(users, key=lambda u: next((a["Value"] for a in u.get("Attributes", [])
                                               if a["Name"] == "email"), "")):
        attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
        if u["Username"] in admins:
            tracks = ", ".join(parse_slugs(attrs.get("custom:accounts", "")))
            access = "every account (admin)" + (f"; tracks {tracks}" if tracks else "")
        elif u["Username"] in viewers:
            access = ", ".join(parse_slugs(attrs.get("custom:accounts", ""))) or "no accounts yet"
        else:
            access = f"none: not in {viewer}"
        status = u.get("UserStatus", "?") + ("" if u.get("Enabled", True) else ", DISABLED")
        rows.append([attrs.get("email", u["Username"]), status, access])
    table(rows)
    return 0


def cmd_users_invite(ops: Ops, args) -> int:
    email = check_email(args.email)
    if get_user(ops, email):
        fail(f"{email} already has an account: `users grant` changes their accounts, "
             f"`users resend` re-sends the invite")
    slugs = parse_slugs(args.accounts)
    if slugs:
        known_slugs(ops, slugs)
    viewer, admin = ops.output("ViewerGroup"), ops.output("AdminGroup")
    groups = [viewer] + ([admin] if args.admin else [])
    waitlist = ops.aws("dynamodb", "get-item", "--table-name", ops.output("JoinRequestsTableName"),
                       "--key", json.dumps({"email": {"S": email}})).get("Item")
    home, publish = _home_plan(ops, groups, slugs)
    plan = [f"create {email} and email them a temporary password, valid {email_layout.PASSWORD_DAYS} days",
            f"add them to {' and '.join(groups)}",
            "let them see " + ("every account" if args.admin else ", ".join(slugs) or "no accounts yet")]
    if waitlist and plain(waitlist).get("status") == "pending":
        plan.append("mark their waitlist request granted")
    if not ops.confirm(plan + home):
        return 1
    pool = ops.output("UserPoolId")
    attrs = [{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}]
    if slugs:
        attrs.append({"Name": "custom:accounts", "Value": ",".join(slugs)})
    ops.aws("cognito-idp", "admin-create-user", "--user-pool-id", pool, "--username", email,
            "--user-attributes", json.dumps(attrs), "--desired-delivery-mediums", "EMAIL", change=True)
    for group in groups:
        ops.aws("cognito-idp", "admin-add-user-to-group", "--user-pool-id", pool, "--username", email,
                "--group-name", group, change=True)
    if waitlist and plain(waitlist).get("status") == "pending":
        close_request(ops, ops.output("JoinRequestsTableName"), {"email": {"S": email}}, "granted")
    if publish:
        publish_pages(ops)
    if not ops.dry_run:
        print(f"Invited {email}. They sign in at /login and choose a password.")
    return 0


def _change_accounts(ops: Ops, args, grant: bool) -> int:
    email = check_email(args.email)
    user = need_user(ops, email)
    slugs = parse_slugs(args.slugs)
    before = user["accounts"]
    after = sorted(set(before) | set(slugs)) if grant else sorted(set(before) - set(slugs))
    drafting = grant and getattr(args, "email_draft", False)
    if after == before:
        print(f"{email} already {'has' if grant else 'lacks'} {', '.join(slugs)}. Nothing to do.")
        if drafting:
            draft_new_accounts(ops, email, slugs)
        return 0
    if grant:
        known_slugs(ops, slugs)
    viewer, admin = ops.output("ViewerGroup"), ops.output("AdminGroup")
    plan = [f"change {email}'s accounts from [{', '.join(before) or 'none'}] to [{', '.join(after) or 'none'}]",
            "it takes effect at their next sign-in"]
    if admin in user["groups"]:
        plan.append("note: they are an admin and see every account anyway; this list marks the accounts "
                    "they track, whose briefs are written for their persona")
    elif viewer not in user["groups"]:
        plan.append(f"note: they are not in {viewer}, so they cannot sign in at all")
    if not grant:
        plan.append("note: " + SESSION_NOTE)
    else:
        for other in pool_users(ops):
            shared = sorted(set(slugs) & set(other["accounts"]))
            if other["email"] != email and shared:
                plan.append(f"note: {other['email']} already tracks {', '.join(shared)}; while two users "
                            f"track an account, its brief is written for the default reader")
    home, publish = _home_plan(ops, user["groups"], after)
    if drafting:
        home.append("draft an email telling them, for you to review: nothing is sent")
    if not ops.confirm(plan + home):
        return 1
    ops.aws("cognito-idp", "admin-update-user-attributes", "--user-pool-id", ops.output("UserPoolId"),
            "--username", email, "--user-attributes", _accounts_attr(after), change=True)
    if publish:
        publish_pages(ops)
    if not ops.dry_run:
        print(f"{email} now sees: {', '.join(after) or 'no accounts'}.")
    if drafting:
        draft_new_accounts(ops, email, slugs)
    return 0


def cmd_users_grant(ops: Ops, args) -> int:
    return _change_accounts(ops, args, grant=True)


def cmd_users_revoke(ops: Ops, args) -> int:
    return _change_accounts(ops, args, grant=False)


def cmd_users_admin(ops: Ops, args) -> int:
    email = check_email(args.email)
    user = need_user(ops, email)
    admin = ops.output("AdminGroup")
    if (admin in user["groups"]) != args.remove:
        print(f"{email} is {'not ' if args.remove else ''}an admin already. Nothing to do.")
        return 0
    plan = ([f"remove {email} from {admin}: they see only their own accounts again",
             "note: " + SESSION_NOTE] if args.remove else
            [f"add {email} to {admin}: they see every account"])
    plan.append("it takes effect at their next sign-in")
    home, publish = ([], False) if not args.remove else _home_plan(ops, [], user["accounts"])
    if not ops.confirm(plan + home):
        return 1
    verb = "admin-remove-user-from-group" if args.remove else "admin-add-user-to-group"
    ops.aws("cognito-idp", verb, "--user-pool-id", ops.output("UserPoolId"), "--username", email,
            "--group-name", admin, change=True)
    if publish:
        publish_pages(ops)
    return 0


def cmd_users_disable(ops: Ops, args) -> int:
    email = check_email(args.email)
    need_user(ops, email)
    if not ops.confirm([f"stop {email} signing in to TICK", "note: " + SESSION_NOTE]):
        return 1
    ops.aws("cognito-idp", "admin-disable-user", "--user-pool-id", ops.output("UserPoolId"),
            "--username", email, change=True)
    return 0


def cmd_users_enable(ops: Ops, args) -> int:
    email = check_email(args.email)
    need_user(ops, email)
    if not ops.confirm([f"let {email} sign in to TICK again, with the accounts they had"]):
        return 1
    ops.aws("cognito-idp", "admin-enable-user", "--user-pool-id", ops.output("UserPoolId"),
            "--username", email, change=True)
    return 0


def cmd_users_resend(ops: Ops, args) -> int:
    email = check_email(args.email)
    user = need_user(ops, email)
    if user["status"] != "FORCE_CHANGE_PASSWORD":
        fail(f"{email} has already chosen a password ({user['status']}); they can reset it from "
             f"the sign-in page's 'Forgot your password?'")
    if not ops.confirm([f"email {email} a new temporary password, valid {email_layout.PASSWORD_DAYS} days"]):
        return 1
    ops.aws("cognito-idp", "admin-create-user", "--user-pool-id", ops.output("UserPoolId"),
            "--username", email, "--message-action", "RESEND", "--desired-delivery-mediums", "EMAIL",
            change=True)
    return 0


# ---------------------------------------------------------------------- emails
#
# An email to a user is drafted to local-data/emails/ first and only sent by
# `users send-email`, after a person has read it: a grant can be a correction
# or a trial, and not every one deserves a message. See docs/email.md.

def account_facts(ops: Ops, slugs: list[str]) -> list[dict]:
    """What the new-account email says about each account: its name, and the
    board's size at the newest crawl (None before the first one)."""
    bucket = ops.output("DataBucketName")
    names = {slug_of(c): (c.get("config") or {}).get("company_name") for c in live_configs(ops)}
    out = []
    for slug in slugs:
        facts = {"slug": slug, "name": names.get(slug) or slug, "open_roles": None, "tech_roles": None,
                 "crawled_on": None}
        keys = sorted(o["Key"] for o in list_objects(ops, bucket, f"curated/company={slug}/dt=")
                      if o["Key"].endswith("/postings.jsonl"))
        if keys:
            text = ops.aws("s3", "cp", f"s3://{bucket}/{keys[-1]}", "-", parse=False)
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
            facts.update(open_roles=len(rows), tech_roles=sum(1 for r in rows if r.get("is_tech_role")),
                         crawled_on=re.search(r"dt=([0-9-]+)", keys[-1]).group(1))
        out.append(facts)
    return out


def draft_new_accounts(ops: Ops, email: str, slugs: list[str]) -> str:
    """Write the new-account email for review; its folder."""
    site = ops.output("SiteUrl")
    subject, body, text = email_layout.new_accounts(account_facts(ops, slugs), site)
    stamp = datetime.now(timezone.utc)
    folder = os.path.join(DRAFTS, f"{stamp:%Y-%m-%d-%H%M%S}-{re.sub(r'[^a-z0-9]+', '-', email)}-accounts")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "email.html"), "w") as f:
        f.write(body)
    with open(os.path.join(folder, "email.txt"), "w") as f:
        f.write(text)
    with open(os.path.join(folder, "message.json"), "w") as f:
        json.dump({"to": email, "subject": subject, "kind": "new-accounts", "accounts": slugs,
                   "drafted_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ")}, f, indent=1)
        f.write("\n")
    rel = os.path.relpath(folder)
    print(f"Drafted \"{subject}\" to {email}. Nothing is sent yet.\n"
          f"  Review: {rel}/email.html (and email.txt, its plain-text part)\n"
          f"  Send:   tools/tick.py users send-email {rel}")
    return folder


def cmd_users_send_email(ops: Ops, args) -> int:
    folder = args.draft.rstrip("/")
    try:
        with open(os.path.join(folder, "message.json")) as f:
            meta = json.load(f)
        with open(os.path.join(folder, "email.html")) as f:
            body = f.read()
        with open(os.path.join(folder, "email.txt")) as f:
            text = f.read()
    except (OSError, ValueError) as exc:
        fail(f"{folder} is not a draft from `users grant --email`: {exc}")
    sent = os.path.join(folder, "sent.json")
    if os.path.exists(sent):
        fail(f"this draft was already sent ({open(sent).read().strip()}); draft a new one to send again")
    to = check_email(meta["to"])
    sender = f"TICK <{ops.output('SenderAddress')}>"
    if not ops.confirm([f"email {to} \"{meta['subject']}\" from {sender}",
                        f"as written in {os.path.relpath(folder)}/email.html and email.txt"]):
        return 1
    content = {"Simple": {"Subject": {"Data": meta["subject"]},
                          "Body": {"Html": {"Data": body}, "Text": {"Data": text}}}}
    result = ops.aws("sesv2", "send-email", "--from-email-address", sender,
                     "--destination", json.dumps({"ToAddresses": [to]}),
                     "--reply-to-addresses", ops.output("SenderAddress"),
                     "--content", json.dumps(content), change=True)
    if result is None:
        return 0
    with open(sent, "w") as f:
        json.dump({"message_id": result.get("MessageId"),
                   "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, f)
        f.write("\n")
    print(f"Sent to {to}.")
    return 0


# -------------------------------------------------------------------- personas

def profiles(ops: Ops) -> dict[str, dict]:
    """Every saved profile, by the Cognito sub it belongs to (profiles/<sub>.json)."""
    bucket = ops.output("DataBucketName")
    keys = [o["Key"] for o in list_objects(ops, bucket, "profiles/") if o["Key"].endswith(".json")]

    def read(key: str) -> tuple[str, dict | None]:
        try:
            return key[len("profiles/"):-len(".json")], ops.aws("s3", "cp", f"s3://{bucket}/{key}", "-")
        except (AwsError, ValueError):
            return key, None

    with ThreadPoolExecutor(8) as pool:
        return {sub: body for sub, body in pool.map(read, keys) if body is not None}


def account_readers(ops: Ops) -> dict[str, dict]:
    """Who each account's brief is written for: the persona of the one user who
    tracks it, or the default reader, with the reason."""
    slugs = sorted({slug_of(c) for c in live_configs(ops) if slug_of(c)})
    users, saved = pool_users(ops), profiles(ops)
    out = {}
    for slug in slugs:
        trackers = [u for u in users if slug in u["accounts"]]
        entry = {"tracked_by": [u["email"] for u in trackers], "persona": None, "ref": None, "reason": None}
        if not trackers:
            entry["reason"] = "no one tracks it"
        elif len(trackers) > 1:
            entry["reason"] = f"{len(trackers)} users track it"
        else:
            raw = (saved.get(trackers[0]["sub"]) or {}).get("persona")
            who, problems = persona_schema.clean(raw)
            if persona_schema.is_blank(who):
                entry["reason"] = "no persona saved"
            else:
                entry.update(persona=who, ref=persona_schema.ref(who), problems=problems)
        out[slug] = entry
    return out


def cmd_personas_set(ops: Ops, args) -> int:
    """Save a user's persona from a JSON file holding the fields in
    tools/persona.py. Users edit their own on /accounts; this is for setting
    one up for them, or for yourself."""
    email = check_email(args.email)
    user = need_user(ops, email)
    try:
        with open(args.file) as f:
            raw = json.load(f)
    except (OSError, ValueError) as exc:
        fail(f"cannot read {args.file}: {exc}")
    who, problems = persona_schema.clean(raw.get("persona", raw) if isinstance(raw, dict) else raw)
    if problems:
        fail(f"{args.file}: " + "; ".join(problems))
    if persona_schema.is_blank(who):
        fail(f"{args.file} describes no one: fill in at least a role, an employer or some terms")
    if not user.get("sub"):
        fail(f"Cognito returned no sub for {email}, and a profile is keyed by it")
    bucket = ops.output("DataBucketName")
    key = f"profiles/{user['sub']}.json"
    try:
        before = (ops.aws("s3", "cp", f"s3://{bucket}/{key}", "-") or {}).get("persona")
    except (AwsError, ValueError):
        before = None
    if before == who:
        print(f"{email}'s persona is already this one. Nothing to do.")
        return 0
    if before:
        sys.stdout.writelines(difflib.unified_diff(_pretty(before), _pretty(who), "saved", args.file, n=2))
    plan = [f"save {email}'s persona: {persona_schema.describe(who)}, "
            f"{len(who['focus_terms'])} focus and {len(who['competitor_terms'])} competitor terms",
            "briefs for the accounts they track are written for it from the next weekly run"]
    if not user["accounts"]:
        plan.append(f"note: they track no accounts yet, so no brief uses it: "
                    f"`tools/tick.py users grant {email} <slug>`")
    if not ops.confirm(plan):
        return 1
    body = {"persona": who, "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "profile.json")
        with open(path, "w") as f:
            json.dump(body, f, indent=1, ensure_ascii=False)
        ops.aws("s3", "cp", path, f"s3://{bucket}/{key}", "--content-type", "application/json",
                "--only-show-errors", change=True, parse=False)
    if not ops.dry_run:
        print(f"Saved {email}'s persona.")
    return 0


def cmd_personas_list(ops: Ops, args) -> int:
    """Each account, the user who tracks it, and who its brief is written for.
    The weekly brief run reads --json; an account tracked by no one, or by
    more than one user, gets the default reader."""
    readers = account_readers(ops)
    if args.json:
        print(json.dumps(readers, indent=1, ensure_ascii=False))
        return 0
    rows = [["ACCOUNT", "TRACKED BY", "BRIEF WRITTEN FOR"]]
    for slug, r in readers.items():
        if r["persona"]:
            terms = r["persona"]["focus_terms"]
            written = (persona_schema.describe(r["persona"])
                       + (f" ({', '.join(terms[:3])}{f' +{len(terms) - 3}' if len(terms) > 3 else ''})"
                          if terms else ""))
        else:
            written = f"default reader: {r['reason']}"
        rows.append([slug, ", ".join(r["tracked_by"]) or "-", written])
    table(rows)
    return 0


# -------------------------------------------------------------------- requests

def scan(ops: Ops, table_name: str) -> list[dict]:
    return [plain(i) for i in ops.aws("dynamodb", "scan", "--table-name", table_name).get("Items", [])]


def close_request(ops: Ops, table_name: str, key: dict, status: str) -> None:
    ops.aws("dynamodb", "update-item", "--table-name", table_name, "--key", json.dumps(key),
            "--update-expression", "SET #s = :s, closed_at = :t",
            "--expression-attribute-names", json.dumps({"#s": "status"}),
            "--expression-attribute-values", json.dumps({
                ":s": {"S": status},
                ":t": {"S": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}}),
            change=True)


def cmd_requests_list(ops: Ops, args) -> int:
    if args.accounts:
        items = scan(ops, ops.output("AccountRequestsTableName"))
        rows = [["CREATED", "STATUS", "ID", "FROM", "COMPANY", "CAREERS URL", "NOTE"]]
        for i in sorted(items, key=lambda i: i.get("created_at", "")):
            if args.pending and i.get("status") != "pending":
                continue
            rows.append([i.get("created_at", ""), i.get("status", ""), i.get("request_id", "")[:8],
                         i.get("requester_email") or i.get("requester_sub", ""), i.get("company", ""),
                         i.get("careers_url") or "-", (i.get("note") or "-")[:60]])
    else:
        items = scan(ops, ops.output("JoinRequestsTableName"))
        rows = [["CREATED", "STATUS", "EMAIL", "NAME", "COMPANY"]]
        for i in sorted(items, key=lambda i: i.get("created_at", "")):
            if args.pending and i.get("status") != "pending":
                continue
            rows.append([i.get("created_at", ""), i.get("status", ""), i.get("email", ""),
                         i.get("name", ""), i.get("company", "")])
    if len(rows) == 1:
        print("No requests.")
    table(rows)
    return 0


def cmd_requests_close(ops: Ops, args) -> int:
    target = args.target.strip()
    if "@" in target:
        name, label = ops.output("JoinRequestsTableName"), "waitlist request"
        key = {"email": {"S": target.lower()}}
        item = ops.aws("dynamodb", "get-item", "--table-name", name, "--key", json.dumps(key)).get("Item")
        if not item:
            fail(f"no waitlist request from {target}")
        what = f"{label} from {target.lower()}"
    else:
        name, label = ops.output("AccountRequestsTableName"), "account request"
        matches = [i for i in scan(ops, name) if i.get("request_id", "").startswith(target)]
        if len(matches) != 1:
            fail(f"{len(matches)} account requests have an id starting {target!r}; "
                 f"`tools/tick.py requests list --accounts` shows them")
        key = {"request_id": {"S": matches[0]["request_id"]}}
        what = f"{label} for {matches[0].get('company')} from {matches[0].get('requester_email') or 'a viewer'}"
    if not ops.confirm([f"mark the {what} {args.status}"]):
        return 1
    close_request(ops, name, key, args.status)
    return 0


# ------------------------------------------------------------------------- dlq

def cmd_dlq_list(ops: Ops, args) -> int:
    """Read the failed crawls without taking them: a zero visibility timeout
    leaves every message where it is."""
    url = ops.output("CollectDLQUrl")
    seen: dict[str, dict] = {}
    for _ in range(5):   # SQS returns a sample per call; a few calls see a small queue whole
        got = ops.aws("sqs", "receive-message", "--queue-url", url, "--max-number-of-messages", "10",
                      "--visibility-timeout", "0", "--attribute-names", "All").get("Messages", [])
        if not got:
            break
        seen.update((m["MessageId"], m) for m in got)
    if not seen:
        print("The DLQ is empty: no crawl has failed three times in the last 14 days.")
        return 0
    rows = [["COMPANY", "FIRST QUEUED", "ATTEMPTS"]]
    for m in seen.values():
        try:
            slug = json.loads(m["Body"]).get("config", {}).get("company_slug", "?")
        except ValueError:
            slug = "unreadable message"
        sent = datetime.fromtimestamp(int(m["Attributes"]["SentTimestamp"]) / 1000, timezone.utc)
        rows.append([slug, sent.strftime("%Y-%m-%d %H:%M UTC"), m["Attributes"].get("ApproximateReceiveCount", "?")])
    table(rows)
    print("\nWhy it failed: tools/tick.py logs collector --since 1d --grep <company>.\n"
          "Usually the board moved: re-run the tick-evaluator skill, `tools/tick.py companies push` the "
          "new config, then `tools/tick.py dlq redrive`.")
    return 0


def cmd_dlq_redrive(ops: Ops, args) -> int:
    url = ops.output("CollectDLQUrl")
    attrs = ops.aws("sqs", "get-queue-attributes", "--queue-url", url, "--attribute-names",
                    "QueueArn", "ApproximateNumberOfMessages")["Attributes"]
    count = int(attrs["ApproximateNumberOfMessages"])
    if not count:
        print("The DLQ is empty. Nothing to redrive.")
        return 0
    if not ops.confirm([f"move {count} failed crawl(s) back to the collect queue, where each is crawled again",
                        "each message carries the config it was queued with; to crawl with a fixed "
                        "config instead, `tools/tick.py collect <slug>` after pushing it"]):
        return 1
    ops.aws("sqs", "start-message-move-task", "--source-arn", attrs["QueueArn"], change=True)
    return 0


# ------------------------------------------------------------------------ logs

FUNCTIONS = ("collector", "dispatcher", "publisher", "auth", "join")


def cmd_logs(ops: Ops, args) -> int:
    cmd = ["aws", "logs", "tail", f"/aws/lambda/{ops.stack}-{args.function}", "--since", args.since,
           "--format", "short", "--region", ops.region]
    if args.grep:
        cmd += ["--filter-pattern", json.dumps(args.grep)]   # quoted: a slug's hyphens are literal
    if args.follow:
        cmd.append("--follow")
    return subprocess.call(cmd)


# ---------------------------------------------------------------------- verify

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url: str) -> tuple[int, str]:
    """(status, Location) for one GET, without following a redirect."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=20) as r:
            return r.status, r.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", "")


def cmd_verify(ops: Ops, args) -> int:
    """The edge's two promises: public pages serve to anyone, and every other
    page sends a visitor without a session to sign in."""
    site = (args.site or ops.output("SiteUrl")).rstrip("/")
    bad = []
    for path in PUBLIC_PATHS:
        code, _ = fetch(site + path)
        print(f"public   {path:<34} {code}")
        if code != 200:
            bad.append(f"{path} returned {code}, expected 200")
    for path in GUARDED_PATHS:
        code, location = fetch(site + path)
        print(f"guarded  {path:<34} {code} {location}")
        if code != 302 or "/auth/login" not in location:
            bad.append(f"{path} returned {code} {location}, expected a 302 to /auth/login")
    for problem in bad:
        prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") == "true" else "FAIL "
        print(prefix + problem, file=sys.stderr)
    return 1 if bad else 0


# ---------------------------------------------------------------------- status

def cmd_status(ops: Ops, args) -> int:
    """One screen: the schedules, each company's last crawl and brief, the last
    publish, the queues and the alarm, and requests waiting on you. Exits 1 if
    anything needs attention, so it can run unattended."""
    data_bucket, site_bucket = ops.output("DataBucketName"), ops.output("SiteBucketName")
    queue, dlq = ops.output("CollectQueueUrl"), ops.output("CollectDLQUrl")

    def schedules():
        out = []
        for s in ops.aws("scheduler", "list-schedules").get("Schedules", []):
            full = ops.aws("scheduler", "get-schedule", "--name", s["Name"], "--group-name",
                           s.get("GroupName", "default"))
            if f":function:{ops.stack}-" in full.get("Target", {}).get("Arn", ""):
                out.append(full)
        return out

    def pending(table_name):
        return ops.aws("dynamodb", "scan", "--table-name", table_name, "--select", "COUNT",
                       "--filter-expression", "#s = :p",
                       "--expression-attribute-names", json.dumps({"#s": "status"}),
                       "--expression-attribute-values", json.dumps({":p": {"S": "pending"}})).get("Count", 0)

    jobs = {
        "schedules": schedules,
        "configs": lambda: live_configs(ops),
        "states": lambda: list_objects(ops, data_bucket, "derived/state/"),
        "briefs": lambda: list_objects(ops, data_bucket, "briefs/"),
        "homes": lambda: list_objects(ops, site_bucket, "home/"),
        "queue": lambda: queue_depth(ops, queue),
        "dlq": lambda: queue_depth(ops, dlq),
        "alarm": lambda: ops.aws("cloudwatch", "describe-alarms", "--alarm-names",
                                 f"{ops.stack}-collect-failures").get("MetricAlarms", []),
        "waitlist": lambda: pending(ops.output("JoinRequestsTableName")),
        "account_requests": lambda: pending(ops.output("AccountRequestsTableName")),
    }
    with ThreadPoolExecutor(len(jobs)) as pool:
        futures = {name: pool.submit(fn) for name, fn in jobs.items()}
        got = {name: f.result() for name, f in futures.items()}

    now = datetime.now(timezone.utc)
    attention = []
    print(f"{ops.stack} in {ops.region}, checked {now:%Y-%m-%d %H:%M} UTC\n")

    print("Schedules")
    rows = []
    for s in sorted(got["schedules"], key=lambda s: s["ScheduleExpression"]):
        what = "collect" if s["Target"]["Arn"].endswith("-dispatcher") else "publish"
        rows.append([what, s["ScheduleExpression"], s.get("ScheduleExpressionTimezone", "UTC"), s["State"]])
        if s["State"] != "ENABLED":
            attention.append(f"the {what} schedule is {s['State']}")
    table(rows)

    crawled = {m.group(1): o["LastModified"] for o in got["states"]
               if (m := re.search(r"company=([^/]+)/state\.json$", o["Key"]))}
    weeks: dict[tuple, set] = {}
    for o in got["briefs"]:
        m = re.search(r"company=([^/]+)/(week|overview)=([\d-]+)/(brief|evidence)\.json$", o["Key"])
        if m:
            weeks.setdefault((m.group(1), m.group(2), m.group(3)), set()).add(m.group(4))
    # the brief the site shows: the newest weekly one, else a new account's overview
    latest_brief: dict[str, tuple] = {}
    for (slug, kind, dt), files in weeks.items():
        if files == {"brief", "evidence"}:
            latest_brief[slug] = max(latest_brief.get(slug, (False, "")), (kind == "week", dt))

    print("\nCompanies")
    rows = [["", "LAST CRAWL", "LATEST BRIEF", ""]]
    for c in sorted(got["configs"], key=lambda c: slug_of(c) or ""):
        slug, cfg = slug_of(c) or c["key"], c.get("config") or {}
        last = crawled.get(slug)
        note = ""
        if cfg.get("needs_human_review"):
            note = "needs review: skipped nightly"
        elif not last:
            note = "not collected yet"
        elif now - datetime.fromisoformat(last) > STALE_AFTER:
            note = "STALE: missed the last nightly crawl"
            attention.append(f"{slug} was last crawled {when(last)}")
        brief = latest_brief.get(slug)
        rows.append([slug, when(last), ("none" if not brief else f"week to {brief[1]}" if brief[0]
                                        else f"overview, {brief[1]}"), note])
    table(rows)

    published = max((o["LastModified"] for o in got["homes"]), default=None)
    waiting, crawling = got["queue"]
    dead = got["dlq"][0]
    alarm = got["alarm"][0]["StateValue"] if got["alarm"] else "missing"
    print(f"\nSite      last published {when(published)}")
    print(f"Queue     {waiting} waiting, {crawling} crawling")
    print(f"DLQ       {dead} failed crawl(s); alarm {alarm}")
    print(f"Requests  {got['waitlist']} on the waitlist, {got['account_requests']} account request(s) pending")
    if dead:
        attention.append(f"{dead} crawl(s) in the DLQ: tools/tick.py dlq list")
    if alarm not in ("OK", "INSUFFICIENT_DATA"):
        attention.append(f"the collect-failures alarm is {alarm}")
    if attention:
        print("\nNeeds attention:")
        for a in attention:
            print(f"  - {a}")
        return 1
    print("\nAll good.")
    return 0


# ------------------------------------------------------------------------ cli

def parser() -> argparse.ArgumentParser:
    # The common options work before or after the command: on a subcommand
    # they default to SUPPRESS, so they only override when given there.
    def common(p: argparse.ArgumentParser, top: bool) -> None:
        d = (lambda v: v) if top else (lambda v: argparse.SUPPRESS)
        p.add_argument("--stack", default=d(STACK), help=f"CloudFormation stack (default {STACK})")
        p.add_argument("--region", default=d(REGION), help=f"AWS region (default {REGION})")
        p.add_argument("-y", "--yes", action="store_true", default=d(False),
                       help="change production without asking")
        p.add_argument("--dry-run", action="store_true", default=d(False),
                       help="print the AWS calls a change would make, and make none")

    top = argparse.ArgumentParser(prog="tools/tick.py", description=__doc__.split("\n\n")[0],
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    common(top, True)
    sub = top.add_subparsers(dest="command", required=True, metavar="<command>")

    def leaf(parent, name, func, help_text):
        p = parent.add_parser(name, help=help_text, description=func.__doc__ or help_text)
        common(p, False)
        p.set_defaults(func=func)
        return p

    def group(name, help_text):
        p = sub.add_parser(name, help=help_text)
        return p.add_subparsers(dest="action", required=True, metavar="<action>")

    leaf(sub, "status", cmd_status, "schedules, last crawls and briefs, queues, alarm, requests")

    g = group("publish", "push the site: app pages, public pages or the sign-in page")
    leaf(g, "pages", cmd_publish_pages, "re-render /accounts and every account's pages now")
    p = leaf(g, "static", cmd_publish_static, "upload /, /docs and /demo from infra/site-static/")
    p.add_argument("--bucket", help="site bucket (default: the stack's SiteBucketName)")
    p.add_argument("--distribution", help="CloudFront distribution (default: SiteDistributionId)")
    leaf(g, "login", cmd_publish_login, "apply the Cognito sign-in page's CSS and banner")

    g = group("cognito", "sign-in page tools")
    leaf(g, "banner", cmd_cognito_banner, "re-render the sign-in banner PNG from its HTML (local)")
    p = leaf(g, "css", cmd_cognito_css, "check the live sign-in CSS against infra/cognito-ui.css")
    p.add_argument("--print", action="store_true", help="print the live CSS instead")

    p = leaf(sub, "collect", cmd_collect, "crawl one company, several, or all of them now")
    p.add_argument("slugs", nargs="*", metavar="slug", help="companies to crawl, by company_slug")
    p.add_argument("--all", action="store_true", help="every company, as the nightly run does")
    p.add_argument("--wait", action="store_true", help="wait until the crawls finish")
    p.add_argument("--publish", action="store_true", help="wait, then re-render the site")
    p.add_argument("--timeout", type=int, default=30, help="minutes to wait (default 30)")

    g = group("companies", "the SourceConfigs the nightly run crawls")
    leaf(g, "list", cmd_companies_list, "every company in the bucket, with its last crawl")
    p = leaf(g, "push", cmd_companies_push, "upload a verified config (shows the diff first)")
    p.add_argument("file", help="a config the tick-evaluator skill verified, e.g. configs/acme_config.json")
    leaf(g, "pull", cmd_companies_pull, "refresh the local configs/ from the bucket")

    g = group("users", "who can sign in, and which accounts they see")
    leaf(g, "list", cmd_users_list, "every user, their status and their accounts")
    p = leaf(g, "invite", cmd_users_invite, "create a user and email them a temporary password")
    p.add_argument("email")
    p.add_argument("--accounts", help="comma-separated company slugs they may open")
    p.add_argument("--admin", action="store_true", help="let them see every account")
    for name, func, verb in (("grant", cmd_users_grant, "add"), ("revoke", cmd_users_revoke, "remove")):
        p = leaf(g, name, func, f"{verb} accounts, keeping the rest of their list")
        p.add_argument("email")
        p.add_argument("slugs", nargs="+", metavar="slug")
        if name == "grant":
            p.add_argument("--email", dest="email_draft", action="store_true",
                           help="also draft an email telling them, for review; `users send-email` sends it")
    p = leaf(g, "send-email", cmd_users_send_email, "send an email drafted by `users grant --email`")
    p.add_argument("draft", help="the draft's folder, as `users grant --email` printed it")
    p = leaf(g, "admin", cmd_users_admin, "make a user an admin (sees every account), or --remove")
    p.add_argument("email")
    p.add_argument("--remove", action="store_true", help="take admin away")
    for name, func, text in (("disable", cmd_users_disable, "stop a user signing in"),
                             ("enable", cmd_users_enable, "let a disabled user sign in again"),
                             ("resend", cmd_users_resend, "re-send an invite whose password expired")):
        leaf(g, name, func, text).add_argument("email")

    g = group("personas", "who each account's weekly brief is written for")
    p = leaf(g, "list", cmd_personas_list, "each account, who tracks it, and their persona")
    p.add_argument("--json", action="store_true", help="the map the weekly brief run reads")
    p = leaf(g, "set", cmd_personas_set, "save a user's persona from a JSON file")
    p.add_argument("email")
    p.add_argument("file", help="a JSON object with the fields in tools/persona.py")

    g = group("requests", "the waitlist and viewers' requests for new accounts")
    p = leaf(g, "list", cmd_requests_list, "list waitlist requests (or --accounts), oldest first")
    p.add_argument("--accounts", action="store_true", help="account requests instead of the waitlist")
    p.add_argument("--pending", action="store_true", help="only those not yet handled")
    p = leaf(g, "close", cmd_requests_close, "mark a request handled")
    p.add_argument("target", help="the email of a waitlist request, or the start of an account request's id")
    p.add_argument("--as", dest="status", required=True, choices=("granted", "declined", "done"))

    g = group("dlq", "crawls that failed three times")
    leaf(g, "list", cmd_dlq_list, "which companies failed, and when")
    leaf(g, "redrive", cmd_dlq_redrive, "send the failed crawls back to be tried again")

    p = leaf(sub, "logs", cmd_logs, "tail a Lambda's logs")
    p.add_argument("function", choices=FUNCTIONS)
    p.add_argument("--since", default="1h", help="how far back, e.g. 30m, 6h, 2d (default 1h)")
    p.add_argument("--grep", help="only lines containing this text, e.g. a company slug")
    p.add_argument("--follow", action="store_true", help="keep printing new lines")

    p = leaf(sub, "verify", cmd_verify, "check public pages serve and private ones ask for sign-in")
    p.add_argument("--site", help="site to check (default: the stack's SiteUrl)")
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    ops = Ops(args.stack, args.region, args.yes, args.dry_run)
    try:
        return args.func(ops, args) or 0
    except AwsError as exc:
        message = str(exc)
        if re.search(r"ExpiredToken|InvalidClientTokenId|Unable to locate credentials|SSO|expired", message):
            message += "\nIs your AWS session current? Check with: aws sts get-caller-identity"
        fail(f"AWS: {message}")
    except KeyboardInterrupt:
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
