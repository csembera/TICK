#!/usr/bin/env python3
"""
Check that every day's change record agrees with the cumulative state.

    python3 tools/audit_diffs.py local-data/lake          # a local lake (tools/preview.py --refresh syncs one)
    python3 tools/audit_diffs.py s3://<DataBucketName>    # straight from the bucket (needs boto3)
      --company SLUG    one company only

state.json is cumulative: each posting's first_seen and closed_on dates.
derived/snapshots/.../dt=<day>/diff.json is that day's record of what was new,
closed and reopened, and it is what the dashboard's "this window" counts and
the weekly brief are built from. If the two disagree, a day's change record
lost movement — the signature of a same-day re-run that overwrote it, which
the collector now merges instead. Run this after any manual re-crawl.

Exits 1 when any company has a gap, listing the postings and days.
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tick_insights_report as report  # noqa: E402

_COMPANY = re.compile(r"company=([^/]+)/")


def audit(lake_io: dict, company: str) -> dict:
    keys, get = lake_io["keys"], lake_io["get"]
    state_key = next((k for k in keys if k.endswith("state.json") and f"company={company}/" in k), None)
    diffs = report._partition_map(keys, "diff.json", company)
    if not state_key or not diffs:
        return {"days": len(diffs), "gaps": [], "skipped": "no state or no daily records"}
    state = get(state_key)
    day = {dt: get(k) for dt, k in diffs.items()}
    first = min(day)   # the first crawl's record holds the whole board as new
    gaps = []
    for pid, e in state.items():
        fs, closed = e.get("first_seen"), e.get("closed_on")
        if fs and fs != first and fs in day:
            d = day[fs]
            if pid not in (d.get("new") or []) and pid not in (d.get("reopened") or []):
                gaps.append((fs, pid, "opened", e.get("title")))
        if closed and closed in day and pid not in (day[closed].get("closed") or []):
            gaps.append((closed, pid, "closed", e.get("title")))
    return {"days": len(day), "gaps": sorted(gaps),
            "multi_run_days": sorted(dt for dt, d in day.items() if len(d.get("runs") or []) > 1)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Check daily change records against cumulative state")
    ap.add_argument("source", help="lake root dir, or s3://bucket")
    ap.add_argument("--company", help="one company_slug only")
    args = ap.parse_args()

    io = report.open_lake(args.source)
    companies = sorted({m.group(1) for k in io["keys"] if k.endswith("state.json")
                        for m in [_COMPANY.search(k)] if m})
    if args.company:
        companies = [c for c in companies if c == args.company]
    if not companies:
        sys.exit(f"no company state found under {args.source}")

    bad = 0
    for c in companies:
        r = audit(io, c)
        multi = f" · days with several runs: {', '.join(r['multi_run_days'])}" if r.get("multi_run_days") else ""
        status = "OK " if not r["gaps"] else "GAP"
        print(f"{status} {c:28} {r['days']:>4} daily records · gaps {len(r['gaps'])}{multi}"
              + (f" ({r['skipped']})" if r.get("skipped") else ""))
        for dt, pid, what, title in r["gaps"][:20]:
            print(f"      {dt}  {what:6}  {pid}  {title or ''}")
        bad += bool(r["gaps"])
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
