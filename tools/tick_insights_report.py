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
  - Brief — per account, the newest weekly brief a model wrote with the
    tick-brief skill and uploaded to the lake's briefs/ prefix (see
    load_brief), its citations numbered against the evidence pack it was
    written from. No Python writes brief prose; an account with no uploaded
    brief gets a "No brief yet" page.
  - Archive — per closed req, its last recorded description and dates, at a
    stable address the brief can cite once the board's own link is dead.

Layout written under --out-dir:

    accounts/index.html           the accounts home, served at /accounts
    <company-slug>/dashboard.html
    <company-slug>/brief.html
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

# open-roles chart geometry, shared by the SVG builder and its hover script
_CH_W, _CH_H, _CH_L, _CH_R, _CH_T, _CH_B = 440, 210, 34, 10, 12, 26


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
  --color-bg:#f2f2f3;--color-surface:#e9e9ea;--color-text:#1d1f20;
  --color-accent:#5980a6;--color-divider:color-mix(in srgb,#1d1f20 16%,transparent);
  --color-accent-100:#eef6ff;--color-accent-700:#416180;--color-accent-800:#2c455d;--color-accent-900:#1d2d3d;
  --color-neutral-100:#f5f5f8;--color-neutral-500:#98989b;--color-neutral-800:#424244;
  --font-heading:"Barlow Condensed",system-ui,sans-serif;--font-body:"Barlow",system-ui,sans-serif;
}
*,*::before,*::after{box-sizing:border-box}
/* the UA rule for [hidden] is display:none, but any author display wins over it —
   .pager's display:flex kept the pager on screen after JS had hidden it */
[hidden]{display:none!important}
body{margin:0;background:var(--color-bg);color:var(--color-text);font-family:var(--font-body);font-size:15px;line-height:1.55}
h1,h2,h3,h4,h5,h6{font-family:var(--font-heading);font-weight:600;line-height:1.12;letter-spacing:-.015em;margin:0}
h6{font-size:13px;letter-spacing:.08em;text-transform:uppercase}
a{color:var(--color-accent);text-underline-offset:3px}
:focus-visible{outline:2px solid var(--color-accent);outline-offset:2px}
.text-muted{color:color-mix(in srgb,var(--color-text) 55%,transparent)}
.visually-hidden{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;
  clip:rect(0 0 0 0);clip-path:inset(50%);white-space:nowrap;border:0}
.skip-link{position:absolute;left:8px;top:-48px;z-index:10;padding:9px 14px;background:var(--color-accent-900);
  color:#fff;text-decoration:none;font-family:var(--font-heading);font-weight:600;font-size:14px;transition:top .12s}
.skip-link:focus{top:8px}
/* page title block: an eyebrow line above the h1, so the heading order stays h1 -> h2 */
.eyebrow{font-family:var(--font-heading);font-weight:600;font-size:13px;letter-spacing:.08em;
  text-transform:uppercase;line-height:1.12;margin:0}
.section-label{font-size:13px;letter-spacing:.08em;text-transform:uppercase}
.wrap{max-width:1280px;margin:0 auto;padding:0 24px 60px}
.wrap.narrow{max-width:940px}
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
.crumb{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12px;color:color-mix(in srgb,var(--color-text) 50%,transparent);margin:18px 0 12px}
.hdr{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;flex-wrap:wrap;padding-bottom:18px;border-bottom:1px solid var(--color-divider)}
.blueprint{position:relative;border:1px solid var(--color-divider)}
.blueprint>.corner{position:absolute;width:11px;height:11px;color:color-mix(in srgb,var(--color-text) 55%,transparent)}
.blueprint>.corner::before,.blueprint>.corner::after{content:"";position:absolute;background:currentColor}
.blueprint>.corner::before{left:5px;top:0;width:1px;height:100%}
.blueprint>.corner::after{top:5px;left:0;width:100%;height:1px}
.blueprint>.corner.tl{top:-6px;left:-6px}.blueprint>.corner.tr{top:-6px;right:-6px}
.blueprint>.corner.bl{bottom:-6px;left:-6px}.blueprint>.corner.br{bottom:-6px;right:-6px}
.card{padding:15px 16px}
.tag{display:inline-flex;align-items:center;font-size:11px;letter-spacing:.02em;padding:3px 10px}
.row-flags{display:flex;flex-wrap:wrap;gap:5px;margin-top:5px}
.tag-flag{font-size:10px;padding:2px 8px}
.tag-accent{background:var(--color-accent-100);color:var(--color-accent-800)}
.tag-neutral{background:var(--color-neutral-100);color:var(--color-neutral-800)}
.tag-outline{border:1px solid var(--color-accent);color:var(--color-accent)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;cursor:pointer;text-decoration:none;font-family:var(--font-heading);font-weight:600;font-size:14px;padding:8px 14px;border:1px solid var(--color-divider);background:transparent;color:var(--color-text)}
.btn-primary{background:var(--color-accent);color:var(--color-bg);border-color:var(--color-accent)}
.btn-primary:hover{background:var(--color-accent-700)}
.btn-secondary:hover{background:color-mix(in srgb,var(--color-text) 7%,transparent)}
.table{width:100%;border-collapse:collapse;font-size:13.5px}
.table th{text-align:left;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:color-mix(in srgb,var(--color-text) 60%,transparent);padding:8px;border-bottom:1px solid var(--color-divider)}
.table td{padding:8px;border-bottom:1px solid color-mix(in srgb,var(--color-text) 8%,transparent);vertical-align:top}
.table tbody tr:hover{background:color-mix(in srgb,var(--color-text) 4%,transparent)}
.stack-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
.stack-row{display:flex;justify-content:space-between;gap:10px;font-size:13px}
.stat-row{display:flex;border-top:1px solid var(--color-divider);margin-top:12px}
.stat{flex:1;padding:9px 0 0}
.stat b{display:block;font-family:var(--font-heading);font-weight:600;font-size:24px;line-height:1}
.stat span{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:color-mix(in srgb,var(--color-text) 55%,transparent)}
.right-col{display:flex;flex-direction:column;gap:14px}
.grid{display:grid;grid-template-columns:7fr 4fr;gap:24px;margin-top:24px;align-items:start}
/* a grid item defaults to min-width:auto, so it refuses to shrink below the
   widest thing inside it — the roles table then pushed the whole page sideways
   instead of scrolling inside its own overflow-x container */
.grid>*,.right-col>*{min-width:0}
.grid.narrative{grid-template-columns:1fr 268px}
.cite{color:var(--color-accent-700);font-size:11px}
.prose p{margin:0 0 14px;font-size:15.5px;line-height:1.7;max-width:62ch}
.bar-track{height:6px;background:color-mix(in srgb,var(--color-text) 8%,transparent)}
.bar-fill{height:100%;background:var(--color-accent)}
.crumb a{color:inherit;text-decoration:none}
.crumb a:hover{color:var(--color-accent)}
/* min() keeps a 330px track from overflowing a 320px phone */
.acct-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(330px,100%),1fr));gap:20px;margin-top:24px;align-items:start}
.acct-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.acct-name{font-family:var(--font-heading);font-weight:600;font-size:19px;line-height:1.15}
.acct-meta{font-size:11.5px;letter-spacing:.02em;margin-top:2px;overflow-wrap:anywhere}
.acct-grid>.card{display:flex;flex-direction:column}
/* inset rather than a real border: a 3px left border would shift the card's
   contents 2px and pull the blueprint corner marks out of alignment */
