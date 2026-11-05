"""
TICK insights report — renders the lake into a small static site:

  - Home — every account the user has: each evaluated config in configs/,
    plus any company found in the lake without one, with whatever the lake
    can support (open roles, delta, tech roles, last crawl) and links into
    the two pages below.
  - Dashboard — per account, evidence tables: tech-stack adoption, postings
    opened, role churn, source coverage.

Comparisons reach back a window (default 7 days, --window-days) rather than to
the previous crawl. On a daily board the overnight delta is mostly noise —
One tracked board swings +12/-7 between consecutive days while drifting far less — so the
page compares the latest snapshot against the most recent one at least that
old, and plots every snapshot in between. Where history is shorter than the
window it compares against the oldest snapshot there is and labels the real
dates, so a short window reads as itself.
  - Brief — per account, the current brief a model wrote with the
    tick-brief skill and uploaded to the lake's briefs/ prefix (the newest
    weekly brief, else a new account's overview; see current_brief), its
    citations numbered against the evidence pack it was written from. No
    Python writes brief prose; an account with no uploaded brief gets a
    "No brief yet" page.
  - Brief archive — every brief the account has had, at /<slug>/briefs,
    each on its own permanent page (see render_brief_pages).
  - Archive — per closed req, its last recorded description and dates, at a
    stable address the brief can cite once the board's own link is dead.
  - Profile — the signed-in viewer's persona, opened from the account menu
    in the menu bar. One page for everyone, filled from /api/profile.

Layout written under --out-dir:

    accounts/index.html           the accounts home, served at /accounts
    profile/index.html            the viewer's profile, served at /profile
    <company-slug>/dashboard.html
    <company-slug>/brief.html
    <company-slug>/briefs/index.html
                                  the brief archive, served at /<company-slug>/briefs
    <company-slug>/briefs/<weekly|overview>-<date>/index.html
                                  one per published brief
    <company-slug>/archive/<posting-id>/index.html
                                  one per closed req, served at
                                  /<company-slug>/archive/<posting-id>

"/" , "/docs" and "/demo" are NOT written here — they are the hand-authored
public pages in infra/site-static/, deployed separately. This renderer
only ever writes pages that are behind the session check.

No fabricated narrative — every claim on the dashboard traces back to a real
posting, snapshot count, or diff, and every claim in a brief carries a
citation that check.py verified before the brief was uploaded. Where the lake can't support a claim
(motivations, budget ownership, etc.), it's left out rather than invented.

Usage:
    python3 tick_insights_report.py <lake-root-dir> [--company SLUG] [--out-dir DIR]
    python3 tick_insights_report.py s3://bucket [--company SLUG] [--out-dir DIR]

With no --company, every company in the lake with a snapshot is rendered.

Pure stdlib for local files; boto3 (runtime-provided / in .venv) only when
reading from S3.
"""

import argparse
import glob
import html
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone

try:   # the publisher Lambda packages it beside this file (a symlink, like report.py)
    import description
except ImportError:   # the CLI, from tools/
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "infra", "src", "collector"))
    import description
import persona   # tools/persona.py; the publisher Lambda gets it through a symlink, like this file
from urllib.parse import urlparse

_CAT_LABELS = {
    "cloud_aws": "AWS",
    "cloud_azure": "Azure",
    "cloud_gcp": "Google Cloud",
    "data_platform": "Data platform",
    "infra_devops": "Infrastructure / DevOps",
    "languages": "Languages & frameworks",
    "analytics_tools": "Analytics tools",
    "signal_terms": "Signal terms",
}


# Where the signed-in app starts. "/" is the public marketing page, so every
# in-app link home is root-absolute to this instead of a relative "index.html".
APP_HOME = "/accounts"
# The viewer's own page, opened from the account menu. One page for everyone,
# filled from /api/profile; the edge serves it from home/profile/index.html.
PROFILE_HOME = "/profile"

# The public origin, for links that have to work outside the site — the
# workbench's markdown briefs. The pages themselves link root-absolute.
SITE_URL = "https://tick.sembasolutions.com"


def archive_segment(pid: str) -> str:
    """A posting id as one URL path segment. The edge resolves an extensionless
    tail to <tail>/index.html, so a '.' or '/' in an id would misroute the page;
    every ATS id seen so far (JR4531, R-005305, 5001215941206) passes untouched."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(pid))


def archive_href(company: str, pid: str) -> str:
    """Where a closed req's own page lives: /<slug>/archive/<pid>. It sits under
    the account's /<slug>/, so the edge's per-account check covers it as is."""
    return f"/{company}/archive/{archive_segment(pid)}"


def archive_key(company: str, pid: str) -> str:
    """The object that backs archive_href — the edge appends /index.html."""
    return f"{company}/archive/{archive_segment(pid)}/index.html"

# ------------------------------------------------------------------ loading

_DT_RE = re.compile(r"dt=(\d{4}-\d{2}-\d{2})/")


DEFAULT_WINDOW_DAYS = 7


def _partition_map(paths: list[str], leaf: str, company: str) -> dict[str, str]:
    """{dt: path} for one leaf filename and company, deduped on the date — a
    lake root holding a copy of itself (a stale .aws-sam build, say) must not
    read as two crawls of the same day."""
    by_dt: dict[str, str] = {}
    for path in sorted(paths):
        if not path.endswith(leaf) or f"company={company}/" not in path:
            continue
        m = _DT_RE.search(path)
        if m:
            by_dt.setdefault(m.group(1), path)
    return by_dt


def pick_baseline(dts: list[str], curr_dt: str, window_days: int) -> str | None:
    """The snapshot the page compares against: the most recent one at least
    window_days old.

    Comparing against whatever was captured last makes the delta a single
    overnight change, which on a daily board is mostly noise — one tracked board swings
    +12/-7 between consecutive days while drifting far less than that. Reaching
    back a fixed window measures drift instead of jitter.

    When history is shorter than the window, falls back to the oldest snapshot
    there is. The page labels both real dates, so a short window shows up as
    itself rather than being claimed as a full one."""
    older = [d for d in dts if date.fromisoformat(d) <= date.fromisoformat(curr_dt) - timedelta(days=window_days)]
    if older:
        return older[-1]
    candidates = [d for d in dts if d != curr_dt]
    return candidates[0] if candidates else None


def _dt_partitions(paths: list[str], leaf: str, company: str) -> list[str]:
    """One path per dt= partition for a leaf filename and company, oldest→newest.
    Deduped on the date itself: a lake root holding a copy of itself (a stale
    .aws-sam build, say) must not read as two crawls of the same day, which
    would render a real snapshot as its own predecessor and a zero delta."""
    by_dt: dict[str, str] = {}
    for path in sorted(paths):
        if not path.endswith(leaf) or f"company={company}/" not in path:
            continue
        m = _DT_RE.search(path)
        if m:
            by_dt.setdefault(m.group(1), path)
    return [by_dt[dt] for dt in sorted(by_dt)]


def open_lake(source: str) -> dict:
    """List a lake root once and return {"keys", "get", "get_text"}. Shared by
    every company in a build, so an S3 lake is paginated a single time."""
    if source.startswith("s3://"):
        import boto3
        bucket, _, prefix = source[5:].partition("/")
        s3 = boto3.client("s3")
        # a bare s3://bucket has no prefix at all — passing "/" would match nothing
        scope = {"Prefix": prefix.strip("/") + "/"} if prefix.strip("/") else {}
        keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(
                    Bucket=bucket, **scope)
                for o in page.get("Contents", [])]
        get = lambda k: json.loads(s3.get_object(Bucket=bucket, Key=k)["Body"].read())
        get_text = lambda k: s3.get_object(Bucket=bucket, Key=k)["Body"].read().decode()
    else:
        # followlinks: tools/preview.py stages draft briefs over a lake of symlinks
        keys = [os.path.join(dp, f) for dp, _, files in os.walk(source, followlinks=True)
                for f in files]
        def get_text(k):
            with open(k) as f:
                return f.read()
        get = lambda k: json.loads(get_text(k))
    return {"keys": keys, "get": get, "get_text": get_text}


_COMPANY_RE = re.compile(r"company=([^/]+)/")


def lake_companies(lake_io: dict) -> list[str]:
    """Every company_slug with at least one tech_stack.json in the lake."""
    return sorted({m.group(1) for k in lake_io["keys"] if k.endswith("tech_stack.json")
                   for m in [_COMPANY_RE.search(k)] if m})


def load_snapshots(lake_io: dict, company: str,
                   window_days: int = DEFAULT_WINDOW_DAYS) -> dict | None:
    """The cheap slice the home screen needs — the current tech_stack.json and
    the one the window compares against, no curated postings. None if the lake
    holds nothing for this company."""
    stacks = _partition_map(lake_io["keys"], "tech_stack.json", company)
    if not stacks:
        return None
    get = lake_io["get"]
    dts = sorted(stacks)
    curr_dt = dts[-1]
    prev_dt = pick_baseline(dts, curr_dt, window_days)
    return {"curr": get(stacks[curr_dt]),
            "prev": get(stacks[prev_dt]) if prev_dt else None,
            "snapshots": len(dts)}


def load_lake(source: str, company: str, lake_io: dict | None = None,
              window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Load one company's window: the current tech_stack.json, the snapshot
    window_days back that it is compared against, every snapshot total between
    them (the trend line), the daily diffs across that window rolled up, plus
    state.json and curated postings.jsonl for description text.

    Returns {"curr", "prev", "series", "history", "diff", "state", "curated",
    "curated_dt", "dts", "term_first_seen", "history_from", "archive"} — archive
    maps every closed posting_id to its last curated record; curated_dt and dts let the caller check that the curated board is
    the current one and reason about crawl cadence."""
    io = lake_io or open_lake(source)
    keys, get, get_text = io["keys"], io["get"], io["get_text"]

    stacks = _partition_map(keys, "tech_stack.json", company)
    if not stacks:
        sys.exit(f"no tech_stack.json found for company={company}")
    diff_paths = _partition_map(keys, "diff.json", company)
    states = sorted(k for k in keys if k.endswith("state.json") and f"company={company}/" in k)
    # People are a SIDE INPUT, not pipeline output: the collector reads reqs and
    # never individuals. Kept in its own partition so nothing downstream can
    # mistake a supplied name for something the board evidenced.
    people_keys = sorted(k for k in keys
                         if k.endswith("people.json") and f"company={company}/" in k)
    curateds = _dt_partitions(keys, "postings.jsonl", company)

    dts = sorted(stacks)
    curr_dt = dts[-1]
    prev_dt = pick_baseline(dts, curr_dt, window_days)
    curr = get(stacks[curr_dt])
    prev = get(stacks[prev_dt]) if prev_dt else None
    state = get(states[0]) if states else {}
    people = (get(people_keys[0]) if people_keys else {}).get("people", []) if people_keys else []

    # Every snapshot in the window, so the chart plots what actually happened
    # between the endpoints rather than interpolating a straight line.
    span = [d for d in dts if prev_dt and prev_dt <= d <= curr_dt] or [curr_dt]
    series = [{"dt": d, "total": get(stacks[d])["postings_total"]} for d in span]

    # The long view the dashboard chart plots: every snapshot inside the trailing
    # year, independent of the comparison window. Read from the same tech_stack
    # totals as `series`, so the chart's right-hand end equals the headline count.
    horizon = (date.fromisoformat(curr_dt) - timedelta(days=HISTORY_DAYS)).isoformat()
    history, term_first_seen = [], {}
    for d in [x for x in dts if x >= horizon]:
        snap = get(stacks[d])
        history.append({"dt": d, "total": snap["postings_total"]})
        # dts is ascending, so the first write for a (cat, term) is its earliest
        # appearance in the scanned window — the age the stack cards bucket on.
        for cat, terms in (snap.get("stack") or {}).items():
            for term in terms:
                term_first_seen.setdefault((cat, term), d)

    # A diff dated D describes the change from the previous crawl into D, so the
    # window's flow is every diff after the baseline up to and including today.
    # These are event counts: a role opened and closed inside the window is
    # counted in both, which is what "opened" and "closed" mean over a period.
    window_diffs = [get(p) for d, p in sorted(diff_paths.items())
                    if prev_dt and prev_dt < d <= curr_dt]
    # Only the flow keys can be summed. 'collected' and 'total_reported' are
    # point-in-time board sizes — adding seven of them yields 1026 for a
    # 152-posting board — so those come from the latest diff alone.
    flow = ("new", "closed", "reopened")
    counts: Counter = Counter()
    opened: list[str] = []
    shut: list[str] = []
    for d in window_diffs:
        for k in flow:
            counts[k] += (d.get("counts") or {}).get(k, 0)
        opened.extend(d.get("new", []))
        shut.extend(d.get("closed", []))
    latest = window_diffs[-1] if window_diffs else {}
    for k, v in (latest.get("counts") or {}).items():
        if k not in flow:
            counts[k] = v
    # A req opened and closed inside the window appears in both lists; that is
    # what "opened" and "closed" mean over a period, so neither is subtracted.
    diff = {"counts": dict(counts), "new": list(dict.fromkeys(opened)),
            "closed": list(dict.fromkeys(shut)), "days": len(window_diffs)}
    def _read_curated(path):
        out = {}
        for line in get_text(path).splitlines():
            if line.strip():
                rec = json.loads(line)
                out[rec["posting_id"]] = rec
        return out

    curated = {}
    curated_dt = None
    # A req that closed this window is gone from the current crawl, so its
    # description only survives in the previous one. Load that too, or the brief
    # can name a closed role but never say anything about what it asked for.
    curated_prev = {}
    if curateds:
        curated_dt = _DT_RE.search(curateds[-1]).group(1)
        curated = _read_curated(curateds[-1])
        if len(curateds) > 1:
            curated_prev = _read_curated(curateds[-2])

    # The archive: every req state.json holds as closed, with the last curated
    # record TICK has for it. A posting's final appearance is the crawl dated its
    # last_seen, so that partition is where its description survives — reading
    # only the newest two would leave anything closed before last week blank.
    # Each partition is read once however many closed reqs it holds.
    closed_ids = {pid: st for pid, st in state.items() if st.get("closed_on")}
    curated_by_dt = _partition_map(keys, "postings.jsonl", company)
    # a req that opened and closed between two runs on the same day is in that
    # day's departed.jsonl, not its postings.jsonl (see the collector)
    departed_by_dt = _partition_map(keys, "departed.jsonl", company)
    loaded = {}
    if curateds:
        loaded[curated_dt] = curated
        if len(curateds) > 1:
            loaded[_DT_RE.search(curateds[-2]).group(1)] = curated_prev
    archive = {}
    for dt in sorted({st.get("last_seen") for st in closed_ids.values()} - {None}):
        if dt not in curated_by_dt:
            continue
        if dt not in loaded:
            loaded[dt] = _read_curated(curated_by_dt[dt])
        day = loaded[dt]
        if dt in departed_by_dt:
            day = {**_read_curated(departed_by_dt[dt]), **day}
        for pid, st in closed_ids.items():
            if st.get("last_seen") == dt and pid in day:
                archive[pid] = day[pid]
    return {"curr": curr, "prev": prev, "series": series, "history": history,
            "diff": diff, "state": state, "curated": curated,
            "curated_dt": curated_dt, "curated_prev": curated_prev, "people": people,
            "archive": archive,
            "dts": dts,
            "term_first_seen": term_first_seen,
            "history_from": history[0]["dt"] if history else curr_dt}


# ---------------------------------------------------------------- derivation

def categorize_terms(curr_stack: dict, prev_stack: dict,
                     first_seen: dict | None = None,
                     cutoff: str | None = None) -> dict:
    """Bucket every (category, term) into adopting / established / receding.

    Adopting vs established is an AGE split, not a window comparison: a term
    whose earliest evidence in the lake is older than `cutoff` (6 months back)
    is part of the standing stack, and one that first showed up inside that
    window is a new bet. Comparing only the two window endpoints answered a
    different question — "did this term appear since last crawl" — which made
    'aws' adopting on any week a single new AWS posting landed, however long
    the company had been on AWS.

    Receding still comes from the window: a term the board has stopped
    mentioning, or mentions in fewer postings than at the baseline, is falling
    away regardless of how old it is.

    first_seen/cutoff are optional. Without enough history to reach back six
    months there is no age to read, so the old endpoint comparison stands in —
    an account on its second crawl still gets sensible cards."""
    buckets = {"adopting": [], "established": [], "receding": []}
    for cat in set(curr_stack) | set(prev_stack):
        curr_terms = curr_stack.get(cat, {})
        prev_terms = prev_stack.get(cat, {})
        for term in set(curr_terms) | set(prev_terms):
            curr_n = len(curr_terms.get(term, []))
            prev_n = len(prev_terms.get(term, []))
            since = (first_seen or {}).get((cat, term))
            row = {"cat": cat, "term": term, "curr_n": curr_n, "prev_n": prev_n,
                   "since": since, "pids": curr_terms.get(term, [])}
            if curr_n == 0 or (prev_n and curr_n < prev_n):
                buckets["receding"].append(row)
            elif cutoff and since:
                buckets["established" if since <= cutoff else "adopting"].append(row)
            elif prev_n == 0:            # no usable history — window fallback
                buckets["adopting"].append(row)
            else:
                buckets["established"].append(row)
    # term breaks ties: the cards truncate at 8 rows, so equal-count terms must
    # not reorder between runs (set iteration order would otherwise decide).
    buckets["adopting"].sort(key=lambda r: (-r["curr_n"], r["term"]))
    buckets["established"].sort(key=lambda r: (-r["curr_n"], r["term"]))
    buckets["receding"].sort(key=lambda r: (-r["prev_n"], r["term"]))
    return buckets


def stack_age_cutoff(lake: dict) -> str | None:
    """The date Adopting/Established splits on, or None when the lake does not
    reach back far enough for the split to mean anything."""
    curr_dt = lake["curr"]["captured_dt"]
    cutoff = (date.fromisoformat(curr_dt) - timedelta(days=STACK_AGE_DAYS)).isoformat()
    return cutoff if lake.get("history_from", curr_dt) <= cutoff else None


def new_postings_with_signals(diff: dict, curr_stack: dict, curr_postings: dict,
                              curated: dict | None = None) -> list[dict]:
    """Every posting opened across the window, tagged with whichever stack terms
    matched it, tech-roles and highest-signal-count first.

    Rows come from the curated crawl as well as the stack index: the index holds
    only postings that matched a term, so keying off it alone silently dropped
    every opening without one — a header reading '0 postings' over a window in
    which 22 roles actually opened. A posting with no terms is a row with no
    terms, which is itself the finding when a board hires outside engineering."""
    term_by_pid: dict[str, list[str]] = {}
    for terms in curr_stack.values():
        for term, pids in terms.items():
            for pid in pids:
                term_by_pid.setdefault(pid, []).append(term)

    rows = []
    for pid in (diff or {}).get("new", []):
        p = curr_postings.get(pid) or (curated or {}).get(pid)
        if not p:
            continue        # opened and closed again inside the window
        rows.append({"pid": pid, "title": p["title"], "location": p["location"],
                      "url": p.get("url", ""), "is_tech_role": p.get("is_tech_role", False),
                      "terms": sorted(term_by_pid.get(pid, []))})
    rows.sort(key=lambda r: (not r["is_tech_role"], -len(r["terms"])))
    return rows


NEW_ROLE_DAYS = 7
HISTORY_DAYS = 365   # trailing window the dashboard's open-roles chart plots
STACK_AGE_DAYS = 183  # 6 months: older evidence is Established, newer is Adopting


def opening_window(dts: list[str], curr_dt: str, first_seen: str | None,
                   days: int = NEW_ROLE_DAYS) -> bool:
    """Can the lake place this posting's opening inside the last `days`?

    first_seen is when TICK first *saw* a role, which is only the same as when
    it opened if a crawl that did not contain it happened recently. A board
    crawled 53 days apart tells you a role appeared somewhere in those 53 days
    — calling that "opened in the past 7 days" would be inventing the other 46.

    So the flag is asserted only when the last crawl without the posting (the
    snapshot immediately before first_seen) is itself inside the window: then
    the role was observably absent and then present, both within `days`.
    Postings first seen on the earliest snapshot were already on the board when
    collection started and can never be flagged — their real open date is not
    in the lake."""
    if not first_seen or first_seen > curr_dt:
        return False
    if (date.fromisoformat(curr_dt) - date.fromisoformat(first_seen)).days > days:
        return False
    prior = [d for d in dts if d < first_seen]
    if not prior:
        return False        # already on the board when we started watching
    return (date.fromisoformat(curr_dt) - date.fromisoformat(prior[-1])).days <= days


def open_postings(lake: dict, days: int = NEW_ROLE_DAYS) -> list[dict]:
    """Every posting open as of the current crawl, tagged with its stack terms
    and whether it can be shown to have opened in the last `days`.

    The board comes from the curated crawl, which holds every posting collected
    that day; tech_stack's own `postings` map holds only the ones that matched a
    stack term (23 of 137 on one tracked board), so it cannot answer "all open roles".
    Where the newest curated partition is older than the current snapshot it is
    not today's board, and the open set is rebuilt from state.json instead —
    every posting not closed and still seen on the current crawl."""
    curr, state, curated = lake["curr"], lake["state"], lake["curated"]
    curr_dt = curr["captured_dt"]
    dts = lake.get("dts") or [curr_dt]
    stack_postings = curr.get("postings", {})
    tech_ids = set(curr.get("tech_role_ids", []))

    term_by_pid: dict[str, list[str]] = {}
    for terms in curr["stack"].values():
        for term, pids in terms.items():
            for pid in pids:
                term_by_pid.setdefault(pid, []).append(term)

    if curated and lake.get("curated_dt") == curr_dt:
        source = curated
    else:
        source = {pid: st for pid, st in state.items()
                  if not st.get("closed_on") and st.get("last_seen") == curr_dt}
    boiler = detect_boilerplate_prefix(curated) if source is curated else 0

    rows = []
    for pid, rec in source.items():
        st = state.get(pid, {})
        sp = stack_postings.get(pid, {})
        fs = st.get("first_seen") or rec.get("first_seen")
        title = rec.get("title") or sp.get("title") or pid
        rows.append({
            "pid": pid,
            "title": title,
            "location": rec.get("location") or sp.get("location") or "",
            "team": rec.get("team") or "",
            "url": rec.get("url") or sp.get("url") or "",
            "is_tech_role": bool(rec.get("is_tech_role", sp.get("is_tech_role", pid in tech_ids))),
            "is_leadership": bool(_LEADERSHIP_RE.search(title)),
            "terms": sorted(set(term_by_pid.get(pid, []))),
            "first_seen": fs,
            "is_new": opening_window(dts, curr_dt, fs, days),
            "pay": pay_of(rec) if rec is not st else None,
            # what the table's search reads: the role's own text, because the
            # board's intro and benefits are on every posting and would make
            # any word in them match the whole board
            "text": role_text_of(rec, boiler) if rec is not st else "",
        })
    # Newest first by first-seen date; postings with no date sink to the end.
    # Within a day: tech roles, then the richest stack evidence, and title breaks
    # ties so the table does not reshuffle between runs. Two stable passes,
    # because the date sorts descending and the tie-breaks ascending.
    rows.sort(key=lambda r: (not r["is_tech_role"], -len(r["terms"]), r["title"].lower()))
    rows.sort(key=lambda r: (r["first_seen"] is not None, r["first_seen"] or ""), reverse=True)
    return rows


def board_domain(postings: dict) -> str:
    for p in postings.values():
        if p.get("url"):
            return urlparse(p["url"]).netloc
    return "?"


