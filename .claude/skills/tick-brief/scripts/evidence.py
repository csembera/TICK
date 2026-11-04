"""
Build the evidence pack the weekly brief is written from.

    evidence.py <lake-root> <company-slug> [--week-end YYYY-MM-DD] [--out PATH]
                [--personas MAP | --persona FILE]
    evidence.py <lake-root> <company-slug> --overview [--as-of YYYY-MM-DD] ...

<lake-root> is a local lake (e.g. local-data/tick-data-backup-2026-10-30) or
s3://bucket. The window is the 7 days ending on --week-end (default: the
latest crawl): every posting that opened, closed or reopened in it, with its
description, plus the board and stack context needed to read that movement.

--overview builds the other pack: no window, just the open board on one crawl
and its stack counted every way the overview prompt reads it (by category,
by team, in pairs, rare, arrived since tracking began), for an account too new
for a week of movement (build_overview).

This is the ONLY input the brief writer gets, and it is deterministic — the
model interprets, it never counts. Every item carries an `id` the brief cites
as [[id]]: a posting id, or BOARD / STACK / PERSONA for the aggregate sections.

The brief is written for the persona of the one user who tracks the account,
when there is one: --personas takes the map `tools/tick.py personas list
--json` writes, --persona a single persona file (for tuning the prompt). The
persona is cleaned by tools/persona.py again here, because it is user-typed
text about to enter a model's context, and its terms are counted in code like
everything else in the pack. Without one, the pack has no persona section and
the brief is written for the default reader.

The Strands agent will call this same builder; keep it free of anything that
only works from a laptop.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import date, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import persona as persona_schema  # noqa: E402
import tick_insights_report as report  # noqa: E402

WINDOW_DAYS = 7
DESC_CHARS = 2500          # per posting, after the shared boilerplate is cut
HISTORY_WEEKS = 12
RECENT_TERM_DAYS = 120     # a term first seen this recently is "recent arrival"
_DT_RE = re.compile(r"dt=(\d{4}-\d{2}-\d{2})")


def _dt(key: str) -> str | None:
    m = _DT_RE.search(key)
    return m.group(1) if m else None


def _days(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def build(source: str, slug: str, week_end: str | None = None, reader: dict | None = None) -> dict:
    io = report.open_lake(source)
    if week_end:
        # Pretend the lake ends on week_end: drop every partition after it.
        io = dict(io, keys=[k for k in io["keys"] if not _dt(k) or _dt(k) <= week_end])
    keys, get, get_text = io["keys"], io["get"], io["get_text"]

    lake = report.load_lake(source, slug, io, window_days=WINDOW_DAYS)
    curr, prev, state = lake["curr"], lake["prev"], lake["state"]
    end = curr["captured_dt"]
    start = prev["captured_dt"] if prev else lake["dts"][0]

    # state.json is as of the newest crawl in the lake; forget anything it
    # learned after the window so a back-dated run cannot see the future.
    def st(pid: str) -> dict:
        s = dict(state.get(pid, {}))
        if s.get("closed_on") and s["closed_on"] > end:
            s.pop("closed_on")
        return s

    # ---- curated partitions, newest first, loaded on demand
    curated_keys = sorted((k for k in keys if k.endswith("postings.jsonl")
                           and f"company={slug}/" in k), key=_dt, reverse=True)
    _cache: dict[str, dict] = {}

    def curated_at(key: str) -> dict:
        if key not in _cache:
            _cache[key] = {json.loads(l)["posting_id"]: json.loads(l)
                           for l in get_text(key).splitlines() if l.strip()}
        return _cache[key]

    # postings that opened and closed between two runs on one day (collector)
    departed_keys = {_dt(k): k for k in keys if k.endswith("departed.jsonl") and f"company={slug}/" in k}

    def latest_record(pid: str, on_or_before: str) -> dict | None:
        for k in curated_keys:
            if _dt(k) <= on_or_before:
                if pid in curated_at(k):
                    return curated_at(k)[pid]
                dk = departed_keys.get(_dt(k))
                if dk and pid in curated_at(dk):
                    return curated_at(dk)[pid]
        return None

    board_now = curated_at(curated_keys[0]) if curated_keys else {}
    bp_n = report.detect_boilerplate_prefix(board_now) if board_now else 0

    def describe(rec: dict | None) -> str:
        # role text only: the board's intro, benefits and equal-opportunity text
        # are the same on every posting and tell the writer nothing
        text = report.role_text_of(rec, bp_n)
        return text if len(text) <= DESC_CHARS else text[:DESC_CHARS].rsplit(" ", 1)[0] + " …"

    def pay(rec: dict | None) -> dict | None:
        p = report.pay_of(rec)
        return {k: p[k] for k in ("min", "max", "currency", "period")} if p else None

    # ---- the reader's terms, matched like the collector's: whole words in the
    # title and the role text, never the board's boilerplate
    who = persona_schema.clean(reader)[0] if reader else None
    if persona_schema.is_blank(who):     # nothing valid survived: the default reader
        who = None
    patterns = {t: report._term_pattern(t)
                for t in ((who["focus_terms"] + who["competitor_terms"]) if who else [])}

    def mentions(rec: dict | None) -> list[str]:
        if not patterns or not rec:
            return []
        hay = f'{rec.get("title") or ""} {report.role_text_of(rec, bp_n)}'
        return [t for t, rx in patterns.items() if rx.search(hay)]

    # ---- the window's movement, straight from the daily diffs
    diff_keys = {_dt(k): k for k in keys if k.endswith("diff.json") and f"company={slug}/" in k}
    window_dts = sorted(d for d in diff_keys if start < d <= end)
    opened, closed, reopened = {}, {}, {}
    for d in window_dts:
        df = get(diff_keys[d])
        for pid in df.get("new", []):
            opened.setdefault(pid, d)
        for pid in df.get("closed", []):
            closed[pid] = d
        for pid in df.get("reopened", []):
            reopened.setdefault(pid, d)

    stack_terms_by_pid: dict[str, set[str]] = {}
    for terms in (curr.get("stack") or {}).values():
        for term, pids in terms.items():
            for pid in pids:
                stack_terms_by_pid.setdefault(pid, set()).add(term)

    def terms_for(pid: str, rec: dict | None) -> list[str]:
        return sorted(stack_terms_by_pid.get(pid, set())
                      | set(report.flat_terms((rec or {}).get("keywords") or {})))

    # Reposts: an opening whose title matches a role that closed recently is a
    # backfill or a re-advertisement, not a new seat.
    recent_closed_titles: dict[str, str] = {}
    for pid, s in state.items():
        if s.get("closed_on") and _days(s["closed_on"], end) is not None \
                and 0 <= _days(s["closed_on"], end) <= 60:
            recent_closed_titles[_norm_title(s.get("title", ""))] = pid
    for pid in closed:
        rec = latest_record(pid, end)
        if rec:
            recent_closed_titles.setdefault(_norm_title(rec["title"]), pid)

    def posting(pid: str, rec: dict | None, extra: dict) -> dict:
        s = st(pid)
        title = (rec or {}).get("title") or s.get("title") or pid
        return {
            "id": pid,
            "title": title,
            "location": (rec or {}).get("location") or s.get("location"),
            "team": (rec or {}).get("team"),
            "remote": (rec or {}).get("remote_flag"),
            "url": (rec or {}).get("url"),
            "is_tech_role": bool((rec or {}).get("is_tech_role")),
            "is_leadership": bool(report._LEADERSHIP_RE.search(title)),
            "first_seen": s.get("first_seen") or (rec or {}).get("first_seen"),
            "stack_terms": terms_for(pid, rec),
            **extra,
            **({"persona_terms": mentions(rec)} if who else {}),
            "pay": pay(rec),
            "description": describe(rec),
        }

    opened_out = []
    for pid, d in sorted(opened.items(), key=lambda kv: kv[1], reverse=True):
        rec = board_now.get(pid) or latest_record(pid, end)
        twin = recent_closed_titles.get(_norm_title((rec or {}).get("title", "")))
        opened_out.append(posting(pid, rec, {
            "opened_on": d,
            "still_open": pid in board_now,
            "likely_repost_of": twin if twin and twin != pid else None,
        }))

    closed_out = []
    for pid, d in sorted(closed.items(), key=lambda kv: kv[1], reverse=True):
        rec = latest_record(pid, end)
        s = st(pid)
        closed_out.append(posting(pid, rec, {
            "closed_on": d,
            "days_open": _days(s.get("first_seen"), d),
            "opened_this_week": pid in opened,
        }))

    reopened_out = [posting(pid, board_now.get(pid) or latest_record(pid, end),
                            {"reopened_on": d}) for pid, d in reopened.items()]

    # ---- stack: what this week's movement did to the terms the board asks for
    def term_counts(snap: dict | None) -> dict[str, int]:
        out: dict[str, int] = {}
        for terms in ((snap or {}).get("stack") or {}).values():
            for term, pids in terms.items():
                out[term] = max(out.get(term, 0), len(pids))
        return out

    now_n, then_n = term_counts(curr), term_counts(prev)
    first_seen = {}
    for (cat, term), d in lake["term_first_seen"].items():
        first_seen[term] = min(first_seen.get(term, d), d)
    moved = {t: [p["id"] for p in opened_out if t in p["stack_terms"]] for t in now_n}
    total = curr["postings_total"] or 1
    stack = {
        "id": "STACK",
        "history_from": lake["history_from"],
        "new_to_board": [{"term": t, "postings": now_n[t], "in_openings": moved[t]}
                         for t in sorted(set(now_n) - set(then_n))],
        "left_board": [{"term": t, "postings_before": then_n[t]}
                       for t in sorted(set(then_n) - set(now_n))],
        "rising": sorted(({"term": t, "from": then_n[t], "to": now_n[t]}
                          for t in now_n if t in then_n and now_n[t] > then_n[t]),
                         key=lambda r: r["from"] - r["to"]),
        "falling": sorted(({"term": t, "from": then_n[t], "to": now_n[t]}
                           for t in now_n if t in then_n and now_n[t] < then_n[t]),
                          key=lambda r: r["to"] - r["from"]),
        "recent_arrivals": sorted(({"term": t, "first_seen": first_seen[t], "postings": now_n[t]}
                                   for t in now_n if t in first_seen
                                   and _days(first_seen[t], end) <= RECENT_TERM_DAYS),
                                  key=lambda r: r["first_seen"], reverse=True),
        "standing_top": [{"term": t, "postings": n, "share_of_board": round(n / total, 2)}
                         for t, n in sorted(now_n.items(), key=lambda kv: (-kv[1], kv[0]))[:12]],
    }

    # ---- board context
    history = lake["history"]
    weekly = history[::-1][::7][:HISTORY_WEEKS][::-1]
    open_ids = set(board_now) if curated_keys and _dt(curated_keys[0]) == end else set()
    standing = []
    for pid in open_ids:
        rec = board_now[pid]
        s = st(pid)
        standing.append({"id": pid, "title": rec["title"],
                         "days_open": _days(s.get("first_seen"), end),
                         "is_leadership": bool(report._LEADERSHIP_RE.search(rec["title"]))})
    # id breaks ties: open_ids is a set, and without it the "longest open" list
    # changed between runs whenever several roles shared an age
    standing.sort(key=lambda r: (-(r["days_open"] or 0), r["id"]))

    expected = [(date.fromisoformat(start) + timedelta(days=i)).isoformat()
                for i in range(1, _days(start, end) + 1)]
    crawls = set(lake["dts"])
    board = {
        "id": "BOARD",
        "open_at_start": prev["postings_total"] if prev else None,
        "open_at_end": curr["postings_total"],
        "net_change": curr["postings_total"] - prev["postings_total"] if prev else None,
        "opened": len(opened_out), "closed": len(closed_out), "reopened": len(reopened_out),
        "tech_roles_open": len(curr.get("tech_role_ids", [])),
        "weekly_open_totals": weekly,
        "open_leadership_roles": [r for r in standing if r["is_leadership"]],
        "longest_open": standing[:5],
    }

    cfg = _config(keys, get, slug)

    start_open = board["open_at_start"] or 0
    flags = []
    missing = [d for d in expected if d not in crawls]
    if missing:
        flags.append(f"No crawl on {', '.join(missing)}: movement on those days is folded "
                     "into the next crawl, and a role could open and close unseen.")
    if start_open and len(closed_out) > 0.3 * start_open:
        flags.append(f"{len(closed_out)} of {start_open} roles closed in one week — check for "
                     "an ATS migration or a crawl fault before reading it as a hiring freeze.")
    if _days(start, end) != WINDOW_DAYS:
        flags.append(f"The window is {_days(start, end)} days, not {WINDOW_DAYS}: history is short.")

    return {
        "company": {"name": curr["company"], "slug": slug, "ats": cfg.get("ats"),
                    "careers_url": cfg.get("careers_url"),
                    "board_host": report.board_domain(curr.get("postings") or board_now)},
        "window": {"start": start, "end": end, "days": _days(start, end),
                   "crawl_days": len([d for d in expected if d in crawls]),
                   "date_trust": next((r.get("date_trust") for r in board_now.values()), None)},
        **({"persona": persona_section(who, start, board_now, curated_keys, curated_at,
                                         opened_out, closed_out, reopened_out, standing,
                                         mentions)} if who else {}),
        "data_quality_flags": flags,
        "board": board,
        "stack": stack,
        "opened": opened_out,
        "closed": closed_out,
        "reopened": reopened_out,
    }


STANDING_ROLES = 8   # open roles that did not move but mention the reader's terms


def persona_section(who: dict, start: str, board_now: dict, curated_keys: list[str], curated_at,
                    opened: list[dict], closed: list[dict], reopened: list[dict],
                    standing: list[dict], mentions) -> dict:
    """The PERSONA section: who the reader is, what the board says about each
    of their terms, and the standing roles that mention them. Counts and ids
    only; what they mean is the writer's job."""
    at_start = next((curated_at(k) for k in curated_keys if _dt(k) == start), None)
    now = {pid: set(mentions(rec)) for pid, rec in board_now.items()}
    before = {t for rec in (at_start or {}).values() for t in mentions(rec)}

    def row(term: str) -> dict:
        on_board = sum(1 for terms in now.values() if term in terms)
        return {"term": term, "open_roles": on_board,
                "in_opened": [p["id"] for p in opened if term in p.get("persona_terms", [])],
                "in_closed": [p["id"] for p in closed if term in p.get("persona_terms", [])],
                # None when there is no crawl at the window's start to compare with
                "new_to_board": (bool(on_board) and term not in before) if at_start is not None else None}

    # A persona's interest often sits in a role that has been open for weeks,
    # which this week's movement never lists; without its id the brief could
    # not name it. Most of the reader's terms first, then the longest open.
    moved = {p["id"] for p in opened + closed + reopened}
    age = {r["id"]: r for r in standing}
    order = who["focus_terms"] + who["competitor_terms"]
    relevant = sorted((pid for pid, terms in now.items() if terms and pid not in moved),
                      key=lambda pid: (-len(now[pid]), -(age.get(pid, {}).get("days_open") or 0), pid))
    standing_roles = [{"id": pid, "title": board_now[pid]["title"],
                       "location": board_now[pid].get("location"),
                       "days_open": age.get(pid, {}).get("days_open"),
                       "is_leadership": age.get(pid, {}).get("is_leadership", False),
                       "persona_terms": sorted(now[pid], key=order.index)}
                      for pid in relevant[:STANDING_ROLES]]

    return {
        "id": "PERSONA",
        "ref": persona_schema.ref(who),
        "role": persona_schema.ROLES.get(who["role"]),
        "sells_for": who["sells_for"] or None,
        "offer": who["offer"] or None,
        "sells_to": [persona_schema.BUYERS[b] for b in who["buyers"]],
        "focus_terms": [row(t) for t in who["focus_terms"]],
        "competitor_terms": [row(t) for t in who["competitor_terms"]],
        "standing_roles": standing_roles,
    }


