"""
Print the company slugs a weekly run writes briefs for, one per line.

    accounts.py <lake-root>

The same set the site renders pages for: every account with at least two
crawls, so a week of movement exists to write about. Uses the renderer's own
definition (build_accounts), so the run and the site cannot disagree.
"""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import tick_insights_report as report  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip().splitlines()[2].strip())
    lake = sys.argv[1]
    io = report.open_lake(lake)
    configs = os.path.join(lake, "configs")
    for a in report.build_accounts(report.load_configs(configs if os.path.isdir(configs) else None, io), io):
        if a["has_pages"]:
            print(a["slug"])