def fmt_dt(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%-d %b %Y, %H:%M UTC")


def first_seen(state: dict, pid: str, fallback: str) -> str:
    return state.get(pid, {}).get("first_seen", fallback)


_LEADERSHIP_RE = re.compile(
    r"(?i)\b(director|vice president|\bvp\b|svp|evp|chief [a-z]+ officer|head of|president|managing director)\b"
)


_REQ_RE = re.compile(r"(?i)\b(requirements?|qualifications?)\b\s*[:.]?\s*(.+)")


# ------------------------------------------------------------ descriptions
#
# Every reader goes through these. A row the collector parsed carries
# `description` (labelled sections) and no `description_raw`; an older row
# carries only `description_raw`, rebuilt into approximate sections here.


def parsed_description(rec: dict | None, boilerplate_n: int = 0) -> dict | None:
    """rec's description in sections: the collector's parse, else a rebuild
    from its flat text. boilerplate_n is how many leading sentences the board
    repeats (detect_boilerplate_prefix): they are filed as its intro whenever
    the parse did not already find one, which is every flat row and every row
    parsed before the board had a learned intro rule."""
    if not rec:
        return None
    parsed = rec.get("description")
    if not parsed:
        return description.from_text(rec.get("description_raw"), skip_sentences=boilerplate_n)
    if not boilerplate_n or "company_intro" in parsed["boilerplate"] or not parsed["sections"].get("summary"):
        return parsed
    sents = split_sentences(" ".join(parsed["sections"]["summary"]))
    intro, rest = sents[:boilerplate_n], sents[boilerplate_n:]
    sections = {**parsed["sections"], "company_intro": intro, "summary": [" ".join(rest)] if rest else []}
    if not rest:
        del sections["summary"]
    return {**parsed, "sections": sections, "boilerplate": [*parsed["boilerplate"], "company_intro"]}


def full_text_of(rec: dict | None) -> str:
    """The whole description as one string, boilerplate included."""
    if not rec:
        return ""
    return rec.get("description_raw") or description.full_text(rec.get("description"))


def role_text_of(rec: dict | None, boilerplate_n: int = 0) -> str:
    """The description without the board's intro, benefits and EEO text."""
    return description.role_text(parsed_description(rec, boilerplate_n))


def pay_of(rec: dict | None) -> dict | None:
    """The posted pay, {min, max, currency, period, text}, if any. Read afresh
    from the description rather than taken from what the collector stored, so
    an improved extractor reaches every row already in the lake. min is None
    for "up to $X"; min == max for a single figure."""
    return description.find_pay(parsed_description(rec))


def pay_label(pay: dict | None, short: bool = False) -> str | None:
    """"$85,000–$110,000 a year", "$170,000 a year", "up to $120,000 a year",
    or "$85K–$110K" for a table cell."""
    if not pay:
        return None
    def money(x):
        if short and pay["period"] == "year" and x >= 1000:
            return f"${x / 1000:.0f}K" if x % 1000 == 0 or x >= 10000 else f"${x / 1000:.1f}K"
        return f"${x:,.2f}".replace(".00", "") if isinstance(x, float) else f"${x:,}"
    if pay["min"] is None:
        rng = f'up to {money(pay["max"])}'
    elif pay["min"] == pay["max"]:
        rng = money(pay["max"])
    else:
        rng = f'{money(pay["min"])}–{money(pay["max"])}'
    if pay.get("currency", "USD") != "USD":
        rng = f'{rng} {pay["currency"]}'
    return rng if short else f'{rng} {"an hour" if pay["period"] == "hour" else "a year"}'


def annual_pay(pay: dict | None) -> int | None:
    """One figure to sort pay by: the middle of the range, or its top for "up
    to $X", as a year's pay. An hourly rate counts 2080 hours, a full-time
    year, so hourly and salaried seats sort on one scale."""
    if not pay:
        return None
    mid = pay["max"] if pay["min"] is None else (pay["min"] + pay["max"]) / 2
    return round(mid * (2080 if pay["period"] == "hour" else 1))


def hiring_profile(rec: dict | None, max_len: int = 260) -> str | None:
    """The candidate profile a req was written against, lifted from its own
    'Requirements:' sentence.

    This is the closest TICK can honestly get to "who did they hire": the lake
    holds postings, never people. It says what the seat was advertised for, and
    the brief has to label it that way rather than as a named individual."""
    quals = (parsed_description(rec) or {}).get("sections", {}).get("qualifications") or []
    if quals and not (parsed_description(rec) or {}).get("approximate"):
        body = "; ".join(q.rstrip(".;") for q in quals[:3])
        return body if len(body) <= max_len else body[:max_len].rsplit(" ", 1)[0] + "\u2026"
    text = full_text_of(rec)
    for sent in split_sentences(text):
        m = _REQ_RE.search(sent)
        if m:
            body = m.group(2).strip()
            if len(body) > max_len:
                body = body[:max_len].rsplit(" ", 1)[0] + "\u2026"
            return body
    return None


def action_items(lead_closed: list[dict], lead_new: list[dict], themes: list[dict],
                 curated_prev: dict, company_name: str,
                 curated: dict | None = None,
                 people: list[dict] | None = None) -> list[dict]:
    """Turn the window's movement into things a reader can actually do.

    Every item is anchored to an observed event — a seat that closed, a
    programme with reach — so the brief never invents an opportunity the lake
    cannot evidence."""
    items = []
    newest = next((e for e in reversed(themes) if e.get("new_terms")), None)
    lead_theme = None
    # candidate names arrive from the people side-input, keyed to the req they
    # are proposed for — never derived from the posting itself
    by_req = {p["match_for"]: p for p in (people or []) if p.get("match_for")}

    for r in lead_closed[:2]:
        rec = curated_prev.get(r["pid"])
        profile = hiring_profile(rec)
        terms = flat_terms(rec["keywords"]) if rec and rec.get("keywords") else []
        # Which programme this seat actually owns, read from its own description.
        # Score by how many of the role's terms a theme covers — taking the first
        # theme that matched *any* term let the highest-reach programme win, so a
        # Chief AI Officer briefed you to open on legacy modernization because its
        # description happened to say "digital transformation" once.
        scored = [(sum(1 for t in terms if t in e["terms"]), -i, e)
                  for i, e in enumerate(themes)]
        # default=None: a board whose postings match no stack term at all yields no
        # themes, and an unguarded max() took the whole render down with it
        best = max(scored, key=lambda x: (x[0], x[1]), default=None)
        lead_theme = best[2] if best and best[0] else newest
        who = by_req.get(r["pid"])
        steps = [
            'Treat the seat as the entry point regardless of who filled it: a new '
            'executive is buying in their first two quarters, and their agenda is '
            'already written in the req you just read.',
        ]
        if who:
            steps = [html.escape(v) for v in who.get("verify", [])] + steps
        items.append({
            "kind": "match",
            "person": who,
            "title": (f'Approach the new {r["title"]}' if who
                      else f'Identify the new {r["title"]}'),
            "pid": r["pid"],
            "why": (f'The {r["title"]} req came off the board after '
                    f'{r["days_open"]} days' if r["days_open"] else
                    f'The {r["title"]} req came off the board'),
            "profile": profile,
            "theme": lead_theme["theme"] if lead_theme else None,
            "quote": None, "quote_pid": None,
            "steps": steps,
        })

    if lead_new:
        titles = _join_en([html.escape(r["title"]) for r in lead_new[:3]])
        items.append({
            "kind": "watch",
            "title": f'Track the {"searches" if len(lead_new) > 1 else "search"} still open',
            "pid": lead_new[0]["pid"],
            "why": f'{titles} {"are" if len(lead_new) > 1 else "is"} advertised and unfilled',
            "profile": None, "theme": None, "quote": None, "quote_pid": None,
            "person": None,
            "steps": ['An open leadership req is a budget line already approved. '
                      'Time the next touch to the close, not to the posting.'],
        })

    target = lead_theme or newest
    if target:
        # Arm the approach with the account's own sentence about the programme —
        # a line they wrote outranks anything the brief could assert about them.
        curated = curated or {}
        boiler = detect_boilerplate_prefix(curated)
        quote, quote_pid = None, None
        # Rank candidates by how much of THIS theme a posting carries, not by set
        # order — which is both arbitrary and unstable between runs. Without it an
        # AI approach quoted a data-engineering req that merely mentioned ML once.
        theme_terms = set(target["terms"])
        cands = sorted(
            (pid for pid in target["pids"] if pid in curated),
            key=lambda pid: (-len(theme_terms & set(flat_terms(
                curated[pid].get("keywords") or {}))), pid))
        for pid in cands:
            rec = curated[pid]
            ex = pick_excerpt(role_text_of(rec, boiler), flat_terms(rec.get("keywords") or {}), 0)
            if not ex:
                continue
            # prefer a sentence that actually names the programme being pitched
            if any(t in ex.lower() for t in theme_terms):
                quote, quote_pid = ex, pid
                break
            if quote is None:
                quote, quote_pid = ex, pid
        fresh = _join_en([html.escape(t) for t in target["new_terms"][:4]]) or None
        when = (f'{date.fromisoformat(target["new_since"]):%B}'
                if target.get("new_since") else None)
        items.append({
            "kind": "pitch",
            "title": f'Open on {target["theme"]}',
            "pid": next(iter(target["pids"]), None),
            "why": (f'{target["theme"]} now reaches {target["reach"]} open roles'
                    + (f', and {fresh} only appeared here in {when}' if fresh and when else '')),
            "profile": None,
            "theme": target["theme"],
            "quote": quote,
            "quote_pid": quote_pid,
            "person": None,
            "steps": [
                (f'Lead with what they wrote, not what you sell: their own postings '
                 f'name {fresh}. Ask which of those are in production versus still '
                 f'being evaluated &mdash; the answer tells you the stage.'
                 if fresh else
                 f'Lead with what they wrote, not what you sell.'),
                'Anchor on the program, not the tool. They are staffing an '
                'initiative, so the useful question is what it has to deliver this '
                'year and what is unresourced.',
                'Bring one reference from a comparable estate at the same stage. '
                'A first executive conversation is bought with proof, not a deck.',
            ],
        })
    return items


def _join_en(items: list[str]) -> str:
    """Oxford-comma join, so generated prose reads as prose."""
    if len(items) <= 1:
        return items[0] if items else ""
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def closed_postings(diff: dict, state: dict) -> list[dict]:
    """Reqs that left the board over the window, titled from state.

    The curated crawl only holds what is still open, so a closed req's title and
    location have to come from state.json — which keeps them precisely so a
    posting can still be named after it disappears."""
    out = []
    for pid in (diff or {}).get("closed", []):
        entry = state.get(pid) or {}
        if not entry.get("title"):
            continue
        out.append({"pid": pid, "title": entry["title"],
                    "location": entry.get("location") or "",
                    "first_seen": entry.get("first_seen"),
                    "closed_on": entry.get("closed_on"),
                    "days_open": _days_between(entry.get("first_seen"), entry.get("closed_on"))})
    # longest-running first: a req that sat open for months says more on closing
    # than one that lasted a week
    out.sort(key=lambda r: (-(r["days_open"] or 0), r["title"]))
    return out


def archived_postings(lake: dict) -> list[dict]:
    """Every req that has left the board, across the lake's whole history — not
    just this window, which is all closed_postings covers.

    state.json names and dates each one; the detail (description, team, posted
    date, stack terms) comes from the last curated crawl it appeared on. A req
    whose final crawl predates curated descriptions still gets a row, just a
    thinner one, and the detail panel says so rather than looking empty."""
    state, archive = lake.get("state") or {}, lake.get("archive") or {}
    # leading sentences the board repeats, filed as its intro when an old flat
    # description is rebuilt into sections
    boiler = detect_boilerplate_prefix(lake.get("curated") or archive)
    rows = []
    for pid, st in state.items():
        if not st.get("closed_on"):
            continue
        rec = archive.get(pid) or {}
        rows.append({
            "pid": pid,
            "title": rec.get("title") or st.get("title") or pid,
            "location": rec.get("location") or st.get("location") or "",
            "team": rec.get("team"),
            "url": rec.get("url") or "",
            "source": rec.get("source"),
            "posted_on": rec.get("posted_on"),
            "date_trust": rec.get("date_trust"),
            "remote": bool(rec.get("remote_flag")),
            "is_tech_role": bool(rec.get("is_tech_role")),
            "terms": flat_terms(rec.get("keywords")),
            "description": full_text_of(rec) or None,
            "parsed": parsed_description(rec, boiler),
            "pay": pay_of(rec),
            "first_seen": st.get("first_seen"),
            "last_seen": st.get("last_seen"),
            "closed_on": st["closed_on"],
            "days_open": _days_between(st.get("first_seen"), st["closed_on"]),
            "has_record": bool(rec),
        })
    # most recently closed first; title breaks ties so rows do not reshuffle
    rows.sort(key=lambda r: r["title"].lower())
    rows.sort(key=lambda r: r["closed_on"], reverse=True)
    return rows


def _days_between(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def detect_boilerplate_prefix(curated: dict, max_check: int = 6, threshold: float = 0.7) -> int:
    """How many leading sentences are shared company-boilerplate (e.g. "Join Dunder Mifflin,
    a global leader...") across most postings, so excerpts can skip past them."""
    sent_lists = [split_sentences(full_text_of(r)) for r in curated.values()]
    sent_lists = [s for s in sent_lists if s]
    if not sent_lists:
        return 0
    n = 0
    for i in range(min(max_check, min(len(s) for s in sent_lists))):
        counts = Counter(s[i] for s in sent_lists)
        if counts.most_common(1)[0][1] / len(sent_lists) >= threshold:
            n = i + 1
        else:
            break
    return n


def _term_pattern(term: str) -> re.Pattern:
    return re.compile(r"(?i)(?<![\w#./-])" + re.escape(term) + r"(?![\w#-])")


def pick_excerpt(description: str, terms: list[str], boilerplate_n: int) -> str | None:
    """The most on-signal sentence in a description: the one matching the most
    of the posting's own matched stack terms, past the shared boilerplate lead-in."""
    sentences = split_sentences(description)[boilerplate_n:]
    scored = []
    for s in sentences:
        hits = sum(1 for t in terms if _term_pattern(t).search(s))
        if hits:
            scored.append((hits, -len(s), s))
    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][2]


def flat_terms(keywords: dict) -> list[str]:
    return sorted({t for terms in (keywords or {}).values() for t in terms})


# ----------------------------------------------------------------- accounts

# What the lake can say about an account, weakest evidence last. The home
# screen never claims more than the tier an account has actually reached.
# Reporting starts at the first crawl: the open board is worth reading on day
# one, and the dashboard says plainly when there is no earlier crawl to compare.
_REPORTING = "Reporting"        # at least one snapshot — dashboard renders
_CONFIGURED = "Not collected"   # verified config, nothing in the lake
_REVIEW = "Needs review"        # config flagged needs_human_review

_STATUS_ORDER = {_REPORTING: 0, _CONFIGURED: 1, _REVIEW: 2}


_CONFIG_KEY_RE = re.compile(r"(?:^|/)configs/[^/]+\.json$")


def _read_configs(get, keys) -> list[dict]:
    """Parse SourceConfigs, skipping anything unreadable or not a config — the
    home screen is a report, not a validator."""
    out = []
    for key in keys:
        try:
            cfg = get(key)
        except (OSError, ValueError):
            continue
        if isinstance(cfg, dict) and cfg.get("company_slug"):
            out.append(cfg)
    return out


def load_configs(location: str | None, lake_io: dict | None = None) -> list[dict]:
    """Every evaluator SourceConfig from a local directory or an s3:// prefix.

    With no location, falls back to the lake's own configs/ prefix — the same
    list the dispatcher collects from, so the home screen shows exactly the
    accounts the pipeline is working on, including ones with nothing collected
    yet. Nothing found is not an error; the lake alone can populate the page."""
    if location and location.startswith("s3://"):
        io = open_lake(location)
        return _read_configs(io["get"], sorted(k for k in io["keys"] if k.endswith(".json")))
    if location:
        if not os.path.isdir(location):
            return []
        return _read_configs(lambda k: json.load(open(k)),
                             sorted(glob.glob(os.path.join(location, "*.json"))))
    if lake_io:
        return _read_configs(lake_io["get"],
                             sorted(k for k in lake_io["keys"] if _CONFIG_KEY_RE.search(k)))
    return []


def build_accounts(configs: list[dict], lake_io: dict,
                   window_days: int = DEFAULT_WINDOW_DAYS) -> list[dict]:
    """One row per account the user has: every config, plus any company sitting
    in the lake without one. Numbers come from the lake only — an account with
    nothing collected carries None, not a zero."""
    by_slug: dict[str, dict] = {}
    for cfg in configs:
        by_slug.setdefault(cfg["company_slug"], cfg)
    for slug in lake_companies(lake_io):
        by_slug.setdefault(slug, {})

    rows = []
    for slug, cfg in by_slug.items():
        snap = load_snapshots(lake_io, slug, window_days)
        curr = snap["curr"] if snap else None
        prev = snap["prev"] if snap else None
        counts = ((snap or {}).get("diff") or {}).get("counts", {})

        delta = pct = None
        if curr and prev:
            delta = curr["postings_total"] - prev["postings_total"]
            pct = round(100 * delta / prev["postings_total"], 1) if prev["postings_total"] else 0.0

        if snap:
            status = _REPORTING
        elif cfg.get("needs_human_review"):
            status = _REVIEW
        else:
            status = _CONFIGURED

        board = None
        if curr and curr.get("postings"):
            board = board_domain(curr["postings"])
        elif cfg.get("endpoint"):
            board = urlparse(cfg["endpoint"]).netloc

        rows.append({
            "slug": slug,
            "name": cfg.get("company_name") or (curr or {}).get("company") or slug,
            "ats": cfg.get("ats"),
            "tier": cfg.get("tier"),
            "confidence": cfg.get("confidence"),
            "careers_url": cfg.get("careers_url"),
            "board": board,
            "status": status,
            "has_pages": status == _REPORTING,
            # A brief writes up a week of movement, so it needs a crawl to
            # compare against; the dashboard does not.
            "has_baseline": prev is not None,
            "snapshots": snap["snapshots"] if snap else 0,
            "open_roles": curr["postings_total"] if curr else None,
            "tech_roles": len(curr.get("tech_role_ids", [])) if curr else None,
            "curr_dt": curr["captured_dt"] if curr else None,
            "prev_dt": prev["captured_dt"] if prev else None,
            "captured_at": curr.get("captured_at") if curr else None,
            "delta": delta,
            "pct": pct,
            "new": counts.get("new"),
            "closed": counts.get("closed"),
        })

    rows.sort(key=lambda a: (_STATUS_ORDER[a["status"]], -(a["open_roles"] or 0), a["name"].lower()))
    return rows


# ---------------------------------------------------------------- shared shell

_STYLE = """
:root{
  color-scheme:light;
  --color-bg:#f5f7fa;--color-surface:#fff;--color-surface-2:#f8fafc;
  --color-text:#17212b;--color-text-2:#4b5866;--color-text-3:#64717f;
  --color-border:#e3e8ee;--color-border-strong:#cdd5df;--color-divider:var(--color-border);
  --color-accent:#3d6a96;--color-accent-100:#edf3f9;--color-accent-200:#d3e1ee;
  --color-accent-700:#30577d;--color-accent-800:#264763;--color-accent-900:#1b3349;
  --color-neutral-100:#f0f3f6;--color-neutral-500:#8b97a4;--color-neutral-800:#3c4652;
  --color-live:#2f8f5b;--color-review:#c27c1a;
  --radius:12px;--radius-sm:8px;
  --shadow-sm:0 1px 2px rgba(16,24,40,.05);
  --shadow:0 1px 2px rgba(16,24,40,.04),0 1px 4px rgba(16,24,40,.04);
  --shadow-md:0 6px 18px rgba(16,24,40,.08),0 1px 3px rgba(16,24,40,.05);
  --shadow-lg:0 24px 56px rgba(16,24,40,.2),0 2px 8px rgba(16,24,40,.06);
  --ring:0 0 0 3px color-mix(in srgb,var(--color-accent) 24%,transparent);
  --font-heading:"Barlow Condensed",system-ui,sans-serif;--font-body:"Barlow",system-ui,sans-serif;
}
*,*::before,*::after{box-sizing:border-box}
/* the UA rule for [hidden] is display:none, but any author display wins over it —
   .pager's display:flex kept the pager on screen after JS had hidden it */
[hidden]{display:none!important}
body{margin:0;background:var(--color-bg);color:var(--color-text);font-family:var(--font-body);font-size:15px;line-height:1.55;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
h1,h2,h3,h4,h5,h6{font-family:var(--font-heading);font-weight:600;line-height:1.15;letter-spacing:-.01em;margin:0;text-wrap:balance}
a{color:var(--color-accent);text-underline-offset:3px;text-decoration-thickness:1px}
a:hover{color:var(--color-accent-700)}
:focus-visible{outline:2px solid var(--color-accent);outline-offset:2px}
.text-muted{color:var(--color-text-3)}
.visually-hidden{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;
  clip:rect(0 0 0 0);clip-path:inset(50%);white-space:nowrap;border:0}
.skip-link{position:absolute;left:8px;top:-48px;z-index:10;padding:9px 14px;background:var(--color-accent-900);border-radius:var(--radius-sm);
  color:#fff;text-decoration:none;font-weight:600;font-size:14px;transition:top .12s}
.skip-link:focus{top:8px}
/* page title block: an eyebrow line above the h1, so the heading order stays h1 -> h2 */
.eyebrow{font-family:var(--font-body);font-weight:600;font-size:12.5px;letter-spacing:.08em;
  text-transform:uppercase;line-height:1.2;margin:0;color:var(--color-accent)}
.section-label{display:flex;align-items:center;gap:10px;margin:0;font-family:var(--font-heading);font-weight:600;
  font-size:21px;letter-spacing:-.005em;color:var(--color-text)}
.count{display:inline-flex;align-items:center;justify-content:center;min-width:26px;height:22px;padding:0 8px;
  border-radius:999px;background:var(--color-neutral-100);color:var(--color-text-2);font-family:var(--font-body);
  font-size:12.5px;font-weight:600;letter-spacing:0;font-variant-numeric:tabular-nums}
.wrap{max-width:1280px;margin:0 auto;padding:0 32px 64px}
.wrap.narrow{max-width:1040px}
/* menu bar: a twin of the one in infra/site-static/demo/ (the source of truth),
   colors pinned to that page's palette. Change both — see CLAUDE.md. */
.nav{display:flex;align-items:center;gap:28px;padding:16px 32px;
  background:#080d12;border-bottom:1px solid rgba(158,186,214,.14);margin-bottom:0;flex-wrap:wrap}
.nav-brand{display:inline-flex;align-items:center;gap:10px;font-family:var(--font-heading);font-weight:700;font-size:25px;letter-spacing:.14em;color:#eef2f5}
.nav-brand svg{display:block;width:26px;height:26px;flex:none;color:#86aace}
.nav-tabs{display:flex;align-items:center;gap:28px;flex-wrap:wrap}
.nav-tabs a{color:#9aa9b8;text-decoration:none;font-size:18px;padding-bottom:2px}
.nav-tabs a:hover{color:#eef2f5}
.nav-tabs a[aria-current='page']{color:#eef2f5;border-bottom:2px solid #86aace}
/* min-height: the demo's Get started pill is 48px tall; without it the bar is
   8px shorter here and the tabs jump between the demo and the app */
.nav-auth{margin-left:auto;display:flex;align-items:center;gap:20px;min-height:48px}
.nav-login{font-family:var(--font-body);font-size:18px;color:#9aa9b8;background:none;border:0;padding:6px 2px;cursor:pointer;text-decoration:none;display:inline-block}
.nav-login:hover{color:#eef2f5}
.nav :focus-visible{outline-color:#eef2f5}
/* the account menu, in the signed-in corner. The demo has no signed-in corner
   (it offers Log in / Get started there), so these rules have no twin in it. */
.nav-menu{position:relative}
.nav-menu summary{display:flex;align-items:center;gap:10px;list-style:none;cursor:pointer;color:#9aa9b8;font-size:18px;
  padding:4px 8px 4px 4px;border-radius:999px;transition:background .12s,color .12s}
.nav-menu summary::-webkit-details-marker{display:none}
.nav-menu summary:hover,.nav-menu[open] summary{color:#eef2f5;background:rgba(134,170,206,.12)}
.nav-menu svg{display:block;flex:none;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.nav-avatar{display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;border-radius:50%;
  background:#456e99;color:#fff;font-family:var(--font-heading);font-weight:600;font-size:17px;line-height:1}
.nav-avatar svg{width:20px;height:20px}
.nav-who{max-width:16ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nav-caret{width:14px;height:14px;transition:transform .12s}
.nav-menu[open] .nav-caret{transform:rotate(180deg)}
.nav-menu-panel{position:absolute;right:0;top:calc(100% + 10px);z-index:20;min-width:240px;max-width:calc(100vw - 32px);
  padding:6px;background:#0d151d;border:1px solid rgba(158,186,214,.22);border-radius:12px;box-shadow:0 18px 40px rgba(0,0,0,.4)}
.nav-menu-email{margin:0 0 4px;padding:8px 10px 10px;font-size:13px;color:#9aa9b8;overflow-wrap:anywhere;
  border-bottom:1px solid rgba(158,186,214,.14)}
.nav-menu-panel a{display:flex;align-items:center;gap:12px;padding:9px 10px;color:#c9d4de;text-decoration:none;font-size:16px;border-radius:8px}
.nav-menu-panel a svg{width:19px;height:19px;color:#86aace}
.nav-menu-panel a:hover{background:rgba(134,170,206,.12);color:#eef2f5}
.nav-menu-panel a[aria-current='page']{color:#eef2f5;background:rgba(134,170,206,.08)}
.crumb{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px;color:var(--color-text-3);margin:18px 0 8px}
.crumb a{color:inherit;text-decoration:none}
.crumb a:hover{color:var(--color-accent)}
.crumb .here{color:var(--color-text);font-weight:500}
.hdr{display:flex;align-items:center;justify-content:space-between;gap:12px 24px;flex-wrap:wrap;padding:0 0 18px}
.hdr-top{padding-top:28px}
.hdr-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.hdr h1{font-size:32px;line-height:1.1;margin:0}
.hdr .eyebrow+h1{margin-top:4px}
.hdr-sub{margin:10px 0 0;font-size:14.5px;line-height:1.55;color:var(--color-text-2);max-width:74ch}
.hdr-meta{display:flex;align-items:center;flex-wrap:wrap;gap:6px 14px;margin:8px 0 0;font-size:13.5px;color:var(--color-text-2)}
.hdr-meta>*+*::before{content:"";display:inline-block;width:4px;height:4px;border-radius:50%;background:var(--color-border-strong);
  margin-right:14px;vertical-align:middle}
.blueprint{position:relative;background:var(--color-surface);border:1px solid var(--color-border);border-radius:var(--radius);box-shadow:var(--shadow)}
.card{padding:16px 18px}
.card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:10px}
.card-title{font-family:var(--font-heading);font-weight:600;font-size:19px;line-height:1.2;margin:0}
.card-sub{display:block;margin:3px 0 0;font-size:13px;line-height:1.45;color:var(--color-text-3)}
.note{margin:10px 0 0;font-size:12.5px;line-height:1.55;color:var(--color-text-3)}
.empty{margin:0;font-size:13.5px;line-height:1.55;color:var(--color-text-3)}
.tag{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:500;line-height:1.45;letter-spacing:.01em;
  padding:2px 10px;border-radius:999px;white-space:nowrap}
.row-flags{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
/* in a table row the flags follow the title: the tables run full width, so a
   flag on its own line only made every flagged row taller */
.table .row-flags{display:inline-flex;margin:0 0 0 8px;vertical-align:1px}
.tag-flag{font-size:11.5px;padding:1px 8px}
.tag-accent{background:var(--color-accent-100);color:var(--color-accent-800)}
.tag-neutral{background:var(--color-neutral-100);color:var(--color-neutral-800)}
.tag-outline{border:1px solid var(--color-accent-200);color:var(--color-accent-800);background:var(--color-surface)}
.tag-solid{background:var(--color-accent);color:#fff}
.tag-mine{background:var(--color-accent-900);color:#fff}
/* account state: a dot beside the word, so state is never told by color alone */
.status{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;font-weight:500;color:var(--color-text-2);white-space:nowrap;
  padding:2px 10px 2px 8px;border-radius:999px;background:var(--color-neutral-100)}
.status::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--color-neutral-500)}
.status-live::before{background:var(--color-live);box-shadow:0 0 0 3px color-mix(in srgb,var(--color-live) 18%,transparent)}
.status-review::before{background:var(--color-review)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:38px;padding:0 16px;cursor:pointer;
  text-decoration:none;font-family:var(--font-body);font-weight:600;font-size:14px;line-height:1.2;border-radius:var(--radius-sm);
  border:1px solid var(--color-border-strong);background:var(--color-surface);color:var(--color-text);box-shadow:var(--shadow-sm);
  transition:background .12s,border-color .12s,color .12s,box-shadow .12s}
.btn:hover{color:var(--color-text)}
.btn-primary{background:var(--color-accent);color:#fff;border-color:var(--color-accent)}
.btn-primary:hover{background:var(--color-accent-700);border-color:var(--color-accent-700);color:#fff}
.btn-secondary:hover{background:var(--color-surface-2);border-color:var(--color-neutral-500)}
.btn-sm{min-height:32px;padding:0 12px;font-size:13px}
.btn[disabled]{opacity:.5;cursor:default}
.btn-link{font:inherit;font-size:13px;font-weight:500;background:none;border:0;padding:0;color:var(--color-accent);cursor:pointer;
  text-decoration:underline;text-underline-offset:3px}
/* every text field in the app shares one look */
.tbl-controls input[type=search],.my-terms-form input,.acct-add input,.acct-add textarea,
.pgrid input[type=text],.pgrid textarea,.pgrid select{font-family:var(--font-body);font-size:14px;line-height:1.4;padding:8px 12px;
  background:var(--color-surface);color:var(--color-text);border:1px solid var(--color-border-strong);border-radius:var(--radius-sm);
  box-shadow:var(--shadow-sm);transition:border-color .12s,box-shadow .12s}
.tbl-controls input[type=search]:focus-visible,.my-terms-form input:focus-visible,.acct-add input:focus-visible,
.acct-add textarea:focus-visible,.pgrid input:focus-visible,.pgrid textarea:focus-visible,.pgrid select:focus-visible{
  outline:none;border-color:var(--color-accent);box-shadow:var(--ring)}
::placeholder{color:var(--color-neutral-500)}
.table{width:100%;border-collapse:separate;border-spacing:0;font-size:14px}
.table th{text-align:left;font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--color-text-3);
  padding:9px 14px;background:var(--color-surface-2);border-bottom:1px solid var(--color-border);white-space:nowrap}
.table td{padding:10px 14px;border-bottom:1px solid var(--color-border);vertical-align:top}
.table tbody tr:last-child td{border-bottom:0}
.table tbody tr{transition:background .1s}
.table tbody tr:hover{background:var(--color-surface-2)}
.table td>a{color:var(--color-accent-800);font-weight:600;text-decoration:none}
.table td>a:hover{color:var(--color-accent);text-decoration:underline}
.table .num{text-align:right;font-variant-numeric:tabular-nums}
.table td.nowrap{white-space:nowrap;font-variant-numeric:tabular-nums}
.tbl-wrap{overflow-x:auto}
/* the stack's buckets are columns of one card: an empty bucket is a short
   column, not a card standing half empty beside a full one */
.stack-card{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr);margin-top:12px}
.stack-col{padding:16px 18px;min-width:0}
.stack-col+.stack-col{border-left:1px solid var(--color-border)}
/* a bucket with the card to itself (a first crawl, or an empty neighbour)
   flows its terms into two columns rather than one long one */
.stack-list.wide{display:block;columns:2;column-gap:36px}
.stack-list.wide .stack-row{break-inside:avoid;border-top:0;border-bottom:1px solid var(--color-border);padding:6px 0}
.stack-list{display:flex;flex-direction:column}
.stack-row{display:grid;grid-template-columns:minmax(0,1fr) 72px minmax(96px,auto);align-items:center;gap:14px;
  padding:6px 0;border-top:1px solid var(--color-border);font-size:14px}
.stack-row:first-child{border-top:0;padding-top:2px}
.stack-meta{font-size:12.5px;color:var(--color-text-3);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
/* a term's share of the busiest term's postings: one hue, the track a lighter step of it */
.meter{height:6px;border-radius:999px;background:var(--color-accent-100);overflow:hidden}
.meter>i{display:block;height:100%;border-radius:999px;background:var(--color-accent)}
/* the headline figures: one card, its cells split by hairlines */
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin:0 0 16px}
.kpi{padding:13px 18px;border-left:1px solid var(--color-border);min-width:0}
.kpi:first-child{border-left:0}
.kpi-label{display:block;font-size:13px;font-weight:500;color:var(--color-text-2)}
.kpi-value{display:block;font-family:var(--font-body);font-weight:600;font-size:28px;line-height:1.1;letter-spacing:-.02em;margin-top:3px}
.kpi-sub{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:4px;font-size:12.5px;color:var(--color-text-3)}
.delta{display:inline-flex;align-items:center;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;
  font-variant-numeric:tabular-nums;background:var(--color-neutral-100);color:var(--color-neutral-800)}
.delta.up{background:var(--color-accent-100);color:var(--color-accent-800)}
.stat-row{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;border-top:1px solid var(--color-border);margin-top:12px;padding-top:12px}
.stat b{display:block;font-family:var(--font-body);font-weight:600;font-size:24px;line-height:1.1;letter-spacing:-.01em}
.stat span{display:block;margin-top:3px;font-size:12px;color:var(--color-text-3)}
.right-col{display:flex;flex-direction:column;gap:16px}
.grid{display:grid;grid-template-columns:minmax(0,7fr) minmax(0,4fr);gap:16px;align-items:start}
/* a grid item defaults to min-width:auto, so it refuses to shrink below the
   widest thing inside it — the roles table then pushed the whole page sideways
   instead of scrolling inside its own overflow-x container */
.grid>*,.right-col>*{min-width:0}
.grid.narrative{grid-template-columns:minmax(0,1fr) 300px;gap:24px}
.section{margin-top:28px}
.kv{display:flex;flex-direction:column;font-size:13.5px}
.kv>div{display:flex;justify-content:space-between;align-items:baseline;gap:12px;padding:6px 0;border-top:1px solid var(--color-border)}
.kv>div:first-child{border-top:0;padding-top:0}
.kv>div:last-child{padding-bottom:0}
.kv>div>:first-child{color:var(--color-text-3)}
.kv>div>:last-child{font-weight:600;text-align:right;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.list-rows{display:flex;flex-direction:column}
.list-rows>div{padding:8px 0;border-top:1px solid var(--color-border)}
.list-rows>div:first-child{border-top:0;padding-top:0}
.list-rows a{font-weight:600;color:var(--color-accent-800);text-decoration:none;font-size:14px}
.list-rows a:hover{color:var(--color-accent);text-decoration:underline}
.list-rows span{display:block;font-size:12.5px;color:var(--color-text-3);margin-top:1px}
.cite{font-size:11px;color:var(--color-accent-800)}
sup.cite{line-height:0;margin-left:1px}
sup.cite a{padding:0 2px;border-radius:4px;color:var(--color-accent);font-size:11px;font-weight:700;
  text-decoration:none;font-variant-numeric:tabular-nums;transition:background .12s,color .12s}
sup.cite a:hover{background:var(--color-accent-100);color:var(--color-accent-800)}
.prose p{margin:0 0 16px;font-size:16.5px;line-height:1.75;max-width:68ch}
.prose p:last-child{margin-bottom:0}
.paper{padding:24px 28px}
.crumb a{color:inherit;text-decoration:none}
/* min() keeps a 340px track from overflowing a 320px phone */
.acct-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(320px,100%),1fr));gap:16px;align-items:stretch}
.acct-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:12px}
.acct-name{font-family:var(--font-heading);font-weight:600;font-size:21px;line-height:1.15}
.acct-name a{color:inherit;text-decoration:none}
.acct-name a:hover{color:var(--color-accent)}
.acct-meta{font-size:12.5px;margin-top:4px;color:var(--color-text-3);overflow-wrap:anywhere}
.acct-add:not([open]){align-self:start}
.acct-grid>.card{display:flex;flex-direction:column;transition:box-shadow .15s,border-color .15s}
.acct-grid>.card.is-reporting:hover{border-color:var(--color-border-strong);box-shadow:var(--shadow-md)}
.acct-lede b{color:var(--color-text);font-weight:600}
.acct-foot{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:auto;padding-top:14px}
/* cards in a row share a height; figures and buttons sit at the bottom, so
   they line up across the row whatever the length of each name */
.acct-grid>.card>.stat-row{margin-top:auto}
.acct-grid>.card>.stat-row+.acct-foot{margin-top:0}
.acct-foot .acct-when{margin-left:auto;font-size:12.5px;color:var(--color-text-3)}
.acct-note{font-size:13px;line-height:1.55;margin:14px 0 0;color:var(--color-text-3)}
/* the request tile: closed, a quiet dashed card; opened, the form */
.acct-add{background:transparent;border:1.5px dashed var(--color-border-strong);box-shadow:none;padding:0}
.acct-add[open]{background:var(--color-surface);border-style:solid;border-width:1px;border-color:var(--color-border);box-shadow:var(--shadow)}
.acct-add>summary{display:flex;align-items:center;gap:14px;padding:16px 18px;cursor:pointer;list-style:none;border-radius:var(--radius)}
.acct-add>summary::-webkit-details-marker{display:none}
.acct-add>summary:hover .acct-plus{background:var(--color-accent);color:#fff;border-color:var(--color-accent)}
.acct-plus{display:inline-flex;align-items:center;justify-content:center;flex:none;width:40px;height:40px;border-radius:50%;
  border:1px solid var(--color-border-strong);background:var(--color-surface);color:var(--color-accent);font-size:22px;line-height:1;
  transition:background .12s,color .12s,border-color .12s}
.acct-add[open] .acct-plus{transform:rotate(45deg)}
.acct-add form{padding:0 18px 16px}
.acct-add label{display:block;font-size:13px;font-weight:500;margin:14px 0 6px;color:var(--color-text-2)}
.acct-add label:first-of-type{margin-top:0}
.acct-add input,.acct-add textarea{width:100%}
.acct-add textarea{resize:vertical;min-height:64px}
.acct-add .acct-status{font-size:13px}
.chart-wrap{position:relative;margin:4px 0 0}
/* the plot stretches to the card, so its strokes do not scale (vector-effect)
   and its labels and dots are HTML laid over it by percentage: text stays
   legible at any width, where a scaled viewBox shrank it to 5px on a phone */
.ch-frame{padding:8px 30px 28px 34px}
.ch-plot{position:relative;height:200px}
.ch-svg{position:absolute;inset:0;display:block;width:100%;height:100%;overflow:visible}
.ch-grid{stroke:var(--color-border);stroke-width:1;vector-effect:non-scaling-stroke}
.ch-base{stroke:var(--color-border-strong);stroke-width:1;vector-effect:non-scaling-stroke}
.ch-line{fill:none;stroke:var(--color-accent);stroke-width:2;stroke-linejoin:round;stroke-linecap:round;vector-effect:non-scaling-stroke}
.ch-area{fill:var(--color-accent);opacity:.1}
.ch-hair{stroke:var(--color-neutral-500);stroke-width:1;vector-effect:non-scaling-stroke}
.ch-dot{position:absolute;width:10px;height:10px;margin:-5px 0 0 -5px;border-radius:50%;background:var(--color-accent);
  box-shadow:0 0 0 2px var(--color-surface);pointer-events:none}
.ch-ylab,.ch-xlab,.ch-endlab{position:absolute;font-size:12px;line-height:1;color:var(--color-text-3);font-variant-numeric:tabular-nums;
  white-space:nowrap;pointer-events:none}
.ch-ylab{left:-10px;transform:translate(-100%,-50%)}
.ch-xlab{top:100%;margin-top:10px;transform:translateX(-50%)}
.ch-xlab.first{transform:none}
.ch-endlab{margin-left:10px;transform:translateY(-50%);font-weight:600;color:var(--color-text)}
.ch-hit{cursor:crosshair}
.ch-hit:focus-visible{outline:2px solid var(--color-accent);outline-offset:2px}
.spark{display:block;width:100%;height:auto;margin:10px 0 8px;overflow:visible}
.chart-tip{position:absolute;top:0;left:0;pointer-events:none;background:var(--color-surface);border:1px solid var(--color-border);
  border-radius:var(--radius-sm);box-shadow:var(--shadow-md);padding:7px 10px;font-size:12px;line-height:1.35;white-space:nowrap;z-index:2}
.chart-tip b{display:block;font-size:15px;font-weight:600;color:var(--color-text)}
.chart-tip span{color:var(--color-text-3)}
.chart-tbl{margin-top:6px}
.chart-tbl summary{font-size:12.5px;color:var(--color-text-3);cursor:pointer}
.chart-tbl summary:hover{color:var(--color-accent)}
.chart-tbl table{width:100%;border-collapse:collapse;margin-top:8px;font-size:12.5px}
.chart-tbl th,.chart-tbl td{text-align:left;padding:4px 0;border-bottom:1px solid var(--color-border)}
.chart-tbl .num{text-align:right;font-variant-numeric:tabular-nums}
.chart-foot{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-top:12px;padding-top:12px;
  border-top:1px solid var(--color-border);font-size:13px;color:var(--color-text-3)}
.tbl-head{display:flex;align-items:center;justify-content:space-between;gap:10px 16px;flex-wrap:wrap;margin-bottom:10px}
.tbl-controls{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.tbl-controls input[type=search]{width:290px;max-width:100%;padding-left:36px;
  background:var(--color-surface) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2364717f' stroke-width='2' stroke-linecap='round'%3E%3Ccircle cx='11' cy='11' r='6.5'/%3E%3Cpath d='M20 20l-4.2-4.2'/%3E%3C/svg%3E") no-repeat 12px center/16px}
.chk{display:inline-flex;align-items:center;gap:8px;font-size:13px;cursor:pointer;color:var(--color-text-2)}
.chk input{accent-color:var(--color-accent);width:15px;height:15px;margin:0}
/* filters and the persona's buyer boxes read as pills; the checkbox stays the control */
.tbl-filters .chk,.pchecks .chk{padding:5px 12px 5px 10px;border:1px solid var(--color-border-strong);border-radius:999px;
  background:var(--color-surface);transition:background .12s,border-color .12s,color .12s}
.tbl-filters .chk:hover,.pchecks .chk:hover{border-color:var(--color-neutral-500)}
.tbl-filters .chk:has(input:checked),.pchecks .chk:has(input:checked){background:var(--color-accent-100);border-color:var(--color-accent);color:var(--color-accent-800)}
.chk:has(input:focus-visible){outline:2px solid var(--color-accent);outline-offset:2px}
.chk-n{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:19px;padding:0 6px;border-radius:999px;
  background:var(--color-neutral-100);color:var(--color-text-2);font-size:11.5px;font-weight:600;font-variant-numeric:tabular-nums}
.chk:has(input:checked) .chk-n{background:var(--color-surface);color:var(--color-accent-800)}
.tbl-filters{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 10px}
.tbl-filters .btn-link{margin-left:6px}
.filter-chip{display:inline-flex;align-items:center;gap:2px;font-size:13px;font-weight:500;padding:4px 4px 4px 12px;border-radius:999px;
  background:var(--color-accent-900);color:#fff}
.chip-x{font:inherit;font-size:16px;line-height:1;background:none;border:0;padding:0 6px;cursor:pointer;color:inherit;border-radius:999px}
.chip-x:hover{background:rgba(255,255,255,.18)}
/* sortable headers: the button is added by the table script, so with JS off a
   header is plain text rather than a control that does nothing */
.th-sort{font:inherit;letter-spacing:inherit;text-transform:inherit;color:inherit;background:none;border:0;
  padding:0;cursor:pointer;white-space:nowrap;text-align:inherit}
.th-sort:hover,.table th[aria-sort=ascending],.table th[aria-sort=descending]{color:var(--color-text)}
.table th[aria-sort=ascending] .th-sort::after{content:" \\2191"}
.table th[aria-sort=descending] .th-sort::after{content:" \\2193"}
/* stack terms are buttons that filter Open roles to the postings behind them */
button.tag{font-family:var(--font-body);cursor:pointer;margin:0 4px 4px 0;transition:background .12s,color .12s,border-color .12s}
button.tag-outline:hover,button.tag-outline[aria-pressed=true]{background:var(--color-accent);border-color:var(--color-accent);color:#fff}
.term-btn{font:inherit;font-weight:500;color:var(--color-text);background:none;border:0;padding:0;cursor:pointer;text-align:left;
  text-decoration:underline;text-decoration-color:var(--color-border-strong);text-underline-offset:4px;overflow-wrap:anywhere}
.term-btn:hover,.term-btn[aria-pressed=true]{color:var(--color-accent-700);text-decoration-color:currentColor}
.term-btn[aria-pressed=true]{font-weight:600}
.row-hit{margin-top:6px;font-size:12.5px;line-height:1.5;color:var(--color-text-3)}
.row-hit mark{background:#fdf1c7;color:var(--color-text);padding:0 2px;border-radius:3px}
.my-terms{display:flex;align-items:center;gap:8px 14px;flex-wrap:wrap;margin:0 0 10px;padding:9px 12px;
  background:var(--color-surface);border:1px solid var(--color-border);border-radius:var(--radius);font-size:13px}
.my-terms-label{font-weight:600;font-size:13.5px;color:var(--color-text)}
.my-terms-list{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.chip{display:inline-flex;align-items:center;border:1px solid var(--color-accent-200);color:var(--color-accent-900);border-radius:999px;
  background:var(--color-surface);overflow:hidden}
.chip-term{font:inherit;font-size:12.5px;font-weight:500;background:none;border:0;padding:3px 4px 3px 11px;cursor:pointer;color:inherit}
.chip-term[aria-pressed=true]{background:var(--color-accent-900);color:#fff}
.chip .chip-x:hover{background:var(--color-accent-100)}
.my-terms-form{display:flex;gap:6px}
.my-terms-form input{width:230px;max-width:100%;padding:6px 11px;font-size:13px}
.my-terms #my-terms-note{flex:1 0 100%;font-size:12.5px}
.tbl-empty{margin:0;padding:18px 16px;font-size:13.5px}
/* the persona editor on /profile: settings sections, a heading beside its fields */
.callout{display:flex;gap:12px;align-items:flex-start;margin:0 0 20px;padding:12px 16px;border-radius:var(--radius);
  background:var(--color-accent-100);color:var(--color-accent-900);font-size:14px;line-height:1.55}
.callout::before{content:"i";flex:none;display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;margin-top:1px;
  border-radius:50%;background:var(--color-accent);color:#fff;font-size:12px;font-weight:700;font-style:italic;font-family:Georgia,serif}
.callout a{color:var(--color-accent-800);font-weight:600}
.psec{display:grid;grid-template-columns:minmax(0,260px) minmax(0,1fr);gap:12px 40px;padding:22px 0;border-top:1px solid var(--color-border)}
.psec:first-of-type{border-top:0;padding-top:0}
.psec h3{font-size:19px}
.psec-head p{margin:6px 0 0;font-size:13.5px;line-height:1.55;color:var(--color-text-3)}
.pform{padding:0}
.pgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px 20px;padding:18px}
.pgrid .wide{grid-column:1/-1}
.pgrid label:not(.chk),.pgrid legend{display:block;font-size:13.5px;font-weight:500;margin:0;color:var(--color-text)}
.pgrid input[type=text],.pgrid textarea,.pgrid select{display:block;width:100%;margin-top:7px}
.pgrid select{appearance:none;padding-right:36px;cursor:pointer;
  background:var(--color-surface) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2364717f' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E") no-repeat right 12px center/16px}
.pgrid textarea{resize:vertical;min-height:72px}
.pgrid fieldset{border:0;padding:0;margin:0;min-width:0}
.pchecks{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.phint{display:block;margin-top:7px;font-size:12.5px;font-weight:400;color:var(--color-text-3)}
.pform-foot{position:sticky;bottom:0;z-index:5;display:flex;align-items:center;justify-content:flex-end;gap:14px;flex-wrap:wrap;margin-top:8px;
  padding:14px 0;background:color-mix(in srgb,var(--color-bg) 88%,transparent);backdrop-filter:blur(6px);border-top:1px solid var(--color-border)}
.pform-foot .acct-status{margin-right:auto;font-size:13.5px}
@media (max-width:760px){.psec{grid-template-columns:1fr}.pgrid{grid-template-columns:1fr}}
.pager{display:flex;align-items:center;gap:12px;margin-top:10px;flex-wrap:wrap}
.pager [role=status]{font-size:13px;font-variant-numeric:tabular-nums}
.pager button[disabled]{opacity:.45;cursor:default;box-shadow:none}
.arch-table tbody tr[data-arch]{cursor:pointer}
.arch-dialog{width:min(780px,calc(100vw - 32px));max-height:calc(100vh - 48px);padding:0;border:0;border-radius:16px;
  background:var(--color-surface);color:var(--color-text);box-shadow:var(--shadow-lg)}
.arch-dialog::backdrop{background:rgba(15,23,32,.5);backdrop-filter:blur(2px)}
.arch-dialog-bar{position:sticky;top:0;z-index:1;display:flex;justify-content:flex-end;padding:12px 14px;
  background:color-mix(in srgb,var(--color-surface) 92%,transparent);backdrop-filter:blur(6px);border-bottom:1px solid var(--color-border)}
.arch-dialog-body{padding:18px 28px 28px}
.arch-title{font-size:30px;margin:6px 0 10px}
.arch-fields{display:grid;grid-template-columns:max-content 1fr;gap:0 24px;margin:20px 0 0;font-size:14px;
  border:1px solid var(--color-border);border-radius:var(--radius);padding:6px 18px;background:var(--color-surface)}
.arch-fields dt,.arch-fields dd{padding:9px 0;border-top:1px solid var(--color-border)}
.arch-fields dt:first-of-type,.arch-fields dd:first-of-type{border-top:0}
.arch-fields dt{color:var(--color-text-3)}
.arch-fields dd{margin:0;overflow-wrap:anywhere}
.arch-desc-h{margin:28px 0 10px;font-size:19px}
.arch-desc{white-space:pre-wrap;font-size:14.5px;line-height:1.65;max-width:72ch}
.arch-desc-sections{max-width:72ch;font-size:14.5px;line-height:1.65}
.arch-sec{font-family:var(--font-body);font-weight:600;font-size:14.5px;margin:18px 0 6px}
.arch-sec:first-child{margin-top:0}
.arch-list{margin:0;padding-left:20px}
.arch-list li+li{margin-top:3px}
.arch-para{margin:0}
/* the brief */
.actions{display:flex;flex-direction:column;gap:12px}
.action{padding:16px 18px}
.action-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.action-title{font-size:19px;line-height:1.25}
.action-why{margin:8px 0 0;font-size:14.5px;line-height:1.6;color:var(--color-text-2)}
.action-quote{margin:14px 0 0;padding:2px 0 2px 14px;border-left:3px solid var(--color-accent-200);font-size:14px;line-height:1.6}
.action-quote q{font-style:italic;quotes:"\\201C" "\\201D"}
.action-quote span{display:block;font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--color-text-3);margin-bottom:2px}
.action-steps{list-style:none;counter-reset:step;margin:16px 0 0;padding:0;font-size:14.5px;line-height:1.6}
.action-steps li{counter-increment:step;position:relative;padding-left:34px}
.action-steps li+li{margin-top:10px}
.action-steps li::before{content:counter(step);position:absolute;left:0;top:1px;display:inline-flex;align-items:center;justify-content:center;
  width:22px;height:22px;border-radius:50%;background:var(--color-accent-100);color:var(--color-accent-800);font-size:12px;font-weight:700}
.sources{padding:6px 22px}
.src{display:flex;gap:12px;padding:10px 0;border-top:1px solid var(--color-border);font-size:13.5px;line-height:1.5;scroll-margin-top:24px;
  transition:background .3s}
.src:first-child{border-top:0}
.src>.cite{flex:none;display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:20px;margin-top:1px;border-radius:999px;
  background:var(--color-accent-100);color:var(--color-accent-800);font-size:11px;font-weight:700;font-variant-numeric:tabular-nums}
.src:target{background:var(--color-accent-100);box-shadow:0 0 0 8px var(--color-accent-100);border-radius:4px}
.src a{color:var(--color-accent-800);text-decoration:none}
.src a:hover{color:var(--color-accent);text-decoration:underline}
.rail{position:sticky;top:20px}
.rail-label{display:block;margin-bottom:10px;font-size:13px;font-weight:600;color:var(--color-text-2)}
.mini-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.mini-stats div{padding:10px 12px;border-radius:var(--radius-sm);background:var(--color-surface-2);border:1px solid var(--color-border)}
.mini-stats b{display:block;font-size:22px;font-weight:600;line-height:1.1}
.mini-stats span{display:block;margin-top:2px;font-size:12px;color:var(--color-text-3)}
.archive-bar{display:flex;align-items:center;gap:8px 14px;flex-wrap:wrap;margin:0 0 16px;padding:12px 16px;border-radius:var(--radius);
  background:var(--color-surface);border:1px solid var(--color-border);font-size:13.5px}
.brief-list{display:flex;flex-direction:column;gap:12px;margin:0;padding:0;list-style:none}
.brief-item{display:block;padding:16px 18px;transition:box-shadow .15s,border-color .15s}
.brief-item:hover{border-color:var(--color-border-strong);box-shadow:var(--shadow-md)}
.brief-item-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.brief-item-head a{font-family:var(--font-heading);font-weight:600;font-size:20px;color:var(--color-text);text-decoration:none}
.brief-item-head a::after{content:"";position:absolute;inset:0;border-radius:var(--radius)}
.brief-item-head a:hover{color:var(--color-accent)}
.brief-item p{margin:8px 0 0;font-size:14.5px;line-height:1.6;color:var(--color-text-2)}
.brief-item .brief-item-meta{margin-top:10px;font-size:12.5px;color:var(--color-text-3)}
.empty-state{max-width:640px;margin:16px auto 0;padding:36px 32px;text-align:center}
.empty-state h1{font-size:32px;margin:8px 0 0}
.empty-state p{margin:12px auto 0;max-width:56ch;font-size:15px;line-height:1.65;color:var(--color-text-2)}
.empty-state .btn{margin-top:22px}
.site-foot{border-top:1px solid var(--color-border);margin-top:40px;padding-top:16px;
  display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;font-size:12.5px;line-height:1.5;color:var(--color-text-3)}
.site-foot a{color:inherit}
@media (max-width:980px){
  .grid,.grid.narrative{grid-template-columns:1fr}.rail{position:static}
  .stack-card{grid-auto-flow:row;grid-template-columns:1fr!important}
  .stack-col+.stack-col{border-left:0;border-top:1px solid var(--color-border)}
  .stack-list.wide{columns:1}
}
@media (max-width:620px){
  .wrap{padding:0 16px 44px}
  .nav{gap:10px 16px;padding:12px 20px}
  .nav-tabs{order:3;flex:1 0 100%;flex-wrap:nowrap;overflow-x:auto;gap:22px;scrollbar-width:none}
  .nav-tabs a{font-size:16px;white-space:nowrap}
  .nav-auth{gap:14px}
  /* the avatar alone: the name stays for screen readers, as .visually-hidden does */
  .nav-who{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
  .tbl-controls,.tbl-controls input[type=search]{width:100%}
  .my-terms-form{flex:1 0 100%}.my-terms-form input{flex:1;width:auto}
  .hdr h1{font-size:28px!important}
  .kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
  .kpi:nth-child(odd){border-left:0}.kpi:nth-child(n+3){border-top:1px solid var(--color-border)}
  .kpi-value{font-size:24px}
  .card,.action,.stack-col{padding:14px}
  .paper{padding:18px 16px}
  .ch-frame{padding-right:24px}
  .ch-plot{height:170px}
  .ch-xlab.alt{display:none}
  .table th,.table td{padding-left:12px;padding-right:12px}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;
    transition-duration:.01ms!important;scroll-behavior:auto!important}
}
@media print{
  .nav,.skip-link,.crumb,.btn,.tbl-controls,.tbl-filters,.my-terms,.pager,.chart-tbl,.arch-dialog{display:none!important}
  body{background:#fff;font-size:11.5pt}
  .wrap{max-width:none;padding:0}
  .grid,.grid.narrative{grid-template-columns:1fr;gap:14px}
  .blueprint{border-color:#bbb;box-shadow:none}
  .rail{position:static}
  .prose p{max-width:none}
  a{color:inherit;text-decoration:none}
  .card,.blueprint{break-inside:avoid}
  tr,.site-foot{break-inside:avoid}
}
"""


# TICK mark, the same step line as the menu bar's brand: navy on a light tab
# strip, pale blue on a dark one. Identical to the icon on every page in
# infra/site-static/ — change both. Inlined as a data URI: one fewer request,
# and no 404 when the site is served from a subdirectory.
_FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Cstyle%3E.p%7Bstroke:%23456e99%7D@media(prefers-color-scheme:dark)%7B.p%7Bstroke:%2386aace%7D%7D%3C/style%3E"
    "%3Cpath class='p' d='M2 19h6.5l4-12 4.5 17 3.5-10H30' fill='none' stroke-width='3'"
    " stroke-linejoin='miter' stroke-linecap='square'/%3E%3C/svg%3E"
)


