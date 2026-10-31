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
Dunder Mifflin swings +12/-7 between consecutive days while drifting far less — so the
page compares the latest snapshot against the most recent one at least that
old, and plots every snapshot in between. Where history is shorter than the
window it compares against the oldest snapshot there is and labels the real
dates, so a short window reads as itself.
  - Brief — per account, a narrative summary with numbered citations, each
    pointing at the posting (or snapshot) it was derived from.
  - Archive — per closed req, its last recorded description and dates, at a
    stable address the brief can cite once the board's own link is dead.

Layout written under --out-dir:

    login/index.html              the accounts home, served at /login
    <company-slug>/dashboard.html
    <company-slug>/brief.html
    <company-slug>/archive/<posting-id>/index.html
                                  one per closed req, served at
                                  /<company-slug>/archive/<posting-id>

"/" , "/docs" and "/demo" are NOT written here — they are the hand-authored
public pages in infra/site-static/, deployed separately. This renderer
only ever writes pages that are behind the session check.

No fabricated narrative — every claim in both pages traces back to a real
posting, snapshot count, or diff. Where the lake can't support a claim
(motivations, budget ownership, etc.), it's left out rather than invented.

Usage:
    python tick_insights_report.py <lake-root-dir> [--company SLUG] [--out-dir DIR]
    python tick_insights_report.py s3://bucket [--company SLUG] [--out-dir DIR]

With no --company, every company in the lake with two snapshots is rendered.

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
APP_HOME = "/login"

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
    overnight change, which on a daily board is mostly noise — Dunder Mifflin swings
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
        keys = [os.path.join(dp, f) for dp, _, files in os.walk(source) for f in files]
        get = lambda k: json.load(open(k))
        get_text = lambda k: open(k).read()
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
        for pid, st in closed_ids.items():
            if st.get("last_seen") == dt and pid in loaded[dt]:
                archive[pid] = loaded[dt][pid]
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
    stack term (23 of 137 for Dunder Mifflin), so it cannot answer "all open roles".
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

    rows = []
    for pid, rec in source.items():
        st = state.get(pid, {})
        sp = stack_postings.get(pid, {})
        fs = st.get("first_seen") or rec.get("first_seen")
        rows.append({
            "pid": pid,
            "title": rec.get("title") or sp.get("title") or pid,
            "location": rec.get("location") or sp.get("location") or "",
            "url": rec.get("url") or sp.get("url") or "",
            "is_tech_role": bool(rec.get("is_tech_role", sp.get("is_tech_role", pid in tech_ids))),
            "terms": sorted(set(term_by_pid.get(pid, []))),
            "first_seen": fs,
            "is_new": opening_window(dts, curr_dt, fs, days),
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


def leadership_postings(new_rows: list[dict]) -> list[dict]:
    """New-since-last-crawl postings whose title reads as director/leadership level."""
    return [r for r in new_rows if _LEADERSHIP_RE.search(r["title"])]


_REQ_RE = re.compile(r"(?i)\b(requirements?|qualifications?)\b\s*[:.]?\s*(.+)")


def hiring_profile(rec: dict | None, max_len: int = 260) -> str | None:
    """The candidate profile a req was written against, lifted from its own
    'Requirements:' sentence.

    This is the closest TICK can honestly get to "who did they hire": the lake
    holds postings, never people. It says what the seat was advertised for, and
    the brief has to label it that way rather than as a named individual."""
    text = (rec or {}).get("description_raw") or ""
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
            ex = pick_excerpt(rec.get("description_raw", ""),
                              flat_terms(rec.get("keywords") or {}), boiler)
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


_NUMBER_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven",
                 "eight", "nine", "ten")


def _num(n: int, cap: bool = False) -> str:
    """Small counts read better spelled out, especially at the start of a sentence."""
    w = _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)
    return w[0].upper() + w[1:] if cap and w[0].isalpha() else w


def _join_semi(items: list[str]) -> str:
    """Join clauses that already contain commas, so the sentence stays parseable."""
    if len(items) <= 1:
        return items[0] if items else ""
    return "; ".join(items[:-1]) + f"; and {items[-1]}"


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
            "description": rec.get("description_raw"),
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


def leadership_closed(rows: list[dict]) -> list[dict]:
    """Closed reqs whose title reads as leadership level."""
    return [r for r in rows if _LEADERSHIP_RE.search(r["title"])]


# Initiative themes: the taxonomy is organised for matching, not for reading, so
# terms are regrouped into the handful of programmes a reader actually cares
# about. Specific terms win over their category (sagemaker is AI work, not AWS).
_THEME_TERMS = {
    "AI and machine learning": {
        "machine learning", "generative ai", "genai", "llm", "sagemaker",
        "bedrock", "vertex ai",
    },
    "legacy modernization": {
        "migration", "modernization", "re-architect", "digital transformation",
        "greenfield", "cloud-native",
    },
}
_THEME_CATS = {
    "cloud platform": {"cloud_aws", "cloud_azure", "cloud_gcp", "infra_devops"},
    "data platform": {"data_platform", "analytics_tools"},
}
_THEME_ORDER = ["cloud platform", "data platform", "AI and machine learning",
                "legacy modernization"]