.acct-grid>.card.is-reporting{box-shadow:inset 3px 0 0 var(--color-accent)}
.acct-grid .stat b{color:var(--color-accent)}
.acct-grid .is-reporting .acct-name{color:var(--color-accent-900)}
.tag-solid{background:var(--color-accent);color:var(--color-bg)}
.acct-lede b{color:var(--color-accent-800);font-weight:600}
.acct-foot{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:auto;padding-top:13px}
.btn-sm{font-size:13px;padding:6px 11px}
.acct-note{font-size:11.5px;line-height:1.5;margin:13px 0 0}
.chart-wrap{position:relative;margin:10px 0 4px}
.ch-svg{display:block;width:100%;height:auto;overflow:visible}
.ch-grid{stroke:color-mix(in srgb,var(--color-text) 13%,transparent);stroke-width:1;vector-effect:non-scaling-stroke}
.ch-line{fill:none;stroke:var(--color-accent);stroke-width:2;stroke-linejoin:round;stroke-linecap:round;vector-effect:non-scaling-stroke}
.ch-area{fill:var(--color-accent);opacity:.10}
.ch-hot{fill:var(--color-accent);stroke:var(--color-bg);stroke-width:2}
.ch-hair{stroke:color-mix(in srgb,var(--color-text) 32%,transparent);stroke-width:1;vector-effect:non-scaling-stroke}
.ch-tick{fill:color-mix(in srgb,var(--color-text) 55%,transparent);font-family:var(--font-body);font-size:10px;font-variant-numeric:tabular-nums}
.ch-hit{cursor:crosshair}
.ch-hit:focus-visible{outline:2px solid var(--color-accent);outline-offset:2px}
.spark{display:block;width:100%;height:auto;margin:8px 0 6px;overflow:visible}
.chart-tip{position:absolute;top:0;pointer-events:none;background:var(--color-bg);
  border:1px solid var(--color-divider);padding:5px 8px;font-size:11.5px;line-height:1.35;white-space:nowrap;z-index:2}
.chart-tip b{display:block;font-family:var(--font-heading);font-size:14px;color:var(--color-text)}
.chart-tip span{color:color-mix(in srgb,var(--color-text) 55%,transparent)}
.chart-tbl{margin-top:2px}
.chart-tbl summary{font-size:11px;color:color-mix(in srgb,var(--color-text) 55%,transparent);cursor:pointer}
.chart-tbl table{width:100%;border-collapse:collapse;margin-top:7px;font-size:11.5px}
.chart-tbl th,.chart-tbl td{text-align:left;padding:2px 0;border-bottom:1px solid var(--color-divider)}
.chart-tbl .num{text-align:right;font-variant-numeric:tabular-nums}
.acct-add{border-style:dashed}
.acct-add .acct-name{font-size:17px}
.acct-add{margin:0}
.acct-add label{display:block;font-size:11.5px;letter-spacing:.02em;margin:11px 0 4px;
  color:color-mix(in srgb,var(--color-text) 70%,transparent)}
.acct-add input,.acct-add textarea{font-family:var(--font-body);font-size:13px;padding:6px 9px;width:100%;
  background:var(--color-bg);color:var(--color-text);border:1px solid var(--color-divider)}