def _head(title: str, description: str) -> str:
    """The shared <head>. Fonts move from an @import inside the stylesheet to
    link tags: an @import cannot start downloading until the stylesheet that
    contains it has arrived, which put the webfonts a full round trip behind."""
    t = html.escape(title)
    d = html.escape(description)
    return f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t}</title>
<meta name="description" content="{d}">
<meta name="color-scheme" content="light">
<meta name="theme-color" content="#080d12">
<meta name="robots" content="noindex, nofollow">
<meta property="og:type" content="website">
<meta property="og:site_name" content="TICK">
<meta property="og:title" content="{t}">
<meta property="og:description" content="{d}">
<meta name="twitter:card" content="summary">
<link rel="icon" href="{_FAVICON}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;700&amp;family=Barlow+Condensed:wght@400;600&amp;display=swap">
<style>{_STYLE}</style>"""


def _footer(generated: str, source: str | None = None) -> str:
    """Every page says when it was built and from what. A brief that names a
    person on 62% confidence should never be read without its date on it."""
    src = (f' &middot; source <a href="https://{html.escape(source)}" target="_blank" '
           f'rel="noopener">{html.escape(source)}</a>') if source else ""
    return (f'<footer class="site-foot text-muted">'
            f'<span>Generated {html.escape(generated)} by TICK{src}</span>'
            f'<span>Internal use &mdash; derived from public job postings. '
            f'Verify before acting.</span></footer>')


def _chart_scale(pts: list[dict]) -> tuple[int, int]:
    """The y-range, rounded out to multiples of 5. Deliberately NOT pinned to
    zero: these are board sizes in the 30s, and a zero floor would flatten a
    year of movement into a hairline. The floor is a labelled tick, so the
    truncation is stated on the axis rather than hidden."""
    totals = [p["total"] for p in pts]
    lo = (min(totals) // 5) * 5
    hi = -(-max(totals) // 5) * 5
    return lo, (hi if hi != lo else lo + 5)


def _history_chart(history: list[dict], chart_id: str = "roles-history") -> str:
    """Open roles over the trailing year — one series, so no legend box: the
    card's own heading names what is plotted.

    The plot stretches to its card (preserveAspectRatio="none", in a 0-100
    viewBox, so every coordinate is a percentage), its strokes keep their
    width (vector-effect), and the tick labels and dots are HTML laid over it
    by the same percentages. A fixed viewBox scaled its text with the card:
    readable in a 400px rail, 5px on a phone."""
    pts = [p for p in history if p.get("total") is not None]
    if len(pts) < 2:
        return _trend_svg(history)   # not enough history to plot a year

    totals = [p["total"] for p in pts]
    lo, hi = _chart_scale(pts)
    x = lambda i: 100 * i / (len(pts) - 1)
    y = lambda v: 100 - 100 * (v - lo) / (hi - lo)

    ticks = list(range(lo, hi + 1, 5))
    if len(ticks) > 6:                    # keep the grid recessive, not a ladder
        ticks = ticks[::2] if ticks[-1] in ticks[::2] else ticks[::2] + [ticks[-1]]
    grid = "".join(f'<line x1="0" y1="{y(t):.2f}" x2="100" y2="{y(t):.2f}" '
                   f'class="{"ch-base" if t == lo else "ch-grid"}"/>' for t in ticks)
    ylab = "".join(f'<span class="ch-ylab" style="top:{y(t):.2f}%">{t}</span>' for t in ticks)

    # Aim for ~7 ticks whatever the crawl cadence. A fixed every-4th-point step
    # is two months apart on a fortnightly lake but only four weeks on a weekly
    # one, which printed "Oct" twice on a 53-point axis.
    step = max(1, round(len(pts) / 7))
    idxs = [i for i in range(0, len(pts), step) if i <= len(pts) - 3]
    xlab, shown_year, last_month, n_lab = "", None, None, 0
    for i in idxs:
        d = date.fromisoformat(pts[i]["dt"])
        if (d.year, d.month) == last_month:      # never print the same month twice
            continue
        last_month = (d.year, d.month)
        # year-stamp the first tick and the first tick of each new year, so the
        # axis says where 2026 starts even when no tick lands in January
        lab = d.strftime("%b") + (f" &rsquo;{d:%y}" if shown_year != d.year else "")
        shown_year = d.year
        # every other label is .alt, which a phone drops: seven months do not fit in 320px
        cls = ("ch-xlab" + (" first" if i == 0 else "") + (" alt" if n_lab % 2 else ""))
        n_lab += 1
        xlab += f'<span class="{cls}" style="left:{x(i):.2f}%">{lab}</span>'

    line = " ".join(f"{x(i):.2f},{y(p['total']):.2f}" for i, p in enumerate(pts))
    area = "M0,100 L" + line.replace(" ", " L") + " L100,100 Z"
    last = pts[-1]
    end = f'left:100%;top:{y(last["total"]):.2f}%'

    rows = "".join(f'<tr><td>{p["dt"]}</td><td class="num">{p["total"]}</td></tr>' for p in pts)
    span_lbl = f'{pts[0]["dt"]} to {last["dt"]}'

    return f"""<div class="chart-wrap" id="{chart_id}">
