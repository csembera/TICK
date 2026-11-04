#!/usr/bin/env python3
"""
Serve any version of TICK on localhost: the public pages from site-static/
plus the app pages rendered from the lake, laid out as the site bucket is.

    python3 tools/preview.py               # the working tree, uncommitted edits included
    python3 tools/preview.py my-branch     # any branch, tag or commit
    python3 tools/preview.py 12            # pull request #12
      --refresh     sync the data bucket into local-data/lake first
      --lake PATH   render from another local lake
      --briefs DIR  also show draft briefs from DIR (e.g. local-data/briefs)
                    that pass check.py, as if published
      --port N      default 8000

A ref is checked out into a temporary git worktree and rendered with its own
copy of the renderer, so the preview shows that version's code against
today's data. The lake is a local copy of the data bucket (raw/ skipped: the
renderer never reads it) under the gitignored local-data/; nothing leaves the
machine and the server binds to 127.0.0.1 only.

Brief pages show what the lake's briefs/ prefix holds: the briefs already
published. --briefs adds every draft from the tick-brief
skill, checked with that version's check.py, so a draft can be read in the
real page before publish.py uploads it. The lake itself is never written to.

Not previewed: the CloudFront edge, Cognito sign-in and per-viewer scoping.
/accounts lists every account, the account-request form gets a stub 200, and
nothing redirects: /login and a signed-in visit to / are the edge's doing.
/api/profile answers from memory, checked by tools/persona.py like the real
one, so the account menu, the profile page and My terms can be tried: one
persona for everyone, signed in as PREVIEW_EMAIL, forgotten when the server
stops. Sign out is the edge's doing too, so it 404s here.
"""

import argparse
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import persona  # noqa: E402  the stub /api/profile checks fields as the real one does
LAKE = os.path.join(ROOT, "local-data", "lake")
OUT = os.path.join(ROOT, "local-data", "preview-site")
STACK = "tick-collector"
STATIC = ["index.html", "og-image.png", "docs", "demo"]   # what `tick.py publish static` deploys


def run(*cmd, **kw) -> str:
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw).stdout.strip()


def sync_lake(lake: str) -> None:
    bucket = run("aws", "cloudformation", "describe-stacks", "--stack-name", STACK,
                 "--query", "Stacks[0].Outputs[?OutputKey=='DataBucketName'].OutputValue",
                 "--output", "text")
    print(f"→ syncing s3://{bucket} into {os.path.relpath(lake, ROOT)} (raw/ skipped)", flush=True)
    subprocess.run(["aws", "s3", "sync", f"s3://{bucket}/", lake, "--exclude", "raw/*",
                    "--delete", "--only-show-errors"], check=True)


def checkout(ref: str) -> tuple[str, str]:
    """(worktree dir, commit) for a branch, tag, commit or PR number."""
    if ref.isdigit():
        run("git", "fetch", "-q", "origin", f"pull/{ref}/head", cwd=ROOT)
        commit = run("git", "rev-parse", "FETCH_HEAD", cwd=ROOT)
    else:
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=ROOT)   # offline is fine
        try:
            commit = run("git", "rev-parse", "--verify", f"origin/{ref}^{{commit}}", cwd=ROOT)
        except subprocess.CalledProcessError:
            commit = run("git", "rev-parse", "--verify", f"{ref}^{{commit}}", cwd=ROOT)
    tree = tempfile.mkdtemp(prefix="tick-preview-")
    run("git", "worktree", "add", "-q", "--detach", tree, commit, cwd=ROOT)
    return tree, commit


def stage_drafts(code: str, lake: str, briefs: str) -> str:
    """A throwaway lake: symlinks to every part of the real one, plus a briefs/
    holding the published briefs and every draft that passes check.py, weekly
    (<slug>/<week end>/) and overview (<slug>/overview-<as_of>/), laid out as
    publish.py would upload it, so the brief archive shows them too."""
    staged = tempfile.mkdtemp(prefix="tick-lake-")
    for name in os.listdir(lake):
        if name != "briefs":
            os.symlink(os.path.join(lake, name), os.path.join(staged, name))
    if os.path.isdir(os.path.join(lake, "briefs")):
        shutil.copytree(os.path.join(lake, "briefs"), os.path.join(staged, "briefs"))
    checker = os.path.join(code, ".claude", "skills", "tick-brief", "scripts", "check.py")
    for slug in sorted(os.listdir(briefs)):
        slug_dir = os.path.join(briefs, slug)
        if not os.path.isdir(slug_dir) or slug.startswith("."):
            continue
        for week in sorted(os.listdir(slug_dir), reverse=True):
            files = [os.path.join(slug_dir, week, f) for f in ("brief.json", "evidence.json")]
            if not all(os.path.isfile(f) for f in files):
                continue
            ok = subprocess.run([sys.executable, checker, *files], capture_output=True).returncode == 0
            print(f"→ draft {slug} {week}: {'shown' if ok else 'fails check.py, not shown'}", flush=True)
            if ok:
                # the same prefix publish.py uploads to (check.py's s3_prefix)
                part = f"overview={week[len('overview-'):]}" if week.startswith("overview-") else f"week={week}"
                dest = os.path.join(staged, "briefs", f"company={slug}", part)
                os.makedirs(dest, exist_ok=True)
                for f in files:
                    shutil.copy2(f, dest)
    return staged


