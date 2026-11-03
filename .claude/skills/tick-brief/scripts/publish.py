"""
Upload verified briefs to the data bucket, where the publisher Lambda renders
them onto the site.

    publish.py [briefs-dir] [--week-end YYYY-MM-DD] [--company SLUG] [--dry-run]

For each <briefs-dir>/<slug>/ (default local-data/briefs), takes the newest
week holding brief.json and evidence.json (or exactly --week-end), re-runs
check.py on it and uploads only a pass:

    s3://<DataBucketName>/briefs/company=<slug>/week=<window end>/evidence.json
    s3://<DataBucketName>/briefs/company=<slug>/week=<window end>/brief.json

The evidence goes with the brief because the page resolves the brief's
citations against it. A brief that fails is reported and stays local: the
failure is a finding about the prompt, never something to hand-fix and ship.
The bucket comes from the tick-collector stack; uploads use the AWS CLI, so
there is nothing to install. The site shows a new brief after the next
nightly publish, or at once after `tools/tick.py publish pages`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, HERE)
import check  # noqa: E402

STACK = "tick-collector"


def data_bucket() -> str:
    r = subprocess.run(
        ["aws", "cloudformation", "describe-stacks", "--stack-name", STACK, "--query",
         "Stacks[0].Outputs[?OutputKey=='DataBucketName'].OutputValue", "--output", "text"],
        text=True, capture_output=True)
    if r.returncode != 0:   # most often an expired session: say so, upload nothing
        sys.exit(f"could not read the data bucket from stack {STACK}: "
                 f"{r.stderr.strip() or r.stdout.strip()}\nNothing was uploaded.")
    return r.stdout.strip()


def pick_week(slug_dir: str, week_end: str | None) -> str | None:
    """The week to publish: exactly week_end, else the newest with both files."""
    weeks = [week_end] if week_end else sorted(os.listdir(slug_dir), reverse=True)
    for week in weeks:
        d = os.path.join(slug_dir, week)
        if all(os.path.isfile(os.path.join(d, f)) for f in ("brief.json", "evidence.json")):
            return week
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Upload briefs that pass check.py to the data bucket")
    ap.add_argument("briefs", nargs="?", default=os.path.join(ROOT, "local-data", "briefs"))
    ap.add_argument("--week-end", help="publish this week only (default: each company's newest)")
    ap.add_argument("--company", help="publish this company_slug only")
    ap.add_argument("--dry-run", action="store_true", help="check and report, upload nothing")
    args = ap.parse_args()

    slugs = sorted(d for d in os.listdir(args.briefs)
                   if os.path.isdir(os.path.join(args.briefs, d)) and not d.startswith("."))
    if args.company:
        slugs = [s for s in slugs if s == args.company]
    if not slugs:
        sys.exit(f"no company directories under {args.briefs}")

    bucket = None if args.dry_run else data_bucket()
    published, refused = [], []
    for slug in slugs:
        week = pick_week(os.path.join(args.briefs, slug), args.week_end)
        if not week:
            refused.append(f"{slug}: no week with both brief.json and evidence.json")
            continue
        d = os.path.join(args.briefs, slug, week)
        ev = json.load(open(os.path.join(d, "evidence.json")))
        # the page links citations to this slug's archive and labels this window
        if ev.get("company", {}).get("slug") != slug or ev.get("window", {}).get("end") != week:
            refused.append(f"{slug} {week}: evidence is for {ev.get('company', {}).get('slug')} "
                           f"week {ev.get('window', {}).get('end')}")
            continue
        fails, warns, brief = check.check(open(os.path.join(d, "brief.json")).read(), ev)
        if fails or brief is None:
            refused.append(f"{slug} {week}: fails check.py — " + "; ".join(fails[:3]))
            continue
        prefix = f"briefs/company={slug}/week={week}"
        if bucket:
            # evidence first: the publisher ignores a brief without its evidence
            for name in ("evidence.json", "brief.json"):
                subprocess.run(["aws", "s3", "cp", os.path.join(d, name), f"s3://{bucket}/{prefix}/{name}",
                                "--content-type", "application/json", "--only-show-errors"], check=True)
        published.append(f"{slug} {week} (prompt v{brief.get('prompt_version', '?')}"
                         f"{', ' + str(len(warns)) + ' warnings' if warns else ''})")

    verb = "would publish" if args.dry_run else f"published to s3://{bucket}/briefs/"
    print(f"{verb}: {len(published)}")
    for line in published:
        print(f"  {line}")
    if refused:
        print(f"not published: {len(refused)}")
        for line in refused:
            print(f"  {line}")
    if published and not args.dry_run:
        print("Live after the next nightly publish, or now with: tools/tick.py publish pages")
    sys.exit(1 if refused else 0)


if __name__ == "__main__":
    main()