<div class="ch-frame"><div class="ch-plot">
<svg viewBox="0 0 100 100" preserveAspectRatio="none" class="ch-svg" role="img"
     aria-label="Open roles from {pts[0]['dt']} to {last['dt']}, {len(pts)} snapshots, ranging {min(totals)} to {max(totals)}.">
  <g>{grid}</g>
  <path d="{area}" class="ch-area"/>
  <polyline points="{line}" class="ch-line"/>
  <line class="ch-hair" x1="0" y1="0" x2="0" y2="100" style="display:none"/>
  <rect x="0" y="0" width="100" height="100" fill="transparent" class="ch-hit"
        tabindex="0" role="img" aria-label="Open-roles history, interactive. Use the left and right
        arrow keys to step through snapshots. Every figure is also in the table below."/>
</svg>
{ylab}{xlab}
<span class="ch-dot" style="{end}" aria-hidden="true"></span>
<span class="ch-endlab" style="{end}" aria-hidden="true">{last["total"]}</span>
<span class="ch-dot ch-hot" hidden></span>
<div class="chart-tip" hidden></div>
</div></div>
<p class="visually-hidden" id="{chart_id}-readout" role="status" aria-live="polite"></p>
<details class="chart-tbl"><summary>Table view &mdash; {len(pts)} snapshots, {span_lbl}</summary>
<table><thead><tr><th>Crawl</th><th class="num">Open roles</th></tr></thead><tbody>{rows}</tbody></table>
</details>
</div>"""


def _history_chart_script(history: list[dict], chart_id: str = "roles-history") -> str:
    """Crosshair + tooltip for the open-roles chart. The pointer only has to be
    nearest in x — nobody can hit a 2px line — and arrow keys drive the same
    readout so the values are reachable without a mouse."""
    pts = [p for p in history if p.get("total") is not None]
    if len(pts) < 2:
        return ""
    lo, hi = _chart_scale(pts)
    return f"""<script>
(function () {{
  var DATA = {json.dumps(pts)};
  var root = document.getElementById({json.dumps(chart_id)});
  if (!root) return;
  var plot = root.querySelector('.ch-plot'), svg = root.querySelector('.ch-svg'),
      tip = root.querySelector('.chart-tip'), hair = root.querySelector('.ch-hair'),
      hot = root.querySelector('.ch-hot'), hit = root.querySelector('.ch-hit'),
      say = document.getElementById("{chart_id}-readout");
  var lo = {lo}, hi = {hi};
  var i = -1;
  function vx(k) {{ return 100 * k / (DATA.length - 1); }}
  function vy(v) {{ return 100 - 100 * (v - lo) / (hi - lo || 1); }}
  function fmt(iso) {{
    var d = new Date(iso + 'T00:00:00');
    return d.toLocaleDateString(undefined, {{ day: 'numeric', month: 'short', year: 'numeric' }});
  }}
  function show(k) {{
    if (k < 0 || k >= DATA.length) return;
    i = k;
    var px = vx(k), py = vy(DATA[k].total);
    hair.setAttribute('x1', px); hair.setAttribute('x2', px); hair.style.display = '';
    hot.style.left = px + '%'; hot.style.top = py + '%'; hot.hidden = false;
    tip.textContent = '';
    var v = document.createElement('b'); v.textContent = DATA[k].total + ' open';
    var s = document.createElement('span'); s.textContent = fmt(DATA[k].dt);
    tip.appendChild(v); tip.appendChild(s);
    tip.hidden = false;
    // the crosshair is a sighted affordance; mirror it into a live region so the
    // same value is spoken when the chart is stepped through from the keyboard
    if (say) {{ say.textContent = DATA[k].total + ' open roles on ' + fmt(DATA[k].dt); }}
    // above the point, clamped inside the plot so the readout never hangs off the card
    var w = plot.clientWidth, h = plot.clientHeight;
    tip.style.left = Math.max(0, Math.min(w - tip.offsetWidth, px / 100 * w - tip.offsetWidth / 2)) + 'px';
    var top = py / 100 * h - tip.offsetHeight - 14;
    tip.style.top = (top < 0 ? py / 100 * h + 14 : top) + 'px';
  }}
  function hide() {{
    tip.hidden = true; hair.style.display = 'none'; hot.hidden = true; i = -1;
    if (say) {{ say.textContent = ''; }}
  }}
  function nearest(evt) {{
    var r = svg.getBoundingClientRect();
    return Math.max(0, Math.min(DATA.length - 1,
      Math.round((evt.clientX - r.left) / r.width * (DATA.length - 1))));
  }}
  svg.addEventListener('pointermove', function (e) {{ show(nearest(e)); }});
  svg.addEventListener('pointerleave', hide);
  hit.addEventListener('focus', function () {{ show(DATA.length - 1); }});
  hit.addEventListener('blur', hide);
  hit.addEventListener('keydown', function (e) {{
    if (e.key === 'ArrowRight') {{ show(Math.min(DATA.length - 1, i + 1)); e.preventDefault(); }}
    if (e.key === 'ArrowLeft')  {{ show(Math.max(0, i - 1)); e.preventDefault(); }}
    if (e.key === 'Escape') {{ hide(); }}
  }});
}})();
</script>"""


def _trend_svg(series: list[dict]) -> str:
    """The open-roles line as a sparkline, for the brief's rail card.

    Two earlier faults: the series handed in was only the comparison window, so
    a year of movement rendered as a single straight diagonal between two dots;
    and preserveAspectRatio="none" stretched the geometry, which turned the end
    markers into ellipses and clipped the ones sitting on x=0. Now it scales
    uniformly, insets the markers by their own radius, and plots whatever span
    the caller passes."""
    pts = [p for p in series if p.get("total") is not None]
    if not pts:
        return ""
    totals = [p["total"] for p in pts]
    lo, hi = min(totals), max(totals)
    span = (hi - lo) or 1
    n = len(totals)
    # inset so a marker on the first or last point is not half outside the box
    X0, X1, Y0, Y1 = 5.0, 235.0, 9.0, 51.0
    y = lambda t: Y1 - (Y1 - Y0) * (t - lo) / span
    if n == 1:
        pts_xy = [(X0, y(totals[0])), (X1, y(totals[0]))]
    else:
        pts_xy = [(X0 + i * (X1 - X0) / (n - 1), y(t)) for i, t in enumerate(totals)]
    line = " ".join(f"{x:.1f},{v:.1f}" for x, v in pts_xy)
    label = (f"Open roles from {pts[0]['dt']} to {pts[-1]['dt']}, "
             f"{n} snapshots, ranging {lo} to {hi}.")
    area = f"M{pts_xy[0][0]:.1f},56 L" + line.replace(" ", " L") + f" L{pts_xy[-1][0]:.1f},56 Z"
    return (f'<svg viewBox="0 0 240 60" class="spark" role="img" '
            f'aria-label="{html.escape(label)}">'
            f'<line x1="0" y1="56" x2="240" y2="56" stroke="var(--color-border-strong)"/>'
            f'<path d="{area}" fill="var(--color-accent)" opacity=".1"/>'
            f'<polyline points="{line}" fill="none" stroke="var(--color-accent)" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{pts_xy[-1][0]:.1f}" cy="{pts_xy[-1][1]:.1f}" r="4" '
            f'fill="var(--color-accent)" stroke="var(--color-surface)" stroke-width="2"/>'
            f'</svg>')


def _signed(n: int) -> str:
    """A change with its sign, minus as a real minus sign: "+5", "−11", "0"."""
    return f"+{n}" if n > 0 else f"&minus;{-n}" if n < 0 else "0"


def _delta(n: int, pct: float | None = None) -> str:
    """A change as a pill. Up and down are not good and bad here (a board can
    grow for reasons a seller does not care about), so only up takes the accent
    and the number carries its own sign."""
    txt = _signed(n)
    if pct is not None:
        txt += " &middot; " + ("+" if pct > 0 else "&minus;" if pct < 0 else "") + f"{abs(pct):.1f}%"
    return f'<span class="delta{" up" if n > 0 else ""}">{txt}</span>'


def _kpi(label: str, value, sub: str = "") -> str:
    """One stat tile: label, value, and an optional line under it."""
    return (f'<div class="kpi"><span class="kpi-label">{label}</span>'
            f'<span class="kpi-value">{value}</span>'
            f'{f"<span class=\"kpi-sub\">{sub}</span>" if sub else ""}</div>')


def _short_date(iso: str | None) -> str:
    return f"{date.fromisoformat(iso):%-d %b}" if iso else ""


_NAV_MARK = ('<svg viewBox="0 0 32 32" aria-hidden="true" focusable="false">'
             '<path d="M2 19h6.5l4-12 4.5 17 3.5-10H30" fill="none" stroke="currentColor" '
             'stroke-width="3" stroke-linejoin="miter" stroke-linecap="square"/></svg>')


def _icon(paths: str) -> str:
    """A 24px line icon for the account menu; its stroke is set in _STYLE."""
    return f'<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">{paths}</svg>'


_ICON_PERSON = _icon('<circle cx="12" cy="8.5" r="3.5"/><path d="M5 20c.9-3.6 3.6-5.5 7-5.5s6.1 1.9 7 5.5"/>')
_ICON_EDIT = _icon('<circle cx="9" cy="7.5" r="3.5"/><path d="M2.5 19.5c.7-3.4 3.2-5.3 6.5-5.3 1.2 0 2.3.3 3.2.8"/>'
                   '<path d="M14.5 20.5l.6-2.7 5-5a1.5 1.5 0 0 1 2.1 2.1l-5 5z"/>')
_ICON_SIGN_OUT = _icon('<path d="M10 4H5.5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1H10"/><path d="M15 8l4 4-4 4"/><path d="M19 12H9.5"/>')
_ICON_CARET = ('<svg class="nav-caret" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
               '<path d="M6 9l6 6 6-6"/></svg>')


def _account_menu(active: str) -> str:
    """The signed-in corner: who is signed in, Edit profile and Sign out. A
    <details> disclosure, so it opens with no script at all. Every app page
    is static and shared by everyone with the same scope, so the viewer's
    name cannot be in the HTML; _ACCOUNT_JS fills it in."""
    cur = ' aria-current="page"' if active == "profile" else ""
    return (f'<details class="nav-menu" id="nav-menu">'
            f'<summary><span class="nav-avatar" id="nav-avatar" aria-hidden="true">{_ICON_PERSON}</span>'
            f'<span class="nav-who"><span id="nav-who">Account</span>'
            f'<span class="visually-hidden"> menu</span></span>{_ICON_CARET}</summary>'
            f'<div class="nav-menu-panel">'
            f'<p class="nav-menu-email" id="nav-email" hidden></p>'
            f'<a href="{PROFILE_HOME}"{cur}>{_ICON_EDIT}<span>Edit profile</span></a>'
            f'<a href="/auth/logout" id="nav-signout">{_ICON_SIGN_OUT}<span>Sign out</span></a>'
            f'</div></details>')


# Same-origin calls to the join Lambda (/api/*). CloudFront signs a request to
# the function URL with OAC, which for a body needs the payload's SHA-256 from
# the browser, in x-amz-content-sha256. Resolves with the JSON reply; rejects
# with .status and .problems.
_API_FN = """
function tickApi(method, url, payload) {
  var body = payload === undefined ? null : JSON.stringify(payload);
  var hashed = body === null ? Promise.resolve(null)
    : crypto.subtle.digest('SHA-256', new TextEncoder().encode(body)).then(function (buf) {
        return Array.prototype.map.call(new Uint8Array(buf), function (b) {
          return ('0' + b.toString(16)).slice(-2);
        }).join('');
      });
  return hashed.then(function (hash) {
    var headers = {};
    if (body !== null) { headers['content-type'] = 'application/json'; headers['x-amz-content-sha256'] = hash; }
    return fetch(url, {method: method, credentials: 'same-origin', headers: headers, body: body});
  }).then(function (res) {
    return res.json().catch(function () { return {}; }).then(function (data) {
      if (res.ok) return data;
      var err = new Error(data.error || 'HTTP ' + res.status);
      err.status = res.status;
      err.problems = data.problems || [];
      throw err;
    });
  });
}
"""

# Runs right after the menu bar of every signed-in page, so tickApi() and
# tickProfile() exist before any later script calls them. tickProfile() is the
# viewer's GET /api/profile, made at most once per page however many scripts
# ask (the menu, My terms, the profile page). The menu's name is kept for the
# tab, so only the first page of a visit waits on the API to show it.
_ACCOUNT_JS = r"""
(function () {
  var KEY = 'tick-who';
  var menu = document.getElementById('nav-menu'), who = document.getElementById('nav-who');
  var avatar = document.getElementById('nav-avatar'), email = document.getElementById('nav-email');
  function remember(address) {
    try { if (address) sessionStorage.setItem(KEY, address); else sessionStorage.removeItem(KEY); } catch (e) {}
  }
  function show(address) {
    if (!address || !menu) return;
    var name = address.split('@')[0];
    who.textContent = name;
    avatar.textContent = name.charAt(0).toUpperCase();
    email.textContent = address;
    email.hidden = false;
  }
  var asked = null;
  window.tickProfile = function () {
    if (!asked) {
      asked = tickApi('GET', '/api/profile');
      asked.then(function (data) { remember(data.email || ''); show(data.email || ''); },
                 function (err) { if (err.status === 401) remember(''); });
    }
    return asked;
  };
  var known = null;
  try { known = sessionStorage.getItem(KEY); } catch (e) {}
  if (known) show(known); else tickProfile().catch(function () {});
  if (!menu) return;

  document.getElementById('nav-signout').addEventListener('click', function () { remember(''); });
  // <details> opens and closes itself on click, Enter and Space; it does not
  // close on Escape, on a click elsewhere, or when focus moves on
  function close() { menu.open = false; }
  document.addEventListener('click', function (e) { if (menu.open && !menu.contains(e.target)) close(); });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || !menu.open) return;
    var inside = menu.contains(document.activeElement);
    close();
    if (inside) menu.querySelector('summary').focus();
  });
  menu.addEventListener('focusout', function (e) {
    if (menu.open && e.relatedTarget && !menu.contains(e.relatedTarget)) close();
  });
})();
"""


def _nav(active: str, account: str = "", signed_in: bool = True) -> str:
    """The menu bar of infra/site-static/demo/, with the app's links: account
    pages get that account's Dashboard and Brief, and the right side holds the
    account menu where the demo says Log in / Get started. The brand goes to
    the accounts home, not to "/": a signed-in viewer has no business on the
    marketing page. Links are root-absolute, so they resolve the same from
    /<slug>/brief.html and an archive page. The 404 page cannot know whether
    its reader is signed in, so it offers Log in and a brand link to "/",
    which the edge forwards to /accounts for anyone with a session."""
    def tab(label, href, key):
        cur = ' aria-current="page"' if key == active else ""
        return f'<a href="{href}"{cur}>{label}</a>'
    tabs = [tab("Accounts", APP_HOME, "home")]
    if account:
        tabs += [tab("Dashboard", f"/{account}/dashboard.html", "dashboard"),
                 tab("Brief", f"/{account}/brief.html", "brief")]
    tabs.append(tab("Docs", "/docs", "docs"))
    auth = _account_menu(active) if signed_in else '<a class="nav-login" href="/login">Log in</a>'
    brand = APP_HOME if signed_in else "/"
    script = f"\n<script>{_API_FN}{_ACCOUNT_JS}</script>" if signed_in else ""
    return (f'<div class="nav"><a class="nav-brand" href="{brand}" style="text-decoration:none">'
            f'{_NAV_MARK}<span>TICK</span></a><div class="nav-tabs">{"".join(tabs)}</div>\n'
            f'  <div class="nav-auth">\n    {auth}\n  </div>\n</div>{script}')


# Shared by the brief/dashboard copy buttons and the home page's command copier.
# navigator.clipboard is undefined on file:// and plain http, and its promise can
# reject even on https when permission is refused; the old code had no failure
# path at all, so the button silently did nothing. The label is cached on the
# element so a second click inside the reset window cannot latch it on "Copied".
_COPY_FN = """
function tickCopy(text, btn) {
  function flash(msg) {
    if (!btn.dataset.label) { btn.dataset.label = btn.textContent; }
    btn.textContent = msg;
    clearTimeout(btn._resetTimer);
    btn._resetTimer = setTimeout(function () { btn.textContent = btn.dataset.label; }, 1600);
  }
  function legacy() {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:0;left:-9999px';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    flash(ok ? 'Copied' : 'Copy failed \u2014 select and copy manually');
  }
  if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function () { flash('Copied'); }, legacy);
  } else {
    legacy();
  }
}
"""


def _copy_script(element_id: str, text: str) -> str:
    return f"""<script>
