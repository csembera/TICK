"""
A one-page teaser of one account, for outreach to a prospect who sells to it.

    python3 tools/teaser.py <slug>
    python3 tools/teaser.py <slug> --paragraph 2 --action 1 --pdf
    python3 tools/teaser.py <slug> --list            # number the brief's paragraphs and moves

It reads the account's current brief and the evidence pack it was written from
(newest weekly brief, else its overview; --brief picks another) and lays out a
glimpse of it: the board's numbers, one paragraph and one move from the brief,
the tech and leadership roles that opened, and the stack terms on the move.
Two files come out: teaser.html, a printable page (--pdf also saves it as a
PDF through headless Chrome), and email.html, the same content in the shared
email layout (tools/email_layout.py) for pasting into an email.

The brief's words are quoted whole, never trimmed or rewritten: a brief is
written by a model, never by code. Its [[id]] citations become numbered links
to the postings. A brief written for one user's persona is refused, since it
is tailored to someone else. Output lands in local-data/teasers/, which is
gitignored, because it holds real lake data.
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys
from datetime import date

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import email_layout as mail  # noqa: E402
import tick_insights_report as report  # noqa: E402

LAKE = os.path.join(ROOT, "local-data", "lake")
OUT = os.path.join(ROOT, "local-data", "teasers")
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
SITE = mail.SITE
_CITE = re.compile(r"\[\[([^\[\]]+)\]\]")
_CITE_RUN = re.compile(r"(?:\s*\[\[[^\[\]]+\]\])+")   # with the space before it, which the mark replaces
MAX_ROLES = 4


# ------------------------------------------------------------------ content

def pick_brief(entries: list[dict], choice: str | None) -> dict:
    """The brief to quote: the account's current one, the newest of a kind
    ("weekly", "overview"), or one by id ("weekly-2026-11-08")."""
    if not entries:
        raise SystemExit("no brief for this account yet: the tick-brief skill writes one")
    if not choice:
        return report.current_brief(entries)
    for e in entries:
        if choice in (e["kind"], e["id"]):
            return e
    raise SystemExit(f"no brief {choice!r}; there are: {', '.join(e['id'] for e in entries)}")


def _day(iso: str) -> date:
    return date.fromisoformat(iso[:10])


def period(evidence: dict) -> str:
    if evidence.get("kind") == "overview":
        d = _day(evidence["as_of"])
        return f"As of {d:%b} {d.day}, {d.year}"
    a, b = _day(evidence["window"]["start"]), _day(evidence["window"]["end"])
    if a.month == b.month:
        return f"Week of {a:%b} {a.day} – {b.day}, {b.year}"
    return f"Week of {a:%b} {a.day} – {b:%b} {b.day}, {b.year}"


def postings_of(evidence: dict) -> dict[str, dict]:
    """Every posting the pack names, by id, as the brief's citations see them."""
    out: dict[str, dict] = {}
    for sec in ("opened", "closed", "reopened", "postings"):
        for p in evidence.get(sec) or []:
            out.setdefault(p["id"], p)
    board = evidence["board"]
    for key in ("open_leadership_roles", "longest_open"):
        for p in board.get(key) or []:
            out.setdefault(p["id"], p)
    for p in (evidence.get("persona") or {}).get("standing_roles", []):
        out.setdefault(p["id"], p)
    return out


def board_rows(lake_io: dict, slug: str) -> dict[str, dict]:
    """posting_id → its curated row, for every role open at the newest crawl.
    The pack's standing lists (open leadership roles, the longest open) carry
    no URL or location, so a role cited from them is filled in from here, as
    the site's brief does; a role missing from here has closed since."""
    dirs = sorted({k.replace(os.sep, "/").rsplit("/", 1)[0] for k in lake_io["keys"]
                   if f"curated/company={slug}/dt=" in k.replace(os.sep, "/")})
    if not dirs:
        return {}
    key = next((k for k in lake_io["keys"] if k.replace(os.sep, "/") == f"{dirs[-1]}/postings.jsonl"), None)
    if not key:
        return {}
    rows = (json.loads(line) for line in lake_io["get_text"](key).splitlines() if line.strip())
    return {r["posting_id"]: r for r in rows}


