"""
TICK — shared deterministic helpers.

Imported by BOTH tick_tools.py (observation) and verify.py (the gate). The
posting locator and the field resolver live here for one reason: the gate must
resolve fields exactly the way the collector does, and the probe must locate
postings exactly the way the gate does. Two copies would drift, and the drift
would show up as a config that verifies clean and then collects nothing.
"""

from __future__ import annotations

import re
from typing import Any, Optional

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# A browser header set clears most bot blocks; a bare client does not.
BROWSER_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

FINGERPRINTS: list[tuple[str, str]] = [
    ("workday",        r"([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com(?:/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+))?"),
    ("greenhouse",     r"greenhouse\.io/embed/job_board(?:/js)?\?for=([a-z0-9\-_]+)|boards\.greenhouse\.io/(?!embed)([a-z0-9\-_]+)|grnhse\.io"),
    ("lever",          r"jobs\.lever\.co/([A-Za-z0-9\-_]+)"),
    ("ashby",          r"jobs\.ashbyhq\.com/([A-Za-z0-9\-_%]+)"),
    ("workable",       r"apply\.workable\.com/([a-z0-9\-_]+)"),
    ("smartrecruiters", r"careers\.smartrecruiters\.com/([A-Za-z0-9]+)"),
    ("adp",            r"myjobs\.adp\.com/([A-Za-z0-9\-_]+)"),
    ("dayforce",       r"jobs\.dayforcehcm\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9\-_]+)/([A-Za-z0-9\-_]+)"),
]

# Default sweep for an SPA's API contract: quoted /api/ paths, versioned routes,
# job/requisition literals, and absolute URLs (incl. template-literal segments).
BUNDLE_PATTERNS: list[str] = [
    r"[\"'`][^\"'`\n]{0,120}/api/[^\"'`\n]{0,120}[\"'`]",
    r"[\"'`][^\"'`\n]{0,120}/v\d+/[^\"'`\n]{0,120}[\"'`]",
    r"[\"'`][^\"'`\n]{0,100}(?:job|requisition|posting|career|staffing)[^\"'`\n]{0,100}[\"'`]",
    r"https?://[a-zA-Z0-9.\-]+/[A-Za-z0-9/_.\-{}$]{0,120}",
]

# Every container key a supported ATS puts its posting list under.
POSTING_KEYS = ("jobPostings", "jobRequisitions", "jobs", "data", "results",
                "postings", "items")

INDEXED = re.compile(r"^(\w+)\[(\d+)\]$")
URL_TOKEN = re.compile(r"\{(\w+(?:\[\d+\])?(?:\.\w+(?:\[\d+\])?)*)\}")


def resolve_field(posting: dict, expr: Optional[str]):
    """Resolve a schema_map expression. Mirrors the collector's resolver exactly —
    if it cannot resolve here, the collector will not resolve it either."""
    if not expr:
        return None
    cur: Any = posting
    for part in expr.split("."):
        m = INDEXED.match(part)
        if m:
            seq = (cur or {}).get(m.group(1)) if isinstance(cur, dict) else None
            idx = int(m.group(2))
            cur = seq[idx] if isinstance(seq, list) and len(seq) > idx else None
        else:
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur


def locate_postings(data: Any) -> tuple[list, Optional[str]]:
    """Find the posting list in a response body.

    A container that exists but is empty is NOT the same as a body with no
    container: the first means the endpoint works and the board is empty, the
    second means the config is pointed at the wrong shape. Returning the
    container name in the empty case is what lets the caller tell them apart.
    """
    if isinstance(data, list):
        return data, "<root list>"
    if not isinstance(data, dict):
        return [], None
    for k in POSTING_KEYS:
        v = data.get(k)
        if isinstance(v, list) and (not v or isinstance(v[0], dict)):
            return v, k
    for k, v in data.items():  # fall back to any list-of-dicts
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v, k
    return [], None