(function () {{{_COPY_FN}
  var btn = document.getElementById('{element_id}');
  if (!btn) return;
  btn.addEventListener('click', function () {{ tickCopy({json.dumps(text)}, btn); }});
}})();
</script>"""


ROLES_PAGE_SIZE = 10


# The dashboard's two tables, by the ids of their controls. `prefix` keeps each
# table's keys apart in the URL fragment; `text` names the JSON holding each
# open role's description (the archive's are already in its detail templates);
# `terms` marks the table that stack-term and My terms clicks filter.
_ROLE_TABLE = {"table": "open-roles", "prefix": "", "search": "role-filter", "flags": "role-flags",
               "count": "row-count", "total": "row-total", "empty": "no-match", "head": "open-roles-head",
               "pager": "roles-pager", "prev": "page-prev", "next": "page-next", "status": "page-status",
               "text": "open-roles-text", "terms": True}
_ARCHIVE_TABLE = {"table": "archive-roles", "prefix": "a", "search": "archive-filter",
                  "flags": "archive-flags", "count": "archive-count", "total": "archive-total",
                  "empty": "archive-no-match", "head": "archive", "pager": "archive-pager",
                  "prev": "archive-prev", "next": "archive-next", "status": "archive-status",
                  "text": None, "terms": False}

# localStorage key for My terms: one list for every dashboard on the site.
_MY_TERMS_KEY = "tick-my-terms"
# My terms are the persona's focus terms, so they share its limits
_MY_TERMS_MAX, _MY_TERM_CHARS = persona.MAX_FOCUS, persona.TERM_CHARS


def _row_attrs(attrs: dict) -> str:
    """A table row's data attributes: what the table script filters and sorts
    on. A sort key is blank where the row has no value, so it sinks either way."""
    return " ".join(f'{k}="{html.escape(str(v), quote=True)}"' for k, v in attrs.items())


def _json_for_script(value) -> str:
    """JSON that is safe inside a <script> element: '<', '>' and '&' become
    unicode escapes, so no posting's text can close the element early."""
    return (json.dumps(value, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _flag_box(flag: str, label: str, count: int = 0, hidden: bool = False) -> str:
    """One filter checkbox. A row passes when its data-<flag> is "1"."""
    return (f'<label class="chk"{" hidden" if hidden else ""}><input type="checkbox" data-flag="{flag}">'
            f'<span>{label}</span><span class="chk-n" data-count>{count}</span></label>')


def _filter_row(cfg: dict, boxes: list[str], label: str) -> str:
    """The filters under a table's heading: its checkboxes, the active term
    filter (if any) and a way to clear them all."""
    return (f'<div class="tbl-filters" id="{cfg["flags"]}" role="group" aria-label="{label}">'
            f'{"".join(boxes)}'
            f'<span class="filter-chip" data-active hidden></span>'
            f'<button type="button" class="btn-link" data-clear="{cfg["table"]}" hidden>Clear filters</button>'
            f'</div>')


def _my_terms_strip() -> str:
    """The My terms editor, above Open roles. Hidden until the script runs: it
    needs both JS and browser storage, and offers nothing without them."""
    return (f'<div class="my-terms" id="my-terms" hidden>'
            f'<span class="my-terms-label">My terms</span>'
            f'<span class="my-terms-list" id="my-terms-list"></span>'
            f'<form class="my-terms-form" id="my-terms-form">'
            f'<input id="my-terms-input" type="text" maxlength="300" autocomplete="off" '
            f'placeholder="Add terms, e.g. kubernetes, sbom" aria-label="Add to My terms">'
            f'<button type="submit" class="btn btn-secondary btn-sm">Add</button></form>'
            f'<span class="text-muted" id="my-terms-note">Kept in this browser. '
            f'Every dashboard tags the roles whose title or description mentions one.</span></div>')


# Search, sort, filters and pages for the dashboard's tables, plus My terms.
#
# Every row ships in the HTML and this only reorders and hides them, so with JS
# off the whole table is still on the page. Filters narrow, sorting orders what
# is left, and paging slices that, so the page numbers always describe what the
# reader is looking at. The view lives in the URL fragment (#q=sbom&sort=pay-desc),
# so a bookmark or a pasted link opens the same view; it is written only when
# the reader changes something, so arriving at #archive still scrolls there.
#
# A search matches anywhere in the title, location or description. My terms and
# term filters match whole words, with the collector's boundaries (_term_pattern),
# so "java" never matches "javascript". A stack term clicked on the page filters
# on the terms the collector matched, so the count on its card and the rows
# shown always agree.
_TABLES_JS = r"""<script>
(function () {
  var PAGE = %PAGE%, STORE = %STORE%, MAX_TERMS = %MAX_TERMS%, TERM_CHARS = %TERM_CHARS%;
  var CONFIG = %CONFIG%;
  // the view in the fragment; a plain anchor such as #archive carries none
  function readParams() {
    try { return new URLSearchParams(location.hash.indexOf('=') > 0 ? location.hash.slice(1) : ''); }
    catch (e) { return new URLSearchParams(''); }
  }
  var params = readParams();

  function writeHash() {
    var s = params.toString();
    try { history.replaceState(null, '', location.pathname + location.search + (s ? '#' + s : '')); }
    catch (e) {}
  }
  function setParam(k, v) { if (v) params.set(k, v); else params.delete(k); }
  function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
  function wordRe(term) {
    return new RegExp('(?:^|[^\\w#./-])' + escapeRe(term) + '(?![\\w#-])', 'i');
  }
  function same(a, b) { return a.toLowerCase() === b.toLowerCase(); }

  // My terms are the focus terms of the viewer's persona, saved through
  // /api/profile so they follow the viewer and reach their weekly briefs.
  // Until the profile answers, and wherever it cannot (signed out, a local
  // preview without the API), they live in this browser instead.
  function loadMine() {
    try {
      var v = JSON.parse(localStorage.getItem(STORE) || '[]');
      return Array.isArray(v) ? v.filter(function (t) { return typeof t === 'string' && t.trim(); })
                                 .slice(0, MAX_TERMS) : [];
    } catch (e) { return []; }
  }
  function forgetLocal() { try { localStorage.removeItem(STORE); } catch (e) {} }
  var mine = loadMine(), synced = false;
  var GOOD_TERM = /^[\w .+#\/&'-]+$/;   // persona.py's _TERM_RE: what a saved term may hold

  // an archived req's description, from the template its detail dialog clones
  function templateText(r) {
    var tpl = document.getElementById(r.getAttribute('data-arch') || '');
    var d = tpl && tpl.content.querySelector('.arch-desc-sections, .arch-desc');
    if (!d) return '';
    var parts = d.querySelectorAll('p, li');
    var text = parts.length ? [].map.call(parts, function (n) { return n.textContent; }).join(' ')
                            : d.textContent;
    return text.replace(/\s+/g, ' ').trim();
  }

  function table(cfg) {
    var el = document.getElementById(cfg.table);
    if (!el || !el.tBodies[0]) return null;
    var tbody = el.tBodies[0];
    var rows = [].slice.call(tbody.rows).filter(function (r) { return r.hasAttribute('data-q'); });
    var texts = [];
    if (cfg.text) {
      try { texts = JSON.parse(document.getElementById(cfg.text).textContent); } catch (e) {}
    }
    rows.forEach(function (r, i) {
      r._i = i;
      r._text = cfg.text ? (texts[i] || '') : templateText(r);
      r._low = r._text.toLowerCase();
      r._q = r.getAttribute('data-q') || '';
      r._title = r.getAttribute('data-s-title') || '';
      r._mine = [];
    });

    var P = cfg.prefix;
    var heads = [].slice.call(el.tHead ? el.tHead.rows[0].cells : [])
      .filter(function (th) { return th.hasAttribute('data-key'); });
    var keys = heads.map(function (th) { return th.getAttribute('data-key'); });
    var search = document.getElementById(cfg.search);
    var box = document.getElementById(cfg.flags);
    var checks = box ? [].slice.call(box.querySelectorAll('input[data-flag]')) : [];
    var chip = box && box.querySelector('[data-active]');
    var clears = [].slice.call(document.querySelectorAll('[data-clear="' + cfg.table + '"]'));
    var count = document.getElementById(cfg.count), total = document.getElementById(cfg.total);
    var empty = document.getElementById(cfg.empty), pager = document.getElementById(cfg.pager);
    var prev = document.getElementById(cfg.prev), next = document.getElementById(cfg.next);
    var status = document.getElementById(cfg.status);
    var matched = rows.slice(), page = 1;
    var mineBox = checks.filter(function (c) { return c.getAttribute('data-flag') === 'mine'; })[0];

    var known = checks.map(function (c) { return c.getAttribute('data-flag'); });
    var state = {};

    // this table's view from the fragment; anything it does not recognise is dropped
    function load() {
      var sort = params.get(P + 'sort') || '';
      state.q = params.get(P + 'q') || '';
      state.sort = keys.indexOf(sort.split('-')[0]) === -1 ? '' : sort;
      state.flags = (params.get(P + 'f') || '').split(',')
        .filter(function (f) { return known.indexOf(f) !== -1; });
      state.stack = cfg.terms ? params.get(P + 'stack') || '' : '';
      state.term = cfg.terms ? params.get(P + 'term') || '' : '';
      if (search) search.value = state.q;
      checks.forEach(function (c) { c.checked = state.flags.indexOf(c.getAttribute('data-flag')) !== -1; });
    }
    load();

    heads.forEach(function (th) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'th-sort';
      while (th.firstChild) b.appendChild(th.firstChild);
      th.appendChild(b);
      b.addEventListener('click', function () {
        var key = th.getAttribute('data-key'), first = th.getAttribute('data-first') || 'asc';
        var cur = state.sort.split('-');
        // a column's natural direction first, then reversed, then the board's own order
        state.sort = cur[0] !== key ? key + '-' + first
                   : cur[1] === first ? key + '-' + (first === 'asc' ? 'desc' : 'asc') : '';
        apply(true);
      });
    });

    function sorted() {
      if (!state.sort) return rows.slice();
      var key = state.sort.split('-')[0], desc = state.sort.split('-')[1] === 'desc';
      var num = heads[keys.indexOf(key)].getAttribute('data-type') === 'num';
      var attr = 'data-s-' + key;
      return rows.slice().sort(function (a, b) {
        var x = a.getAttribute(attr) || '', y = b.getAttribute(attr) || '';
        // a row without the value (no pay posted, no date) sinks either way
        if (!x || !y) return (!x && !y) ? a._i - b._i : !x ? 1 : -1;
        var c = num ? (+x) - (+y) : x.localeCompare(y);
        return (desc ? -c : c) || a._i - b._i;
      });
    }

    // where a search matched inside the description, show the words around it
    function snippet(r, needle) {
      if (r._hit) { r._hit.parentNode.removeChild(r._hit); r._hit = null; }
      var at = needle ? r._low.indexOf(needle) : -1;
      if (at === -1 || r._q.indexOf(needle) !== -1) return;
      var end = at + needle.length;
      var from = Math.max(0, at - 70), to = Math.min(r._text.length, end + 70);
      var sp = r._text.indexOf(' ', from);
      if (from && sp !== -1 && sp < at) from = sp + 1;
      sp = r._text.lastIndexOf(' ', to);
      if (to < r._text.length && sp > end) to = sp;
      var div = document.createElement('div'), mark = document.createElement('mark');
      div.className = 'row-hit';
      mark.textContent = r._text.slice(at, end);
      div.appendChild(document.createTextNode((from ? '…' : '') + r._text.slice(from, at)));
      div.appendChild(mark);
      div.appendChild(document.createTextNode(r._text.slice(end, to) + (to < r._text.length ? '…' : '')));
      r.cells[0].appendChild(div);
      r._hit = div;
    }

    function render() {
      var pages = Math.max(1, Math.ceil(matched.length / PAGE));
      page = Math.min(Math.max(page, 1), pages);
      var start = (page - 1) * PAGE, slice = matched.slice(start, start + PAGE);
      rows.forEach(function (r) { r.hidden = true; });
      slice.forEach(function (r) { r.hidden = false; });
      if (count) count.textContent = matched.length;
      if (total) total.hidden = matched.length === rows.length;
      if (empty) empty.hidden = matched.length !== 0;
      if (pager) pager.hidden = matched.length <= PAGE;
      if (status) {
        status.textContent = matched.length
          ? (start + 1) + '–' + (start + slice.length) + ' of ' + matched.length
            + ' · page ' + page + ' of ' + pages
          : '';
      }
      if (prev) prev.disabled = page <= 1;
      if (next) next.disabled = page >= pages;
    }

    function showChip() {
      if (!chip) return;
      chip.textContent = '';
      var label = state.stack ? 'Stack term: ' + state.stack : state.term ? 'Term: ' + state.term : '';
      chip.hidden = !label;
      if (!label) return;
      var x = document.createElement('button');
      x.type = 'button';
      x.className = 'chip-x';
      x.setAttribute('aria-label', 'Remove filter ' + label);
      x.textContent = '×';
      x.addEventListener('click', function () { state.stack = state.term = ''; apply(true); });
      chip.appendChild(document.createTextNode(label));
      chip.appendChild(x);
    }

    function apply(write) {
      var needle = state.q.trim().toLowerCase();
      var re = state.term ? wordRe(state.term) : null;
      var order = sorted();
      order.forEach(function (r) { tbody.appendChild(r); });
      matched = order.filter(function (r) {
        if (needle && r._q.indexOf(needle) === -1 && r._low.indexOf(needle) === -1) return false;
        for (var i = 0; i < state.flags.length; i++) {
          if (r.getAttribute('data-' + state.flags[i]) !== '1') return false;
        }
        if (state.stack && ('|' + (r.getAttribute('data-terms') || '') + '|')
            .indexOf('|' + state.stack + '|') === -1) return false;
        return !re || re.test(r._title + ' ' + r._text);
      });
      rows.forEach(function (r) { snippet(r, needle); });
      page = 1;   // a new filter or order always starts the reader on page one
      heads.forEach(function (th) {
        var s = state.sort.split('-');
        if (s[0] === th.getAttribute('data-key')) {
          th.setAttribute('aria-sort', s[1] === 'desc' ? 'descending' : 'ascending');
        } else {
          th.removeAttribute('aria-sort');
        }
      });
      showChip();
      var filtering = !!(needle || state.flags.length || state.stack || state.term);
      clears.forEach(function (c) { if (c.closest('.tbl-filters')) c.hidden = !filtering; });
      if (cfg.terms) pressed();
      if (write) {
        setParam(P + 'q', state.q.trim());
        setParam(P + 'sort', state.sort);
        setParam(P + 'f', state.flags.join(','));
        setParam(P + 'stack', state.stack);
        setParam(P + 'term', state.term);
        writeHash();
      }
      render();
    }

    // which of My terms each row mentions: tags on the row, and data-mine for the filter
    function refreshMine() {
      var res = mine.map(function (t) { return {t: t, re: wordRe(t)}; });
      var n = 0;
      rows.forEach(function (r) {
        var hay = r._title + ' ' + r._text;
        r._mine = res.filter(function (x) { return x.re.test(hay); }).map(function (x) { return x.t; });
        r.setAttribute('data-mine', r._mine.length ? '1' : '0');
        if (r._mineTags) { r._mineTags.parentNode.removeChild(r._mineTags); r._mineTags = null; }
        if (!r._mine.length) return;
        n++;
        var div = document.createElement('div');
        div.className = 'row-flags';
        r._mine.forEach(function (t) {
          var s = document.createElement('span');
          s.className = 'tag tag-mine tag-flag';
          s.textContent = t;
          div.appendChild(s);
        });
        r.cells[0].insertBefore(div, r._hit || null);
        r._mineTags = div;
      });
      if (mineBox) {
        mineBox.parentNode.hidden = !mine.length;
        mineBox.parentNode.querySelector('[data-count]').textContent = n;
        if (!mine.length && mineBox.checked) {
          mineBox.checked = false;
          state.flags = state.flags.filter(function (f) { return f !== 'mine'; });
        }
      }
    }

    function pressed() {
      [].forEach.call(document.querySelectorAll('[data-stack-term]'), function (b) {
        b.setAttribute('aria-pressed', String(b.getAttribute('data-stack-term') === state.stack));
      });
      [].forEach.call(document.querySelectorAll('[data-my-term]'), function (b) {
        b.setAttribute('aria-pressed', String(!!state.term && same(b.getAttribute('data-my-term'), state.term)));
      });
    }

    if (search) search.addEventListener('input', function () { state.q = search.value; apply(true); });
    checks.forEach(function (c) {
      c.addEventListener('change', function () {
        state.flags = checks.filter(function (x) { return x.checked; })
                            .map(function (x) { return x.getAttribute('data-flag'); });
        apply(true);
      });
    });
    clears.forEach(function (c) {
      c.addEventListener('click', function () {
        state.q = state.stack = state.term = '';
        state.flags = [];
        if (search) search.value = '';
        checks.forEach(function (x) { x.checked = false; });
        apply(true);
      });
    });
    if (prev) prev.addEventListener('click', function () { page--; render(); });
    if (next) next.addEventListener('click', function () { page++; render(); });

    return {
      cfg: cfg, load: load, apply: apply, refreshMine: refreshMine, pressed: pressed,
      // one term filter at a time; choosing the active one again clears it
      setStack: function (t) { state.term = ''; state.stack = state.stack === t ? '' : t; apply(true); },
      setTerm: function (t) {
        state.stack = '';
        state.term = state.term && same(state.term, t) ? '' : t;
        apply(true);
      },
      head: function () { return document.getElementById(cfg.head); }
    };
  }

  var tables = CONFIG.map(table).filter(Boolean);
  var roles = tables.filter(function (t) { return t.cfg.terms; })[0];
  tables.forEach(function (t) { t.refreshMine(); t.apply(false); });

  // another view of this page, opened in place (a pasted link): show it
  window.addEventListener('hashchange', function () {
    if (location.hash.indexOf('=') === -1) return;
    params = readParams();
    tables.forEach(function (t) { t.load(); t.refreshMine(); t.apply(false); });
  });

  // a stack term anywhere on the page filters Open roles to the postings behind it
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-stack-term]');
    if (!b || !roles) return;
    roles.setStack(b.getAttribute('data-stack-term'));
    var head = roles.head();
    if (head && !head.parentNode.contains(b)) head.scrollIntoView({block: 'start'});
  });

  var wrap = document.getElementById('my-terms');
  if (!wrap || !roles) return;
  var list = document.getElementById('my-terms-list');
  var form = document.getElementById('my-terms-form');
  var input = document.getElementById('my-terms-input');
  var note = document.getElementById('my-terms-note');
  var SYNCED_NOTE = 'Saved to your profile: every dashboard tags the roles that mention them, '
                  + 'and the weekly briefs for the accounts you track watch them too.';
  wrap.hidden = false;

  function draw() {
    list.textContent = '';
    if (!mine.length) {
      var none = document.createElement('span');
      none.className = 'text-muted';
      none.textContent = 'None yet.';
      list.appendChild(none);
    }
    mine.forEach(function (t) {
      var chip = document.createElement('span'), b = document.createElement('button');
      var x = document.createElement('button');
      chip.className = 'chip';
      b.type = x.type = 'button';
      b.className = 'chip-term';
      b.setAttribute('data-my-term', t);
      b.title = 'Show only the open roles that mention ' + t + '.';
      b.textContent = t;
      b.addEventListener('click', function () { roles.setTerm(t); });
      x.className = 'chip-x';
      x.setAttribute('aria-label', 'Remove ' + t + ' from My terms');
      x.textContent = '×';
      x.addEventListener('click', function () {
        mine = mine.filter(function (m) { return m !== t; });
        changed();
      });
      chip.appendChild(b);
      chip.appendChild(x);
      list.appendChild(chip);
    });
    roles.pressed();
  }

  function setNote(text) { if (note) note.textContent = text; }
  // message, if any, replaces the usual note once the save lands: set any
  // sooner and the save's own note would cover it
  function saveMine(message) {
    if (!synced) {
      try { localStorage.setItem(STORE, JSON.stringify(mine)); } catch (e) {}
      if (message) setNote(message);
      return;
    }
    tickApi('POST', '/api/profile', {persona: {focus_terms: mine}}).then(function () {
      forgetLocal();
      setNote(message || SYNCED_NOTE);
    }).catch(function (err) {
      setNote(err.status === 401 ? 'Your session expired: reload to sign in, then add the term again.'
                                 : 'Couldn\u2019t save to your profile: ' + err.message);
    });
  }
  function changed(message) {
    saveMine(message);
    tables.forEach(function (t) { t.refreshMine(); t.apply(true); });
    draw();
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var skipped = [];
    input.value.split(',').forEach(function (raw) {
      var t = raw.replace(/\s+/g, ' ').trim();
      if (!t) return;
      if (t.length > TERM_CHARS || !GOOD_TERM.test(t)) { skipped.push(t); return; }
      if (mine.length < MAX_TERMS && !mine.some(function (m) { return same(m, t); })) mine.push(t);
    });
    input.value = '';
    changed(skipped.length ? 'Skipped ' + skipped.join(', ')
                             + ": a term is letters, digits, spaces and . + # / & ' - only." : '');
  });
  // the list was changed in another tab: follow it (only while it lives here)
  window.addEventListener('storage', function (e) {
    if (e.key !== STORE || synced) return;
    mine = loadMine();
    tables.forEach(function (t) { t.refreshMine(); t.apply(false); });
    draw();
  });
  draw();

  tickProfile().then(function (data) {
    synced = true;
    var saved = (data.persona && data.persona.focus_terms) || [];
    if (!saved.length && mine.length) {
      saveMine();            // this browser's list predates the persona: move it there
    } else {
      mine = saved.slice(0, MAX_TERMS);
      forgetLocal();
      setNote(SYNCED_NOTE);
      tables.forEach(function (t) { t.refreshMine(); t.apply(false); });
      draw();
    }
  }).catch(function () {});  // no profile to sync with: this browser's list stands
})();
</script>"""


def _tables_script() -> str:
    """The script behind both dashboard tables and My terms; see _TABLES_JS.
    My terms load and save through tickProfile() and tickApi(), which the
    menu bar's script defines (_nav) before this runs."""
    subs = {"%PAGE%": str(ROLES_PAGE_SIZE), "%STORE%": json.dumps(_MY_TERMS_KEY),
            "%MAX_TERMS%": str(_MY_TERMS_MAX), "%TERM_CHARS%": str(_MY_TERM_CHARS),
            "%CONFIG%": _json_for_script([_ROLE_TABLE, _ARCHIVE_TABLE])}
    out = _TABLES_JS
    for k, v in subs.items():
        out = out.replace(k, v)
    return out


class Citations:
    """Numbers citations in the order they're used and keeps the reference list in sync."""
    def __init__(self):
        self.items: list[dict] = []

    def cite(self, label: str, url: str | None = None) -> str:
        self.items.append({"n": len(self.items) + 1, "label": label, "url": url})
        return self.marker(len(self.items))

    @staticmethod
    def marker(n: int) -> str:
        """The superscript in the text, a link down to its source."""
        return f'<sup class="cite"><a href="#src-{n}" aria-label="Source {n}">{n}</a></sup>'

    def render_list(self) -> str:
        rows = []
        for c in self.items:
            body = (f'<a href="{html.escape(c["url"])}" target="_blank" rel="noopener">'
                    f'{html.escape(c["label"])}</a>') if c["url"] else html.escape(c["label"])
            rows.append(f'<div class="src" id="src-{c["n"]}"><span class="cite">{c["n"]}</span><span>{body}</span></div>')
        return "".join(rows)


# ---------------------------------------------------------------- dashboard page

def _term_bucket(title: str, sub: str, rows: list[dict], footer_fn, scale: int = 0,
                 wide: bool = False) -> str:
    """One bucket of stack terms, a column of the stack card. Each row's meter
    is its postings against the busiest term on the page (scale), so the
    columns read on one scale."""
    body = []
    for r in rows[:8]:
        count = f'{r["curr_n"]} specs' if r["curr_n"] else f'was {r["prev_n"]}'
        if r.get("since"):
            count += f' &middot; since {date.fromisoformat(r["since"]):%b &rsquo;%y}'
        term = html.escape(r["term"])
        # a term still on the board filters Open roles to the postings behind it
        label = (f'<button type="button" class="term-btn" data-stack-term="{term}" '
                 f'title="Show the open roles that mention {term}">{term}</button>'
                 if r["curr_n"] else f'<span class="text-muted">{term}</span>')
        share = round(100 * r["curr_n"] / scale) if scale else 0
        body.append(f'<div class="stack-row">{label}'
                    f'<span class="meter" aria-hidden="true"><i style="width:{share}%"></i></span>'
                    f'<span class="stack-meta">{count}</span></div>')
    stack = (f'<div class="stack-list{" wide" if wide else ""}">{"".join(body)}</div>' if rows
             else '<p class="empty">None this period.</p>')
    footer = footer_fn(rows[0]) if rows else ""
    return (f'<div class="stack-col">'
            f'<div class="card-head"><div><h3 class="card-title">{title}</h3>'
            f'<span class="card-sub">{sub}</span></div>'
            f'<span class="count">{len(rows)}</span></div>'
            f'{stack}'
            f'{f"<p class=\"note\">{footer}</p>" if footer else ""}'
            f'</div>')


def _archive_record(r: dict, href: str | None = None, heading: str = "h2") -> str:
    """The full record for one archived req — the body of both the dashboard's
    detail dialog and the req's own page at archive_href. Every field the lake
    kept is shown; one it never had is said to be missing rather than silently
    left out. href, when given, is the req's own page, linked from the dialog."""
    def field(label: str, value: str | None) -> str:
        shown = html.escape(value) if value else '<span class="text-muted">Not captured</span>'
        return f'<dt>{label}</dt><dd>{shown}</dd>'

    flags = []
    if r["is_tech_role"]:
        flags.append('<span class="tag tag-accent tag-flag">Tech role</span>')
    if r["remote"]:
        flags.append('<span class="tag tag-neutral tag-flag">Remote</span>')
    flags.append('<span class="tag tag-outline tag-flag">Closed</span>')
    posted = r["posted_on"]
    if posted and r["date_trust"]:
        posted = f'{posted} (board date, trust: {r["date_trust"]})'
    days = f'{r["days_open"]} day{"s" if r["days_open"] != 1 else ""}' if r["days_open"] is not None else None
    terms = "".join(f'<span class="tag tag-outline" style="margin:0 4px 4px 0">{html.escape(t)}</span>'
                    for t in r["terms"]) or '<span class="text-muted">None matched</span>'
    if r["url"]:
        link = (f'<a href="{html.escape(r["url"])}" target="_blank" rel="noopener">'
                f'{html.escape(r["url"])}</a><br><span class="text-muted" style="font-size:12.5px">'
                f'The req has closed, so the board may no longer serve this page.</span>')
    else:
        link = '<span class="text-muted">Not captured</span>'
    page = (f'<dt>TICK page</dt><dd><a href="{html.escape(href)}">{html.escape(href)}</a></dd>'
            if href else "")
    if r.get("parsed"):
        desc = _description_sections(r["parsed"])
    elif r["description"]:
        desc = f'<div class="arch-desc">{html.escape(r["description"])}</div>'
    elif r["has_record"]:
        desc = ('<p class="empty">The board served no '
                'description for this req on its last crawl.</p>')
    else:
        desc = (f'<p class="empty">No curated crawl for '
                f'{html.escape(r["last_seen"] or "its last sighting")} is in the lake, so only the '
                f'title, location and dates tracked in state survive for this req.</p>')
    return (f'<p class="eyebrow">Archived posting</p>'
            f'<{heading} class="arch-title">{html.escape(r["title"])}</{heading}>'
            f'<div class="row-flags">{"".join(flags)}</div>'
            f'<dl class="arch-fields">'
            f'{field("Location", r["location"])}{field("Team", r["team"])}'
            f'{field("Posted on", posted)}{field("First seen", r["first_seen"])}'
            f'{field("Last seen", r["last_seen"])}{field("Closed on", r["closed_on"])}'
            f'{field("Days open", days)}{field("Pay range", pay_label(r.get("pay")))}'
            f'{field("Source", r["source"])}'
            f'{field("Posting ID", r["pid"])}'
            f'<dt>Original posting</dt><dd>{link}</dd>{page}'
            f'<dt>Stack signals</dt><dd>{terms}</dd>'
            f'</dl>'
            f'<h3 class="arch-desc-h">Description</h3>{desc}')


_SECTION_LABELS = {
    "summary": "Summary", "responsibilities": "Responsibilities", "qualifications": "Qualifications",
    "preferred_qualifications": "Preferred qualifications", "work_arrangement": "Work arrangement",
    "compensation": "Compensation",
}


_PROSE_SECTIONS = ("summary", "work_arrangement", "compensation")


def _description_sections(parsed: dict) -> str:
    """A description in its labelled sections, role content only: the board's
    intro, benefits and equal-opportunity text are said to be left out, not
    shown. An approximate rebuild says so."""
    secs = parsed["sections"]
    out = []
    for kind, label in _SECTION_LABELS.items():
        lines = secs.get(kind) or []
        if not lines:
            continue
        if kind in _PROSE_SECTIONS:   # prose reads as paragraphs, not one bullet per sentence
            body = f'<p class="arch-para">{html.escape(" ".join(lines))}</p>'
        else:
            body = '<ul class="arch-list">' + "".join(f"<li>{html.escape(t)}</li>" for t in lines) + "</ul>"
        out.append(f'<h4 class="arch-sec">{label}</h4>{body}')
    for o in secs.get("other") or []:
        items = "".join(f"<li>{html.escape(t)}</li>" for t in o["lines"])
        out.append(f'<h4 class="arch-sec">{html.escape(o["heading"])}</h4><ul class="arch-list">{items}</ul>')
    notes = []
    left_out = [k.replace("_", " ") for k in description.BOILERPLATE if secs.get(k)]
    if left_out:
        notes.append(f'The board\'s {_join_en(left_out)} text is left out.')
    if parsed.get("approximate"):
        notes.append("Sections were rebuilt from the posting's flattened text, so a heading "
                     "the board worded unusually may sit inside the text above it.")
    note = f'<p class="note">{" ".join(notes)}</p>' if notes else ""
    return f'<div class="arch-desc-sections">{"".join(out)}</div>{note}'


def _archive_detail(r: dict, i: int, href: str) -> str:
    """One archived req, pre-rendered into a <template> the detail dialog clones
    on click."""
    return f'<template id="arch-{i}">{_archive_record(r, href)}</template>'


def render_archive_page(r: dict, company: str, company_name: str, generated: str,
                        domain: str | None = None) -> str:
    """A closed req's own page, served at archive_href. The board stops serving
    a posting once it closes, so this is the stable address for one: briefs cite
    it, and the dashboard's archive rows link to it. Static like every other
    page — no script, so it reads the same with JS off and prints whole."""
    dash = f"/{company}/dashboard.html"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{_head(f"{r['title']} — {company_name} archive — TICK",
       f"Archived {company_name} posting {r['pid']}, closed {r['closed_on']}.")}
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
{_nav("archive", company)}
<div class="wrap narrow">
  <div class="crumb"><a href="{APP_HOME}">Accounts</a><span>/</span><a href="{dash}">{html.escape(company_name)}</a><span>/</span><a href="{dash}#archive">Archive</a><span>/</span><span class="here">{html.escape(r["pid"])}</span></div>
  <main id="main">
  <div class="blueprint paper">{_archive_record(r, heading="h1")}</div>
  <p style="margin:24px 0 0"><a class="btn btn-secondary btn-sm" href="{dash}#archive">&larr; Back to the archive</a></p>
  </main>
  {_footer(generated, domain)}
</div>
</body>
</html>
"""


def render_archive_pages(lake: dict, company: str) -> dict[str, str]:
    """{key: html} — one page per closed req, keyed by archive_key."""
    curr = lake["curr"]
    generated, domain = fmt_dt(curr["captured_at"]), board_domain(curr["postings"])
    return {archive_key(company, r["pid"]): render_archive_page(r, company, curr["company"],
                                                                generated, domain)
            for r in archived_postings(lake)}


def _archive_script() -> str:
    """Opens the detail dialog for an archive row. A click anywhere on the row
    works, as does the title link (which is what keyboard users tab to). The
    link is a real one to the req's own page, so a modified click opens that
    in a new tab and with JS off it is simply followed."""
    return """<script>
(function () {
  var dlg = document.getElementById('archive-dialog');
  var body = document.getElementById('archive-dialog-body');
  var table = document.getElementById('archive-roles');
  if (!dlg || !body || !table) return;
  var opener = null;
  function open(id, from) {
    var tpl = document.getElementById(id);
    if (!tpl) return;
    body.innerHTML = '';
    body.appendChild(tpl.content.cloneNode(true));
    opener = from;
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
    body.scrollTop = 0;
  }
  function close() {
    if (dlg.close) dlg.close(); else dlg.removeAttribute('open');
  }
  table.addEventListener('click', function (e) {
    var row = e.target.closest('tr[data-arch]');
    if (!row) return;
    if (e.target.closest('a') && (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey
                                  || e.button !== 0)) return;
    e.preventDefault();
    open(row.getAttribute('data-arch'), row.querySelector('a') || row);
  });
  document.getElementById('archive-dialog-close').addEventListener('click', close);
  // a click on the backdrop lands on the dialog element itself, not its content
  dlg.addEventListener('click', function (e) { if (e.target === dlg) close(); });
  dlg.addEventListener('close', function () { if (opener) opener.focus(); });
})();
</script>"""


def render_dashboard(lake: dict, company: str) -> str:
    """One account's dashboard. On the first crawl there is no earlier snapshot,
    so every figure that is a change between two crawls (the delta, opened and
    closed, the Adopting/Established split) gives way to what the one crawl
    can show: the open board, its stack terms and its tech roles."""
    curr, prev, diff, state = lake["curr"], lake["prev"], lake["diff"], lake["state"]
    first = prev is None
    company_name = curr["company"]
    curr_dt = curr["captured_dt"]
    prev_dt = None if first else prev["captured_dt"]
    age_cutoff = stack_age_cutoff(lake)
    buckets = categorize_terms(curr["stack"], {} if first else prev["stack"],
                               lake.get("term_first_seen"), age_cutoff)
    new_rows = new_postings_with_signals(diff, curr["stack"], curr["postings"], lake["curated"])
    tech_new = [r for r in new_rows if r["is_tech_role"]]
    open_rows = open_postings(lake)
    new_open = [r for r in open_rows if r["is_new"]]
    domain = board_domain(curr["postings"])
    counts = (diff or {}).get("counts", {})
    delta = pct = window_days = None
    if not first:
        delta = curr["postings_total"] - prev["postings_total"]
        pct = round(100 * delta / prev["postings_total"], 1) if prev["postings_total"] else 0
        # the span actually covered, which is shorter than the requested window
        # while a company is still building up history
        window_days = (date.fromisoformat(curr_dt) - date.fromisoformat(prev_dt)).days

    scale = max((r["curr_n"] for k in ("adopting", "established") for r in buckets[k][:8]), default=0)
    if first:
        # With nothing to compare against, categorize_terms files every term as
        # adopting. That is a window artefact, not a finding, so the first crawl
        # shows one unlabelled card of what the board mentions. Its first-seen
        # date is only the day collection started, so it is left off.
        stack_cards = _term_bucket(
            "On the board", f"Terms in the {curr_dt} postings",
            [{**r, "since": None} for r in buckets["adopting"]],
            lambda r: (f'Present on the {curr_dt} board. A term is split into Adopting or '
                       f'Established once there is an earlier crawl to date it from.'), scale, wide=True)
        stack_cols = ""
    else:
        if age_cutoff:
            adopt_note = (f'First seen in a posting after {age_cutoff} &mdash; inside the last '
                          f'6 months, so these read as new bets rather than standing stack.')
            estab_note = (f'First seen on or before {age_cutoff} &mdash; carried for more than '
                          f'6 months, back to at least {lake.get("history_from", prev_dt)}.')
        else:
            adopt_note = (f'Present {curr_dt}; absent in the {prev_dt} snapshot {window_days} days '
                          f'earlier. (Lake reaches back under 6 months, so this is a window '
                          f'comparison, not a term age.)')
            estab_note = f'Present in both the {prev_dt} and {curr_dt} crawls.'
        adopt_sub, estab_sub = (("New in the last 6 months", "Carried for 6 months or more") if age_cutoff
                                else (f"New since {prev_dt}", "In both crawls"))
        # an empty bucket keeps a narrow column; the other takes the room
        adopt, estab = bool(buckets["adopting"]), bool(buckets["established"])
        lopsided = adopt != estab
        stack_cards = (_term_bucket("Adopting", adopt_sub, buckets["adopting"],
                                    lambda r: adopt_note, scale, wide=lopsided)
                       + _term_bucket("Established", estab_sub, buckets["established"],
                                      lambda r: estab_note, scale, wide=lopsided))
        stack_cols = ("" if not lopsided else
                      ' style="grid-template-columns:minmax(200px,1fr) minmax(0,3fr)"' if estab else
                      ' style="grid-template-columns:minmax(0,3fr) minmax(200px,1fr)"')

    def _open_row(r: dict) -> str:
        title = html.escape(r["title"])
        link = (f'<a href="{html.escape(r["url"])}" target="_blank" rel="noopener">{title}</a>'
                if r["url"] else title)
        # flags sit on their own line under the title: inline, they pushed long
        # titles into an extra wrap and the badge landed alone anyway
        flags = []
        if r["is_new"]:
            flags.append('<span class="tag tag-solid tag-flag">New</span>')
        if r["is_tech_role"]:
            flags.append('<span class="tag tag-accent tag-flag">Tech role</span>')
        badges = f'<div class="row-flags">{"".join(flags)}</div>' if flags else ""
        terms = "".join(f'<button type="button" class="tag tag-outline" data-stack-term="{html.escape(t)}">'
                        f'{html.escape(t)}</button>'
                        for t in r["terms"]) or "&mdash;"
        seen = html.escape(r["first_seen"] or "—")
        pay = html.escape(pay_label(r.get("pay"), short=True) or "—")
        attrs = _row_attrs({
            "data-q": " ".join([r["title"], r["location"], r["team"]]).lower(),
            "data-new": int(r["is_new"]), "data-tech": int(r["is_tech_role"]),
            "data-lead": int(r["is_leadership"]),
            "data-terms": "|".join(r["terms"]),
            "data-s-title": r["title"].lower(), "data-s-loc": r["location"].lower(),
            "data-s-seen": r["first_seen"] or "", "data-s-pay": annual_pay(r["pay"]) or "",
            "data-s-n": len(r["terms"]),
        })
        return (f'<tr {attrs}>'
                f'<td>{link}{badges}</td>'
                f'<td class="text-muted">{html.escape(r["location"])}</td>'
                f'<td class="text-muted nowrap">{seen}</td>'
                f'<td class="text-muted nowrap">{pay}</td>'
                f'<td>{terms}</td></tr>')

    open_table_rows = "".join(_open_row(r) for r in open_rows)

    archive_rows = archived_postings(lake)
    for r in archive_rows:
        r["is_leadership"] = bool(_LEADERSHIP_RE.search(r["title"]))

    def _archive_row(r: dict, i: int) -> str:
        days = "&mdash;" if r["days_open"] is None else str(r["days_open"])
        badge = ('<div class="row-flags"><span class="tag tag-accent tag-flag">Tech role</span></div>'
                 if r["is_tech_role"] else "")
        attrs = _row_attrs({
            "data-arch": f"arch-{i}",
            "data-q": " ".join([r["title"], r["location"], r["team"] or ""]).lower(),
            "data-tech": int(r["is_tech_role"]), "data-lead": int(r["is_leadership"]),
            "data-s-title": r["title"].lower(), "data-s-loc": r["location"].lower(),
            "data-s-closed": r["closed_on"],
            "data-s-days": "" if r["days_open"] is None else r["days_open"],
        })
        return (f'<tr {attrs}>'
                f'<td><a href="{archive_href(company, r["pid"])}" aria-haspopup="dialog">'
                f'{html.escape(r["title"])}</a>{badge}</td>'
                f'<td class="text-muted">{html.escape(r["location"])}</td>'
                f'<td class="text-muted nowrap">{html.escape(r["closed_on"])}</td>'
                f'<td class="text-muted num">{days}</td></tr>')

    role_filters = _filter_row(_ROLE_TABLE, [
        _flag_box("new", f"New in last {NEW_ROLE_DAYS} days", len(new_open)),
        _flag_box("tech", "Tech role", sum(r["is_tech_role"] for r in open_rows)),
        _flag_box("lead", "Leadership", sum(r["is_leadership"] for r in open_rows)),
        _flag_box("mine", "My terms", hidden=True),
    ], "Filter open roles")
    archive_filters = _filter_row(_ARCHIVE_TABLE, [
        _flag_box("tech", "Tech role", sum(r["is_tech_role"] for r in archive_rows)),
        _flag_box("lead", "Leadership", sum(r["is_leadership"] for r in archive_rows)),
        _flag_box("mine", "My terms", hidden=True),
    ], "Filter archived roles")

    archive_table_rows = "".join(_archive_row(r, i) for i, r in enumerate(archive_rows))
    archive_details = "".join(_archive_detail(r, i, archive_href(company, r["pid"]))
                               for i, r in enumerate(archive_rows))

    # Why some roles can never carry the flag: first_seen is when TICK first saw
    # a posting, so it only dates the opening if a crawl that lacked it ran
    # inside the window. State the cadence rather than letting the reader assume
    # an unflagged role is an old one.
    prev_crawl = lake["dts"][-2] if len(lake.get("dts") or []) >= 2 else None
    gap = (date.fromisoformat(curr_dt) - date.fromisoformat(prev_crawl)).days if prev_crawl else None
    predates = sum(1 for r in open_rows if r["first_seen"] and r["first_seen"] <= (lake["dts"] or [curr_dt])[0])
    if gap is None:
        recency_note = (f"One crawl so far ({curr_dt}), so no opening can be dated: "
                        f"every role here was already on the board when collection started.")
    elif gap > NEW_ROLE_DAYS:
        recency_note = (f"The crawl before {curr_dt} ran {gap} days earlier, so nothing on this board can be "
                        f"placed inside a {NEW_ROLE_DAYS}-day window &mdash; a role that appeared since then "
                        f"may have opened any time across those {gap} days. The flag needs a crawl without the "
                        f"posting inside the last {NEW_ROLE_DAYS} days.")
    else:
        # the carve-out only needs saying when there is actually something carved out
        carve = ""
        if predates:
            carve = (f" {predates} role{'s' if predates != 1 else ''} already on the board at the first "
                     f"crawl ({(lake['dts'] or [curr_dt])[0]}) predate{'' if predates != 1 else 's'} "
                     f"collection, so {'they are' if predates != 1 else 'it is'} never flagged.")
        recency_note = (f"Flagged when the posting was absent from a crawl inside the last {NEW_ROLE_DAYS} days "
                        f"and present on {curr_dt} &mdash; observed absence, not a claimed post date."
                        + carve)

    # Nothing has opened yet on a first crawl, so the card lists the tech roles
    # already on the board rather than an empty "opened" list. A whole board's
    # tech roles can run to hundreds, so the rail shows the first few and the
    # table holds the rest.
    tech_rows = [r for r in open_rows if r["is_tech_role"]] if first else tech_new
    tech_shown = tech_rows[:8] if first else tech_rows
    tech_title, tech_sub = (("Tech roles open", f"On the {curr_dt} board and classified as technical")
                            if first else
                            ("Tech roles opened", "Opened this period and classified as technical"))
    tech_new_html = (f'<div class="list-rows">' + "".join(
        f'<div><a href="{html.escape(r["url"])}" target="_blank" rel="noopener">{html.escape(r["title"])}</a>'
        f'<span>{html.escape(r["location"])}</span></div>' for r in tech_shown) + '</div>'
        if tech_shown else '<p class="empty">None this period.</p>')
    if len(tech_rows) > len(tech_shown):
        tech_new_html += f'<p class="note">{len(tech_rows) - len(tech_shown)} more in Open roles.</p>'

    history = lake.get("history") or lake["series"]
    history_chart = _history_chart(history)
    hist_span = (f'{date.fromisoformat(history[0]["dt"]):%b %Y} &ndash; '
                 f'{date.fromisoformat(history[-1]["dt"]):%b %Y}'
                 if len(history) > 1 else f'{curr_dt}')

    if first:
        brief_lines = [
            f"{company_name} — dashboard snapshot ({curr_dt}, first crawl)",
            f"{curr['postings_total']} open postings. No earlier crawl to compare against yet.",
            f"{len(open_rows)} roles open on the {curr_dt} board.",
        ]
        if buckets["adopting"]:
            brief_lines.append("On the board: " + ", ".join(r["term"] for r in buckets["adopting"][:6]) + ".")
        if tech_rows:
            brief_lines.append(f"{len(tech_rows)} tech roles open: "
                               + "; ".join(f'{r["title"]} ({r["location"]})' for r in tech_shown)
                               + ("; …" if len(tech_rows) > len(tech_shown) else "") + ".")
    else:
        brief_lines = [
            f"{company_name} — dashboard snapshot ({curr_dt}, vs {prev_dt})",
            f"{curr['postings_total']} open postings ({delta:+d}, {pct:+.1f}% since {prev_dt}).",
            f"New: {counts.get('new', 0)} · Closed: {counts.get('closed', 0)} · Reopened: {counts.get('reopened', 0)}.",
        ]
        brief_lines.append(f"{len(open_rows)} roles open on the {curr_dt} board; "
                           f"{len(new_open)} flagged opened in the last {NEW_ROLE_DAYS} days.")
        if buckets["adopting"]:
            brief_lines.append("Adopting: " + ", ".join(r["term"] for r in buckets["adopting"][:6]) + ".")
        if tech_new:
            brief_lines.append("New tech roles: " + "; ".join(f'{r["title"]} ({r["location"]})' for r in tech_new) + ".")
    brief_lines.append(f"Source: {domain} · {len(curr['postings'])} postings with stack signals, captured {fmt_dt(curr['captured_at'])}.")
    brief_text = "\n".join(brief_lines)

    tech_open = sum(r["is_tech_role"] for r in open_rows)
    tech_share = (f"{round(100 * tech_open / len(open_rows))}% of open roles" if open_rows
                  else "No open roles")
    if first:
        head_desc = (f"Hiring signals for {company_name}: {curr['postings_total']} open postings "
                     f"captured {curr_dt}, the first crawl of this board.")
        window_label = f"First crawl {curr_dt}"
        kpis = (_kpi("Open roles", curr["postings_total"], f"First crawl {curr_dt}")
                + _kpi("Opened", "&mdash;", "From the next crawl")
                + _kpi("Closed", "&mdash;", "From the next crawl")
                + _kpi("Tech roles", tech_open, tech_share))
    else:
        head_desc = (f"Hiring signals for {company_name}: {curr['postings_total']} open postings "
                     f"captured {curr_dt}, compared with {prev_dt}.")
        window_label = f"{prev_dt} &ndash; {curr_dt}"
        since = f"since {_short_date(prev_dt)}"
        reopened = counts.get("reopened") or 0
        kpis = (_kpi("Open roles", curr["postings_total"], f"{_delta(delta, pct)} {since}")
                + _kpi("Opened", counts.get("new", "?"),
                       since + (f" &middot; {reopened} reopened" if reopened else ""))
                + _kpi("Closed", counts.get("closed", "?"), since)
                + _kpi("Tech roles", tech_open, tech_share))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{_head(f"{company_name} — TICK dashboard", head_desc)}
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
{_nav("dashboard", company)}
<div class="wrap">
  <div class="crumb"><a href="{APP_HOME}">Accounts</a><span>/</span><span>{html.escape(company_name)}</span><span>/</span><span class="here">Dashboard</span></div>
  <main id="main">
  <div class="hdr">
    <h1>{html.escape(company_name)}</h1>
    <div class="hdr-actions"><button class="btn btn-secondary btn-sm" type="button" id="copy-brief">Copy summary</button></div>
  </div>

  <section class="blueprint kpis" aria-label="This window at a glance">{kpis}</section>

  <div class="grid">
    <div class="card blueprint">
      <div class="card-head"><div><h2 class="card-title">Open roles over time</h2>
        <span class="card-sub">{hist_span} &middot; {len(history)} crawl{"s" if len(history) != 1 else ""}</span></div></div>
      {history_chart}
    </div>
    <div class="right-col">
      <div class="card blueprint">
        <div class="card-head"><div><h2 class="card-title">{tech_title}</h2><span class="card-sub">{tech_sub}</span></div>
          <span class="count">{len(tech_rows)}</span></div>
        {tech_new_html}
      </div>
      <div class="card blueprint">
        <div class="card-head"><h2 class="card-title">Source coverage</h2></div>
        <div class="kv">
          <div><span>Postings analyzed</span><span>{curr['postings_total']}</span></div>
          <div><span>Window</span><span>{window_label}</span></div>
          <div><span>Last crawl</span><span>{fmt_dt(curr['captured_at'])}</span></div>
          <div><span>Board</span><span><a href="https://{html.escape(domain)}" target="_blank" rel="noopener">{html.escape(domain)}</a></span></div>
        </div>
      </div>
    </div>
  </div>

  <section class="section" aria-labelledby="stack-h">
    <h2 class="section-label" id="stack-h">Tech stack, as evidenced by hiring</h2>
    <div class="blueprint stack-card"{stack_cols}>{stack_cards}</div>
  </section>

  <section class="section" aria-labelledby="open-roles-h">
    <div class="tbl-head" id="open-roles-head">
      <h2 class="section-label" id="open-roles-h">Open roles <span class="count"><span id="row-count">{len(open_rows)}</span><span id="row-total" hidden>&nbsp;of {len(open_rows)}</span></span></h2>
      <div class="tbl-controls">
        <input id="role-filter" type="search" placeholder="Search title, location or description" aria-label="Search open roles by title, location or description">
      </div>
    </div>
    {role_filters}
    {_my_terms_strip()}
    <div class="blueprint tbl-wrap">
      <table class="table" id="open-roles">
        <caption class="visually-hidden">Open roles on the {curr_dt} board. Titles link to the
          posting on the company&rsquo;s job board and open in a new tab. Column headers sort the table.</caption>
        <thead><tr><th scope="col" data-key="title">Posting</th><th scope="col" data-key="loc">Location</th><th scope="col" data-key="seen" data-first="desc">First seen</th><th scope="col" data-key="pay" data-type="num" data-first="desc">Pay</th><th scope="col" data-key="n" data-type="num" data-first="desc">Stack signals matched</th></tr></thead>
        <tbody>{open_table_rows or '<tr><td colspan="5" class="text-muted">No open postings on this crawl.</td></tr>'}</tbody>
      </table>
      <p class="tbl-empty text-muted" id="no-match" hidden>No open role matches these filters. <button type="button" class="btn-link" data-clear="open-roles">Clear filters</button> to see the full board.</p>
    </div>
    <div class="pager" id="roles-pager" hidden>
      <button type="button" class="btn btn-secondary btn-sm" id="page-prev">&larr; Prev</button>
      <span class="text-muted" id="page-status" role="status" aria-live="polite"></span>
      <button type="button" class="btn btn-secondary btn-sm" id="page-next">Next &rarr;</button>
    </div>
    <p class="note">{recency_note}</p>
  </section>

  <section class="section" id="archive" aria-labelledby="archive-h">
    <div class="tbl-head">
      <h2 class="section-label" id="archive-h">Archive <span class="count"><span id="archive-count">{len(archive_rows)}</span><span id="archive-total" hidden>&nbsp;of {len(archive_rows)}</span></span> <span class="visually-hidden">closed</span></h2>
      <div class="tbl-controls">
        <input id="archive-filter" type="search" placeholder="Search title, location, team or description" aria-label="Search archived roles by title, location, team or description">
      </div>
    </div>
    {archive_filters}
    <div class="blueprint tbl-wrap">
      <table class="table arch-table" id="archive-roles">
        <caption class="visually-hidden">Postings that have left the board, most recently closed
          first. Select a posting to see everything TICK recorded about it. Column headers sort the table.</caption>
        <thead><tr><th scope="col" data-key="title">Posting</th><th scope="col" data-key="loc">Location</th><th scope="col" data-key="closed" data-first="desc">Closed</th><th scope="col" class="num" data-key="days" data-type="num" data-first="desc">Days open</th></tr></thead>
        <tbody>{archive_table_rows or '<tr><td colspan="4" class="text-muted">No postings have closed since collection started.</td></tr>'}</tbody>
      </table>
      <p class="tbl-empty text-muted" id="archive-no-match" hidden>No archived role matches these filters. <button type="button" class="btn-link" data-clear="archive-roles">Clear filters</button></p>
    </div>
    <div class="pager" id="archive-pager" hidden>
      <button type="button" class="btn btn-secondary btn-sm" id="archive-prev">&larr; Prev</button>
      <span class="text-muted" id="archive-status" role="status" aria-live="polite"></span>
      <button type="button" class="btn btn-secondary btn-sm" id="archive-next">Next &rarr;</button>
    </div>
    <p class="note">Every req that has
      disappeared from the board since collection began. Closed is the first crawl it was missing from;
      details are from the last crawl it appeared on. A req that comes back moves to Open roles.</p>
  </section>
  </main>
  {_footer(fmt_dt(curr['captured_at']), domain)}
</div>
{_copy_script('copy-brief', brief_text)}
<dialog id="archive-dialog" class="arch-dialog" aria-label="Archived posting details">
  <div class="arch-dialog-bar"><button type="button" class="btn btn-secondary btn-sm" id="archive-dialog-close">Close</button></div>
  <div id="archive-dialog-body" class="arch-dialog-body"></div>
</dialog>
{archive_details}
<script type="application/json" id="open-roles-text">{_json_for_script([" ".join(r["text"].split()) for r in open_rows])}</script>
{_tables_script()}
{_archive_script()}
{_history_chart_script(history)}
</body>
</html>
"""


# ---------------------------------------------------------------- brief page


# ---------------------------------------------------------------- home page

def _account_card(a: dict) -> str:
    """One account. Every number shown is one the lake actually holds; an
    account that has not collected gets a state line instead of figures."""
    # the page's one real distinction — an account with pages to open, or not —
    # so the live dot is spent there and nowhere else
    status_class = ("status-live" if a["has_pages"] else
                    "status-review" if a["status"] == _REVIEW else "")
    name = html.escape(a["name"])
    if a["has_pages"]:
        name = f'<a href="/{html.escape(a["slug"])}/dashboard.html">{name}</a>'

    if a["open_roles"] is not None:
        if a["delta"] is None:
            delta_val, delta_lbl = "&mdash;", "No prior crawl"
        else:
            delta_val, delta_lbl = _signed(a["delta"]), f'Since {_short_date(a["prev_dt"])}'
        stats = (f'<div class="stat-row">'
                 f'<div class="stat"><b>{a["open_roles"]}</b><span>Open roles</span></div>'
                 f'<div class="stat"><b>{delta_val}</b><span>{delta_lbl}</span></div>'
                 f'<div class="stat"><b>{a["tech_roles"]}</b><span>Tech roles</span></div>'
                 f'</div>')
    else:
        stats = ""

    if a["has_pages"]:
        # Root-absolute: the home page is served at /accounts, so a relative slug
        # would resolve under it (/accounts/<slug>/dashboard.html) and 404.
        foot = (f'<a class="btn btn-primary btn-sm" href="/{html.escape(a["slug"])}/dashboard.html">Dashboard</a>'
                f'<a class="btn btn-secondary btn-sm" href="/{html.escape(a["slug"])}/brief.html">Brief</a>'
                f'<span class="acct-when">Crawled {a["curr_dt"]}</span>')
        note = ""
    else:
        foot = (f'<a class="btn btn-secondary btn-sm" href="{html.escape(a["careers_url"])}">Careers board</a>'
                if a["careers_url"] else "")
        if a["status"] == _REVIEW:
            conf = f' (confidence {a["confidence"]})' if a["confidence"] is not None else ""
            note = f'Config flagged for human review{conf} — not collecting yet.'
        else:
            note = 'Config verified. Nothing in the lake for this account yet.'
        note = f'<p class="acct-note">{note}</p>'

    return (f'<div class="card blueprint{" is-reporting" if a["has_pages"] else ""}">'
            f'<div class="acct-head"><h2 class="acct-name">{name}</h2>'
            f'<span class="status {status_class}">{a["status"]}</span></div>'
            f'{stats}{note}'
            f'{f'<div class="acct-foot">{foot}</div>' if foot else ""}'
            f'</div>')


def _add_account_card(open_: bool = False) -> str:
    """Trailing tile in the account grid: any viewer can ask for a company the
    lake does not track yet. The form posts to /api/account-request, which
    records the request against the viewer's session and alerts an admin, who
    onboards it with the evaluator and assigns it to the viewer. Folded into a
    disclosure, so the grid ends in one quiet tile rather than a tall form; a
    viewer with no accounts yet finds it open."""
    return (
        f'<details class="blueprint acct-add"{" open" if open_ else ""}>'
        '<summary><span class="acct-plus" aria-hidden="true">+</span><span>'
        '<span class="acct-name" style="display:block">Track another company</span>'
        '<span class="acct-meta" style="display:block">Request an account &middot; reviewed by an administrator</span>'
        '</span></summary>'
        '<form id="acct-form" novalidate>'
        '<p class="acct-note" style="margin:0 0 16px">Tell us which company to track. Once '
        'it&rsquo;s set up and crawled, it appears here with a dashboard.</p>'
        '<label for="acct-name-in">Company name</label>'
        '<input id="acct-name-in" name="company" type="text" placeholder="Acme Industrial Supply" '
        'autocomplete="organization" spellcheck="false" maxlength="120" required>'
        '<label for="acct-url-in">Careers URL <span class="text-muted">(optional)</span></label>'
        '<input id="acct-url-in" name="careers_url" type="url" placeholder="https://careers.example.com" '
        'inputmode="url" autocomplete="url" spellcheck="false" maxlength="500">'
        '<label for="acct-note-in">Notes <span class="text-muted">(optional)</span></label>'
        '<textarea id="acct-note-in" name="note" rows="2" maxlength="1000" '
        'placeholder="Why this company..."></textarea>'
        '<div class="acct-foot">'
        '<button class="btn btn-primary btn-sm" id="acct-submit" type="submit">Request account</button>'
        '<span class="acct-status text-muted" id="acct-status" role="status" aria-live="polite"></span>'
        '</div></form></details>')


def _add_account_script() -> str:
    """Posts the request same-origin. CloudFront signs it to the function URL
    with OAC, which for a body needs the payload's SHA-256 from the browser —
    the x-amz-content-sha256 header, as on the public waitlist form."""
    return r"""<script>
(function () {
  var form = document.getElementById('acct-form'),
      name = document.getElementById('acct-name-in'),
      url  = document.getElementById('acct-url-in'),
      note = document.getElementById('acct-note-in'),
      btn  = document.getElementById('acct-submit'),
      out  = document.getElementById('acct-status');
  if (!form || !name || !url || !note || !btn || !out) return;
  var fields = [name, url, note, btn];
  function busy(on) { fields.forEach(function (f) { f.disabled = on; }); }
  function send(payload) {
    var body = JSON.stringify(payload);
    return crypto.subtle.digest('SHA-256', new TextEncoder().encode(body)).then(function (buf) {
      var hash = Array.prototype.map.call(new Uint8Array(buf), function (b) {
        return ('0' + b.toString(16)).slice(-2);
      }).join('');
      return fetch('/api/account-request', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'content-type': 'application/json', 'x-amz-content-sha256': hash },
        body: body
      });
    }).then(function (res) {
      if (res.ok) return;
      return res.json().catch(function () { return {}; }).then(function (data) {
        var err = new Error(data.error || 'HTTP ' + res.status);
        err.status = res.status;
        throw err;
      });
    });
  }
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var company = name.value.trim(), careers = url.value.trim();
    if (!company) { out.textContent = 'Enter the company name.'; name.focus(); return; }
    if (careers && !/^https?:\/\/\S+$/i.test(careers)) {
      out.textContent = 'The careers URL must start with https://'; url.focus(); return;
    }
    busy(true);
    out.textContent = 'Sending…';
    send({ company: company, careers_url: careers, note: note.value.trim() }).then(function () {
      form.reset();
      busy(false);
      out.textContent = 'Request sent ✓ — we’ll add it to your accounts once it’s set up.';
    }).catch(function (err) {
      busy(false);
      out.textContent = err.status === 401 ? 'Your session expired — reload the page to sign in again.'
                       : err.status === 400 ? err.message
                       : 'Couldn’t send — please try again.';
    });
  });
})();
</script>"""


def render_home(accounts: list[dict]) -> str:
    """The accounts grid. The publisher renders one per viewer scope, passing
    only that viewer's accounts; every home ends with the request tile."""
    reporting = [a for a in accounts if a["has_pages"]]
    collected = [a for a in accounts if a["open_roles"] is not None]
    tracked = sum(a["open_roles"] for a in collected)
    last = max((a["captured_at"] for a in collected if a["captured_at"]), default=None)

    sub = (f'<span><b>{len(accounts)}</b> account{"s" if len(accounts) != 1 else ""}</span>'
           f'<span><b>{len(reporting)}</b> reporting</span><span><b>{tracked}</b> open postings tracked</span>'
           f'{f"<span>Last crawl {fmt_dt(last)}</span>" if last else ""}')

    cards = "".join(_account_card(a) for a in accounts)
    if not accounts:
        cards = ('<div class="card blueprint">'
                 '<h2 class="acct-name">No accounts yet</h2>'
                 '<p class="acct-note">No accounts have been '
                 'assigned to you yet. Request one alongside; once it&rsquo;s set up it appears '
                 'here the next time you sign in.</p></div>')
    cards += _add_account_card(open_=not accounts)
    last_crawl_label = fmt_dt(last) if last else "no crawl yet"
    description = (f'TICK tracks hiring signals across {len(accounts)} '
                   f'account{"s" if len(accounts) != 1 else ""} — '
                   f'{tracked} open postings, {len(reporting)} reporting.')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{_head("TICK — accounts", description)}
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
{_nav("home")}
<div class="wrap">
  <main id="main">
  <div class="hdr hdr-top">
    <div><h1>Accounts</h1>
      <p class="hdr-meta acct-lede">{sub}</p></div>
  </div>
  <div class="acct-grid">{cards}</div>
  </main>
  {_footer(last_crawl_label)}
</div>
{_add_account_script()}
</body>
</html>
"""


# ---------------------------------------------------------------- profile page

def _persona_section() -> str:
    """The viewer's persona editor. Hidden until /api/profile answers: the
    page is shared by every viewer, so the form holds no one's data until the
    script fills it, and without the API it can do nothing. Laid out as
    settings: each group's heading and purpose beside its fields."""
    roles = "".join(f'<option value="{k}">{html.escape(v)}</option>' for k, v in persona.ROLES.items())
    buyers = "".join(f'<label class="chk"><input type="checkbox" name="buyers" value="{k}">'
                     f'<span>{html.escape(v)}</span></label>' for k, v in persona.BUYERS.items())
    return f"""<section class="persona" id="persona" aria-labelledby="persona-h" hidden>
  <h2 class="visually-hidden" id="persona-h">Your persona</h2>
  <p class="callout" id="persona-tracks">Your weekly briefs are written for the reader you
    describe here: who you are, what you sell, and the terms you watch.</p>
  <form class="pform" id="persona-form" novalidate>
    <div class="psec">
      <div class="psec-head"><h3>About you</h3>
        <p>Who the brief is written for. It decides what ranks first, never what is true.</p></div>
      <div class="blueprint pgrid">
        <label>Your role<select id="p-role" name="role"><option value="">Choose&hellip;</option>{roles}</select></label>
        <label>The company you sell for<input id="p-sells-for" name="sells_for" type="text"
          maxlength="{persona.NAME_CHARS}" autocomplete="organization" placeholder="Initech"></label>
      </div>
    </div>
    <div class="psec">
      <div class="psec-head"><h3>What you sell</h3>
        <p>The offer and the teams that buy it, so a brief can say which postings matter to you.</p></div>
      <div class="blueprint pgrid">
        <label class="wide">What you sell<textarea id="p-offer" name="offer" rows="2" maxlength="{persona.OFFER_CHARS}"
          placeholder="Hardened container images with signed SBOMs and build provenance"></textarea></label>
        <fieldset class="wide"><legend>Who you sell to</legend><div class="pchecks">{buyers}</div></fieldset>
      </div>
    </div>
    <div class="psec">
      <div class="psec-head"><h3>What you watch</h3>
        <p>Terms every brief counts for you, and the ones that name a competing product.</p></div>
      <div class="blueprint pgrid">
        <label class="wide">Focus terms<input id="p-focus" name="focus_terms" type="text" autocomplete="off"
          placeholder="kubernetes, sbom, terraform">
          <span class="phint">Comma-separated, up to {persona.MAX_FOCUS}. These are also your My terms on every dashboard.</span></label>
        <label class="wide">Competitor terms<input id="p-competitors" name="competitor_terms" type="text"
          autocomplete="off" placeholder="products that compete with yours">
          <span class="phint">Comma-separated, up to {persona.MAX_COMPETITORS}. A posting that names one says what the company uses or is weighing.</span></label>
      </div>
    </div>
    <div class="pform-foot">
      <span class="acct-status text-muted" id="p-status" role="status" aria-live="polite"></span>
      <button class="btn btn-primary" id="p-save" type="submit">Save</button>
    </div>
  </form>
</section>"""


def _persona_script() -> str:
    """Loads the viewer's profile into the page and saves the persona back.
    The API checks every field against tools/persona.py; its first complaint
    is shown. tickProfile() and tickApi() come from the menu bar (_nav)."""
    return r"""<script>
(function () {
  var section = document.getElementById('persona'), form = document.getElementById('persona-form');
  var status = document.getElementById('p-status'), tracks = document.getElementById('persona-tracks');
  var save = document.getElementById('p-save'), lede = document.getElementById('profile-lede');
  if (!section || !form) return;
  var field = function (id) { return document.getElementById(id); };
  var terms = function (el) {
    return el.value.split(',').map(function (t) { return t.replace(/\s+/g, ' ').trim(); }).filter(Boolean);
  };
  function fill(p) {
    p = p || {};
    field('p-role').value = p.role || '';
    field('p-sells-for').value = p.sells_for || '';
    field('p-offer').value = p.offer || '';
    field('p-focus').value = (p.focus_terms || []).join(', ');
    field('p-competitors').value = (p.competitor_terms || []).join(', ');
    [].forEach.call(form.querySelectorAll('input[name=buyers]'), function (b) {
      b.checked = (p.buyers || []).indexOf(b.value) !== -1;
    });
  }
  // each account the viewer tracks links to its brief, the page this persona writes
  function describeTracks(list) {
    tracks.textContent = '';
    if (!list.length) {
      tracks.textContent = 'You track no accounts yet, so no brief is written for this persona until you do. '
        + 'Your focus terms still work as My terms on every dashboard.';
      return;
    }
    tracks.appendChild(document.createTextNode('Your weekly briefs for '));
    list.forEach(function (slug, i) {
      if (i) tracks.appendChild(document.createTextNode(i === list.length - 1 ? ' and ' : ', '));
      var a = document.createElement('a');
      a.href = '/' + encodeURIComponent(slug) + '/brief.html';
      a.textContent = slug;
      tracks.appendChild(a);
    });
    tracks.appendChild(document.createTextNode(' are written for the reader you describe here.'));
  }
  tickProfile().then(function (data) {
    fill(data.persona);
    describeTracks(data.tracks || []);
    lede.textContent = data.email ? 'Signed in as ' + data.email : 'Signed in.';
    section.hidden = false;
  }).catch(function (err) {
    lede.textContent = err.status === 401 ? 'Your session expired: reload the page to sign in again.'
                     : 'Your profile couldn’t be loaded. Reload the page to try again.';
  });

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var p = {
      role: field('p-role').value, sells_for: field('p-sells-for').value.trim(),
      offer: field('p-offer').value.trim(), focus_terms: terms(field('p-focus')),
      competitor_terms: terms(field('p-competitors')),
      buyers: [].filter.call(form.querySelectorAll('input[name=buyers]'), function (b) { return b.checked; })
                .map(function (b) { return b.value; })
    };
    save.disabled = true;
    status.textContent = 'Saving…';
    tickApi('POST', '/api/profile', {persona: p}).then(function (data) {
      fill(data.persona);
      status.textContent = 'Saved ✓ Your next weekly briefs are written for it.';
    }).catch(function (err) {
      status.textContent = err.status === 401 ? 'Your session expired: reload the page to sign in again.'
                         : err.status === 400 ? err.message : 'Couldn’t save: please try again.';
    }).then(function () { save.disabled = false; });
  });
})();
</script>"""


def render_profile() -> str:
    """/profile, opened from the account menu: the viewer's persona, and the
    accounts whose briefs it writes. One page for every viewer, so it holds no
    one's data; until /api/profile answers, or if it cannot, the lede says so
    in place of an empty form."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{_head("TICK — your profile", "Your TICK profile: the persona your weekly briefs are written for.")}
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
{_nav("profile")}
<div class="wrap narrow">
  <div class="crumb"><a href="{APP_HOME}">Accounts</a><span>/</span><span class="here">Profile</span></div>
  <main id="main">
  <div class="hdr">
    <div><p class="eyebrow">Settings</p><h1>Your profile</h1>
      <p class="hdr-sub" id="profile-lede" role="status">Loading your profile&hellip;</p></div>
  </div>
  {_persona_section()}
  </main>
</div>
{_persona_script()}
</body>
</html>
"""


# ---------------------------------------------------------------- AI brief page

# Keep in step with s3_prefix() in .claude/skills/tick-brief/scripts/check.py,
# which is where publish.py uploads each kind.
_BRIEF_RE = re.compile(r"(?:^|/)briefs/company=([^/]+)/(week|overview)=(\d{4}-\d{2}-\d{2})/(brief|evidence)\.json$")
_BRIEF_KINDS = {"week": "weekly", "overview": "overview"}


def list_briefs(lake_io: dict, company: str) -> list[dict]:
    """Every brief the lake holds both files for, newest first, as
    {"id", "kind", "date", "brief_key", "evidence_key"}. kind is "weekly" or
    "overview"; on one date a weekly brief sorts before an overview. Briefs
    reach briefs/ only through the tick-brief skill's publish.py, which
    uploads a brief only after check.py passes it — so a brief here is
    already verified, and nothing on this side re-writes or repairs one."""
    found: dict[tuple[str, str], dict[str, str]] = {}
    for k in lake_io["keys"]:
        m = _BRIEF_RE.search(k.replace(os.sep, "/"))
        if m and m.group(1) == company:
            found.setdefault((_BRIEF_KINDS[m.group(2)], m.group(3)), {})[m.group(4)] = k
    out = [{"id": f"{kind}-{dt}", "kind": kind, "date": dt,
            "brief_key": files["brief"], "evidence_key": files["evidence"]}
           for (kind, dt), files in found.items() if "brief" in files and "evidence" in files]
    out.sort(key=lambda e: (e["date"], e["kind"] == "weekly"), reverse=True)
    return out


def load_briefs(lake_io: dict, company: str) -> list[dict]:
    """list_briefs with each entry's brief and evidence loaded."""
    return [dict(e, brief=lake_io["get"](e["brief_key"]), evidence=lake_io["get"](e["evidence_key"]))
            for e in list_briefs(lake_io, company)]


def current_brief(entries: list[dict]) -> dict | None:
    """The brief an account's brief.html shows: its newest weekly brief, else
    its newest overview. An overview is only ever a new account's first brief,
    so once weekly briefs begin it steps aside, and stays in the archive."""
    return next((e for e in entries if e["kind"] == "weekly"), entries[0] if entries else None)


def load_brief(lake_io: dict, company: str) -> tuple[dict, dict] | None:
    """(brief, evidence) of the account's current brief, or None."""
    cur = current_brief(list_briefs(lake_io, company))
    return (lake_io["get"](cur["brief_key"]), lake_io["get"](cur["evidence_key"])) if cur else None


def briefs_href(company: str) -> str:
    """The account's brief archive. Under /<slug>/, so the edge's per-account
    check covers it, and every key the archive writes keeps to <slug>/."""
    return f"/{company}/briefs"


def brief_href(company: str, entry: dict) -> str:
    """One published brief's own, permanent address: /<slug>/briefs/<kind>-<date>.
    brief.html moves on each week; this does not, so a shared link keeps
    pointing at the brief it was shared for."""
    return f"{briefs_href(company)}/{entry['id']}"


def _brief_title(entry: dict) -> str:
    return (f"Overview as of {entry['date']}" if entry["kind"] == "overview"
            else f"Week to {entry['date']}")


def render_brief_pages(lake_io: dict, lake: dict, company: str, company_name: str) -> dict[str, str]:
    """{key: html} for everything brief-shaped under /<slug>/: brief.html,
    which shows the current brief (see current_brief) or says there is none
    yet, and the brief archive — an index of every published brief, weekly and
    overview, and one page per brief at brief_href. The one entry point for
    the CLI and the publisher, so the two cannot disagree about which brief is
    current, and the lake's briefs are read once for all of it.

    Each brief is rendered against its own evidence pack, its citations
    resolved against the lake as it stands now, so a role an old brief cites
    that has since closed links to its archive page."""
    entries = load_briefs(lake_io, company)
    cur = current_brief(entries)
    if not cur:
        return {f"{company}/brief.html": render_no_brief(company, company_name,
                                                         fmt_dt(lake["curr"]["captured_at"]))}
    pages = {
        f"{company}/brief.html": render_ai_brief(
            cur["brief"], cur["evidence"], lake,
            archive=_archive_nav(company, entries, cur, cur, on_archive=False)),
        f"{company}/briefs/index.html": render_briefs_index(
            company, company_name, entries, cur, fmt_dt(lake["curr"]["captured_at"])),
    }
    for e in entries:
        pages[f"{company}/briefs/{e['id']}/index.html"] = render_ai_brief(
            e["brief"], e["evidence"], lake, archive=_archive_nav(company, entries, e, cur, on_archive=True))
    return pages


def render_account_brief(lake_io: dict, lake: dict, company: str, company_name: str) -> str:
    """An account's brief.html alone (see render_brief_pages)."""
    return render_brief_pages(lake_io, lake, company, company_name)[f"{company}/brief.html"]


def _shows_reader(entry: dict, cur: dict) -> bool:
    """A brief names the persona it was written for. An archived brief written
    for someone other than the current brief's reader keeps its text but drops
    that line: the account may since have passed to another user, who has no
    business reading the last one's role and employer."""
    return entry["brief"].get("persona_ref") == cur["brief"].get("persona_ref")


def _archive_nav(company: str, entries: list[dict], entry: dict, cur: dict, on_archive: bool) -> dict:
    i = next(n for n, e in enumerate(entries) if e is entry)
    near = lambda e: {"href": brief_href(company, e), "label": _brief_title(e)} if e else None
    return {"count": len(entries), "index_href": briefs_href(company),
            "newer": near(entries[i - 1] if i > 0 else None),
            "older": near(entries[i + 1] if i + 1 < len(entries) else None),
            "on_archive": on_archive, "is_current": entry is cur,
            "current_href": f"/{company}/brief.html",
            "show_reader": _shows_reader(entry, cur), "title": _brief_title(entry)}


def _first_sentence(paragraphs: list[str], limit: int = 220) -> str:
    # a citation leaves a space before the punctuation that followed it
    text = re.sub(r"\s+([,.;:])", r"\1", _AI_CITE_RE.sub("", (paragraphs or [""])[0])).strip()
    first = split_sentences(text)[0] if text else ""
    return first if len(first) <= limit else first[:limit].rsplit(" ", 1)[0] + " …"


def render_briefs_index(company: str, company_name: str, entries: list[dict], cur: dict,
                        generated: str) -> str:
    """/<slug>/briefs: every brief the account has had, newest first."""
    rows = []
    for e in entries:
        ev, b = e["evidence"], e["brief"]
        board = ev.get("board") or {}
        if e["kind"] == "overview":
            stats = f'{board.get("open_roles", "?")} open roles &middot; {board.get("tech_roles", "?")} tech'
        else:
            stats = (f'{board.get("opened", 0)} opened &middot; {board.get("closed", 0)} closed'
                     f'{" &middot; quiet week" if b.get("quiet_week") else ""}')
        p = ev.get("persona") or {}
        reader = ", ".join(x for x in (p.get("role"), p.get("sells_for")) if x)
        reader = f" &middot; written for {html.escape(reader)}" if reader and _shows_reader(e, cur) else ""
        tags = (f'<span class="tag tag-neutral tag-flag">{"Overview" if e["kind"] == "overview" else "Weekly"}</span>'
                + (' <span class="tag tag-solid tag-flag">Current</span>' if e is cur else ""))
        rows.append(
            f'<li class="blueprint brief-item">'
            f'<div class="brief-item-head"><a href="{brief_href(company, e)}">{html.escape(_brief_title(e))}</a>'
            f'<span>{tags}</span></div>'
            f'<p>{html.escape(_first_sentence(b.get("paragraphs")))}</p>'
            f'<p class="brief-item-meta">{stats}{reader}</p></li>')
    n = len(entries)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{_head(f"{company_name} — TICK brief archive", f"Every brief published for {company_name}, newest first.")}
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
{_nav("brief", company)}
<div class="wrap narrow">
  <div class="crumb"><a href="{APP_HOME}">Accounts</a><span>/</span><span>{html.escape(company_name)}</span><span>/</span><a href="/{company}/brief.html">Brief</a><span>/</span><span class="here">Archive</span></div>
  <main id="main">
  <div class="hdr">
    <div><p class="eyebrow">Brief archive</p><h1>{html.escape(company_name)}</h1>
      <p class="hdr-sub">{n} brief{"s" if n != 1 else ""}, newest first &middot; each as it was published</p></div>
  </div>
  <ol class="brief-list">{"".join(rows)}</ol>
  </main>
  {_footer(generated)}
</div>
</body>
</html>
"""


def render_no_brief(company: str, company_name: str, generated: str) -> str:
    """The brief page of an account with no uploaded brief yet. It holds the
    account's /brief.html address, so the Brief tab and the home card's link
    never 404, and says when one will appear rather than filling the space."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{_head(f"{company_name} — TICK brief", f"The weekly brief for {company_name} has not been written yet.")}
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
{_nav("brief", company)}
<div class="wrap narrow">
  <div class="crumb"><a href="{APP_HOME}">Accounts</a><span>/</span><span>{html.escape(company_name)}</span><span>/</span><span class="here">Brief</span></div>
  <main id="main">
  <div class="blueprint empty-state">
    <p class="eyebrow">{html.escape(company_name)}</p><h1>No brief yet</h1>
    <p>A newly tracked account gets an overview of its
    board and stack first, then a brief each week from the movement on its job board,
    and every claim in one cites the posting it rests on.
    This account's first brief has not been published. Until it is, the
    <a href="/{company}/dashboard.html">dashboard</a> has everything the board shows today.</p>
    <a class="btn btn-primary" href="/{company}/dashboard.html">Open the dashboard</a>
  </div>
  </main>
  {_footer(generated)}
</div>
</body>
</html>
"""


_AI_CITE_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


def _rail_card(label: str, body: str) -> str:
    return f'<div class="card blueprint"><span class="rail-label">{label}</span>{body}</div>'


def _rows(pairs: list[tuple[str, object]]) -> str:
    return ('<div class="kv">'
            + "".join(f'<div><span>{html.escape(str(k))}</span><span>{html.escape(str(v))}</span></div>'
                      for k, v in pairs)
            + "</div>")


def _archive_button(nav: dict | None) -> str:
    """The way into the brief archive, a button in the brief's header."""
    if not nav:
        return ""
    return f'<a class="btn btn-secondary btn-sm" href="{nav["index_href"]}">Brief archive ({nav["count"]})</a>'


def _archive_banner(nav: dict | None) -> str:
    """On an archived brief's own page: which brief it is, and whether it is
    still the current one."""
    if not nav or not nav["on_archive"]:
        return ""
    status = ("This is the account's current brief." if nav["is_current"] else
              f'An earlier brief, kept as it was published. '
              f'<a href="{nav["current_href"]}">Read the current brief</a>.')
    return (f'<div class="archive-bar"><b>{html.escape(nav["title"])}</b>'
            f'<span>{status}</span></div>')


def _archive_pager(nav: dict | None) -> str:
    if not nav or not nav["on_archive"] or not (nav["older"] or nav["newer"]):
        return ""
    older = (f'<a class="btn btn-secondary btn-sm" href="{nav["older"]["href"]}">&larr; '
             f'{html.escape(nav["older"]["label"])}</a>') if nav["older"] else "<span></span>"
    newer = (f'<a class="btn btn-secondary btn-sm" href="{nav["newer"]["href"]}">'
             f'{html.escape(nav["newer"]["label"])} &rarr;</a>') if nav["newer"] else "<span></span>"
    return (f'<nav aria-label="Other briefs" style="display:flex;justify-content:space-between;gap:10px;'
            f'flex-wrap:wrap;margin-top:32px">{older}{newer}</nav>')


def render_ai_brief(brief: dict, evidence: dict, lake: dict | None = None, archive: dict | None = None) -> str:
    """A brief a model wrote (see .claude/skills/tick-brief): a weekly brief,
    or a new account's overview when the pack's kind is "overview". The model's
    text is escaped like any other untrusted input; its [[id]] citations
    become numbered sources resolved against the evidence pack it was written
    from, each number a link down to its source.

    Every cited role links somewhere, resolved against the lake as it stands
    at render time rather than the pack: the pack's standing lists carry no
    URL, and a role open that week may have closed since. A closed role links
    to its archive page, which the publisher writes for every req state.json
    holds as closed; an open one to its posting on the board.

    archive (see _archive_nav) places the brief among the account's others:
    brief.html links to the brief archive, an archived page names itself and
    its neighbours, and an archived brief written for another reader drops its
    "written for" line."""
    overview = evidence.get("kind") == "overview"
    company_name = evidence["company"]["name"]
    slug = evidence["company"]["slug"]
    state = (lake or {}).get("state") or {}
    curated = (lake or {}).get("curated") or {}
    board = evidence["board"]
    window = evidence.get("window") or {}
    as_of = evidence["as_of"] if overview else window["end"]
    postings: dict[str, dict] = {}
    for sec in ("opened", "closed", "reopened", "postings"):
        for p in evidence.get(sec, []):
            postings.setdefault(p["id"], p)
    for key in ("open_leadership_roles", "longest_open"):
        for p in board.get(key, []):
            postings.setdefault(p["id"], p)
    for p in (evidence.get("stack") or {}).get("leadership_stack", []):
        postings.setdefault(p["id"], p)
    persona = evidence.get("persona") or {}
    for p in persona.get("standing_roles", []):
        postings.setdefault(p["id"], p)
    # who the brief was written for: the persona of the user who tracks the
    # account (see tools/persona.py), or nobody named for the default reader
    reader = ", ".join(x for x in (persona.get("role"), persona.get("sells_for")) if x)
    if archive and not archive["show_reader"]:
        reader = ""

    cx = Citations()
    numbered: dict[str, int] = {}

    def label(cid: str) -> tuple[str, str | None]:
        if cid == "BOARD" and overview:
            return (f'Board on {as_of}: {board["open_roles"]} open roles, {board["tech_roles"]} tech, '
                    f'{board["leadership_roles"]} leadership.', None)
        if cid == "BOARD":
            return (f'Board {window["start"]} → {window["end"]}: {board["open_at_start"]} → '
                    f'{board["open_at_end"]} open; {board["opened"]} opened, {board["closed"]} '
                    f'closed, {board["reopened"]} reopened.', None)
        if cid == "STACK":
            return (f"Stack terms across the open board on {as_of}." if overview
                    else "Stack terms across the open board."), None
        if cid == "PERSONA":
            return f"The reader's terms on the board ({reader or 'persona'}).", None
        p = postings.get(cid)
        if not p:
            return f"{cid} — not in the evidence pack.", None
        what = (f'closed {p["closed_on"]} after {p.get("days_open")} days' if p.get("closed_on")
                else f'opened {p["opened_on"]}' if p.get("opened_on")
                else f'reopened {p["reopened_on"]}' if p.get("reopened_on")
                else f'open {p.get("days_open")} days' + (f" on {as_of}" if overview else ""))
        where = f' ({p["location"]})' if p.get("location") else ""
        # today's state wins over the pack's: a req can close, or reopen, after its brief
        closed = state[cid].get("closed_on") if cid in state else p.get("closed_on")
        if closed and not p.get("closed_on"):
            what += f"; closed since, on {closed}"
        # a closed req's board URL now answers "role not found"; its archive page does not
        url = (archive_href(slug, cid) if closed
               else p.get("url") or (curated.get(cid) or {}).get("url"))
        return f'{cid} — {p["title"]}{where}, {what}.', url

    def sup(m: re.Match) -> str:
        cid = html.unescape(m.group(1)).strip()
        if cid not in numbered:
            cx.cite(*label(cid))
            numbered[cid] = len(cx.items)
        return Citations.marker(numbered[cid])

    def rich(text: str) -> str:
        # the marker sits against the word it supports, as a footnote mark does
        return _AI_CITE_RE.sub(sup, html.escape(re.sub(r"\s+(?=\[\[)", "", text or "")))

    def plain(text: str) -> str:
        return _AI_CITE_RE.sub("", text or "").replace("  ", " ").strip()

    # No headline: a one-line title over the first paragraph only restated it.
    paras = "".join(f"<p>{rich(p)}</p>" for p in brief.get("paragraphs", []))

    act_html = []
    for a in brief.get("action_items") or []:
        quote = ""
        q = a.get("quote")
        if q and q.get("text"):
            quote = (f'<blockquote class="action-quote"><span>In their words</span>'
                     f'<em>&ldquo;{html.escape(q["text"])}&rdquo;</em>'
                     f'{rich(f"[[{q.get("source", "")}]]")}</blockquote>')
        steps = "".join(f'<li>{rich(s)}</li>' for s in a.get("steps", []))
        kind = a.get("kind", "")
        act_html.append(
            f'<div class="blueprint action">'
            f'<div class="action-head"><h3 class="action-title">{html.escape(a.get("title", ""))}</h3>'
            f'<span class="tag tag-{"solid" if kind == "outreach" else "neutral"} tag-flag">'
            f'{html.escape(kind)}</span></div>'
            f'<p class="action-why">{rich(a.get("why", ""))}</p>'
            f'{quote}'
            f'{f"<ol class=\"action-steps\">{steps}</ol>" if steps else ""}</div>')
    actions_html = "".join(act_html) or (
        '<p class="empty">'
        + ("Nothing on the board yet warrants an approach." if overview
           else "Nothing moved this week that warrants an approach.") + "</p>")

    weekly = board.get("weekly_open_totals") or []
    sparkline = _trend_svg(weekly)
    spark_span = f'{weekly[0]["dt"]} &rarr; {weekly[-1]["dt"]}' if weekly else ""
    if overview:
        title, heading = "account overview", "Account overview"
        dated = f"Board as of {as_of}"
        meta = f"What {company_name}'s open job board says about its stack, as of {as_of}."
        top = sorted((t for rows in (evidence.get("stack") or {}).get("by_category", {}).values() for t in rows),
                     key=lambda t: (-t["postings"], t["term"]))[:6]
        trend_card = _rail_card(
            "Open board", _rows([("Open roles", board["open_roles"]), ("Tech roles", board["tech_roles"]),
                                 ("Leadership roles", board["leadership_roles"]),
                                 ("Stack terms", (evidence.get("stack") or {}).get("terms_on_board", 0))]))
        moves_card = _rail_card("Most-asked stack", _rows([(t["term"], f'{t["postings"]} role{"s" if t["postings"] != 1 else ""}')
                                                           for t in top])
                                if top else '<p class="empty">No stack terms.</p>')
    else:
        title, heading = "account brief", "Account brief"
        dated = f"Week to {window['end']}"
        meta = f"What moved on {company_name}'s job board in the week to {window['end']}, and what to do about it."
        delta = board.get("net_change") or 0
        trend_card = (
            f'<div class="card blueprint"><span class="rail-label">Open roles, {spark_span}</span>'
            f'{sparkline}'
            f'<div class="chart-foot"><span>{window["start"]} &rarr; {window["end"]}</span>'
            f'<span>{board["open_at_start"]} &rarr; {board["open_at_end"]} {_delta(delta)}</span></div></div>')
        moves_card = _rail_card(
            "This week", '<div class="mini-stats">' + "".join(
                f'<div><b>{board[k]}</b><span>{k.capitalize()}</span></div>'
                for k in ("opened", "closed", "reopened")) + '</div>')

    brief_text = "\n\n".join(
        [f"{company_name} — {title}, {dated[0].lower() + dated[1:]}"]
        + [plain(p) for p in brief.get("paragraphs", [])]
        + (["Action items:"] + [f'- {a.get("title", "")} — {plain(a.get("why", ""))}'
                                + "".join(f"\n    * {plain(s)}" for s in a.get("steps", []))
                                for a in brief["action_items"]] if brief.get("action_items") else []))

    if archive and archive["on_archive"]:
        crumb = (f'<a href="/{slug}/brief.html">Brief</a><span>/</span><a href="{archive["index_href"]}">Archive</a>'
                 f'<span>/</span><span class="here">{html.escape(archive["title"])}</span>')
    else:
        crumb = '<span class="here">Brief</span>'
    hdr_meta = "".join(f"<span>{x}</span>" for x in (
        dated, f"Brief written for {html.escape(reader)}" if reader else "") if x)
    n_actions = len(brief.get("action_items") or [])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{_head(f"{company_name} — TICK {'overview' if overview else 'brief'}", meta)}
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
{_nav("brief", slug)}
<div class="wrap narrow">
  <div class="crumb"><a href="{APP_HOME}">Accounts</a><span>/</span><span>{html.escape(company_name)}</span><span>/</span>{crumb}</div>
  <main id="main">
  <div class="hdr">
    <div><p class="eyebrow">{heading}</p><h1>{html.escape(company_name)}</h1>
      <p class="hdr-meta">{hdr_meta}</p></div>
    <div class="hdr-actions">{_archive_button(archive)}<button class="btn btn-secondary btn-sm" type="button" id="copy-brief">Copy {'overview' if overview else 'brief'}</button></div>
  </div>
  {_archive_banner(archive)}
  <div class="grid narrative">
    <div>
      <article class="blueprint paper prose">{paras}</article>
      <section class="section" aria-labelledby="actions-h">
        <h2 class="section-label" id="actions-h">Action items <span class="count">{n_actions}</span></h2>
        <div class="actions" style="margin-top:14px">{actions_html}</div>
      </section>
      <section class="section" aria-labelledby="sources-h">
        <h2 class="section-label" id="sources-h">Sources <span class="count">{len(cx.items)}</span></h2>
        <div class="blueprint sources" style="margin-top:14px">{cx.render_list()}</div>
      </section>
      {_archive_pager(archive)}
    </div>
    <aside class="right-col rail" aria-label="The board behind this brief">
      {trend_card}
      {moves_card}
    </aside>
  </div>
  </main>
  {_footer(as_of, evidence['company'].get('board_host'))}
</div>
{_copy_script('copy-brief', brief_text)}
</body>
</html>
"""


def render_404() -> str:
    """Served by the host for any unknown path. The stylesheet is inlined like
    every other page, so it renders correctly however deep the bad URL was."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{_head("Page not found — TICK", "That page is not part of this TICK site.")}
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
{_nav("none", signed_in=False)}
<div class="wrap narrow">
  <main id="main">
  <div class="blueprint empty-state" style="margin-top:48px">
    <p class="eyebrow">Error 404</p><h1>Page not found</h1>
    <p>That address is not part of this
      site. Account pages are only written for companies TICK has crawled, so a dashboard or brief
      you had bookmarked can disappear if the account stopped reporting.</p>
    <a class="btn btn-primary" href="{APP_HOME}">Back to accounts</a>
  </div>
  </main>
</div>
</body>
</html>
"""


ROBOTS_TXT = """# TICK renders competitive-intelligence briefs on named accounts.
# Nothing here is meant for a search index.
User-agent: *
Disallow: /
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Render the TICK site — accounts home, plus a dashboard + brief per account")
    ap.add_argument("source", help="lake root dir (containing out/derived/...), or s3://bucket[/prefix]")
    ap.add_argument("--company", default=None,
                    help="render just this company_slug (default: every company in the lake with a snapshot)")
    ap.add_argument("--configs", default=None,
                    help="directory or s3:// prefix of evaluator SourceConfig JSON files, so accounts "
                         "with nothing collected yet still appear (default: the lake's own configs/ "
                         "prefix on S3, or ./configs when rendering a local lake)")
    ap.add_argument("--out-dir", default="site",
                    help="directory to write accounts/index.html, profile/index.html and <slug>/ into")
    ap.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
                    help="how far back the comparison reaches: the page compares the latest snapshot "
                         "against the most recent one at least this old, so the delta measures drift "
                         f"rather than one night's noise (default: {DEFAULT_WINDOW_DAYS})")
    args = ap.parse_args()

    lake_io = open_lake(args.source)
    configs_loc = args.configs
    if configs_loc is None and not args.source.startswith("s3://"):
        configs_loc = "configs" if os.path.isdir("configs") else None
    accounts = build_accounts(load_configs(configs_loc, lake_io), lake_io, args.window_days)
    if not accounts:
        sys.exit(f"no accounts found — no SourceConfigs and no snapshots at {args.source}")

    os.makedirs(args.out_dir, exist_ok=True)
    home_path = os.path.join(args.out_dir, "accounts", "index.html")
    os.makedirs(os.path.dirname(home_path), exist_ok=True)
    with open(home_path, "w") as f:
        f.write(render_home(accounts))
    print(f"wrote {home_path} — {len(accounts)} account{'s' if len(accounts) != 1 else ''}")

    profile_path = os.path.join(args.out_dir, "profile", "index.html")
    os.makedirs(os.path.dirname(profile_path), exist_ok=True)
    with open(profile_path, "w") as f:
        f.write(render_profile())
    print(f"wrote {profile_path}")

    for fname, body in (("404.html", render_404()), ("robots.txt", ROBOTS_TXT)):
        path = os.path.join(args.out_dir, fname)
        with open(path, "w") as f:
            f.write(body)
        print(f"wrote {path}")

    targets = [a for a in accounts if a["has_pages"]]
    if args.company:
        targets = [a for a in accounts if a["slug"] == args.company]
        if not targets:
            sys.exit(f"no account for company={args.company}")
        if not targets[0]["has_pages"]:
            sys.exit(f"{args.company}: nothing collected yet — no snapshot to render")

    for a in targets:
        lake = load_lake(args.source, a["slug"], lake_io, args.window_days)
        acct_dir = os.path.join(args.out_dir, a["slug"])
        os.makedirs(acct_dir, exist_ok=True)
        dash_path = os.path.join(acct_dir, "dashboard.html")
        with open(dash_path, "w") as f:
            f.write(render_dashboard(lake, a["slug"]))
        print(f"wrote {dash_path}")
        briefs = render_brief_pages(lake_io, lake, a["slug"], a["name"])
        for key, body in briefs.items():
            path = os.path.join(args.out_dir, key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(body)
        print(f"wrote {os.path.join(acct_dir, 'brief.html')}"
              + (f" and {len(briefs) - 2} archived brief{'s' if len(briefs) != 3 else ''} under "
                 f"{os.path.join(acct_dir, 'briefs')}" if len(briefs) > 1 else ""))
        archive = render_archive_pages(lake, a["slug"])
        for key, body in archive.items():
            path = os.path.join(args.out_dir, key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(body)
        print(f"wrote {len(archive)} archive page{'s' if len(archive) != 1 else ''} "
              f"under {os.path.join(acct_dir, 'archive')}")

    skipped = [a for a in accounts if not a["has_pages"]]
    if skipped and not args.company:
        print(f"listed on the home screen without pages: "
              + ", ".join(f'{a["slug"]} ({a["status"].lower()})' for a in skipped))