class Citations:
    """Numbers the brief's [[id]] citations in reading order, one number per
    id, and keeps the footnotes they point to."""

    def __init__(self, evidence: dict, board: dict[str, dict] | None = None):
        self.postings = postings_of(evidence)
        for pid, row in (board or {}).items():
            if pid in self.postings and not self.postings[pid].get("url"):
                self.postings[pid] = dict(self.postings[pid], url=row.get("url"))
        self.name = evidence["company"]["name"]
        self.period = period(evidence)
        self.notes: list[dict] = []
        self.number: dict[str, int] = {}

    def note(self, cid: str) -> dict:
        if cid not in self.number:
            p = self.postings.get(cid) or {}
            label = {"BOARD": f"{self.name}'s open board, {self.period[0].lower()}{self.period[1:]}",
                     "STACK": "Stack terms across the open board",
                     "PERSONA": "The reader's profile"}.get(cid) or p.get("title") or cid
            self.notes.append({"n": len(self.notes) + 1, "label": label, "url": p.get("url")})
            self.number[cid] = len(self.notes)
        return self.notes[self.number[cid] - 1]

    def html(self, text: str, link_style: str = "") -> str:
        """The model's text escaped, its citations as numbered superscripts."""
        style = f' style="{link_style}"' if link_style else ""

        def mark(cid: str) -> str:
            n = self.note(cid.strip())
            return f'<a href="{html.escape(n["url"])}"{style}>{n["n"]}</a>' if n["url"] else str(n["n"])

        def sup(m: re.Match) -> str:
            # a run of citations is one superscript, "1,2", not "12"
            return "<sup>" + ",".join(dict.fromkeys(mark(c) for c in _CITE.findall(m.group(0)))) + "</sup>"
        # escaping first leaves [[...]] untouched, so the markers still match
        return _CITE_RUN.sub(sup, html.escape(text, quote=False))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def kpis(evidence: dict) -> list[dict]:
    """The board's numbers. Open roles always shows; any other at zero is
    left out, since "0 leadership seats" sells nothing."""
    b = evidence["board"]
    if evidence.get("kind") == "overview":
        tiles = [{"label": "Open roles", "value": b["open_roles"]},
                 {"label": "Tech roles open", "value": b["tech_roles"]},
                 {"label": "Leadership seats open", "value": b["leadership_roles"]}]
    else:
        net = b["net_change"]
        tiles = [{"label": "Open roles", "value": b["open_at_end"],
                  "delta": (f"+{net}" if net > 0 else f"−{-net}") if net else None, "up": net > 0},
                 {"label": "Closed this week", "value": b["closed"]},
                 {"label": "Tech roles open", "value": b["tech_roles_open"]},
                 {"label": "Leadership seats open", "value": len(b.get("open_leadership_roles") or [])}]
    return tiles[:1] + [t for t in tiles[1:] if t["value"]]


def headline(evidence: dict) -> str:
    name, b = evidence["company"]["name"], evidence["board"]
    if evidence.get("kind") != "overview" and b["opened"]:
        return f"{name} opened {_plural(b['opened'], 'role')} last week. Here's what they're building."
    total = b["open_roles"] if evidence.get("kind") == "overview" else b["open_at_end"]
    return f"{name} has {_plural(total, 'role')} open. Here's what they're hiring for."


def new_terms(evidence: dict) -> set[str]:
    stack = evidence.get("stack") or {}
    rows = stack.get("new_to_board") if evidence.get("kind") != "overview" else stack.get("since_first_crawl")
    return {r["term"] for r in rows or []}