def initiative_themes(curr_stack: dict, cutoff: str | None = None,
                      first_seen: dict | None = None) -> list[dict]:
    """Group the live stack into named programmes, each with the postings
    backing it and whichever of its terms are newly adopted.

    reach = distinct postings mentioning any of the theme's terms, which is the
    honest measure of how much of the board a programme actually touches. A term
    list alone cannot say that: five terms in one posting is one posting.

    "New" here is the term's age against `cutoff`, NOT membership of the adopting
    bucket. Adopting excludes anything whose count dipped this window, so a
    single closed req could push 'generative ai' into receding and quietly drop
    it from the AI programme — reading a filled seat as fading interest."""
    seen = first_seen or {}
    themes: dict[str, dict] = {}
    for cat, terms in (curr_stack or {}).items():
        for term, pids in terms.items():
            theme = next((t for t, ts in _THEME_TERMS.items() if term in ts), None)
            if theme is None:
                theme = next((t for t, cs in _THEME_CATS.items() if cat in cs), None)
            if theme is None:
                continue
            e = themes.setdefault(theme, {"theme": theme, "terms": {}, "pids": set(),
                                          "hits": {},
                                          "new_terms": [], "dates": [], "new_dates": []})
            e["terms"][term] = len(pids)
            e["pids"].update(pids)
            # how many of this theme's terms each posting carries, so the brief can
            # cite the req that best exemplifies the theme rather than just any one
            for pid in pids:
                e["hits"][pid] = e["hits"].get(pid, 0) + 1
            d = seen.get((cat, term))
            if d:
                e["dates"].append(d)
                if cutoff and d > cutoff:
                    e["new_terms"].append(term)
                    e["new_dates"].append(d)
    out = []
    for t in _THEME_ORDER:
        e = themes.get(t)
        if not e:
            continue
        e["reach"] = len(e["pids"])
        # Freeze the set into a stable order. Callers pick an exemplar posting to
        # cite out of this collection, and iterating a set handed them a different
        # posting on every run — three renders of one lake produced three
        # different citations. Ordering by theme coverage makes the pick both
        # reproducible and the most representative req available, with the id as
        # a tie-break so equally-good candidates still resolve the same way.
        e["pids"] = sorted(e["pids"], key=lambda pid: (-e["hits"].get(pid, 0), pid))
        e["top_terms"] = sorted(e["terms"], key=lambda k: (-e["terms"][k], k))
        e["new_terms"].sort(key=lambda k: (-e["terms"][k], k))
        # when the programme first showed up at all, and when its newest piece did
        e["since"] = min(e["dates"], default=None)
        e["new_since"] = min(e["new_dates"], default=None)
        e["all_new"] = len(e["new_terms"]) == len(e["terms"])
        out.append(e)
    out.sort(key=lambda e: -e["reach"])
    return out


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def detect_boilerplate_prefix(curated: dict, max_check: int = 6, threshold: float = 0.7) -> int:
    """How many leading sentences are shared company-boilerplate (e.g. "Join Dunder Mifflin,
    a global leader...") across most postings, so excerpts can skip past them."""
    sent_lists = [split_sentences(r.get("description_raw", "")) for r in curated.values()]
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
_REPORTING = "Reporting"        # two snapshots — dashboard + brief render
_FIRST_CRAWL = "First crawl"    # one snapshot — no delta to derive yet
_CONFIGURED = "Not collected"   # verified config, nothing in the lake
_REVIEW = "Needs review"        # config flagged needs_human_review

