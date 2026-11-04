"""
Verify a brief against the evidence it was written from, then render it.
Two kinds: a weekly brief (prompt.md, a 7-day evidence pack) and an overview
(prompt-overview.md, a pack built with evidence.py --overview). The pack's
`kind` says which rules apply.

    check.py <brief.json> <evidence.json> [--md OUT.md]

The model's claims are not trusted; these are re-checked in code:
  - the output is bare JSON in the prompt's schema
  - every [[id]] citation names something in the evidence pack
  - the brief was written for the pack's reader (its persona_ref)
  - every paragraph and action item carries at least one citation
  - every quote appears verbatim in the cited posting's description
  - the length, quiet-week and movement limits the prompt sets
  - the brief is the pack's kind, for the pack's week or as_of date

Exit 0 when nothing FAILs (WARNs are allowed), 1 otherwise. A failure is a
finding about the prompt — fix prompt.md and regenerate, never hand-edit the
brief, or the test stops measuring what the agent will do.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# keep in step with SITE_URL and archive_href() in tools/tick_insights_report.py
SITE_URL = "https://tick.sembasolutions.com"

CITE_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
KINDS = {"outreach", "pitch", "watch"}
MAX_WORDS, QUIET_WORDS, MAX_QUOTE_WORDS = 450, 120, 40
OVERVIEW_WORDS, OVERVIEW_PARAS = 550, (3, 5)


def kind_of(ev: dict) -> str:
    """'overview' for a pack built with --overview, else 'weekly' (every pack
    from before overviews existed is weekly and carries no kind)."""
    return "overview" if ev.get("kind") == "overview" else "weekly"


def date_of(ev: dict) -> str:
    """The date a brief is filed under: an overview's as_of, a week's end."""
    return ev["as_of"] if kind_of(ev) == "overview" else ev["window"]["end"]


def s3_prefix(ev: dict) -> str:
    """Where publish.py uploads a brief and its pack, and where the renderer
    (tools/tick_insights_report.py, _BRIEF_RE) finds them — keep in step."""
    part = "overview" if kind_of(ev) == "overview" else "week"
    return f"briefs/company={ev['company']['slug']}/{part}={date_of(ev)}"


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _words(s: str) -> int:
    return len(CITE_RE.sub("", s).split())


def check(raw: str, ev: dict) -> tuple[list[str], list[str], dict | None]:
    fails, warns = [], []
    text = raw.strip()
    if not text.startswith("{"):
        fails.append("output is not bare JSON (prose or a code fence around it)")
        m = re.search(r"\{.*\}", text, re.S)
        text = m.group(0) if m else "{}"
    try:
        brief = json.loads(text)
    except ValueError as exc:
        return fails + [f"not valid JSON: {exc}"], warns, None

    postings = cited_postings(ev)
    persona = ev.get("persona")
    valid = set(postings) | {"BOARD", "STACK"} | ({"PERSONA"} if persona else set())

    overview = kind_of(ev) == "overview"
    required = ("paragraphs", "action_items", "company_slug") + (("as_of",) if overview else ("quiet_week", "week"))
    for key in required:
        if key not in brief:
            fails.append(f"missing key: {key}")
    if brief.get("company_slug") != ev["company"]["slug"]:
        fails.append(f"company_slug {brief.get('company_slug')!r} != {ev['company']['slug']!r}")
    if (brief.get("kind") or "weekly") != kind_of(ev):
        fails.append(f"kind {brief.get('kind')!r} but the pack is {kind_of(ev)!r}")
    if overview:
        if brief.get("as_of") != ev["as_of"]:
            fails.append(f"as_of {brief.get('as_of')!r} does not match the pack's {ev['as_of']!r}")
    else:
        week = brief.get("week") or {}
        if (week.get("start"), week.get("end")) != (ev["window"]["start"], ev["window"]["end"]):
            fails.append(f"week {week} does not match the evidence window")
    # A brief carries the ref of the persona it was written for; a mismatch is
    # a brief written from another pack, or for another reader.
    expected = persona["ref"] if persona else None
    if brief.get("persona_ref") != expected:
        fails.append(f"persona_ref {brief.get('persona_ref')!r} != the pack's {expected!r}")

    def cites(s: str, where: str, required: bool = True) -> None:
        found = CITE_RE.findall(s or "")
        for c in found:
            if c.strip() not in valid:
                fails.append(f"{where}: citation [[{c}]] is not in the evidence pack")
        if required and not found:
            fails.append(f"{where}: no citation")

    if brief.get("headline"):
        warns.append("brief has a headline; the prompt no longer asks for one and it is not shown")

    paras = brief.get("paragraphs") or []
    for i, p in enumerate(paras, 1):
        cites(p, f"paragraph {i}")
    words = sum(_words(p) for p in paras)

    if overview:
        lo, hi = OVERVIEW_PARAS
        if not lo <= len(paras) <= hi:
            fails.append(f"{len(paras)} paragraphs (expected {lo}-{hi})")
        if words > OVERVIEW_WORDS:
            fails.append(f"narrative is {words} words (limit {OVERVIEW_WORDS})")
        if brief.get("quiet_week"):
            fails.append("an overview has no quiet week: it describes the board, not a week of it")
        check_items(brief, postings, cites, fails)
        return fails, warns, brief

    moved = ev["board"]["opened"] + ev["board"]["closed"] + ev["board"]["reopened"]
    quiet = bool(brief.get("quiet_week"))
    if quiet:
        if len(paras) != 1 or words > QUIET_WORDS:
            fails.append(f"quiet week: {len(paras)} paragraphs / {words} words "
                         f"(limit 1 / {QUIET_WORDS})")
        if len(brief.get("action_items") or []) > 1:
            fails.append("quiet week: more than one action item")
    else:
        if not 2 <= len(paras) <= 4:
            fails.append(f"{len(paras)} paragraphs (expected 2-4)")
        if words > MAX_WORDS:
            fails.append(f"narrative is {words} words (limit {MAX_WORDS})")
    if moved == 0 and not quiet:
        warns.append("nothing moved, yet quiet_week is false")

    # action items scale with movement: closures alone are not new demand
    items = brief.get("action_items") or []
    if ev["board"]["opened"] + ev["board"]["reopened"] == 0 and len(items) > 1:
        fails.append(f"{len(items)} action items in a week where nothing opened or reopened (limit 1)")
    check_items(brief, postings, cites, fails)
    return fails, warns, brief


def check_items(brief: dict, postings: dict, cites, fails: list[str]) -> None:
    """The action-item rules both kinds share: count, kind, citations, steps,
    and quotes copied verbatim from a cited posting's description."""
    items = brief.get("action_items") or []
    if len(items) > 3:
        fails.append(f"{len(items)} action items (limit 3)")
    for i, a in enumerate(items, 1):
        where = f"action item {i}"
        if a.get("kind") not in KINDS:
            fails.append(f"{where}: kind {a.get('kind')!r} not in {sorted(KINDS)}")
        cites(a.get("why", ""), f"{where} why")
        steps = a.get("steps") or []
        if not 2 <= len(steps) <= 4:
            fails.append(f"{where}: {len(steps)} steps (expected 2-4)")
        for j, s in enumerate(steps, 1):
            cites(s, f"{where} step {j}", required=False)
        q = a.get("quote")
        if q:
            src = postings.get(q.get("source"))
            if not src:
                fails.append(f"{where}: quote source {q.get('source')!r} is not a posting in the pack")
            elif _ws(q.get("text")) not in _ws(src.get("description") or ""):
                fails.append(f"{where}: quote is not verbatim in {q['source']}'s description")
            if len((q.get("text") or "").split()) > MAX_QUOTE_WORDS:
                fails.append(f"{where}: quote over {MAX_QUOTE_WORDS} words")