.acct-add textarea{resize:vertical;min-height:52px}
.acct-add input:focus-visible,.acct-add textarea:focus-visible{outline:2px solid var(--color-accent);outline-offset:-1px}
.acct-add .acct-status{font-size:12px}
.tbl-head{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-bottom:12px}
.tbl-controls{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.tbl-controls input[type=search]{font-family:var(--font-body);font-size:13px;padding:6px 10px;width:230px;max-width:100%;
  background:var(--color-bg);color:var(--color-text);border:1px solid var(--color-divider)}
.tbl-controls input[type=search]:focus-visible{outline:2px solid var(--color-accent);outline-offset:-1px}
.chk{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;cursor:pointer;color:color-mix(in srgb,var(--color-text) 70%,transparent)}
.tbl-filters{display:flex;align-items:center;gap:8px 16px;flex-wrap:wrap;margin:-2px 0 12px}
.btn-link{font:inherit;font-size:12.5px;background:none;border:0;padding:0;color:var(--color-accent);cursor:pointer;
  text-decoration:underline;text-underline-offset:3px}
.filter-chip{display:inline-flex;align-items:center;gap:2px;font-size:12px;padding:2px 2px 2px 10px;
  background:var(--color-accent-100);color:var(--color-accent-800)}
.chip-x{font:inherit;font-size:15px;line-height:1;background:none;border:0;padding:0 6px;cursor:pointer;color:inherit}
/* sortable headers: the button is added by the table script, so with JS off a
   header is plain text rather than a control that does nothing */
.th-sort{font:inherit;letter-spacing:inherit;text-transform:inherit;color:inherit;background:none;border:0;
  padding:0;cursor:pointer;white-space:nowrap;text-align:inherit}
.th-sort:hover,.table th[aria-sort=ascending],.table th[aria-sort=descending]{color:var(--color-text)}
.table th[aria-sort=ascending] .th-sort::after{content:" \\2191"}
.table th[aria-sort=descending] .th-sort::after{content:" \\2193"}
/* stack terms are buttons that filter Open roles to the postings behind them */
button.tag{font-family:var(--font-body);background:transparent;cursor:pointer}
button.tag-outline:hover,button.tag-outline[aria-pressed=true]{background:var(--color-accent);color:var(--color-bg)}
.term-btn{font:inherit;color:inherit;background:none;border:0;padding:0;cursor:pointer;text-align:left;
  text-decoration:underline;text-decoration-color:var(--color-divider);text-underline-offset:3px}
.term-btn:hover,.term-btn[aria-pressed=true]{color:var(--color-accent-700);text-decoration-color:currentColor}
.term-btn[aria-pressed=true]{font-weight:600}
.row-hit{margin-top:5px;font-size:12px;line-height:1.45;color:color-mix(in srgb,var(--color-text) 62%,transparent)}
.row-hit mark{background:var(--color-accent-100);color:var(--color-accent-900);padding:0 1px}
.tag-mine{background:var(--color-accent-900);color:#fff}
.my-terms{display:flex;align-items:center;gap:8px 12px;flex-wrap:wrap;margin:0 0 12px;padding:9px 11px;
  border:1px dashed var(--color-divider);font-size:12.5px}
.my-terms-label{font-family:var(--font-heading);font-weight:600;font-size:13px;letter-spacing:.08em;text-transform:uppercase}
.my-terms-list{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.chip{display:inline-flex;align-items:center;border:1px solid var(--color-accent-900);color:var(--color-accent-900)}
.chip-term{font:inherit;font-size:12px;background:none;border:0;padding:2px 4px 2px 9px;cursor:pointer;color:inherit}
.chip-term b{font-weight:600;color:var(--color-accent-700);margin-left:3px}
.chip-term[aria-pressed=true],.chip-term[aria-pressed=true] b{background:var(--color-accent-900);color:#fff}
.my-terms-form{display:flex;gap:6px}
.my-terms-form input{font-family:var(--font-body);font-size:13px;padding:5px 9px;width:210px;max-width:100%;
  background:var(--color-bg);color:var(--color-text);border:1px solid var(--color-divider)}
.my-terms-form input:focus-visible{outline:2px solid var(--color-accent);outline-offset:-1px}
.tbl-empty{margin:0;padding:14px;font-size:12.5px}
.pager{display:flex;align-items:center;gap:12px;margin-top:11px;flex-wrap:wrap}
.arch-table tbody tr[data-arch]{cursor:pointer}
.arch-table .num{text-align:right;font-variant-numeric:tabular-nums}
.arch-dialog{width:min(760px,calc(100vw - 32px));max-height:calc(100vh - 48px);padding:0;
  border:1px solid var(--color-divider);background:var(--color-bg);color:var(--color-text)}
.arch-dialog::backdrop{background:color-mix(in srgb,#1d1f20 45%,transparent)}
.arch-dialog-bar{position:sticky;top:0;display:flex;justify-content:flex-end;padding:10px 12px;
  background:var(--color-bg);border-bottom:1px solid var(--color-divider)}
.arch-dialog-body{padding:16px 22px 22px}
.arch-title{font-size:26px;margin:4px 0 8px}
.arch-fields{display:grid;grid-template-columns:max-content 1fr;gap:6px 18px;margin:16px 0 0;font-size:13px}
.arch-fields dt{color:color-mix(in srgb,var(--color-text) 55%,transparent)}
.arch-fields dd{margin:0;overflow-wrap:anywhere}
.arch-desc{white-space:pre-wrap;font-size:13.5px;line-height:1.6;max-width:72ch}
.arch-desc-sections{max-width:72ch;font-size:13.5px;line-height:1.6}
.arch-sec{font-family:var(--font-heading);font-weight:600;font-size:14px;margin:14px 0 4px}
.arch-sec:first-child{margin-top:0}
.arch-list{margin:0;padding-left:18px}
.arch-para{margin:0}
.pager [role=status]{font-size:12px;font-variant-numeric:tabular-nums}
.pager button[disabled]{opacity:.4;cursor:default}
.site-foot{border-top:1px solid var(--color-divider);margin-top:38px;padding-top:14px;
  display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;font-size:11.5px;line-height:1.5}
@media (max-width:980px){.grid,.grid.narrative{grid-template-columns:1fr}.stack-grid{grid-template-columns:1fr}}
@media (max-width:620px){
  .wrap{padding:0 16px 44px}
  .nav{gap:10px 16px;padding:12px 20px}
  .nav-tabs{order:3;flex:1 0 100%;flex-wrap:nowrap;overflow-x:auto;gap:22px;scrollbar-width:none}
  .nav-tabs a{font-size:16px;white-space:nowrap}
  .nav-auth{gap:14px}
  .tbl-controls input[type=search]{width:100%}
  .my-terms-form{flex:1 0 100%}.my-terms-form input{flex:1;width:auto}
  .hdr h1{font-size:26px!important}
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
  .blueprint{border-color:#bbb}
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


def _history_chart(history: list[dict], chart_id: str = "roles-history") -> str:
    """Open roles over the trailing year — one series, so no legend box: the
    card's own heading names what is plotted.

    Geometry is a fixed viewBox scaled to the card's width. The y-axis is
    deliberately NOT pinned to zero: these are board sizes in the 30s, and a
    zero floor would flatten a year of movement into a hairline. The floor is
    rounded to a labelled multiple of 5 so the truncation is stated on the axis
    rather than hidden."""
    pts = [p for p in history if p.get("total") is not None]
    if len(pts) < 2:
        return _trend_svg(history)   # not enough history to plot a year

    W, H = _CH_W, _CH_H
    L, R, T, B = _CH_L, _CH_R, _CH_T, _CH_B   # margins: y-labels, air, x-band
    pw, ph = W - L - R, H - T - B
    totals = [p["total"] for p in pts]
    lo = (min(totals) // 5) * 5
    hi = -(-max(totals) // 5) * 5
    if hi == lo:
        hi = lo + 5
    x = lambda i: L + (pw * i / (len(pts) - 1))
    y = lambda v: T + ph - ph * (v - lo) / (hi - lo)

    ticks = list(range(lo, hi + 1, 5))
    if len(ticks) > 6:                    # keep the grid recessive, not a ladder
        ticks = ticks[::2] if ticks[-1] in ticks[::2] else ticks[::2] + [ticks[-1]]
    grid = "".join(
        f'<line x1="{L}" y1="{y(t):.1f}" x2="{L + pw}" y2="{y(t):.1f}" '
        f'class="ch-grid"/>'
        f'<text x="{L - 7}" y="{y(t) + 3.5:.1f}" class="ch-tick" text-anchor="end">{t}</text>'
        for t in ticks)

    # Aim for ~7 ticks whatever the crawl cadence. A fixed every-4th-point step
    # is two months apart on a fortnightly lake but only four weeks on a weekly
    # one, which printed "Oct" twice on a 53-point axis.
    step = max(1, round(len(pts) / 7))
    idxs = [i for i in range(0, len(pts), step) if i <= len(pts) - 3]
    xlab, shown_year, last_month = "", None, None
    for i in idxs:
        d = date.fromisoformat(pts[i]["dt"])
        if (d.year, d.month) == last_month:      # never print the same month twice
            continue
        last_month = (d.year, d.month)
        # year-stamp the first tick and the first tick of each new year, so the
        # axis says where 2026 starts even when no tick lands in January
        lab = d.strftime("%b") + (f" &rsquo;{d:%y}" if shown_year != d.year else "")
        shown_year = d.year
        xlab += (f'<text x="{x(i):.1f}" y="{T + ph + 17}" class="ch-tick" '
                 f'text-anchor="middle">{lab}</text>')

    line = " ".join(f"{x(i):.1f},{y(p['total']):.1f}" for i, p in enumerate(pts))
    area = (f'M{L},{T + ph} L' + line.replace(" ", " L") +
            f' L{L + pw},{T + ph} Z')
    last = pts[-1]

    rows = "".join(f'<tr><td>{p["dt"]}</td><td class="num">{p["total"]}</td></tr>' for p in pts)
    span_lbl = f'{pts[0]["dt"]} to {last["dt"]}'

    return f"""<div class="chart-wrap" id="{chart_id}">
<svg viewBox="0 0 {W} {H}" class="ch-svg" role="img"
     aria-label="Open roles from {pts[0]['dt']} to {last['dt']}, {len(pts)} snapshots, ranging {min(totals)} to {max(totals)}.">
  <g>{grid}</g>
  <path d="{area}" class="ch-area"/>
  <polyline points="{line}" class="ch-line"/>
  <line class="ch-hair" x1="0" y1="{T}" x2="0" y2="{T + ph}" style="display:none"/>
  <circle class="ch-hot" r="4.5" cx="0" cy="0" style="display:none"/>
  {xlab}
  <rect x="{L}" y="{T}" width="{pw}" height="{ph}" fill="transparent" class="ch-hit"
        tabindex="0" role="img" aria-label="Open-roles history, interactive. Use the left and right
        arrow keys to step through snapshots. Every figure is also in the table below."/>
</svg>
<div class="chart-tip" hidden></div>
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
    return f"""<script>
(function () {{
  var DATA = {json.dumps(pts)};
  var root = document.getElementById({json.dumps(chart_id)});
  if (!root) return;
  var svg = root.querySelector('.ch-svg'), tip = root.querySelector('.chart-tip'),
      hair = root.querySelector('.ch-hair'), hot = root.querySelector('.ch-hot'),
      hit = root.querySelector('.ch-hit'),
      say = document.getElementById("{chart_id}-readout");
  var L = {_CH_L}, PW = {_CH_W - _CH_L - _CH_R}, T = {_CH_T}, PH = {_CH_H - _CH_T - _CH_B};
  var lo = {(min(p["total"] for p in pts) // 5) * 5}, hi = {-(-max(p["total"] for p in pts) // 5) * 5};
  var i = -1;
  function vx(k) {{ return L + PW * k / (DATA.length - 1); }}
  function vy(v) {{ return T + PH - PH * (v - lo) / (hi - lo || 1); }}
  function fmt(iso) {{
    var d = new Date(iso + 'T00:00:00');
    return d.toLocaleDateString(undefined, {{ day: 'numeric', month: 'short', year: 'numeric' }});
  }}
  function show(k) {{
    if (k < 0 || k >= DATA.length) return;
    i = k;
    var px = vx(k), py = vy(DATA[k].total);
    hair.setAttribute('x1', px); hair.setAttribute('x2', px); hair.style.display = '';
    hot.setAttribute('cx', px); hot.setAttribute('cy', py); hot.style.display = '';
    tip.textContent = '';
    var v = document.createElement('b'); v.textContent = DATA[k].total + ' open';
    var s = document.createElement('span'); s.textContent = fmt(DATA[k].dt);
    tip.appendChild(v); tip.appendChild(s);
    tip.hidden = false;
    // the crosshair is a sighted affordance; mirror it into a live region so the
    // same value is spoken when the chart is stepped through from the keyboard
    if (say) {{ say.textContent = DATA[k].total + ' open roles on ' + fmt(DATA[k].dt); }}
    // clamp inside the card so the readout never hangs off the edge
    var frac = px / {_CH_W}, w = root.clientWidth;
    tip.style.left = Math.max(4, Math.min(w - tip.offsetWidth - 4, frac * w - tip.offsetWidth / 2)) + 'px';
  }}
  function hide() {{
    tip.hidden = true; hair.style.display = 'none'; hot.style.display = 'none'; i = -1;
    if (say) {{ say.textContent = ''; }}
  }}
  function nearest(evt) {{
    var r = svg.getBoundingClientRect();
    var vxp = (evt.clientX - r.left) / r.width * {_CH_W};
    return Math.max(0, Math.min(DATA.length - 1,
      Math.round((vxp - L) / PW * (DATA.length - 1))));
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
    return (f'<svg viewBox="0 0 240 60" class="spark" role="img" '
            f'aria-label="{html.escape(label)}">'
            f'<line x1="0" y1="56" x2="240" y2="56" stroke="var(--color-divider)"/>'
            f'<polyline points="{line}" fill="none" stroke="var(--color-accent)" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{pts_xy[0][0]:.1f}" cy="{pts_xy[0][1]:.1f}" r="3" '
            f'fill="var(--color-accent)" opacity=".55"/>'
            f'<circle cx="{pts_xy[-1][0]:.1f}" cy="{pts_xy[-1][1]:.1f}" r="3.5" '
            f'fill="var(--color-accent)"/>'
            f'</svg>')


def _corners() -> str:
    return '<i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>'


_NAV_MARK = ('<svg viewBox="0 0 32 32" aria-hidden="true" focusable="false">'
             '<path d="M2 19h6.5l4-12 4.5 17 3.5-10H30" fill="none" stroke="currentColor" '
             'stroke-width="3" stroke-linejoin="miter" stroke-linecap="square"/></svg>')


def _nav(active: str, account: str = "", signed_in: bool = True) -> str:
    """The menu bar of infra/site-static/demo/, with the app's links: account
    pages get that account's Dashboard and Brief, and the right side says Sign
    out where the demo says Log in / Get started. The brand goes to the
    accounts home, not to "/": a signed-in viewer has no business on the
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
    auth = ('<a class="nav-login" href="/auth/logout">Sign out</a>' if signed_in
            else '<a class="nav-login" href="/login">Log in</a>')
    brand = APP_HOME if signed_in else "/"
    return (f'<div class="nav"><a class="nav-brand" href="{brand}" style="text-decoration:none">'
            f'{_NAV_MARK}<span>TICK</span></a><div class="nav-tabs">{"".join(tabs)}</div>\n'
            f'  <div class="nav-auth">\n    {auth}\n  </div>\n</div>')


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
_MY_TERMS_MAX, _MY_TERM_CHARS = 30, 60


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
    # one span for the text: .chk is a flex row, so a bare count would get its own gap
    return (f'<label class="chk"{" hidden" if hidden else ""}><input type="checkbox" data-flag="{flag}">'
            f'<span>{label} (<span data-count>{count}</span>)</span></label>')


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
            f'<span class="text-muted" style="font-size:11.5px">Kept in this browser. Every dashboard '
            f'tags the roles whose title or description mentions one.</span></div>')


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

  function loadMine() {
    try {
      var v = JSON.parse(localStorage.getItem(STORE) || '[]');
      return Array.isArray(v) ? v.filter(function (t) { return typeof t === 'string' && t.trim(); })
                                 .slice(0, MAX_TERMS) : [];
    } catch (e) { return []; }
  }
  function storeMine() { try { localStorage.setItem(STORE, JSON.stringify(mine)); } catch (e) {} }
  var mine = loadMine();

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
      mentions: function (t) {
        return rows.filter(function (r) { return r._mine.indexOf(t) !== -1; }).length;
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
      var n = roles.mentions(t);
      var chip = document.createElement('span'), b = document.createElement('button');
      var x = document.createElement('button'), num = document.createElement('b');
      chip.className = 'chip';
      b.type = x.type = 'button';
      b.className = 'chip-term';
      b.setAttribute('data-my-term', t);
      b.title = n + ' open role' + (n === 1 ? '' : 's') + ' mention ' + t + '. Select to show only those.';
      b.appendChild(document.createTextNode(t));
      num.textContent = n;
      b.appendChild(num);
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

  function changed() {
    storeMine();
    tables.forEach(function (t) { t.refreshMine(); t.apply(true); });
    draw();
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    input.value.split(',').forEach(function (raw) {
      var t = raw.replace(/\s+/g, ' ').trim().slice(0, TERM_CHARS);
      if (t && mine.length < MAX_TERMS && !mine.some(function (m) { return same(m, t); })) mine.push(t);
    });
    input.value = '';
    changed();
  });
  // the list was changed in another tab: follow it
  window.addEventListener('storage', function (e) {
    if (e.key !== STORE) return;
    mine = loadMine();
    tables.forEach(function (t) { t.refreshMine(); t.apply(false); });
    draw();
  });
  draw();
})();
</script>"""


def _tables_script() -> str:
    """The script behind both dashboard tables and My terms; see _TABLES_JS."""
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
        return f'<sup class="cite"> {len(self.items)}</sup>'

    def render_list(self) -> str:
        rows = []
        for c in self.items:
            body = (f'<a href="{html.escape(c["url"])}" target="_blank" rel="noopener">'
                    f'{html.escape(c["label"])}</a>') if c["url"] else html.escape(c["label"])
            rows.append(f'<div style="display:flex;gap:9px"><span class="cite" style="width:16px;flex:none">{c["n"]}</span><span>{body}</span></div>')
        return "".join(rows)


# ---------------------------------------------------------------- dashboard page

def _term_bucket_card(title: str, tag_class: str, rows: list[dict], footer_fn) -> str:
    body = []
    for r in rows[:8]:
        count = f'{r["curr_n"]} specs' if r["curr_n"] else f'was {r["prev_n"]}'
        if r.get("since"):
            count += f' &middot; since {date.fromisoformat(r["since"]):%b &rsquo;%y}'
        term = html.escape(r["term"])
        # a term still on the board filters Open roles to the postings behind it
        label = (f'<button type="button" class="term-btn" data-stack-term="{term}" '
                 f'title="Show the open roles that mention {term}">{term}</button>'
                 if r["curr_n"] else f'<span>{term}</span>')
        body.append(f'<div class="stack-row">{label}'
                    f'<span class="text-muted" style="font-size:11.5px">{count}</span></div>')
    if not rows:
        body.append('<p class="text-muted" style="margin:0;font-size:12.5px">None this period.</p>')
    footer = footer_fn(rows[0]) if rows else ""
    return (f'<div class="card blueprint">{_corners()}'
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">'
            f'<span style="font-family:var(--font-heading);font-weight:600;font-size:14px;'
            f'letter-spacing:.06em;text-transform:uppercase">{title}</span>'
            f'<span class="tag {tag_class}">{len(rows)}</span></div>'
            f'<div style="display:flex;flex-direction:column;gap:8px">{"".join(body)}</div>'
            f'{f"<p style=\"margin:11px 0 0;font-size:11.5px;line-height:1.5\" class=\"text-muted\">{footer}</p>" if footer else ""}'
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
                f'{html.escape(r["url"])}</a><br><span class="text-muted" style="font-size:11.5px">'
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
        desc = ('<p class="text-muted" style="margin:0;font-size:12.5px">The board served no '
                'description for this req on its last crawl.</p>')
    else:
        desc = (f'<p class="text-muted" style="margin:0;font-size:12.5px">No curated crawl for '
                f'{html.escape(r["last_seen"] or "its last sighting")} is in the lake, so only the '
                f'title, location and dates tracked in state survive for this req.</p>')
    return (f'<p class="eyebrow text-muted" style="margin:0">Archived posting</p>'
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
            f'<h3 class="section-label text-muted" style="margin:18px 0 8px">Description</h3>{desc}')


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
    note = (f'<p class="text-muted" style="margin:10px 0 0;font-size:11.5px">{" ".join(notes)}</p>'
            if notes else "")
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
  <div class="crumb"><a href="{APP_HOME}">Accounts</a><span>/</span><a href="{dash}">{html.escape(company_name)}</a><span>/</span><a href="{dash}#archive">Archive</a><span>/</span><span style="color:var(--color-text)">{html.escape(r["pid"])}</span></div>
  <main id="main">
  {_archive_record(r, heading="h1")}
  <p style="margin:26px 0 0"><a class="btn btn-secondary btn-sm" href="{dash}#archive">&larr; Back to the archive</a></p>
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

    if first:
        # With nothing to compare against, categorize_terms files every term as
        # adopting. That is a window artefact, not a finding, so the first crawl
        # shows one unlabelled card of what the board mentions. Its first-seen
        # date is only the day collection started, so it is left off.
        stack_cards = _term_bucket_card(
            "On the board", "tag-neutral", [{**r, "since": None} for r in buckets["adopting"]],
            lambda r: (f'Present on the {curr_dt} board. A term is split into Adopting or '
                       f'Established once there is an earlier crawl to date it from.'))
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
        stack_cards = (_term_bucket_card("Adopting", "tag-accent", buckets["adopting"],
                                         lambda r: adopt_note)
                       + _term_bucket_card("Established", "tag-neutral", buckets["established"],
                                           lambda r: estab_note))

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
        terms = "".join(f'<button type="button" class="tag tag-outline" data-stack-term="{html.escape(t)}" '
                        f'style="margin:0 4px 4px 0">{html.escape(t)}</button>'
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
                f'<td class="text-muted" style="white-space:nowrap">{seen}</td>'
                f'<td class="text-muted" style="white-space:nowrap">{pay}</td>'
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
                f'<td class="text-muted" style="white-space:nowrap">{html.escape(r["closed_on"])}</td>'
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
    tech_new_html = "".join(
        f'<div style="padding-bottom:11px;border-bottom:1px solid color-mix(in srgb,var(--color-text) 8%,transparent)">'
        f'<div style="font-size:13.5px;font-weight:500"><a href="{html.escape(r["url"])}" target="_blank" rel="noopener">{html.escape(r["title"])}</a></div>'
        f'<div style="font-size:12px" class="text-muted">{html.escape(r["location"])}</div></div>'
        for r in tech_shown) or '<p class="text-muted" style="margin:0;font-size:12.5px">None this period.</p>'
    if len(tech_rows) > len(tech_shown):
        tech_new_html += (f'<p class="text-muted" style="margin:0;font-size:12.5px">'
                          f'{len(tech_rows) - len(tech_shown)} more in Open roles.</p>')

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

    if first:
        head_desc = (f"Hiring signals for {company_name}: {curr['postings_total']} open postings "
                     f"captured {curr_dt}, the first crawl of this board.")
        lede = (f"Built from {curr['postings_total']} open postings captured {curr_dt}, the first crawl "
                f"of this board. Change figures start with the next crawl. Every row links back to "
                f"the posting it came from.")
        change_row = (f'<span class="text-muted">First crawl {curr_dt}</span>'
                      f'<span>{curr["postings_total"]} open</span>')
        flow = {k: "&mdash;" for k in ("new", "closed", "reopened")}
        window_label = f"First crawl {curr_dt}"
    else:
        head_desc = (f"Hiring signals for {company_name}: {curr['postings_total']} open postings "
                     f"captured {curr_dt}, compared with {prev_dt}.")
        lede = (f"Built from {curr['postings_total']} open postings captured {curr_dt},\n"
                f"        measured against the crawl {window_days} days earlier ({prev_dt}). Every row "
                f"links back to the posting it came from.")
        change_row = (f'<span class="text-muted">{prev_dt} &rarr; {curr_dt}</span>\n'
                      f'          <span>{prev["postings_total"]} &rarr; {curr["postings_total"]}\n'
                      f'            <b style="color:{"var(--color-accent-700)" if delta >= 0 else "inherit"}">'
                      f'{delta:+d} ({pct:+.1f}%)</b></span>')
        flow = {k: counts.get(k, "?") for k in ("new", "closed", "reopened")}
        window_label = f"{prev_dt} &ndash; {curr_dt}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{_head(f"{company_name} — TICK dashboard", head_desc)}
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
{_nav("dashboard", company)}
<div class="wrap">
  <div class="crumb"><a href="{APP_HOME}">Accounts</a><span>/</span><span>{html.escape(company_name)}</span><span>/</span><span style="color:var(--color-text)">Dashboard</span></div>
  <main id="main">
  <div class="hdr">
    <div><p class="eyebrow text-muted">{html.escape(company_name)}</p><h1 style="font-size:34px;margin:3px 0">Dashboard</h1>
      <p class="text-muted" style="margin:6px 0 0;font-size:13px;max-width:70ch">{lede}</p></div>
    <button class="btn btn-primary" type="button" id="copy-brief">Copy summary</button>
  </div>

  <div class="grid">
    <div style="display:flex;flex-direction:column;gap:24px">
      <div>
        <h2 class="section-label text-muted" style="margin-bottom:12px">Tech stack, as evidenced by hiring</h2>
        <div class="stack-grid">{stack_cards}</div>
      </div>
      <div>
        <div class="tbl-head" id="open-roles-head">
          <h2 class="section-label text-muted" style="margin:0">Open roles &middot; <span id="row-count">{len(open_rows)}</span><span id="row-total" hidden> of {len(open_rows)}</span></h2>
          <div class="tbl-controls">
            <input id="role-filter" type="search" placeholder="Title, location or description" aria-label="Search open roles by title, location or description">
          </div>
        </div>
        {role_filters}
        {_my_terms_strip()}
        <div class="blueprint" style="overflow-x:auto">
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
        <p class="text-muted" style="margin:10px 0 0;font-size:11.5px;line-height:1.5">{recency_note}</p>
      </div>
      <div id="archive">
        <div class="tbl-head">
          <h2 class="section-label text-muted" style="margin:0">Archive &middot; <span id="archive-count">{len(archive_rows)}</span><span id="archive-total" hidden> of {len(archive_rows)}</span> closed</h2>
          <div class="tbl-controls">
            <input id="archive-filter" type="search" placeholder="Title, location, team or description" aria-label="Search archived roles by title, location, team or description">
          </div>
        </div>
        {archive_filters}
        <div class="blueprint" style="overflow-x:auto">
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
        <p class="text-muted" style="margin:10px 0 0;font-size:11.5px;line-height:1.5">Every req that has
          disappeared from the board since collection began. Closed is the first crawl it was missing from;
          details are from the last crawl it appeared on. A req that comes back moves to Open roles.</p>
      </div>
    </div>

    <div class="right-col">
      <div class="card blueprint">{_corners()}
        <span style="font-size:10px;letter-spacing:.1em;text-transform:uppercase" class="text-muted">Open roles &mdash; {hist_span}</span>
        {history_chart}
        <div style="display:flex;justify-content:space-between;font-size:12.5px;padding-top:9px;border-top:1px solid var(--color-divider)">
          {change_row}
        </div>
        <div class="stat-row">
          <div class="stat"><b>{flow['new']}</b><span>Opened</span></div>
          <div class="stat"><b>{flow['closed']}</b><span>Closed</span></div>
          <div class="stat"><b>{flow['reopened']}</b><span>Reopened</span></div>
        </div>
      </div>
      <div class="card blueprint">{_corners()}
        <h2 style="margin:0 0 3px;font-size:20px">{tech_title}</h2>
        <span style="font-size:11.5px;display:block;margin-bottom:12px" class="text-muted">{tech_sub}</span>
        <div style="display:flex;flex-direction:column;gap:11px">{tech_new_html}</div>
      </div>
      <div class="card blueprint">{_corners()}
        <h2 style="margin:0 0 10px;font-size:20px">Source coverage</h2>
        <div style="display:flex;flex-direction:column;gap:8px;font-size:13px">
          <div style="display:flex;justify-content:space-between"><span class="text-muted">Postings analyzed</span><span>{curr['postings_total']}</span></div>
          <div style="display:flex;justify-content:space-between"><span class="text-muted">Window</span><span>{window_label}</span></div>
          <div style="display:flex;justify-content:space-between"><span class="text-muted">Last crawl</span><span>{fmt_dt(curr['captured_at'])}</span></div>
          <div style="display:flex;justify-content:space-between"><span class="text-muted">Board</span><span><a href="https://{html.escape(domain)}" target="_blank" rel="noopener">{html.escape(domain)}</a></span></div>
        </div>
      </div>
    </div>
  </div>
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
    meta = " &middot; ".join(html.escape(str(x)) for x in [
        a["ats"] or "ats unknown", f'tier {a["tier"]}' if a["tier"] else None, a["board"]] if x)
    # the page's one real distinction — an account with pages to open, or not —
    # so the accent is spent there and nowhere else
    tag_class = "tag-solid" if a["has_pages"] else "tag-neutral"

    if a["open_roles"] is not None:
        if a["delta"] is None:
            delta_val, delta_lbl = "&mdash;", "no prior crawl"
        else:
            delta_val, delta_lbl = f'{a["delta"]:+d}', f'vs {a["prev_dt"]}'
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
                f'<span class="text-muted" style="margin-left:auto;font-size:11.5px">'
                f'Crawled {a["curr_dt"]}</span>')
        note = ""
    else:
        foot = (f'<a class="btn btn-secondary btn-sm" href="{html.escape(a["careers_url"])}">Careers board</a>'
                if a["careers_url"] else "")
        if a["status"] == _REVIEW:
            conf = f' (confidence {a["confidence"]})' if a["confidence"] is not None else ""
            note = f'Config flagged for human review{conf} — not collecting yet.'
        else:
            note = 'Config verified. Nothing in the lake for this account yet.'
        note = f'<p class="acct-note text-muted">{note}</p>'

    return (f'<div class="card blueprint{" is-reporting" if a["has_pages"] else ""}">{_corners()}'
            f'<div class="acct-head"><div>'
            f'<h2 class="acct-name">{html.escape(a["name"])}</h2>'
            f'<div class="acct-meta text-muted">{meta}</div></div>'
            f'<span class="tag {tag_class}" style="white-space:nowrap">{a["status"]}</span></div>'
            f'{stats}{note}'
            f'{f'<div class="acct-foot">{foot}</div>' if foot else ""}'
            f'</div>')


def _add_account_card() -> str:
    """Trailing tile in the account grid: any viewer can ask for a company the
    lake does not track yet. The form posts to /api/account-request, which
    records the request against the viewer's session and alerts an admin, who
    onboards it with the evaluator and assigns it to the viewer."""
    return (
        '<form class="card blueprint acct-add" id="acct-form" novalidate>' + _corners() +
        '<div class="acct-head"><div>'
        '<h2 class="acct-name">Track another company</h2>'
        '<div class="acct-meta text-muted">request &middot; reviewed by an administrator</div>'
        '</div><span class="tag tag-neutral" style="white-space:nowrap">Not tracked</span></div>'
        '<p class="acct-note text-muted" style="margin-top:11px">Tell us which company to track. Once '
        'it&rsquo;s set up and crawled, it appears here with a dashboard.</p>'
        '<label for="acct-name-in">Company name</label>'
        '<input id="acct-name-in" name="company" type="text" placeholder="Acme Industrial Supply" '
        'autocomplete="organization" spellcheck="false" maxlength="120" required>'
        '<label for="acct-url-in">Careers URL <span style="opacity:.65">(optional)</span></label>'
        '<input id="acct-url-in" name="careers_url" type="url" placeholder="https://careers.example.com" '
        'inputmode="url" autocomplete="url" spellcheck="false" maxlength="500">'
        '<label for="acct-note-in">Notes <span style="opacity:.65">(optional)</span></label>'
        '<textarea id="acct-note-in" name="note" rows="2" maxlength="1000" '
        'placeholder="Why this company..."></textarea>'
        '<div class="acct-foot">'
        '<button class="btn btn-primary btn-sm" id="acct-submit" type="submit">Request account</button>'
        '<span class="acct-status text-muted" id="acct-status" role="status" aria-live="polite"></span>'
        '</div></form>')


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

    sub = (f'{len(accounts)} account{"s" if len(accounts) != 1 else ""} &middot; '
           f'<b>{len(reporting)} reporting</b> &middot; <b>{tracked}</b> open postings tracked'
           f'{f" &middot; last crawl {fmt_dt(last)}" if last else ""}')

    cards = "".join(_account_card(a) for a in accounts)
    if not accounts:
        cards = ('<div class="card blueprint">' + _corners() +
                 '<h2 class="acct-name">No accounts yet</h2>'
                 '<p class="acct-note text-muted" style="margin-top:11px">No accounts have been '
                 'assigned to you yet. Request one alongside; once it&rsquo;s set up it appears '
                 'here the next time you sign in.</p></div>')
    cards += _add_account_card()
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
  <div class="crumb"><span style="color:var(--color-text)">Accounts</span></div>
  <main id="main">
  <div class="hdr">
    <div><p class="eyebrow text-muted">TICK</p><h1 style="font-size:34px;margin:3px 0">Accounts</h1>
      <p class="text-muted acct-lede" style="margin:6px 0 0;font-size:13px">{sub}</p></div>
  </div>
  <div class="acct-grid">{cards}</div>
  </main>
  {_footer(last_crawl_label)}
</div>
{_add_account_script()}
</body>
</html>
"""


# ---------------------------------------------------------------- AI brief page

_BRIEF_RE = re.compile(r"(?:^|/)briefs/company=([^/]+)/week=(\d{4}-\d{2}-\d{2})/(brief|evidence)\.json$")


def load_brief(lake_io: dict, company: str) -> tuple[dict, dict] | None:
    """(brief, evidence) for the newest week the lake holds both files for,
    or None. Briefs live at briefs/company=<slug>/week=<window end>/ and get
    there only through the tick-brief skill's publish.py, which uploads a
    brief only after check.py passes it — so a brief here is already
    verified, and nothing on this side re-writes or repairs one."""
    weeks: dict[str, dict[str, str]] = {}
    for k in lake_io["keys"]:
        m = _BRIEF_RE.search(k.replace(os.sep, "/"))
        if m and m.group(1) == company:
            weeks.setdefault(m.group(2), {})[m.group(3)] = k
    for week in sorted(weeks, reverse=True):
        files = weeks[week]
        if "brief" in files and "evidence" in files:
            return lake_io["get"](files["brief"]), lake_io["get"](files["evidence"])
    return None


def render_account_brief(lake_io: dict, lake: dict, company: str, company_name: str) -> str:
    """An account's brief.html: its newest uploaded brief, else the page
    saying there is none yet. The one entry point for the CLI and the
    publisher, so the two cannot disagree about which brief is current."""
    found = load_brief(lake_io, company)
    if found:
        return render_ai_brief(*found)
    return render_no_brief(company, company_name, fmt_dt(lake["curr"]["captured_at"]))


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
  <div class="crumb"><a href="{APP_HOME}">Accounts</a><span>/</span><span>{html.escape(company_name)}</span><span>/</span><span style="color:var(--color-text)">Brief</span></div>
  <main id="main">
  <div class="hdr" style="border:0">
    <div><p class="eyebrow text-muted">{html.escape(company_name)}</p><h1 style="font-size:30px;margin:3px 0">No brief yet</h1></div>
  </div>
  <p style="margin:6px 0 0;max-width:62ch">Briefs are written weekly from the movement on this
  account's job board, and every claim in one cites the posting it rests on.
  This account's first brief has not been published. Until it is, the
  <a href="/{company}/dashboard.html">dashboard</a> has everything the board shows today.</p>
  </main>
  {_footer(generated)}
</div>
</body>
</html>
"""


_AI_CITE_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


def render_ai_brief(brief: dict, evidence: dict) -> str:
    """The weekly brief a model wrote (see .claude/skills/tick-brief). The model's text is escaped like any other
    untrusted input; its [[id]] citations become numbered sources resolved
    against the evidence pack it was written from."""
    company_name = evidence["company"]["name"]
    window, board = evidence["window"], evidence["board"]
    postings: dict[str, dict] = {}
    for sec in ("opened", "closed", "reopened"):
        for p in evidence.get(sec, []):
            postings.setdefault(p["id"], p)
    for key in ("open_leadership_roles", "longest_open"):
        for p in board.get(key, []):
            postings.setdefault(p["id"], p)
    persona = evidence.get("persona") or {}
    for p in persona.get("standing_roles", []):
        postings.setdefault(p["id"], p)
    # who the brief was written for: the persona of the user who tracks the
    # account (see tools/persona.py), or nobody named for the default reader
    reader = ", ".join(x for x in (persona.get("role"), persona.get("sells_for")) if x)

    cx = Citations()
    numbered: dict[str, int] = {}

    def label(cid: str) -> tuple[str, str | None]:
        if cid == "BOARD":
            return (f'Board {window["start"]} → {window["end"]}: {board["open_at_start"]} → '
                    f'{board["open_at_end"]} open; {board["opened"]} opened, {board["closed"]} '
                    f'closed, {board["reopened"]} reopened.', None)
        if cid == "STACK":
            return "Stack terms across the open board.", None
        if cid == "PERSONA":
            return f"The reader's terms on the board ({reader or 'persona'}).", None
        p = postings.get(cid)
        if not p:
            return f"{cid} — not in the evidence pack.", None
        what = (f'closed {p["closed_on"]} after {p.get("days_open")} days' if p.get("closed_on")
                else f'opened {p["opened_on"]}' if p.get("opened_on")
                else f'reopened {p["reopened_on"]}' if p.get("reopened_on")
                else f'open {p.get("days_open")} days')
        where = f' ({p["location"]})' if p.get("location") else ""
        # a closed req's board URL now answers "role not found"; its archive page does not
        url = (archive_href(evidence["company"]["slug"], cid) if p.get("closed_on")
               else p.get("url"))
        return f'{cid} — {p["title"]}{where}, {what}.', url

    def sup(m: re.Match) -> str:
        cid = html.unescape(m.group(1)).strip()
        if cid not in numbered:
            cx.cite(*label(cid))
            numbered[cid] = len(cx.items)
        return f'<sup class="cite"> {numbered[cid]}</sup>'

    def rich(text: str) -> str:
        return _AI_CITE_RE.sub(sup, html.escape(text or ""))

    def plain(text: str) -> str:
        return _AI_CITE_RE.sub("", text or "").replace("  ", " ").strip()

    # No headline: a one-line title over the first paragraph only restated it.
    paras = "".join(f"<p>{rich(p)}</p>" for p in brief.get("paragraphs", []))

    act_html = []
    for a in brief.get("action_items") or []:
        quote = ""
        q = a.get("quote")
        if q and q.get("text"):
            quote = (f'<p style="margin:9px 0 0;font-size:12.5px;line-height:1.55">'
                     f'<span class="text-muted">In their words &mdash; </span>'
                     f'<span style="font-style:italic">&ldquo;{html.escape(q["text"])}&rdquo;</span>'
                     f'{rich(f"[[{q.get("source", "")}]]")}</p>')
        steps = "".join(f'<li style="margin-top:6px">{rich(s)}</li>' for s in a.get("steps", []))
        kind = a.get("kind", "")
        act_html.append(
            f'<div class="card blueprint" style="padding:14px 16px;margin-bottom:12px">{_corners()}'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px">'
            f'<div style="font-size:14.5px;font-weight:600;font-family:var(--font-heading)">'
            f'{html.escape(a.get("title", ""))}</div>'
            f'<span class="tag tag-{"solid" if kind == "outreach" else "neutral"} tag-flag">'
            f'{html.escape(kind)}</span></div>'
            f'<p class="text-muted" style="margin:5px 0 0;font-size:12.5px">{rich(a.get("why", ""))}</p>'
            f'{quote}'
            f'<ul style="margin:9px 0 0;padding-left:17px;font-size:12.5px;line-height:1.55">{steps}</ul></div>')
    actions_html = "".join(act_html) or (
        '<p class="text-muted" style="margin:0;font-size:12.5px">'
        'Nothing moved this week that warrants an approach.</p>')

    weekly = board.get("weekly_open_totals") or []
    sparkline = _trend_svg(weekly)
    spark_span = f'{weekly[0]["dt"]} &rarr; {weekly[-1]["dt"]}' if weekly else ""
    delta = board.get("net_change") or 0
    moves = [("Opened", board["opened"]), ("Closed", board["closed"]), ("Reopened", board["reopened"])]
    moves_html = "".join(f'<div style="display:flex;justify-content:space-between"><span>{k}</span>'
                         f'<b>{v}</b></div>' for k, v in moves)

    brief_text = "\n\n".join(
        [f"{company_name} — account brief, week to {window['end']}"]
        + [plain(p) for p in brief.get("paragraphs", [])]
        + (["Action items:"] + [f'- {a.get("title", "")} — {plain(a.get("why", ""))}'
                                + "".join(f"\n    * {plain(s)}" for s in a.get("steps", []))
                                for a in brief["action_items"]] if brief.get("action_items") else []))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{_head(f"{company_name} — TICK brief",
       f"What moved on {company_name}'s job board in the week to {window['end']}, and what to do about it.")}
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
{_nav("brief", evidence["company"]["slug"])}
<div class="wrap narrow">
  <div class="crumb"><a href="{APP_HOME}">Accounts</a><span>/</span><span>{html.escape(company_name)}</span><span>/</span><span style="color:var(--color-text)">Brief</span></div>
  <main id="main">
  <div class="hdr">
    <div><p class="eyebrow text-muted">{html.escape(company_name)}</p><h1 style="font-size:30px;margin:3px 0">Account brief</h1>
      <p class="text-muted" style="margin:6px 0 0;font-size:13px">Week to {window['end']} &middot; every claim carries a citation{f" &middot; written for {html.escape(reader)}" if reader else ""}</p></div>
    <button class="btn btn-primary" type="button" id="copy-brief">Copy brief</button>
  </div>
  <div class="grid narrative">
    <div class="prose">
      {paras}
      <div style="margin-top:26px">
        <h2 class="section-label text-muted" style="margin-bottom:12px">Action items</h2>
        {actions_html}
      </div>
      <div style="margin-top:24px;padding-top:16px;border-top:1px solid var(--color-divider)">
        <h2 class="section-label text-muted" style="margin-bottom:10px">Sources &middot; {len(cx.items)} citations</h2>
        <div style="display:flex;flex-direction:column;gap:7px;font-size:12.5px;line-height:1.5">{cx.render_list()}</div>
      </div>
    </div>
    <div class="right-col">
      <div class="card blueprint">{_corners()}
        <span style="font-size:10px;letter-spacing:.1em;text-transform:uppercase" class="text-muted">Open roles, {spark_span}</span>
        {sparkline}
        <div style="display:flex;justify-content:space-between;gap:10px;font-size:12.5px;flex-wrap:wrap;
                    padding-top:9px;border-top:1px solid var(--color-divider)">
          <span class="text-muted">{window['start']} &rarr; {window['end']}</span>
          <span>{board['open_at_start']} &rarr; {board['open_at_end']}
            <b style="color:{'var(--color-accent-700)' if delta >= 0 else 'inherit'}">{delta:+d}</b></span>
        </div>
      </div>
      <div class="card blueprint">{_corners()}
        <span style="font-size:10px;letter-spacing:.1em;text-transform:uppercase;display:block;margin-bottom:9px" class="text-muted">This week</span>
        <div style="display:flex;flex-direction:column;gap:6px;font-size:12.5px">{moves_html}</div>
      </div>
      <div class="card blueprint">{_corners()}
        <span style="font-size:10px;letter-spacing:.1em;text-transform:uppercase;display:block;margin-bottom:9px" class="text-muted">Generated</span>
        <p style="margin:0;font-size:12.5px;line-height:1.55" class="text-muted">Written from {window['crawl_days']} daily
        crawls of {html.escape(evidence['company'].get('board_host') or '?')}, prompt v{html.escape(str(brief.get('prompt_version', '?')))}.
        Every claim above cites the posting or board count it rests on.</p>
      </div>
    </div>
  </div>
  </main>
  {_footer(window['end'], evidence['company'].get('board_host'))}
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
  <div class="hdr" style="border:0">
    <div><p class="eyebrow text-muted">Error 404</p><h1 style="font-size:34px;margin:3px 0">Page not found</h1>
      <p class="text-muted" style="margin:8px 0 0;font-size:14px;max-width:60ch">That address is not part of this
        site. Account pages are only written for companies TICK has crawled, so a dashboard or brief
        you had bookmarked can disappear if the account stopped reporting.</p></div>
  </div>
  <p style="margin:22px 0 0"><a class="btn btn-primary" href="{APP_HOME}">Back to accounts</a></p>
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
                    help="directory to write accounts/index.html and <slug>/ into")
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
        brief_path = os.path.join(acct_dir, "brief.html")
        with open(dash_path, "w") as f:
            f.write(render_dashboard(lake, a["slug"]))
        with open(brief_path, "w") as f:
            f.write(render_account_brief(lake_io, lake, a["slug"], a["name"]))
        print(f"wrote {dash_path}")
        print(f"wrote {brief_path}")
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