def roles(evidence: dict, cited: list[str] = (), board: dict[str, dict] | None = None) -> list[dict]:
    """Up to MAX_ROLES open roles, one row per title with how many share it.
    First the ones the quoted text cites, in the order it cites them, then the
    week's openings (an overview: the open board): tech roles with named
    stack, then other tech and leadership roles, then the rest, newest first
    within each. Open means on the newest crawl when the lake is at hand,
    else open by the pack."""
    overview = evidence.get("kind") == "overview"
    pack = postings_of(evidence)
    opened = {p["id"]: p for p in evidence.get("opened") or []}
    closed = {p["id"] for p in evidence.get("closed") or []}

    def is_open(pid: str) -> bool:
        if board:
            return pid in board
        return opened[pid].get("still_open", True) if pid in opened else pid in pack and pid not in closed

    if overview:
        fresh = sorted(evidence.get("postings") or [],
                       key=lambda p: p.get("days_open") if p.get("days_open") is not None else 1 << 30)
    else:
        fresh = sorted(opened.values(), key=lambda p: p.get("opened_on") or "", reverse=True)
    tier = lambda p: 0 if p.get("is_tech_role") and p.get("stack_terms") else \
        1 if p.get("is_tech_role") or p.get("is_leadership") else 2
    ids = [pid for pid in dict.fromkeys(cited) if pid in pack or pid in (board or {})]
    ids += [p["id"] for p in sorted(fresh, key=tier)]
    rows: dict[str, dict] = {}
    for pid in dict.fromkeys(ids):
        if not is_open(pid):
            continue
        p, b = pack.get(pid) or {}, (board or {}).get(pid) or {}
        title = p.get("title") or b.get("title")
        if not title:
            continue
        if title in rows:
            rows[title]["count"] += 1
        elif len(rows) < MAX_ROLES:
            rows[title] = {"title": title, "url": p.get("url") or b.get("url"),
                           "location": p.get("location") or b.get("location"),
                           "when": None if overview else (opened.get(pid) or {}).get("opened_on"),
                           "terms": (p.get("stack_terms") or [])[:3], "count": 1}
    new = new_terms(evidence)
    for row in rows.values():
        row["terms"] = [{"term": t, "new": t in new} for t in row["terms"]]
    return list(rows.values())


def stack_rows(evidence: dict) -> list[tuple[str, str]]:
    stack = evidence.get("stack") or {}
    if evidence.get("kind") == "overview":
        terms = [t for cat in (stack.get("by_category") or {}).values() for t in cat if not t.get("baseline")]
        terms.sort(key=lambda t: -t["postings"])
        return [(t["term"], _plural(t["postings"], "role")) for t in terms[:4]]
    out = [(r["term"], f"{r['from']} → {r['to']} roles") for r in (stack.get("rising") or [])[:3]]
    fresh = [r["term"] for r in stack.get("new_to_board") or []]
    if fresh:
        out.append((", ".join(fresh[:3]), "first time on the board"))
    return out


def build(brief: dict, evidence: dict, paragraph: int, action: int,
          board: dict[str, dict] | None = None) -> dict:
    """Everything the teaser shows, in reading order, with the brief's text
    already escaped and its citations numbered."""
    if brief.get("persona_ref"):
        raise SystemExit("this brief was written for one user's persona; pick a brief for the "
                         "default reader with --brief")
    paras, moves = brief.get("paragraphs") or [], brief.get("action_items") or []
    if not 1 <= paragraph <= len(paras):
        raise SystemExit(f"--paragraph must be 1 to {len(paras)}")
    if moves and not 1 <= action <= len(moves):
        raise SystemExit(f"--action must be 1 to {len(moves)}")
    overview = evidence.get("kind") == "overview"
    cx = Citations(evidence, board)
    excerpt = paras[paragraph - 1]
    move = None
    if moves:
        m = moves[action - 1]
        quote = m.get("quote") or {}
        source = postings_of(evidence).get(quote.get("source") or "") or {}
        move = {"title": m["title"], "why": m["why"], "quote": quote.get("text"),
                "source": source.get("title"), "source_url": source.get("url"),
                "opened": source.get("opened_on")}
    rows = roles(evidence, _CITE.findall(excerpt + " " + (move or {}).get("why", "")), board)
    return {"company": evidence["company"]["name"], "slug": evidence["company"]["slug"],
            "overview": overview,
            "headline": headline(evidence), "kpis": kpis(evidence), "excerpt": excerpt, "move": move,
            "roles": rows, "roles_label": "New on the board" if rows and all(r["when"] for r in rows)
            else "On the board", "stack": stack_rows(evidence), "cites": cx, "board": board or {},
            "brief_label": "From the first brief" if overview else "From this week's brief"}


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    d = _day(iso)
    return f"{d:%b} {d.day}"


