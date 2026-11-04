"""
Print the company slugs a run writes briefs for, one per line.

    accounts.py <lake-root> [--overview]

Weekly: every account with at least two crawls, so a week of movement exists
to write about. The site renders a dashboard from the first crawl; this is the
narrower set. Uses the renderer's own definition (build_accounts'
has_baseline), so the run and the site cannot disagree about which accounts
have a baseline.

--overview: every account with at least one crawl and no published brief of
either kind — a newly tracked account, which gets one overview of its board
and stack before its weekly briefs begin.
"""

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import tick_insights_report as report  # noqa: E402


def accounts(lake: str, overview: bool = False) -> list[str]:
    io = report.open_lake(lake)
    configs = os.path.join(lake, "configs")
    out = []
    for a in report.build_accounts(report.load_configs(configs if os.path.isdir(configs) else None, io), io):
        if overview:
            if a["has_pages"] and not report.list_briefs(io, a["slug"]):
                out.append(a["slug"])
        elif a["has_baseline"]:
            out.append(a["slug"])
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("lake")
    ap.add_argument("--overview", action="store_true",
                    help="newly tracked accounts: a crawl, and no brief published yet")
    args = ap.parse_args()
    for slug in accounts(args.lake, args.overview):
        print(slug)