# ---------------------------------------------------------------- overview pack
#
# A new account has no week of movement to write about, so its first brief is
# an overview of the board as it stands, mostly its stack. Same rules as the
# weekly pack: code counts, every item carries an id, the model interprets.

OVERVIEW_DESCRIBED = 40      # roles that keep their role text, tech and leadership first
OVERVIEW_DESC_CHARS = 1200   # per described role: enough for the systems it names
OVERVIEW_LISTED = 150        # every role on a board this size; past it, the most stack-heavy
BASELINE_SHARE = 0.25        # a term on this share of the board is baseline, not a finding...
BASELINE_MIN = 5             # ...once enough roles ask for it that a share means something
TERM_ROLES = 6               # role ids listed per stack term
PAIRS = 15                   # most frequent term pairs
RARE_MAX = 2                 # a term this few roles ask for is "rare"


def build_overview(source: str, slug: str, as_of: str | None = None, reader: dict | None = None) -> dict:
    """The overview pack: the open board on its newest crawl (or as_of),
    with the stack read every way code can count it."""
    io = report.open_lake(source)
    if as_of:
        io = dict(io, keys=[k for k in io["keys"] if not _dt(k) or _dt(k) <= as_of])
    keys, get, get_text = io["keys"], io["get"], io["get_text"]
    lake = report.load_lake(source, slug, io, window_days=WINDOW_DAYS)
    curr, state, dts = lake["curr"], lake["state"], lake["dts"]
    end = curr["captured_dt"]
    board_now = lake["curated"] if lake["curated_dt"] == end else {}
    bp_n = report.detect_boilerplate_prefix(board_now) if board_now else 0

    def first_seen(pid: str) -> str | None:
        s = state.get(pid, {})
        return s.get("first_seen") or (board_now.get(pid) or {}).get("first_seen")

    # ---- the stack, by role: which terms each open role asks for
    terms_by_pid: dict[str, set[str]] = {}
    cat_of: dict[str, str] = {}
    for cat, terms in (curr.get("stack") or {}).items():
        for term, pids in terms.items():
            cat_of.setdefault(term, cat)
            for pid in pids:
                terms_by_pid.setdefault(pid, set()).add(term)
    for pid, rec in board_now.items():
        terms_by_pid.setdefault(pid, set()).update(report.flat_terms(rec.get("keywords") or {}))

    total = curr["postings_total"] or len(board_now) or 1
    tech_ids = set(curr.get("tech_role_ids", []))
    ids = sorted(board_now) or sorted(terms_by_pid)

    def team_of(pid: str) -> str:
        return ((board_now.get(pid) or {}).get("team") or "").strip() or "Unspecified"

    def title_of(pid: str) -> str:
        return (board_now.get(pid) or {}).get("title") or state.get(pid, {}).get("title") or pid

    def leadership(pid: str) -> bool:
        return bool(report._LEADERSHIP_RE.search(title_of(pid)))

    roles_by_term: dict[str, list[str]] = {}
    for pid in ids:
        for t in terms_by_pid.get(pid, ()):
            roles_by_term.setdefault(t, []).append(pid)

    def term_row(t: str) -> dict:
        pids = sorted(roles_by_term[t], key=lambda p: (not leadership(p), -len(terms_by_pid[p]), p))
        n = len(pids)
        teams = Counter(team_of(p) for p in pids)
        return {"term": t, "postings": n, "share_of_board": round(n / total, 2),
                "share_of_tech_roles": round(sum(p in tech_ids for p in pids) / len(tech_ids), 2)
                if tech_ids else None,
                "baseline": n >= BASELINE_MIN and n / total >= BASELINE_SHARE,
                "teams": [{"team": k, "roles": v} for k, v in teams.most_common(3)],
                "roles": pids[:TERM_ROLES]}

    categories: dict[str, list[dict]] = {}
    for t in sorted(roles_by_term, key=lambda t: (-len(roles_by_term[t]), t)):
        label = report._CAT_LABELS.get(cat_of.get(t, ""), "Other")
        categories.setdefault(label, []).append(term_row(t))

    pairs: Counter = Counter()
    for pid in ids:
        ts = sorted(terms_by_pid.get(pid, ()))
        for i, a in enumerate(ts):
            for b in ts[i + 1:]:
                pairs[(a, b)] += 1
    co = [{"terms": [a, b], "postings": n}
          for (a, b), n in sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0])) if n >= 2][:PAIRS]

    first_term = {}
    for (_, term), d in lake["term_first_seen"].items():
        first_term[term] = min(first_term.get(term, d), d)
    since_first = ([{"term": t, "first_seen": first_term[t], "postings": len(roles_by_term[t])}
                    for t in roles_by_term if first_term.get(t, dts[0]) > dts[0]]
                   if len(dts) > 1 else [])

    stack = {
        "id": "STACK",
        "baseline_rule": f"on at least {BASELINE_SHARE:.0%} of the board and {BASELINE_MIN} roles",
        "terms_on_board": len(roles_by_term),
        "by_category": categories,
        "co_occurring": co,
        "rare": [{"term": t, "roles": sorted(p)} for t, p in sorted(roles_by_term.items())
                 if len(p) <= RARE_MAX],
        "since_first_crawl": sorted(since_first, key=lambda r: r["first_seen"], reverse=True),
        "leadership_stack": [{"id": pid, "title": title_of(pid), "stack_terms": sorted(terms_by_pid.get(pid, ()))}
                             for pid in ids if leadership(pid)],
    }

    # ---- the board around the stack
    ages = {pid: _days(first_seen(pid), end) for pid in ids}
    bands = Counter("unknown" if a is None else "under_30" if a < 30 else "30_to_89" if a < 90 else "90_plus"
                    for a in ages.values())
    locs = Counter(((board_now.get(p) or {}).get("location") or "Unspecified") for p in ids)
    paid = [p for p in ids if report.pay_of(board_now.get(p))]
    history = lake["history"]
    weekly = history[::-1][::7][:HISTORY_WEEKS][::-1]
    board = {
        "id": "BOARD",
        "open_roles": curr["postings_total"],
        "tech_roles": len(tech_ids),
        "leadership_roles": sum(leadership(p) for p in ids),
        "by_team": [{"team": k, "roles": v} for k, v in Counter(team_of(p) for p in ids).most_common(12)],
        "by_location": [{"location": k, "roles": v} for k, v in locs.most_common(8)],
        "age_bands": {k: bands.get(k, 0) for k in ("under_30", "30_to_89", "90_plus", "unknown")},
        "roles_with_pay": len(paid),
        "weekly_open_totals": weekly,
    }

    # ---- the roles: tech and leadership seats keep their role text, the rest
    # are listed by title, team and stack so the pack stays a readable size
    def rank(pid: str) -> tuple:
        return (not leadership(pid), pid not in tech_ids, -len(terms_by_pid.get(pid, ())), pid)

    ordered = sorted(ids, key=rank)
    described = {p for p in ordered[:OVERVIEW_DESCRIBED] if leadership(p) or p in tech_ids}
    listed = ordered[:max(OVERVIEW_LISTED, len(described))]
    board["roles_listed"], board["roles_not_listed"] = len(listed), len(ids) - len(listed)

    def describe(rec: dict | None) -> str | None:
        text = report.role_text_of(rec, bp_n)
        if not text:
            return None
        return text if len(text) <= OVERVIEW_DESC_CHARS else text[:OVERVIEW_DESC_CHARS].rsplit(" ", 1)[0] + " …"

    who = persona_schema.clean(reader)[0] if reader else None
    if persona_schema.is_blank(who):
        who = None
    patterns = {t: report._term_pattern(t)
                for t in ((who["focus_terms"] + who["competitor_terms"]) if who else [])}

    def mentions(rec: dict | None) -> list[str]:
        if not patterns or not rec:
            return []
        hay = f'{rec.get("title") or ""} {report.role_text_of(rec, bp_n)}'
        return [t for t, rx in patterns.items() if rx.search(hay)]

    postings = []
    for pid in listed:
        rec = board_now.get(pid)
        p = report.pay_of(rec)
        postings.append({
            "id": pid, "title": title_of(pid),
            "location": (rec or {}).get("location"), "team": (rec or {}).get("team"),
            "url": (rec or {}).get("url"),
            "is_tech_role": pid in tech_ids, "is_leadership": leadership(pid),
            "first_seen": first_seen(pid), "days_open": ages[pid],
            "stack_terms": sorted(terms_by_pid.get(pid, ())),
            **({"persona_terms": mentions(rec)} if who else {}),
            "pay": {k: p[k] for k in ("min", "max", "currency", "period")} if p else None,
            "description": describe(rec) if pid in described else None,
        })

    cfg = _config(keys, get, slug)
    date_trust = next((r.get("date_trust") for r in board_now.values()), None)
    flags = []
    if not board_now:
        flags.append("No role text for the newest crawl: postings carry titles and stack terms only.")
    if len(dts) == 1:
        flags.append("One crawl so far: nothing here says what is changing, only what is open.")
    if date_trust and date_trust != "high":
        flags.append(f"Posted dates are {date_trust}-trust on this board: read days_open as a rough age.")
    if board["roles_not_listed"]:
        flags.append(f'{board["roles_not_listed"]} of {len(ids)} roles are counted in BOARD and STACK '
                     "but not listed individually.")

    standing = [{"id": pid, "days_open": ages[pid], "is_leadership": leadership(pid)} for pid in ids]
    curated_keys = sorted((k for k in keys if k.endswith("postings.jsonl") and f"company={slug}/" in k),
                          key=_dt, reverse=True)
    start = dts[0] if len(dts) > 1 else None
    out = {
        "kind": "overview",
        "company": {"name": curr["company"], "slug": slug, "ats": cfg.get("ats"),
                    "careers_url": cfg.get("careers_url"),
                    "board_host": report.board_domain(curr.get("postings") or board_now)},
        "as_of": end,
        "tracking": {"first_crawl": dts[0], "crawls": len(dts), "date_trust": date_trust},
        # start is the first crawl, so a term's new_to_board means "since
        # tracking began"; None on a single crawl, when nothing compares
        **({"persona": persona_section(who, start, board_now, curated_keys, _curated_reader(get_text),
                                         [], [], [], standing, mentions)} if who else {}),
        "data_quality_flags": flags,
        "board": board,
        "stack": stack,
        "postings": postings,
    }
    return out