_STATUS_ORDER = {_REPORTING: 0, _FIRST_CRAWL: 1, _CONFIGURED: 2, _REVIEW: 3}


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

        if snap and snap["snapshots"] >= 2:
            status = _REPORTING
        elif snap:
            status = _FIRST_CRAWL
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
.nav{display:flex;align-items:center;gap:26px;padding:11px 24px;background:var(--color-accent-900);color:var(--color-bg);margin-bottom:0;flex-wrap:wrap}
.nav-brand{font-family:var(--font-heading);font-weight:600;font-size:18px;letter-spacing:.06em}
.nav-badge{font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--color-accent);border:1px solid var(--color-accent);padding:2px 7px;font-family:var(--font-heading)}
.nav-tabs{margin-left:auto;display:flex;gap:22px;align-items:center;flex-wrap:wrap}
.nav-tabs a{color:color-mix(in srgb,#f2f2f3 82%,transparent);text-decoration:none;font-size:14px;padding-bottom:2px}
.nav-tabs a:hover{color:#fff}
.nav-tabs a[aria-current='page']{color:#fff;border-bottom:2px solid var(--color-accent)}
.nav :focus-visible{outline-color:#fff}
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
.pager [role=status]{font-size:12px;font-variant-numeric:tabular-nums}
.pager button[disabled]{opacity:.4;cursor:default}
.site-foot{border-top:1px solid var(--color-divider);margin-top:38px;padding-top:14px;
  display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;font-size:11.5px;line-height:1.5}
@media (max-width:980px){.grid,.grid.narrative{grid-template-columns:1fr}.stack-grid{grid-template-columns:1fr}}
@media (max-width:620px){
  .wrap{padding:0 16px 44px}
  .nav{gap:12px;padding:10px 16px}
  .nav-tabs{gap:16px;margin-left:0;width:100%}
  .tbl-controls input[type=search]{width:100%}
  .hdr h1{font-size:26px!important}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;
    transition-duration:.01ms!important;scroll-behavior:auto!important}
}
@media print{
  .nav,.skip-link,.crumb,.btn,.tbl-controls,.pager,.chart-tbl,.arch-dialog{display:none!important}
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


# TICK mark: a rising step line on the navy field, so a pinned tab reads as the
# product rather than as a blank page icon. Inlined as a data URI — one fewer
# request, and no 404 when the site is served from a subdirectory.
_FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' fill='%231d2d3d'/%3E"
    "%3Cpath d='M6 22h5v-6h5v-4h5V7h5' fill='none' stroke='%235980a6'"
    " stroke-width='2.6' stroke-linejoin='miter'/%3E%3C/svg%3E"
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
<meta name="theme-color" content="#1d2d3d">
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


def _nav(active: str, prefix: str = "") -> str:
    """prefix is vestigial — the home page link is root-absolute now, because
    "/" belongs to the public marketing page and the app starts at APP_HOME."""
    def tab(label, href, key):
        cur = ' aria-current="page"' if key == active else ""
        return f'<a href="{href}"{cur}>{label}</a>'
    tabs = [tab("Accounts", APP_HOME, "home")]
    if active in ("dashboard", "brief"):
        tabs += [tab("Dashboard", "dashboard.html", "dashboard"),
                 tab("Brief", "brief.html", "brief")]
    tabs += [tab("Sign out", "/auth/logout", "logout")]
    return (f'<div class="nav"><a class="nav-brand" href="{APP_HOME}" '
            f'style="color:inherit;text-decoration:none">TICK</a>'
            f'<span class="nav-badge">Targeted Intelligence Companion Kit</span>'
            f'<div class="nav-tabs">{"".join(tabs)}</div></div>')


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


_ROLE_TABLE_IDS = {"table": "open-roles", "filter": "role-filter", "flag": "new-only",
                   "flag_attr": "data-new", "count": "row-count", "total": "row-total",
                   "empty": "no-match", "pager": "roles-pager", "prev": "page-prev",
                   "next": "page-next", "status": "page-status"}
_ARCHIVE_TABLE_IDS = {"table": "archive-roles", "filter": "archive-filter", "flag": "archive-tech",
                      "flag_attr": "data-tech", "count": "archive-count", "total": "archive-total",
                      "empty": "archive-no-match", "pager": "archive-pager", "prev": "archive-prev",
                      "next": "archive-next", "status": "archive-status"}


def _filter_script(ids: dict = _ROLE_TABLE_IDS) -> str:
    """Client-side filtering + pagination for a dashboard table — the open roles
    and the archive share it, each passing the ids of its own controls.

    The table ships complete in the HTML and this only toggles row visibility,
    so with JS off every row is still on the page — which is also why the
    pager starts hidden and is revealed here rather than rendered ready-made.
    Paging applies to the FILTERED set, not the raw rows, so the page numbers
    always describe what the reader is actually looking at."""
    return f"""<script>
(function () {{
  var PAGE = {ROLES_PAGE_SIZE};
  var q = document.getElementById('{ids["filter"]}');
  var flag = document.getElementById('{ids["flag"]}');
  var table = document.getElementById('{ids["table"]}');
  var count = document.getElementById('{ids["count"]}');
  var empty = document.getElementById('{ids["empty"]}');
  var pager = document.getElementById('{ids["pager"]}');
  var prev = document.getElementById('{ids["prev"]}');
  var next = document.getElementById('{ids["next"]}');
  var status = document.getElementById('{ids["status"]}');
  if (!q || !table) return;
  var rows = Array.prototype.slice.call(table.tBodies[0].rows)
    .filter(function (r) {{ return r.hasAttribute('data-q'); }});
  var matched = rows.slice();
  var page = 1;

  function pages() {{ return Math.max(1, Math.ceil(matched.length / PAGE)); }}

  function render() {{
    if (page > pages()) page = pages();
    if (page < 1) page = 1;
    var start = (page - 1) * PAGE;
    var slice = matched.slice(start, start + PAGE);
    rows.forEach(function (r) {{ r.hidden = true; }});
    slice.forEach(function (r) {{ r.hidden = false; }});

    if (count) count.textContent = matched.length;
    var total = document.getElementById('{ids["total"]}');
    if (total) total.hidden = matched.length === rows.length;
    if (empty) empty.hidden = matched.length !== 0;

    if (pager) pager.hidden = matched.length <= PAGE;
    if (status) {{
      status.textContent = matched.length
        ? (start + 1) + '\u2013' + (start + slice.length) + ' of ' + matched.length
          + ' \u00b7 page ' + page + ' of ' + pages()
        : '';
    }}
    if (prev) prev.disabled = page <= 1;
    if (next) next.disabled = page >= pages();
  }}

  function apply() {{
    var needle = q.value.trim().toLowerCase();
    var only = flag && flag.checked;
    matched = rows.filter(function (r) {{
      var hay = r.getAttribute('data-q') || '';
      return (!needle || hay.indexOf(needle) !== -1)
          && (!only || r.getAttribute('{ids["flag_attr"]}') === '1');
    }});
    page = 1;               // a new filter always lands the reader on page one
    render();
  }}

  q.addEventListener('input', apply);
  if (flag) flag.addEventListener('change', apply);
  if (prev) prev.addEventListener('click', function () {{ page--; render(); }});
  if (next) next.addEventListener('click', function () {{ page++; render(); }});
  apply();
}})();
</script>"""


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
        body.append(f'<div class="stack-row"><span>{html.escape(r["term"])}</span>'
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
    if r["description"]:
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
            f'{field("Days open", days)}{field("Source", r["source"])}'
            f'{field("Posting ID", r["pid"])}'
            f'<dt>Original posting</dt><dd>{link}</dd>{page}'
            f'<dt>Stack signals</dt><dd>{terms}</dd>'
            f'</dl>'
            f'<h3 class="section-label text-muted" style="margin:18px 0 8px">Description</h3>{desc}')


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
{_nav("archive")}
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
    curr, prev, diff, state = lake["curr"], lake["prev"], lake["diff"], lake["state"]
    company_name = curr["company"]
    curr_dt, prev_dt = curr["captured_dt"], prev["captured_dt"]
    age_cutoff = stack_age_cutoff(lake)
    buckets = categorize_terms(curr["stack"], prev["stack"],
                               lake.get("term_first_seen"), age_cutoff)
    new_rows = new_postings_with_signals(diff, curr["stack"], curr["postings"], lake["curated"])
    tech_new = [r for r in new_rows if r["is_tech_role"]]
    open_rows = open_postings(lake)
    new_open = [r for r in open_rows if r["is_new"]]
    domain = board_domain(curr["postings"])
    counts = (diff or {}).get("counts", {})
    delta = curr["postings_total"] - prev["postings_total"]
    pct = round(100 * delta / prev["postings_total"], 1) if prev["postings_total"] else 0
    # the span actually covered, which is shorter than the requested window
    # while a company is still building up history
    window_days = (date.fromisoformat(curr_dt) - date.fromisoformat(prev_dt)).days

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
    adopting_card = _term_bucket_card("Adopting", "tag-accent", buckets["adopting"],
        lambda r: adopt_note)
    established_card = _term_bucket_card("Established", "tag-neutral", buckets["established"],
        lambda r: estab_note)

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
        terms = "".join(f'<span class="tag tag-outline" style="margin:0 4px 4px 0">{html.escape(t)}</span>'
                        for t in r["terms"]) or "&mdash;"
        seen = html.escape(r["first_seen"] or "—")
        return (f'<tr data-new="{int(r["is_new"])}" '
                f'data-q="{html.escape((r["title"] + " " + r["location"]).lower(), quote=True)}">'
                f'<td>{link}{badges}</td>'
                f'<td class="text-muted">{html.escape(r["location"])}</td>'
                f'<td class="text-muted" style="white-space:nowrap">{seen}</td>'
                f'<td>{terms}</td></tr>')

    open_table_rows = "".join(_open_row(r) for r in open_rows)

    archive_rows = archived_postings(lake)
    archive_tech = sum(1 for r in archive_rows if r["is_tech_role"])

    def _archive_row(r: dict, i: int) -> str:
        days = "&mdash;" if r["days_open"] is None else str(r["days_open"])
        hay = " ".join([r["title"], r["location"], r["team"] or ""]).lower()
        badge = ('<div class="row-flags"><span class="tag tag-accent tag-flag">Tech role</span></div>'
                 if r["is_tech_role"] else "")
        return (f'<tr data-arch="arch-{i}" data-tech="{int(r["is_tech_role"])}" '
                f'data-q="{html.escape(hay, quote=True)}">'
                f'<td><a href="{archive_href(company, r["pid"])}" aria-haspopup="dialog">'
                f'{html.escape(r["title"])}</a>{badge}</td>'
                f'<td class="text-muted">{html.escape(r["location"])}</td>'
                f'<td class="text-muted" style="white-space:nowrap">{html.escape(r["closed_on"])}</td>'
                f'<td class="text-muted num">{days}</td></tr>')

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

    tech_new_html = "".join(
        f'<div style="padding-bottom:11px;border-bottom:1px solid color-mix(in srgb,var(--color-text) 8%,transparent)">'
        f'<div style="font-size:13.5px;font-weight:500"><a href="{html.escape(r["url"])}" target="_blank" rel="noopener">{html.escape(r["title"])}</a></div>'
        f'<div style="font-size:12px" class="text-muted">{html.escape(r["location"])}</div></div>'
        for r in tech_new) or '<p class="text-muted" style="margin:0;font-size:12.5px">None this period.</p>'

    history = lake.get("history") or lake["series"]
    history_chart = _history_chart(history)
    hist_span = (f'{date.fromisoformat(history[0]["dt"]):%b %Y} &ndash; '
                 f'{date.fromisoformat(history[-1]["dt"]):%b %Y}'
                 if len(history) > 1 else f'{curr_dt}')

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

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{_head(f"{company_name} — TICK dashboard",
       f"Hiring signals for {company_name}: {curr['postings_total']} open postings "
       f"captured {curr_dt}, compared with {prev_dt}.")}
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
{_nav("dashboard", "../")}
<div class="wrap">
  <div class="crumb"><a href="{APP_HOME}">Accounts</a><span>/</span><span>{html.escape(company_name)}</span><span>/</span><span style="color:var(--color-text)">Dashboard</span></div>
  <main id="main">
  <div class="hdr">
    <div><p class="eyebrow text-muted">{html.escape(company_name)}</p><h1 style="font-size:34px;margin:3px 0">Dashboard</h1>
      <p class="text-muted" style="margin:6px 0 0;font-size:13px;max-width:70ch">Built from {curr['postings_total']} open postings captured {curr_dt},
        measured against the crawl {window_days} days earlier ({prev_dt}). Every row links back to the posting it came from.</p></div>
    <button class="btn btn-primary" type="button" id="copy-brief">Copy summary</button>
  </div>

  <div class="grid">
    <div style="display:flex;flex-direction:column;gap:24px">
      <div>
        <h2 class="section-label text-muted" style="margin-bottom:12px">Tech stack, as evidenced by hiring</h2>
        <div class="stack-grid">{adopting_card}{established_card}</div>
      </div>
      <div>
        <div class="tbl-head">
          <h2 class="section-label text-muted" style="margin:0">Open roles &middot; <span id="row-count">{len(open_rows)}</span><span id="row-total" hidden> of {len(open_rows)}</span></h2>
          <div class="tbl-controls">
            <input id="role-filter" type="search" placeholder="Filter by title or location" aria-label="Filter open roles">
            <label class="chk"><input id="new-only" type="checkbox"> New in last {NEW_ROLE_DAYS} days ({len(new_open)})</label>
          </div>
        </div>
        <div class="blueprint" style="overflow-x:auto">
          <table class="table" id="open-roles">
            <caption class="visually-hidden">Open roles on the {curr_dt} board. Titles link to the
              posting on the company&rsquo;s job board and open in a new tab.</caption>
            <thead><tr><th scope="col">Posting</th><th scope="col">Location</th><th scope="col">First seen</th><th scope="col">Stack signals matched</th></tr></thead>
            <tbody>{open_table_rows or '<tr><td colspan="4" class="text-muted">No open postings on this crawl.</td></tr>'}</tbody>
          </table>
          <p class="tbl-empty text-muted" id="no-match" hidden>No open role matches that filter. Clear the search box, or untick &ldquo;New in last {NEW_ROLE_DAYS} days&rdquo;, to see the full board.</p>
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
            <input id="archive-filter" type="search" placeholder="Filter by title, location or team" aria-label="Filter archived roles">
            <label class="chk"><input id="archive-tech" type="checkbox"> Tech roles only ({archive_tech})</label>
          </div>
        </div>
        <div class="blueprint" style="overflow-x:auto">
          <table class="table arch-table" id="archive-roles">
            <caption class="visually-hidden">Postings that have left the board, most recently closed
              first. Select a posting to see everything TICK recorded about it.</caption>
            <thead><tr><th scope="col">Posting</th><th scope="col">Location</th><th scope="col">Closed</th><th scope="col" class="num">Days open</th></tr></thead>
            <tbody>{archive_table_rows or '<tr><td colspan="4" class="text-muted">No postings have closed since collection started.</td></tr>'}</tbody>
          </table>
          <p class="tbl-empty text-muted" id="archive-no-match" hidden>No archived role matches that filter.</p>
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
          <span class="text-muted">{prev_dt} &rarr; {curr_dt}</span>
          <span>{prev['postings_total']} &rarr; {curr['postings_total']}
            <b style="color:{'var(--color-accent-700)' if delta >= 0 else 'inherit'}">{delta:+d} ({pct:+.1f}%)</b></span>
        </div>
        <div class="stat-row">
          <div class="stat"><b>{counts.get('new', '?')}</b><span>Opened</span></div>
          <div class="stat"><b>{counts.get('closed', '?')}</b><span>Closed</span></div>
          <div class="stat"><b>{counts.get('reopened', '?')}</b><span>Reopened</span></div>
        </div>
      </div>
      <div class="card blueprint">{_corners()}
        <h2 style="margin:0 0 3px;font-size:20px">Tech roles opened</h2>
        <span style="font-size:11.5px;display:block;margin-bottom:12px" class="text-muted">Opened this period and classified as technical</span>
        <div style="display:flex;flex-direction:column;gap:11px">{tech_new_html}</div>
      </div>
      <div class="card blueprint">{_corners()}
        <h2 style="margin:0 0 10px;font-size:20px">Source coverage</h2>
        <div style="display:flex;flex-direction:column;gap:8px;font-size:13px">
          <div style="display:flex;justify-content:space-between"><span class="text-muted">Postings analyzed</span><span>{curr['postings_total']}</span></div>
          <div style="display:flex;justify-content:space-between"><span class="text-muted">Window</span><span>{prev_dt} &ndash; {curr_dt}</span></div>
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
{_filter_script()}
{_filter_script(_ARCHIVE_TABLE_IDS)}
{_archive_script()}
{_history_chart_script(history)}
</body>
</html>
"""


# ---------------------------------------------------------------- brief page

def render_brief(lake: dict, company: str) -> str:
    curr, prev, diff, state = lake["curr"], lake["prev"], lake["diff"], lake["state"]
    company_name = curr["company"]
    curr_dt, prev_dt = curr["captured_dt"], prev["captured_dt"]
    age_cutoff = stack_age_cutoff(lake)
    buckets = categorize_terms(curr["stack"], prev["stack"],
                               lake.get("term_first_seen"), age_cutoff)
    new_rows = new_postings_with_signals(diff, curr["stack"], curr["postings"], lake["curated"])
    tech_new = [r for r in new_rows if r["is_tech_role"]]
    domain = board_domain(curr["postings"])
    counts = (diff or {}).get("counts", {})
    delta = curr["postings_total"] - prev["postings_total"]
    pct = round(100 * delta / prev["postings_total"], 1) if prev["postings_total"] else 0

    cx = Citations()
    paras = []
    days = (date.fromisoformat(curr_dt) - date.fromisoformat(prev_dt)).days
    period = "week" if 5 <= days <= 9 else f"{days} days"
    opened_n, closed_n = counts.get("new", 0), counts.get("closed", 0)

    closed_rows = closed_postings(diff, state)
    lead_new = leadership_postings(new_rows)
    lead_closed = leadership_closed(closed_rows)
    prev_tech_ids = set((prev or {}).get("tech_role_ids", []))

    def posting_url(pid: str) -> str | None:
        """A req still on the board is cited to the board; one that has left it
        to its archive page, since the board's own URL now answers "role not
        found"."""
        if pid in curr["postings"]:
            return curr["postings"][pid].get("url")
        if (state.get(pid) or {}).get("closed_on"):
            return archive_href(company, pid)
        return None

    closed_tech_n = sum(1 for r in closed_rows if r["pid"] in prev_tech_ids)

    diff_label = (f"Diff rollup over {days} days: {opened_n} opened, {closed_n} closed, "
                  f"{counts.get('reopened', 0)} reopened; {curr['postings_total']} open at close of period.")

    # ---------------------------------------------------------- leadership para
    # The net-change clause used to close this sentence. It restated the delta the
    # rail card already carries, and the paragraph reads better opening on the
    # movement rather than on the arithmetic.
    # Paragraph two names every opening, so stating the count here as well made the
    # brief lead on a number the reader meets again two sentences later. This
    # paragraph owns the closures; that one owns the openings.
    split = (f', {_num(closed_tech_n)} of them technical'
             if closed_rows and closed_tech_n else "")
    lead = [
        f'<p>Over the past {period} {html.escape(company_name)} closed {_num(closed_n)} '
        f'role{"s" if closed_n != 1 else ""}{split}{cx.cite(diff_label)}.'
    ]

    if lead_closed:
        named = []
        for r in lead_closed[:3]:
            held = f', open {r["days_open"]} days' if r["days_open"] else ""
            named.append(f'{html.escape(r["title"])}{cx.cite(f"{r["pid"]} — {r["title"]}, closed after {r["days_open"]} days on the board." if r["days_open"] else f"{r["pid"]} — {r["title"]}, closed.", posting_url(r["pid"]))}')
        longest = lead_closed[0]
        lead.append(
            f' The notable movement is at the top: {_join_en(named)} came off the board. '
            f'A leadership req that disappears without being reposted is far more often a '
            f'filled seat than an abandoned search'
            f'{f", and this one had been advertised for {longest["days_open"]} days" if longest["days_open"] else ""} — '
            f'so read {"these" if len(lead_closed) > 1 else "this"} as '
            f'{"hires" if len(lead_closed) > 1 else "a hire"} landing, and expect the '
            f'agenda that seat owns to start moving.')
    if closed_rows:
        longest_all = closed_rows[0]
        # The leadership sentence already gives this req's age when the longest
        # closure is also the leadership one — the usual case, and it printed
        # "154 days" twice in consecutive sentences.
        said_age = bool(lead_closed and lead_closed[0]["pid"] == longest_all["pid"]
                        and lead_closed[0]["days_open"])
        age = (f' The longest had sat open {longest_all["days_open"]} days.'
               if longest_all["days_open"] and not said_age else "")
        lead.append(
            f'{age} Roles leaving the board are the half of the picture a headcount number '
            f'hides: they say what is finished, or abandoned, not what is starting.')

    if lead_new:
        named = []
        for r in lead_new[:3]:
            named.append(f'{html.escape(r["title"])}{cx.cite(f"{r["pid"]} — {r["title"]}, opened this period.", r["url"])}')
        lead.append(
            f' Going the other way, {_join_en(named)} {"were" if len(lead_new) > 1 else "was"} '
            f'put out this {period.split()[-1] if "day" not in period else "period"} — '
            f'seats being created rather than backfilled, which is where the next '
            f'budget has already been committed.')
    if not lead_closed and not lead_new:
        lead.append(' Nothing moved at director level or above, so the week is an '
                    'execution week rather than a structural one.')
    paras.append("".join(lead) + "</p>")

    # ------------------------------------------------------- tech/initiative para
    themes = initiative_themes(curr["stack"], age_cutoff, lake.get("term_first_seen"))
    # The paragraph is about THIS WEEK's openings and the stack they moved, not a
    # census of the standing board. Reach counts over all 39 open roles described
    # a company rather than a week, and read the same however little had happened.
    newest = max((e for e in themes if e["since"]), key=lambda e: e["since"], default=None)
    prev_terms = {t for terms in (prev.get("stack") or {}).values() for t in terms}
    board_n = {t: len(pids) for terms in (curr.get("stack") or {}).values()
               for t, pids in terms.items()}
    carried: list[str] = []

    if new_rows:
        named = []
        for r in new_rows[:5]:
            named.append(f'{html.escape(r["title"])}'
                         + cx.cite(f'{r["pid"]} — {r["title"]} ({r["location"]}), opened this '
                                   f'period.', r["url"]))
        more = len(new_rows) - len(named)
        if more > 0:
            named.append(f'{_num(more)} other{"s" if more != 1 else ""}')
        # semicolons, not commas: job titles carry their own commas ("Senior Data
        # Platform Engineer, Databricks"), so a comma-joined list of them reads as
        # one run-on and you cannot see where a title ends
        roll = _join_semi(named) if len(named) > 1 else named[0]
        n_tech = sum(1 for r in new_rows if r["is_tech_role"])
        span = period.split()[-1] if "day" not in period else "period"
        one = len(new_rows) == 1
        tail = ""
        if n_tech:
            tail = (" &mdash; a technical role" if one
                    else f" &mdash; {_num(n_tech)} of them technical")
        tech = (f'<p>{_num(len(new_rows), cap=True)} role{"" if one else "s"} '
                f'opened this {span}: {roll}{tail}.')

        # What the openings ask for, split by whether the board was already asking.
        opened_terms = sorted({t for r in new_rows for t in r["terms"]})
        fresh = [t for t in opened_terms if t not in prev_terms]
        carried = [t for t in opened_terms if t in prev_terms]

        if not opened_terms:
            tech += (' None of them carry stack terms, so this week says nothing about the '
                     'technical direction &mdash; it is hiring outside engineering.')
        else:
            tech += (f'{" It asks" if one else " Between them they ask"} for '
                     f'{_num(len(opened_terms))} stack '
                     f'term{"s" if len(opened_terms) != 1 else ""}')
            if not fresh:
                tech += (f', every one of which the board was already hiring against. '
                         f'Read that as depth rather than direction: '
                         f'{"the opening staffs" if one else "the openings staff"} an '
                         f'existing push harder, and nothing this week points somewhere new.')
            else:
                # name the req that brought the new term in — that is the thing to read
                bringer = next((r for r in new_rows if fresh[0] in r["terms"]), None)
                fresh_txt = _join_en([html.escape(t) for t in fresh[:4]])
                if len(fresh) <= 2 and carried:
                    tech += (f', and only {fresh_txt} '
                             f'{"are" if len(fresh) > 1 else "is"} new to the board &mdash; the '
                             f'other {_num(len(carried))} were already being hired against. Read that '
                             f'as depth rather than direction: the week staffs an existing push '
                             f'harder')
                    # naming the req again here repeated a title from the list above;
                    # point back into that list instead, and only spell the title out
                    # when the roll was truncated and the reader has not seen it
                    listed = bringer in new_rows[:5] if bringer else False
                    where = ("a single req" if listed
                             else f'{html.escape(bringer["title"])}' if bringer else "")
                    tech += (f', and {"it arrives" if len(fresh) == 1 else "they arrive"} '
                             f'inside {where} rather than as a program of its own.'
                             if bringer else '.')
                else:
                    all_fresh = len(fresh) == len(opened_terms)
                    shown_items = [html.escape(t) for t in fresh[:4]]
                    if len(fresh) > 4:
                        shown_items.append(f'{_num(len(fresh) - 4)} more')
                    shown = _join_en(shown_items)
                    tech += (f', {"all" if all_fresh else _num(len(fresh))} of them new to '
                             f'the board: {shown}. '
                             f'That is a wider change than a week of backfill produces')
                    tech += (f', and {html.escape(bringer["title"])} is where most of it '
                             f'lands &mdash; worth reading in full.' if bringer else '.')
    else:
        tech = (f'<p>Nothing opened this '
                f'{period.split()[-1] if "day" not in period else "period"}, so the stack the '
                f'board is hiring against is unchanged.')

    # What the board keeps re-buying. A term asked for again in several of this
    # week's reqs, on top of an already-wide base, is a settled commitment rather
    # than an evaluation — which is the more useful read than the receding side,
    # where a single closing req moves three terms and says nothing about intent.
    if new_rows and carried:
        span = period.split()[-1] if "day" not in period else "period"
        week_n = {t: sum(1 for r in new_rows if t in r["terms"]) for t in carried}
        ranked = sorted(carried, key=lambda t: (-week_n[t], -board_n.get(t, 0), t))
        top = [t for t in ranked if week_n[t] > 1][:3] or ranked[:3]
        total = curr["postings_total"]
        # one list, so the serial comma is consistent across all three entries
        items = ([f'{html.escape(top[0])} is now named in {board_n.get(top[0], 0)} of the '
                  f'{total} open postings']
                 + [f'{html.escape(t)} in {board_n.get(t, 0)}' for t in top[1:]])
        again = (f'all {_num(len(top))} asked for again this {span}' if len(top) > 1
                 else f'and it was asked for again this {span}')
        tech += (f' The board keeps re-buying the same signals: {_join_en(items)}, {again}. '
                 f'Terms that survive that much repetition are settled decisions rather than '
                 f'live evaluations &mdash; the ground a conversation opens on, not the thing '
                 f'to sell.')

    paras.append(tech + "</p>")

    # ---- Leadership: new director/leadership-level postings since last crawl ----
    # (hiring events, not hires — the pipeline tracks posting lifecycle, not who
    # actually got hired, so "new hires" isn't a claim this can support.)
    # ---- Action items: the roles themselves are gone from the body; what
    #      survives is what to do about them, cited back to the req.
    acts = action_items(lead_closed, lead_new, themes, lake.get("curated_prev", {}),
                        company_name, lake["curated"], lake.get("people"))
    act_html = []
    for a in acts:
        mark = ""
        if a["pid"]:
            mark = cx.cite(f'{a["pid"]} — {a["title"]}: {a["why"]}.', posting_url(a["pid"]))
        profile = ""
        who = a.get("person")
        if who:
            conf = who.get("confidence")
            why = "".join(f'<li style="margin-top:4px">{html.escape(v)}</li>'
                          for v in who.get("why_matched", []))
            profile += (
                f'<div style="margin:10px 0 0;padding:11px 12px;'
                f'background:color-mix(in srgb,var(--color-accent) 7%,transparent)">'
                f'<div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px">'
                f'<span style="font-family:var(--font-heading);font-weight:600;font-size:14px">'
                f'{html.escape(who["name"])}</span>'
                f'<span class="tag tag-neutral tag-flag">'
                f'{html.escape(who.get("status", "unconfirmed"))}'
                f'{f" &middot; {round(conf * 100)}%" if conf is not None else ""}</span></div>'
                f'<p style="margin:6px 0 0;font-size:12.5px;line-height:1.55">'
                f'{html.escape(who.get("background", ""))}</p>'
                f'{f"<ul style=\"margin:8px 0 0;padding-left:17px;font-size:12px;line-height:1.5\" class=\"text-muted\">{why}</ul>" if why else ""}'
                f'</div>')
        if a["profile"]:
            profile += (f'<p style="margin:9px 0 0;font-size:12.5px;line-height:1.55">'
                       f'<span class="text-muted">Profile they hired against &mdash; </span>'
                       f'<span style="font-style:italic">&ldquo;{html.escape(a["profile"])}'
                       f'&rdquo;</span></p>')
        if a.get("quote"):
            qsrc = curr["postings"].get(a["quote_pid"]) or {}
            qmark = cx.cite(f'{a["quote_pid"]} — {qsrc.get("title", "?")}, from the job '
                            f'description.', posting_url(a["quote_pid"]))
            profile += (f'<p style="margin:9px 0 0;font-size:12.5px;line-height:1.55">'
                        f'<span class="text-muted">In their words &mdash; </span>'
                        f'<span style="font-style:italic">&ldquo;'
                        f'{html.escape(a["quote"])}&rdquo;</span>{qmark}</p>')
        steps = "".join(f'<li style="margin-top:6px">{t}</li>' for t in a["steps"])
        act_html.append(
            f'<div class="card blueprint" style="padding:14px 16px;margin-bottom:12px">'
            f'{_corners()}'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px">'
            f'<div style="font-size:14.5px;font-weight:600;font-family:var(--font-heading)">'
            f'{html.escape(a["title"])}{mark}</div>'
            f'<span class="tag tag-{"solid" if a["kind"] == "match" else "neutral"} tag-flag">'
            f'{a["kind"]}</span></div>'
            f'<p class="text-muted" style="margin:5px 0 0;font-size:12.5px">{a["why"]}.</p>'
            f'{profile}'
            f'<ul style="margin:9px 0 0;padding-left:17px;font-size:12.5px;line-height:1.55">'
            f'{steps}</ul></div>')
    actions_html = "".join(act_html) or (
        '<p class="text-muted" style="margin:0;font-size:12.5px">'
        'Nothing moved this period that warrants an approach.</p>')

    # Evidence weight — each bar is a share of the open board, so the three are
    # actually comparable. The adopting bar used to sum per-term posting counts,
    # which double-counts any posting carrying several terms and pinned the bar
    # at a meaningless 100%; count distinct postings instead.
    total = max(curr["postings_total"], 1)
    adopt_pids = set()
    for r in buckets["adopting"]:
        adopt_pids.update(r.get("pids") or [])
    bars = [
        ("Adopting stack", len(adopt_pids),
         f'{len(adopt_pids)} of {total} postings'),
        ("New tech roles", len(tech_new), f'{len(tech_new)} of {total} postings'),
        ("Leadership moves", len(lead_new) + len(lead_closed),
         f'{len(lead_new)} opened / {len(lead_closed)} closed'),
    ]
    bars_html = ""
    for label, support, denom_label in bars:
        width = min(100, round(100 * support / total)) if total else 0
        bars_html += (f'<div><div style="display:flex;justify-content:space-between;margin-bottom:3px">'
                       f'<span>{label}</span><span class="text-muted">{denom_label}</span></div>'
                       f'<div class="bar-track"><div class="bar-fill" style="width:{width}%"></div></div></div>')

    # Plot the trailing half-year, not just the two ends of the comparison window:
    # two points can only ever draw a straight line, which said nothing about the
    # shape of the year the paragraph above it is describing.
    spark_pts = (lake.get("history") or lake["series"])[-26:]
    sparkline = _trend_svg(spark_pts)
    spark_span = (f'{spark_pts[0]["dt"]} &rarr; {spark_pts[-1]["dt"]}'
                  if spark_pts else f'{prev_dt} &rarr; {curr_dt}')

    def _plain(x):
        return html.unescape(re.sub(r"<[^>]+>", "", x)).replace("&rarr;", "->")

    plain_paras = [_plain(p) for p in paras]
    plain_actions = ["Action items:"] + [
        f'- {a["title"]} — {a["why"]}.' + "".join(f"\n    * {_plain(t)}" for t in a["steps"])
        for a in acts] if acts else ["Action items: none this period."]
    brief_text = (f"{company_name} — account brief\n\n" + "\n\n".join(plain_paras)
                  + "\n\n" + "\n".join(plain_actions))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{_head(f"{company_name} — TICK brief",
       f"What {company_name} is hiring for and what changed this period, "
       f"read out of {curr['postings_total']} open postings as of {curr_dt}.")}
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
{_nav("brief", "../")}
<div class="wrap narrow">
  <div class="crumb"><a href="{APP_HOME}">Accounts</a><span>/</span><span>{html.escape(company_name)}</span><span>/</span><span style="color:var(--color-text)">Brief</span></div>
  <main id="main">
  <div class="hdr">
    <div><p class="eyebrow text-muted">{html.escape(company_name)}</p><h1 style="font-size:30px;margin:3px 0">Account brief</h1>
      <p class="text-muted" style="margin:6px 0 0;font-size:13px">Week to {curr_dt} &middot; every claim carries a citation</p></div>
    <button class="btn btn-primary" type="button" id="copy-brief">Copy brief</button>
  </div>
  <div class="grid narrative">
    <div class="prose">
      {"".join(paras)}
      <div style="margin-top:26px">
        <h2 class="section-label text-muted" style="margin-bottom:6px">Action items</h2>
        <p class="text-muted" style="margin:0 0 12px;font-size:12.5px;max-width:62ch">Each one is anchored to a
          posting event this period; the citation goes back to the req it came from.</p>
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
          <span class="text-muted">{prev_dt} &rarr; {curr_dt}</span>
          <span>{prev['postings_total']} &rarr; {curr['postings_total']}
            <b style="color:{'var(--color-accent-700)' if delta >= 0 else 'inherit'}">{delta:+d} ({pct:+.1f}%)</b></span>
        </div>
      </div>
      <div class="card blueprint">{_corners()}
        <span style="font-size:10px;letter-spacing:.1em;text-transform:uppercase;display:block;margin-bottom:9px" class="text-muted">Evidence weight</span>
        <p class="text-muted" style="margin:0 0 10px;font-size:11.5px;line-height:1.45">How much of the
        {curr['postings_total']}-posting board stands behind each claim above.</p>
        <div style="display:flex;flex-direction:column;gap:9px;font-size:12.5px">{bars_html}</div>
      </div>
      <div class="card blueprint">{_corners()}
        <span style="font-size:10px;letter-spacing:.1em;text-transform:uppercase;display:block;margin-bottom:9px" class="text-muted">Generated</span>
        <p style="margin:0;font-size:12.5px;line-height:1.55" class="text-muted">{fmt_dt(curr['captured_at'])}, from {curr['postings_total']} postings on {html.escape(domain)}.
        Every claim above carries a citation; nothing is inferred beyond the postings and snapshot counts.</p>
      </div>
    </div>
  </div>
  </main>
  {_footer(fmt_dt(curr['captured_at']), domain)}
</div>
{_copy_script('copy-brief', brief_text)}
</body>
</html>
"""


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
        # Root-absolute: the home page is served at /login, so a relative slug
        # would resolve under it (/login/<slug>/dashboard.html) and 404.
        foot = (f'<a class="btn btn-primary btn-sm" href="/{html.escape(a["slug"])}/dashboard.html">Dashboard</a>'
                f'<a class="btn btn-secondary btn-sm" href="/{html.escape(a["slug"])}/brief.html">Brief</a>'
                f'<span class="text-muted" style="margin-left:auto;font-size:11.5px">'
                f'Crawled {a["curr_dt"]}</span>')
        note = ""
    else:
        foot = (f'<a class="btn btn-secondary btn-sm" href="{html.escape(a["careers_url"])}">Careers board</a>'
                if a["careers_url"] else "")
        if a["status"] == _FIRST_CRAWL:
            note = (f'One snapshot so far ({a["curr_dt"]}). The dashboard and brief both read a '
                    f'delta between two crawls, so they render from the next one.')
        elif a["status"] == _REVIEW:
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
        'it&rsquo;s set up and has two crawls to compare, it appears here with a dashboard and brief.</p>'
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

_AI_CITE_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


def render_ai_brief(brief: dict, evidence: dict) -> str:
    """The weekly brief a model wrote (see .claude/skills/tick-brief), in the
    same page as render_brief. The model's text is escaped like any other
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

    cx = Citations()
    numbered: dict[str, int] = {}

    def label(cid: str) -> tuple[str, str | None]:
        if cid == "BOARD":
            return (f'Board {window["start"]} → {window["end"]}: {board["open_at_start"]} → '
                    f'{board["open_at_end"]} open; {board["opened"]} opened, {board["closed"]} '
                    f'closed, {board["reopened"]} reopened.', None)
        if cid == "STACK":
            return "Stack terms across the open board.", None
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
{_nav("brief", "../")}
<div class="wrap narrow">
  <div class="crumb"><a href="{APP_HOME}">Accounts</a><span>/</span><span>{html.escape(company_name)}</span><span>/</span><span style="color:var(--color-text)">Brief</span></div>
  <main id="main">
  <div class="hdr">
    <div><p class="eyebrow text-muted">{html.escape(company_name)}</p><h1 style="font-size:30px;margin:3px 0">Account brief</h1>
      <p class="text-muted" style="margin:6px 0 0;font-size:13px">Week to {window['end']} &middot; every claim carries a citation</p></div>
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
{_nav("none", "/")}
<div class="wrap narrow">
  <main id="main">
  <div class="hdr" style="border:0">
    <div><p class="eyebrow text-muted">Error 404</p><h1 style="font-size:34px;margin:3px 0">Page not found</h1>
      <p class="text-muted" style="margin:8px 0 0;font-size:14px;max-width:60ch">That address is not part of this
        site. Account pages are only written for companies with at least two crawls, so a dashboard or brief
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
                    help="render just this company_slug (default: every company in the lake with two snapshots)")
    ap.add_argument("--configs", default=None,
                    help="directory or s3:// prefix of evaluator SourceConfig JSON files, so accounts "
                         "with nothing collected yet still appear (default: the lake's own configs/ "
                         "prefix on S3, or ./configs when rendering a local lake)")
    ap.add_argument("--out-dir", default="site",
                    help="directory to write login/index.html and <slug>/ into")
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
    home_path = os.path.join(args.out_dir, "login", "index.html")
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
            sys.exit(f"{args.company}: {targets[0]['snapshots']} snapshot(s) — "
                     "need at least two crawls to derive trend/adoption")

    for a in targets:
        lake = load_lake(args.source, a["slug"], lake_io, args.window_days)
        acct_dir = os.path.join(args.out_dir, a["slug"])
        os.makedirs(acct_dir, exist_ok=True)
        dash_path = os.path.join(acct_dir, "dashboard.html")
        brief_path = os.path.join(acct_dir, "brief.html")
        with open(dash_path, "w") as f:
            f.write(render_dashboard(lake, a["slug"]))
        with open(brief_path, "w") as f:
            f.write(render_brief(lake, a["slug"]))
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
