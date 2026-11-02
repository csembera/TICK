"""
Oracle Taleo Enterprise career sections, collected through the job board's
REST search, and their descriptions, read from each posting's job page.

One file run two ways: the collector imports it, and so does the evaluator's
gate (through tick_common), so a config that verifies collects the same
postings every night. Standard library only. The caller passes the HTTP call,
which is what lets the gate run this over its own client.

A Taleo tenant (<tenant>.taleo.net) has one or more career sections, each
with its own portal id. The sections share one requisition-number sequence,
so a company's sections collect as one board: a config lists them under
`portals`, and every posting is tagged with its section, which its job page
URL needs. The search pages at a size the section fixes (25 is common): a
pageSize in the request is echoed back and ignored, so every page is fetched.
Without tz and tzname headers the search answers 500; a config carries them
in request_headers.

The list carries no description. The job page (jobdetail.ftl) embeds it in a
hidden input, initialHistory: a '!|!'-separated list in which each rich-text
field starts with '!*!', is URL-encoded and backslash-escapes Taleo's
delimiter characters. A posting has two such fields (the description and the
qualifications) and Taleo writes each one twice. Their positions in the list
move between career sections, so they are found by that marker, not by index.
"""

import html
import json
import re
import urllib.parse
from datetime import datetime

# Added to each posting: the career section it was listed in. Taleo's list
# payload has no such key, so it cannot collide with a source field.
SECTION_KEY = "careerSection"

DEFAULT_BODY = {
    "multilineEnabled": False,
    "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
    "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
    "filterSelectionParam": {"searchFilterSelections": []},
    "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
    "pageNo": 1,
}

_HISTORY = re.compile(r'id="initialHistory"[^>]*\bvalue="([^"]*)"'
                      r'|\bvalue="([^"]*)"[^>]*id="initialHistory"')
_ESCAPED = re.compile(r"\\(.)", re.S)
_TOKEN = re.compile(r"\{(\w+)\}")


def _key(posting: dict):
    return posting.get("contestNo") or posting.get("jobId")


def _portal(url: str, body: dict, post_json, max_pages: int) -> tuple[list, int]:
    """One career section's postings, page by page, and the total it reported.

    A posting added while paging shifts the rest down a page, so the next page
    can repeat one already seen; repeats are dropped by requisition number."""
    jobs, ids, reported = [], set(), 0
    for page in range(1, max_pages + 1):
        data = post_json(url, {**body, "pageNo": page})
        reported = int((data.get("pagingData") or {}).get("totalCount") or 0)
        batch = data.get("requisitionList") or []
        for j in batch:
            if _key(j) not in ids:
                ids.add(_key(j))
                jobs.append(j)
        if not batch or len(jobs) >= reported:
            break
    return jobs, reported


def fetch_all(config: dict, post_json, max_pages: int = 100) -> tuple[list, int]:
    """Every posting in every career section in config['portals'], each tagged
    with its section, and the total the sections reported.

    post_json(url, body) POSTs body as JSON and returns the parsed response.
    A section that still comes up short of its own total after one retry
    raises: a partial board diffs the missing postings as closures."""
    endpoint = config["endpoint"]
    body = {**DEFAULT_BODY, **(config.get("request_body") or {})}
    sep = "&" if "?" in endpoint else "?"
    postings, seen, total = [], set(), 0
    for p in config.get("portals") or []:
        url = f"{endpoint}{sep}portal={p['portal']}"
        jobs, reported = _portal(url, body, post_json, max_pages)
        if len(jobs) < reported:
            jobs, reported = _portal(url, body, post_json, max_pages)
        if len(jobs) < reported:
            raise RuntimeError(
                f"taleo: portal {p['portal']} reported {reported} postings but "
                f"{len(jobs)} were collected")
        total += reported
        for j in jobs:
            # a requisition listed in two sections is one opening; the first
            # section in the config keeps it
            if _key(j) in seen:
                continue
            seen.add(_key(j))
            postings.append({**j, SECTION_KEY: p["section"]})
    return postings, total


def detail_url(template: str, posting: dict) -> str:
    """The job page URL: detail_endpoint with {field} filled from the posting."""
    return _TOKEN.sub(lambda m: str(posting.get(m.group(1), "")), template)


def description(page: str) -> str | None:
    """The description HTML embedded in a job page, or None if it has none."""
    m = _HISTORY.search(page or "")
    if not m:
        return None
    parts = []
    for field in html.unescape(m.group(1) or m.group(2)).split("!|!"):
        if not field.startswith("!*!"):
            continue
        text = _ESCAPED.sub(r"\1", urllib.parse.unquote(field[3:])).strip()
        if "<" in text and text not in parts:
            parts.append(text)
    return "".join(parts) or None


def location_text(value):
    """Taleo sends a location column as a JSON array inside a string:
    '["US-TX-San Antonio"]' becomes 'US-TX-San Antonio', several are joined
    with '; '. Anything else passes through."""
    if not (isinstance(value, str) and value.startswith("[")):
        return value
    try:
        items = json.loads(value)
    except ValueError:
        return value
    if not isinstance(items, list):
        return value
    return "; ".join(str(i) for i in items if i)


def iso_date(value):
    """The list's posted date, 'Oct 31, 2026', as '2026-10-31'. Anything else
    passes through."""
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), "%b %d, %Y").date().isoformat()
        except ValueError:
            pass
    return value
