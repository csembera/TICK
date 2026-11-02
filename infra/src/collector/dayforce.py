"""
Dayforce job boards, collected through the job feed Dayforce publishes for
external job boards: GET https://www.dayforcehcm.com/Api/<clientNamespace>/V1/JobFeeds.

One file run two ways, like taleo.py: the collector imports it, and so does
the evaluator's gate (through tick_common), so a config that verifies
collects the same postings every night. Standard library only. The caller
passes the HTTP call.

The board itself (jobs.dayforcehcm.com) searches through an API that sits
behind a bot rule, so the collector does not use it. The feed is the
documented syndication route: a tenant opens it by enabling external job
board sourcing, and it then answers without credentials and lists active
postings only. A tenant that has not enabled it answers with an error, which
fails the run, as any broken source should.

The feed sends one row per posting per location, and a company often posts
one requisition several times, once per office, sometimes weeks apart; each
save of a posting can also mint a new posting number. A requisition is the
opening, so rows are grouped by ParentRequisitionCode into one posting each.
The first posting stands for the requisition (its title, page and text), the
others add their locations. Locations are sorted, so the feed's row order
cannot change a posting from one day to the next.

Descriptions come in the feed as plain text, a line per paragraph and '•'
bullets. They are rewritten as paragraphs and lists so the description parser
sees the same structure a board's HTML would give it.
"""

import html
import re

# Added to each grouped posting. The feed's own keys are PascalCase without
# these names, so they cannot collide with a source field.
LOCATIONS_KEY = "Locations"
POSTINGS_KEY = "ReferenceNumbers"
DESCRIPTION_KEY = "DescriptionHtml"
ROWS_KEY = "FeedRows"   # every feed row grouped here, so raw/ keeps the whole feed

REMOTE = "Remote"

# Tags a description written as HTML would use; '<anything>' in plain text is not one
_TAG = re.compile(r"</?(?:p|br|div|span|ul|ol|li|b|strong|em|i|u|h[1-6]|table)\b[^>]*>", re.I)
_BULLET = re.compile(r"^[•▪●◦·]\s*")


def _key(row: dict) -> str:
    """The requisition a row belongs to. A row without one stays its own
    posting, under a prefix that keeps it apart from requisition codes."""
    req = row.get("ParentRequisitionCode")
    return str(req) if req not in (None, "") else f"posting-{row.get('ReferenceNumber')}"


def _ref(row: dict) -> int:
    try:
        return int(row.get("ReferenceNumber"))
    except (TypeError, ValueError):
        return 0


def _place(row: dict) -> str:
    """'City, ST', or Remote for a virtual location with no address."""
    place = ", ".join(str(p).strip() for p in (row.get("City"), row.get("State"))
                      if p and str(p).strip())
    if place:
        return place
    return REMOTE if row.get("IsVirtualLocation") in (True, "True", "true") else ""


def text_html(text):
    """The feed's plain-text description as HTML: a <p> per line, and runs of
    bulleted lines as a <ul>. A description that is already HTML passes
    through."""
    if not isinstance(text, str) or not text.strip() or _TAG.search(text):
        return text
    out, items = [], []
    for line in (ln.strip() for ln in text.splitlines()):
        if not line:
            continue
        if _BULLET.match(line):
            items.append(_BULLET.sub("", line))
            continue
        if items:
            out.append("<ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in items) + "</ul>")
            items = []
        out.append(f"<p>{html.escape(line)}</p>")
    if items:
        out.append("<ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in items) + "</ul>")
    return "".join(out)


def group(rows: list) -> list:
    """One posting per requisition, in the order the feed first lists each."""
    by_req: dict[str, list] = {}
    for r in rows:
        by_req.setdefault(_key(r), []).append(r)
    postings = []
    for req_rows in by_req.values():
        first = min(req_rows, key=lambda r: (str(r.get("DatePosted") or ""), _ref(r)))
        places = {_place(r) for r in req_rows} - {""}
        # Remote last: an office list reads first
        locations = sorted(places - {REMOTE}) + ([REMOTE] if REMOTE in places else [])
        postings.append({
            **first,
            "Title": (first.get("Title") or "").strip(),
            "Location": "; ".join(locations),
            "DatePosted": (str(first.get("DatePosted") or "")[:10]) or None,
            LOCATIONS_KEY: locations,
            POSTINGS_KEY: sorted({_ref(r) for r in req_rows}),
            DESCRIPTION_KEY: text_html(first.get("Description")),
            ROWS_KEY: req_rows,
        })
    return postings


def fetch_all(config: dict, get_json) -> tuple[list, int]:
    """Every requisition on the feed, and how many there are.

    get_json(url) GETs url and returns the parsed response. The feed answers
    with a bare list; anything else means the endpoint has changed, and
    raising sends the company back to the evaluator."""
    data = get_json(config["endpoint"])
    if not isinstance(data, list):
        raise RuntimeError(
            f"dayforce: job feed answered {type(data).__name__}, not a list of postings")
    postings = group(data)
    return postings, len(postings)