def _config(keys: list[str], get, slug: str) -> dict:
    """The account's SourceConfig from the lake's configs/ prefix, or {}."""
    for k in keys:
        if "/configs/" in k and k.endswith(".json"):
            try:
                c = get(k)
            except (ValueError, OSError):
                continue
            if c.get("company_slug") == slug:
                return c
    return {}


def _curated_reader(get_text):
    cache: dict[str, dict] = {}

    def curated_at(key: str) -> dict:
        if key not in cache:
            cache[key] = {json.loads(l)["posting_id"]: json.loads(l)
                          for l in get_text(key).splitlines() if l.strip()}
        return cache[key]
    return curated_at


def reader_for(slug: str, persona_file: str | None, personas_map: str | None) -> dict | None:
    if persona_file:
        with open(persona_file) as f:
            data = json.load(f)
        return data.get("persona", data)      # a bare persona, or a whole profile
    if personas_map:
        with open(personas_map) as f:
            return (json.load(f).get(slug) or {}).get("persona")
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("source")
    ap.add_argument("slug")
    ap.add_argument("--week-end", help="last day of the window (default: latest crawl)")
    ap.add_argument("--overview", action="store_true",
                    help="build the overview pack instead: the open board and its stack as of one crawl")
    ap.add_argument("--as-of", help="with --overview: the crawl to describe (default: latest)")
    ap.add_argument("--out", help="write JSON here (default: stdout)")
    who = ap.add_mutually_exclusive_group()
    who.add_argument("--personas", help="the map `tools/tick.py personas list --json` writes")
    who.add_argument("--persona", help="one persona JSON file, for tuning the prompt")
    args = ap.parse_args()

    reader = reader_for(args.slug, args.persona, args.personas)
    if args.overview:
        ev = build_overview(args.source, args.slug, args.as_of, reader)
    else:
        ev = build(args.source, args.slug, args.week_end, reader)
    text = json.dumps(ev, indent=1, ensure_ascii=False)
    who = (", ".join(filter(None, [ev["persona"]["role"], ev["persona"]["sells_for"]]))
           if "persona" in ev else "default")
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            f.write(text)
        if args.overview:
            b = ev["board"]
            print(json.dumps({
                "out": args.out, "as_of": ev["as_of"], "crawls": ev["tracking"]["crawls"],
                "open": b["open_roles"], "tech": b["tech_roles"], "listed": b["roles_listed"],
                "described": sum(1 for p in ev["postings"] if p["description"]),
                "terms": ev["stack"]["terms_on_board"], "flags": len(ev["data_quality_flags"]),
                "reader": who, "approx_tokens": len(text) // 4,
            }))
            return
        b, w = ev["board"], ev["window"]
        print(json.dumps({
            "out": args.out, "window": f'{w["start"]} -> {w["end"]}', "crawl_days": w["crawl_days"],
            "open": f'{b["open_at_start"]} -> {b["open_at_end"]}',
            "opened": b["opened"], "closed": b["closed"], "reopened": b["reopened"],
            "new_terms": len(ev["stack"]["new_to_board"]), "flags": len(ev["data_quality_flags"]),
            "reader": who,
            "approx_tokens": len(text) // 4,
        }))
    else:
        print(text)


if __name__ == "__main__":
    main()