def build(code: str, lake: str, out: str) -> None:
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    static = os.path.join(code, "infra", "site-static")
    for name in STATIC:
        src = os.path.join(static, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(out, name))
        elif os.path.isfile(src):
            shutil.copy2(src, out)
    configs = os.path.join(lake, "configs")
    subprocess.run([sys.executable, os.path.join(code, "tools", "tick_insights_report.py"), lake,
                    "--out-dir", out, *(["--configs", configs] if os.path.isdir(configs) else [])],
                   check=True, cwd=code)


PREVIEW_EMAIL = "viewer@example.com"   # who the stub /api/profile says is signed in


class Handler(http.server.SimpleHTTPRequestHandler):
    profile: dict = {}   # the one persona /api/profile holds while the server runs

    def reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] != "/api/profile":
            return super().do_GET()
        who = Handler.profile.get("persona")
        self.reply(200, {"persona": None if persona.is_blank(who) else who,
                         "updated_at": Handler.profile.get("updated_at"), "tracks": [],
                         "email": PREVIEW_EMAIL})

    def do_POST(self):   # /api/join, /api/account-request and /api/profile
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        if self.path.split("?")[0] != "/api/profile":
            return self.reply(200, {"ok": True})
        try:
            changes = json.loads(raw or b"{}").get("persona")
            if not isinstance(changes, dict):
                raise ValueError
        except (ValueError, AttributeError):
            return self.reply(400, {"error": 'Send {"persona": {...}}.'})
        merged = {**(Handler.profile.get("persona") or {}),
                  **{k: v for k, v in changes.items() if k in persona.FIELDS}}
        who, problems = persona.clean(merged)
        if problems:
            return self.reply(400, {"error": problems[0], "problems": problems})
        Handler.profile = {"persona": who, "updated_at": "preview"}
        self.reply(200, Handler.profile)

    def send_error(self, code, message=None, explain=None):
        page = os.path.join(self.directory, "404.html")
        if code != 404 or not os.path.isfile(page):
            return super().send_error(code, message, explain)
        body = open(page, "rb").read()
        self.send_response(404)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve a branch, PR or the working tree on localhost")
    ap.add_argument("ref", nargs="?", help="branch, tag, commit or PR number (default: the working tree)")
    ap.add_argument("--refresh", action="store_true", help="sync the data bucket into the lake first")
    ap.add_argument("--lake", default=LAKE, help="local lake to render from (default: local-data/lake)")
    ap.add_argument("--briefs", help="draft briefs to show if they pass check.py (e.g. local-data/briefs)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = ap.parse_args()

    lake = os.path.abspath(args.lake)
    if args.refresh or not os.path.isdir(lake):
        if lake != LAKE:
            sys.exit(f"{lake} does not exist (--refresh only syncs into {LAKE})")
        sync_lake(lake)

    tree = staged = None
    try:
        if args.ref:
            tree, commit = checkout(args.ref)
            label = f"{args.ref} @ {commit[:7]}"
        else:
            label = "working tree"
        if args.briefs:
            staged = stage_drafts(tree or ROOT, lake, os.path.abspath(args.briefs))
        build(tree or ROOT, staged or lake, OUT)
    finally:
        if tree:   # the render is on disk; the checkout is no longer needed
            subprocess.run(["git", "worktree", "remove", "--force", tree], cwd=ROOT)
        if staged:
            shutil.rmtree(staged, ignore_errors=True)   # symlinks only; the lake is untouched

    url = f"http://127.0.0.1:{args.port}"
    print(f"\nTICK preview of {label}\n  {url}/          public pages\n  {url}/accounts/ accounts home\n"
          f"  {url}/profile/  profile\n"
          "Ctrl-C to stop.", flush=True)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", args.port), functools.partial(Handler, directory=OUT))
    if not args.no_open:
        webbrowser.open(f"{url}/accounts/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