# ------------------------------------------------------------------ the page

_MARK = ('<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M2 19h6.5l4-12 4.5 17 3.5-10H30" fill="none" '
         'stroke="currentColor" stroke-width="3" stroke-linejoin="miter" stroke-linecap="square"/></svg>')

_PAGE_STYLE = """
:root{--bg:#f2f2f3;--surface:#fff;--s2:#f5f7fa;--text:#17212b;--t2:#4b5866;--t3:#64717f;--border:#e3e8ee;
  --accent:#3d6a96;--a100:#edf3f9;--a200:#d3e1ee;--live:#2f8f5b;--ink:#080d12;
  --h:"Barlow Condensed",system-ui,sans-serif;--b:"Barlow",system-ui,sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--b);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
.page{width:816px;max-width:100%;margin:24px auto;background:var(--surface);box-shadow:0 6px 18px rgba(16,24,40,.08)}
a{color:var(--accent);text-decoration:none}
.top{background:var(--ink);color:#eef2f5;padding:22px 40px 20px;display:flex;justify-content:space-between;align-items:flex-end;gap:16px;border-bottom:3px solid var(--accent)}
.brand{display:flex;align-items:center;gap:10px;font-family:var(--h);font-weight:700;font-size:24px;letter-spacing:.16em}
.brand svg{width:24px;height:24px;color:#86aace}
.hero{padding:28px 40px 6px}
h1{font-family:var(--h);font-weight:600;font-size:34px;line-height:1.1;margin:0 0 8px;letter-spacing:-.005em}
.lede{margin:0;color:var(--t2);font-size:15px;max-width:640px}
.kpis{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;margin:20px 40px 0;border:1px solid var(--border);border-radius:10px;overflow:hidden}
.kpi{padding:12px 16px;border-right:1px solid var(--border);background:var(--s2)}
.kpi:last-child{border-right:0}
.kpi span{font-size:11.5px;color:var(--t3);font-weight:600;letter-spacing:.04em}
.kpi b{display:block;font-size:26px;font-weight:600;line-height:1.15;letter-spacing:-.02em}
.kpi b small{font-size:13px;font-weight:600;color:var(--t3);margin-left:4px}
.kpi b small.up{color:var(--live)}
.grid{display:grid;grid-template-columns:1.35fr 1fr;gap:28px;padding:24px 40px 8px}
h2{font-family:var(--h);font-weight:600;font-size:19px;margin:0 0 8px;display:flex;align-items:center;gap:8px}
h2 .tag{font-family:var(--b);font-size:10.5px;font-weight:600;letter-spacing:.08em;color:var(--accent);background:var(--a100);border-radius:999px;padding:2px 8px}
.brief p{margin:0 0 10px;font-size:14px}
sup{font-size:10px;font-weight:600;line-height:0;padding:0 1px}
.move{border:1px solid var(--a200);background:var(--a100);border-radius:10px;padding:14px 16px;margin-top:6px}
.move .k{font-size:11px;font-weight:700;letter-spacing:.08em;color:var(--accent);text-transform:uppercase}
.move h3{font-family:var(--h);font-size:18px;font-weight:600;margin:2px 0 6px}
.move .why{font-size:13px;color:var(--t2)}
.move blockquote{margin:8px 0 0;padding:0 0 0 12px;border-left:3px solid var(--accent);color:var(--t2);font-style:italic;font-size:13px}
.move .src{font-size:11.5px;color:var(--t3);margin-top:4px}
.roles{list-style:none;margin:0;padding:0}
.roles li{padding:9px 0;border-bottom:1px solid var(--border)}
.roles li:first-child{padding-top:2px}
.roles .t{font-weight:600;font-size:13.5px;line-height:1.3;display:block;color:var(--text)}
.roles .x{color:var(--t3);font-weight:500}
.roles .m{font-size:12px;color:var(--t3)}
.chip{display:inline-block;font-size:10.5px;padding:1px 7px;border-radius:999px;background:var(--s2);border:1px solid var(--border);color:var(--t2);margin:3px 3px 0 0}
.chip.new{background:#e9f5ee;border-color:#cde8d7;color:var(--live)}
.stack{margin-top:18px}
.stack table{width:100%;border-collapse:collapse;font-size:12.5px}
.stack td{padding:4px 0;border-bottom:1px solid var(--border)}
.stack td:last-child{text-align:right;color:var(--t3);font-variant-numeric:tabular-nums}
.cta{margin:18px 40px 0;padding:18px 20px;border-radius:10px;background:var(--ink);color:#c9d4de;display:flex;align-items:center;justify-content:space-between;gap:20px}
.cta b{color:#eef2f5;font-family:var(--h);font-weight:600;font-size:19px;display:block;line-height:1.2}
.cta .btn{flex:none;background:var(--accent);color:#fff;font-weight:600;padding:10px 18px;border-radius:8px;font-size:14px}
.foot{padding:14px 40px 22px;font-size:11px;color:var(--t3);display:flex;justify-content:space-between;gap:16px}
.foot a{color:var(--t3)}
@media print{body{background:#fff;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .page{margin:0 auto;box-shadow:none;zoom:var(--fit,1)}@page{size:letter;margin:0}}
@media (max-width:700px){.grid{grid-template-columns:1fr}.kpis{grid-auto-flow:row;grid-template-columns:repeat(2,1fr)}
  .top,.hero,.grid,.foot{padding-left:16px;padding-right:16px}.kpis,.cta{margin-left:16px;margin-right:16px}.cta{flex-direction:column;align-items:flex-start}}
"""

