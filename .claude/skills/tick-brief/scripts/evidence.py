"""
Build the evidence pack the weekly brief is written from.

    evidence.py <lake-root> <company-slug> [--week-end YYYY-MM-DD] [--out PATH]

<lake-root> is a local lake (e.g. local-data/tick-data-backup-2026-10-30) or
s3://bucket. The window is the 7 days ending on --week-end (default: the
latest crawl): every posting that opened, closed or reopened in it, with its
description, plus the board and stack context needed to read that movement.

This is the ONLY input the brief writer gets, and it is deterministic — the
model interprets, it never counts. Every item carries an `id` the brief cites
as [[id]]: a posting id, or BOARD / STACK for the aggregate sections.

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


def build(source: str, slug: str, week_end: str | None = None) -> dict:
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

    cfg = {}
    for k in keys:
        if "/configs/" in k and k.endswith(".json"):
            try:
                c = get(k)
            except (ValueError, OSError):
                continue
            if c.get("company_slug") == slug:
                cfg = c
                break

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
        "data_quality_flags": flags,
        "board": board,
        "stack": stack,
        "opened": opened_out,
        "closed": closed_out,
        "reopened": reopened_out,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("source")
    ap.add_argument("slug")
    ap.add_argument("--week-end", help="last day of the window (default: latest crawl)")
    ap.add_argument("--out", help="write JSON here (default: stdout)")
    args = ap.parse_args()

    ev = build(args.source, args.slug, args.week_end)
    text = json.dumps(ev, indent=1, ensure_ascii=False)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            f.write(text)
        b, w = ev["board"], ev["window"]
        print(json.dumps({
            "out": args.out, "window": f'{w["start"]} -> {w["end"]}', "crawl_days": w["crawl_days"],
            "open": f'{b["open_at_start"]} -> {b["open_at_end"]}',
            "opened": b["opened"], "closed": b["closed"], "reopened": b["reopened"],
            "new_terms": len(ev["stack"]["new_to_board"]), "flags": len(ev["data_quality_flags"]),
            "approx_tokens": len(text) // 4,
        }))
    else:
        print(text)


if __name__ == "__main__":
    main()