def cited_postings(ev: dict) -> dict[str, dict]:
    """Every posting a brief may cite, by id: the week's movement (an
    overview's listed roles), the board's open leadership and longest-open
    roles, the overview's leadership stack, and the reader's standing roles."""
    postings = {p["id"]: p for sec in ("opened", "closed", "reopened", "postings") for p in ev.get(sec, [])}
    for p in (ev.get("stack") or {}).get("leadership_stack", []):
        postings.setdefault(p["id"], p)
    for key in ("open_leadership_roles", "longest_open"):
        for p in ev["board"].get(key, []):
            postings.setdefault(p["id"], p)
    for p in (ev.get("persona") or {}).get("standing_roles", []):
        postings.setdefault(p["id"], p)
    return postings


def reader_of(ev: dict) -> str:
    """"Account executive, Chainguard" for a pack with a persona, else ""."""
    p = ev.get("persona") or {}
    return ", ".join(x for x in (p.get("role"), p.get("sells_for")) if x)


def render(brief: dict, ev: dict) -> str:
    """Markdown with numbered citations, in the order they first appear."""
    postings = cited_postings(ev)
    order: list[str] = []

    def num(m: re.Match) -> str:
        cid = m.group(1).strip()
        if cid not in order:
            order.append(cid)
        return f"<sup>{order.index(cid) + 1}</sup>"

    def sub(s: str) -> str:
        return CITE_RE.sub(num, s or "")

    c = ev["company"]
    reader = f"{' · written for ' + reader_of(ev) if reader_of(ev) else ''}"
    if kind_of(ev) == "overview":
        out = [f"# {c['name']} — overview as of {ev['as_of']}",
               f"*{ev['tracking']['crawls']} crawls since {ev['tracking']['first_crawl']} · "
               f"overview prompt v{brief.get('prompt_version', '?')}{reader}*", ""]
    else:
        w = ev["window"]
        out = [f"# {c['name']} — week to {w['end']}",
               f"*{w['start']} → {w['end']} · prompt v{brief.get('prompt_version', '?')}"
               f"{' · quiet week' if brief.get('quiet_week') else ''}{reader}*", ""]
    out += [sub(p) + "\n" for p in brief.get("paragraphs", [])]
    if brief.get("action_items"):
        out.append("## Action items\n")
        for a in brief["action_items"]:
            out.append(f"### {a.get('title', '')} · `{a.get('kind', '')}`\n")
            out.append(sub(a.get("why", "")) + "\n")
            if a.get("quote"):
                out.append(f"> “{a['quote']['text']}” — {a['quote']['source']}\n")
            out += [f"- {sub(s)}" for s in a.get("steps", [])] + [""]
    out.append("---\n**Sources**\n")
    for i, cid in enumerate(order, 1):
        if cid == "BOARD" and kind_of(ev) == "overview":
            b = ev["board"]
            label = f"Board on {ev['as_of']}: {b['open_roles']} open, {b['tech_roles']} tech roles"
        elif cid == "BOARD":
            b = ev["board"]
            label = (f"Board: {b['open_at_start']} → {b['open_at_end']} open; {b['opened']} opened, "
                     f"{b['closed']} closed, {b['reopened']} reopened")
        elif cid == "STACK":
            label = "Stack terms across the open board"
        elif cid == "PERSONA":
            label = f"The reader's terms on the board ({reader_of(ev)})"
        elif cid in postings:
            p = postings[cid]
            what = (f"closed {p['closed_on']} after {p.get('days_open')} days" if p.get("closed_on")
                    else f"opened {p['opened_on']}" if p.get("opened_on")
                    else f"reopened {p['reopened_on']}" if p.get("reopened_on")
                    else f"open {p.get('days_open')} days")
            # a closed req's board URL is dead; its TICK archive page is not
            url = (f"{SITE_URL}/{ev['company']['slug']}/archive/{re.sub(r'[^A-Za-z0-9_-]', '-', cid)}"
                   if p.get("closed_on") else p.get("url"))
            title = f"[{p['title']}]({url})" if url else p["title"]
            label = f"{cid} — {title}, {what}"
        else:
            label = f"{cid} — not in evidence"
        out.append(f"{i}. {label}")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("brief")
    ap.add_argument("evidence")
    ap.add_argument("--md", help="write the rendered brief here")
    args = ap.parse_args()

    ev = json.load(open(args.evidence))
    fails, warns, brief = check(open(args.brief).read(), ev)
    for f in fails:
        print(f"FAIL  {f}")
    for w in warns:
        print(f"WARN  {w}")
    if brief is not None:
        words = sum(_words(p) for p in brief.get("paragraphs") or [])
        print(f"{'PASS' if not fails else 'FAILED'} — {len(brief.get('paragraphs') or [])} paragraphs, "
              f"{words} words, {len(brief.get('action_items') or [])} action items")
        if args.md:
            with open(args.md, "w") as f:
                f.write(render(brief, ev))
            print(f"rendered {args.md}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