# A brief's paragraph runs from three lines to ten, so no fixed layout fits
# every account on one sheet: once the fonts are in, shrink the printed page
# by however much it overflows a letter page (8.5 x 11in at 96px = 816 x 1056).
_FIT = """document.fonts.ready.then(function(){var p=document.querySelector('.page');
var f=Math.min(1,1056/p.offsetHeight);document.documentElement.style.setProperty('--fit',f.toFixed(3));});"""

LEDE = ("TICK reads every posting on {name}'s careers site daily and tells you what changed, "
        "what it means, and who to call.")
CTA_TITLE = "Get this every Monday for the accounts you sell to."
CTA_TEXT = "A dashboard of every posting and a brief written for what you sell."
CTA_BUTTON = "Start with one account"


def _e(s) -> str:
    return html.escape(str(s))


def unlinked_notes(cx: Citations) -> str:
    """The citations a reader cannot follow by clicking (the board, the stack),
    spelled out; every posting's number is already a link, and listing those
    too pushes the page onto a second sheet."""
    return " · ".join(f'{n["n"]} {_e(n["label"])}' for n in cx.notes if not n["url"])


def _role_meta(r: dict) -> str:
    return " · ".join(x for x in (r["location"], _when(r["when"])) if x)


def render_page(c: dict) -> str:
    cx = c["cites"]
    excerpt = cx.html(c["excerpt"])
    move = ""
    if c["move"]:
        m = c["move"]
        quote = f"<blockquote>“{_e(m['quote'])}”</blockquote>" if m["quote"] else ""
        src = " · ".join(x for x in (m["source"], f"opened {_when(m['opened'])}" if m["opened"] else "") if x)
        move = (f'<div class="move"><div class="k">One move to make</div><h3>{_e(m["title"])}</h3>'
                f'<div class="why">{cx.html(m["why"])}</div>{quote}'
                + (f'<div class="src">{_e(src)}</div>' if src else "") + "</div>")
    kpis = "".join(
        f'<div class="kpi"><span>{_e(k["label"])}</span><b>{k["value"]}'
        + (f'<small class="{"up" if k.get("up") else ""}">{k["delta"]}</small>' if k.get("delta") else "")
        + "</b></div>" for k in c["kpis"])
    roles = "".join(
        "<li>" + (f'<a class="t" href="{_e(r["url"])}">' if r["url"] else '<span class="t">') + _e(r["title"])
        + (f' <span class="x">×{r["count"]}</span>' if r["count"] > 1 else "")
        + ("</a>" if r["url"] else "</span>")
        + f'<span class="m">{_e(_role_meta(r))}</span>'
        + ("<br>" + "".join(f'<span class="chip{" new" if t["new"] else ""}">{_e(t["term"])}</span>'
                            for t in r["terms"]) if r["terms"] else "")
        + "</li>" for r in c["roles"])
    roles_block = (f'<h2>{c["roles_label"]}</h2>'
                   f'<ul class="roles">{roles}</ul>') if roles else ""
    stack = "".join(f"<tr><td>{_e(t)}</td><td>{_e(v)}</td></tr>" for t, v in c["stack"])
    stack_block = (f'<div class="stack"><h2>{"Stack on the board" if c["overview"] else "Stack on the rise"}'
                   f'</h2><table>{stack}</table></div>') if stack else ""
    notes = unlinked_notes(cx)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TICK · {_e(c["company"])}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600;700&family=Barlow+Condensed:wght@500;600;700&display=swap">
