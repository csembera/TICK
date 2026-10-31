"""
Render the site from a local lake, with the workbench's AI briefs in place of
the rule-based ones, and optionally serve it.

    preview.py <lake-root> [--briefs local-data/briefs] [--out local-data/preview] [--serve PORT]

For each company, the newest local-data/briefs/<slug>/<week-end>/brief.json
that passes check.py becomes <slug>/brief.html; a company with no passing
brief keeps the rule-based one, and the summary says which is which. The
accounts home lists every account (no per-viewer scoping locally), and the
"Track another company" form posts nowhere — the preview server answers it
with a stub 200 so the form can be tried.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, HERE)
import tick_insights_report as report  # noqa: E402
import check  # noqa: E402


def latest_brief(briefs_dir: str, slug: str) -> tuple[dict, dict, str] | None:
    """(brief, evidence, week) for the newest week whose brief passes check.py."""
    base = os.path.join(briefs_dir, slug)
    if not os.path.isdir(base):
        return None
    for week in sorted(os.listdir(base), reverse=True):
        b, e = os.path.join(base, week, "brief.json"), os.path.join(base, week, "evidence.json")
        if not (os.path.isfile(b) and os.path.isfile(e)):
            continue
        ev = json.load(open(e))
        fails, _, brief = check.check(open(b).read(), ev)
        if brief is not None and not fails:
            return brief, ev, week
        print(f"  {slug} {week}: brief.json fails check.py — skipped")
    return None


def build(lake: str, briefs_dir: str, out: str) -> None:
    io = report.open_lake(lake)
    configs = os.path.join(lake, "configs")
    accounts = report.build_accounts(
        report.load_configs(configs if os.path.isdir(configs) else None, io), io)
    pages = {"login/index.html": report.render_home(accounts),
             "404.html": report.render_404()}
    for a in accounts:
        if not a["has_pages"]:
            continue
        slug = a["slug"]
        data = report.load_lake(lake, slug, io)
        pages[f"{slug}/dashboard.html"] = report.render_dashboard(data, slug)
        pages.update(report.render_archive_pages(data, slug))
        ai = latest_brief(briefs_dir, slug)
        if ai:
            brief, ev, week = ai
            pages[f"{slug}/brief.html"] = report.render_ai_brief(brief, ev)
            print(f"  {slug}: AI brief, week to {week} (prompt v{brief.get('prompt_version')})")
        else:
            pages[f"{slug}/brief.html"] = report.render_brief(data, slug)
            print(f"  {slug}: rule-based brief (no passing AI brief)")
    for key, body in pages.items():
        path = os.path.join(out, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(body)
    print(f"wrote {len(pages)} pages to {out}")


def serve(out: str, port: int) -> None:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=out, **kw)

        def do_POST(self):  # the account-request form
            self.rfile.read(int(self.headers.get("content-length", 0)))
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

    print(f"serving http://127.0.0.1:{port}/login/", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("lake")
    ap.add_argument("--briefs", default=os.path.join(ROOT, "local-data", "briefs"))
    ap.add_argument("--out", default=os.path.join(ROOT, "local-data", "preview"))
    ap.add_argument("--serve", type=int, metavar="PORT")
    args = ap.parse_args()
    build(args.lake, args.briefs, args.out)
    if args.serve:
        serve(args.out, args.serve)


if __name__ == "__main__":
    main()