<style>{_PAGE_STYLE}</style></head><body>
<div class="page">
<div class="top"><div class="brand">{_MARK}TICK</div></div>
<div class="hero"><h1>{_e(c["headline"])}</h1><p class="lede">{_e(LEDE.format(name=c["company"]))}</p></div>
<div class="kpis">{kpis}</div>
<div class="grid">
<div><div class="brief"><h2>{c["brief_label"]} <span class="tag">WRITTEN BY TICK</span></h2><p>{excerpt}</p></div>{move}</div>
<div>{roles_block}{stack_block}</div>
</div>
<div class="cta"><div><b>{CTA_TITLE}</b>{CTA_TEXT}</div><a class="btn" href="{SITE}/">{CTA_BUTTON}</a></div>
<div class="foot"><span>Every number in the text links to the public {_e(c["company"])} posting it cites. {notes}</span><span>tick.sembasolutions.com</span></div>
</div>
<script>{_FIT}</script>
</body></html>
"""


# ------------------------------------------------------------------ the email

# The email is the shared layout (tools/email_layout.py) with the teaser's
# own tiles, roles and stack inside it: see docs/email.md.
_LINK = mail.LINK
OPT_OUT = "Sent by TICK. Not useful? Reply \u201cno thanks\u201d and we won't send another."


def email_rows(c: dict, nested: bool = False) -> list[str]:
    """The teaser's body as bands of the shared layout, without its call to
    action. nested: the bands sit inside another email (the welcome email of
    `tools/tick.py users invite --teaser`), so they open with a section
    label and a smaller heading instead of the page's own headline."""
    cx = c["cites"]
    last = len(c["kpis"]) - 1
    cells = "".join(
        f'<td style="padding:12px 14px;background:{mail.PANEL};'
        + ("" if i == last else f"border-right:1px solid {mail.BORDER};") + f'{mail.FONT}">'
        f'<div style="font-size:11px;font-weight:600;color:{mail.TEXT3}">{_e(k["label"])}</div>'
        f'<div style="font-size:24px;font-weight:600;color:{mail.TEXT}">{k["value"]}'
        + (f'<span style="font-size:13px;color:{mail.LIVE if k.get("up") else mail.TEXT3};margin-left:4px">'
           f'{k["delta"]}</span>' if k.get("delta") else "")
        + "</div></td>" for i, k in enumerate(c["kpis"]))
    if nested:
        top = mail.row(mail.label(f"A first look at {_e(c['company'])}", margin="0 0 6px")
                       + mail.h1(_e(c["headline"])), "36px 32px 0")
    else:
        top = mail.row(mail.h1(_e(c["headline"])) + mail.para(_e(LEDE.format(name=c["company"])), 15),
                       "32px 32px 0")
    rows = [top,
            mail.row(mail.table(f"<tr>{cells}</tr>", f"border:1px solid {mail.BORDER};border-radius:8px"),
                     "20px 32px 0"),
            mail.row(mail.label(c["brief_label"])
                     + mail.para(cx.html(c["excerpt"], _LINK), 15, mail.TEXT))]
    if c["move"]:
        m = c["move"]
        quote = (f'<p style="margin:8px 0 0;padding-left:12px;border-left:3px solid {mail.ACCENT};'
                 f'font-style:italic;font-size:13px;color:{mail.TEXT2}">\u201c{_e(m["quote"])}\u201d</p>'
                 ) if m["quote"] else ""
        rows.append(mail.row(mail.card(
            mail.label("One move to make", mail.ACCENT, "0 0 2px")
            + f'<div style="{mail.FONT_HEAD};font-size:18px;font-weight:600;color:{mail.TEXT};margin:0 0 6px">'
            f'{_e(m["title"])}</div>'
            f'<div style="font-size:13px;line-height:1.55;color:{mail.TEXT2}">{cx.html(m["why"], _LINK)}</div>'
            + quote, tint=True, padding="14px 16px"), "12px 32px 0"))
    roles = "".join(
        f'<tr><td style="padding:8px 0;border-bottom:1px solid {mail.BORDER};{mail.FONT}">'
        + (f'<a href="{_e(r["url"])}" style="font-weight:600;font-size:14px;color:{mail.TEXT};text-decoration:none">'
           if r["url"] else '<span style="font-weight:600;font-size:14px">') + _e(r["title"])
        + (f' <span style="color:{mail.TEXT3};font-weight:500">\u00d7{r["count"]}</span>' if r["count"] > 1 else "")
        + ("</a>" if r["url"] else "</span>")
        + f'<div style="font-size:12px;color:{mail.TEXT3}">{_e(_role_meta(r))}'
        + "".join(f' \u00b7 <span style="color:{mail.LIVE if t["new"] else mail.TEXT2}">{_e(t["term"])}</span>'
                  for t in r["terms"])
        + "</div></td></tr>" for r in c["roles"])
    if roles:
        rows.append(mail.row(mail.label(c["roles_label"]) + mail.table(roles)))
    stack = "".join(f'<tr><td style="padding:4px 0;border-bottom:1px solid {mail.BORDER};font-size:13px;{mail.FONT}">'
                    f'{_e(t)}</td><td align="right" style="padding:4px 0;border-bottom:1px solid {mail.BORDER};'
                    f'font-size:13px;color:{mail.TEXT3};{mail.FONT}">{_e(v)}</td></tr>' for t, v in c["stack"])
    if stack:
        rows.append(mail.row(mail.label("Stack on the board" if c["overview"] else "Stack on the rise")
                             + mail.table(stack)))
    return rows


def cite_note(c: dict) -> str:
    return f"Every number in the text links to the public {_e(c['company'])} posting it cites. " \
           f"{unlinked_notes(c['cites'])}".strip()


def email_text(c: dict) -> list[str]:
    """The teaser's body as plain-text paragraphs, its citations dropped:
    the brief's words still come through whole."""
    plain = lambda t: _CITE_RUN.sub("", t).strip()
    lines = [c["headline"], f"{c['brief_label']}:\n{plain(c['excerpt'])}"]
    if c["move"]:
        lines.append(f"One move to make: {c['move']['title']}\n{plain(c['move']['why'])}")
    if c["roles"]:
        lines.append(c["roles_label"] + ":\n" + "\n".join(
            f"- {r['title']}" + (f" \u00d7{r['count']}" if r["count"] > 1 else "")
            + (f" ({_role_meta(r)})" if _role_meta(r) else "") for r in c["roles"]))
    return lines


def render_email(c: dict) -> str:
    rows = email_rows(c)
    rows.append(mail.row(mail.dark_panel(CTA_TITLE, CTA_TEXT, f"{SITE}/", CTA_BUTTON), "28px 32px 0"))
    return mail.document(f"TICK \u00b7 {c['company']}", c["headline"], rows, note=cite_note(c), reason=OPT_OUT)


# ------------------------------------------------------------------ cli

def save_pdf(page: str, pdf: str) -> None:
    if not os.path.exists(CHROME):
        raise SystemExit(f"Chrome not found at {CHROME}; set CHROME to its binary")
    # the virtual-time budget lets the Barlow web fonts load before printing
    # the window is the sheet's width, so _FIT measures the page as it prints
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--window-size=816,1056",
                    "--virtual-time-budget=8000", f"--print-to-pdf={pdf}", f"file://{os.path.abspath(page)}"],
                   capture_output=True, check=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="A one-page teaser of one account, for a prospect.")
    ap.add_argument("slug", help="the account's company_slug")
    ap.add_argument("--lake", default=LAKE, help="lake root: a local copy (default) or s3://bucket")
    ap.add_argument("--brief", help='"weekly", "overview" or a brief id; default: the current brief')
    ap.add_argument("--paragraph", type=int, default=1, help="which paragraph of the brief to quote (from 1)")
    ap.add_argument("--action", type=int, default=1, help="which move of the brief to show (from 1)")
    ap.add_argument("--list", action="store_true", help="number the brief's paragraphs and moves, then stop")
    ap.add_argument("--pdf", action="store_true", help="also save the page as a PDF through headless Chrome")
    ap.add_argument("--out", help="output directory; default: local-data/teasers/<slug>-<brief>")
    args = ap.parse_args(argv)

    lake_io = report.open_lake(args.lake)
    entry = pick_brief(report.list_briefs(lake_io, args.slug), args.brief)
    brief, evidence = lake_io["get"](entry["brief_key"]), lake_io["get"](entry["evidence_key"])
    if args.list:
        print(f"{entry['id']}" + (" (written for a persona: not usable)" if brief.get("persona_ref") else ""))
        for i, p in enumerate(brief.get("paragraphs") or [], 1):
            print(f"  --paragraph {i}: {re.sub(r' ?' + _CITE.pattern, '', p)[:110].strip()}…")
        for i, m in enumerate(brief.get("action_items") or [], 1):
            print(f"  --action {i}: {m['title']}")
        return 0
    c = build(brief, evidence, args.paragraph, args.action,
              board_rows(lake_io, args.slug))
    out = args.out or os.path.join(OUT, f"{args.slug}-{entry['id']}")
    os.makedirs(out, exist_ok=True)
    page = os.path.join(out, "teaser.html")
    with open(page, "w") as f:
        f.write(render_page(c))
    c["cites"] = Citations(evidence, c["board"])   # the email numbers its citations afresh, in its own order
    with open(os.path.join(out, "email.html"), "w") as f:
        f.write(render_email(c))
    if args.pdf:
        save_pdf(page, os.path.join(out, "teaser.pdf"))
    print(f"Wrote {os.path.relpath(out)}/: teaser.html, email.html" + (", teaser.pdf" if args.pdf else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
