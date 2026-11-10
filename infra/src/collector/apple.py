"""
Apple's own careers site, jobs.apple.com, collected through the JSON API its
search page calls: POST /api/v1/search for the list, GET
/api/v1/jobDetails/<positionId> for each description.

One file run two ways, like taleo.py: the collector imports it, and so does
the evaluator's gate (through tick_common), so a config that verifies
collects the same postings every night. Standard library only.

Unlike the other boards, the search needs a session. It answers an empty
list until the client has loaded the search page (which sets a cookie) and
sends the CSRF token /api/v1/CSRFToken returns in a response header, and its
body must carry the `format` block the site sends. So this module owns its
HTTP: a cookie jar and the token, under the collector's own User-Agent.
Tests pass their own opener.

The search answers 20 rows a page and lists a position once per location
(same positionId, a reqId suffix per location). A position is the opening, so
rows are grouped by positionId into one posting each, as dayforce.py groups
by requisition: the first row stands for the position, the others add their
locations. Rows on a team in the config's exclude_teams (Apple Retail's
store roles, APPST) are dropped before grouping.

A detail takes two to five seconds to answer, and a US board holds
thousands of positions, so descriptions are fetched by fetch_descriptions():
a few at a time, newest first, until the config's time budget runs out or
the run's own deadline (the Lambda's remaining time, less what writing the
results needs) comes, whichever is first. Slow paging then costs that
night's descriptions, never the run. The collector asks only for positions
it has no description for yet (reuse_descriptions); any left over are
fetched on the next run.
"""

import html
import http.cookiejar
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

# Added to each grouped posting. Apple's own keys are camelCase, so these
# PascalCase names cannot collide with a source field.
LOCATION_KEY = "Location"
LOCATIONS_KEY = "Locations"
TEAM_KEY = "Team"
POSTED_KEY = "PostedOn"
REQ_IDS_KEY = "ReqIds"
ROWS_KEY = "SearchRows"   # every search row grouped here, so raw/ keeps them
DESCRIPTION_KEY = "DescriptionHtml"

UA = "Mozilla/5.0 (tick-collector)"   # the collector's, as in app._http_json
PAGE_SIZE = 20       # fixed by the site
MAX_PAGES = 400      # 8,000 rows: a US board is ~4,500
TIMEOUT_S = 30
DETAIL_TIMEOUT_S = 15   # a straggler holds up its whole batch; most answer in 2-5 s
DEFAULT_BUDGET_S = 600
DEFAULT_WORKERS = 4
PAGE_TRIES = 3       # one slow page in ~230 must not fail the night's run

# The detail's text fields, in page order, under the headings the site shows.
# Qualifications are one requirement per line, so they become list items.
_SECTIONS = (("jobSummary", "Summary", False),
             ("description", "Description", False),
             ("minimumQualifications", "Minimum Qualifications", True),
             ("preferredQualifications", "Preferred Qualifications", True))


class Session:
    """A cookie jar and the CSRF token, which the search needs. open(req)
    returns the response; tests replace it."""

    def __init__(self, base: str, locale: str = "en-us", opener=None, timeout: float | None = None):
        self.base = base.rstrip("/")
        self.locale = locale
        self.timeout = timeout or TIMEOUT_S
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.token = None

    def _open(self, url: str, data: bytes | None = None, headers: dict | None = None,
              method: str = "GET"):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"User-Agent": UA, **(headers or {})})
        return self.opener.open(req, timeout=self.timeout)

    def start(self) -> None:
        """Load the search page for its cookie, then take the CSRF token."""
        self._open(f"{self.base}/{self.locale}/search").read()
        resp = self._open(f"{self.base}/api/v1/CSRFToken")
        resp.read()
        self.token = resp.headers.get("X-Apple-CSRF-Token")
        if not self.token:
            raise RuntimeError("apple: /api/v1/CSRFToken returned no X-Apple-CSRF-Token header")

    def post_json(self, url: str, body: dict) -> dict:
        resp = self._open(url, json.dumps(body).encode(), method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "X-Apple-CSRF-Token": self.token or ""})
        return json.loads(resp.read().decode())

    def get_json(self, url: str, timeout: float | None = None) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        resp = self.opener.open(req, timeout=timeout or self.timeout)
        return json.loads(resp.read().decode())


def session_for(config: dict, opener=None) -> Session:
    parts = urlsplit(config["endpoint"])
    locale = (config.get("request_body") or {}).get("locale") or "en-us"
    return Session(f"{parts.scheme}://{parts.netloc}", locale, opener, config.get("timeout_s"))


def _place(loc: dict) -> str:
    """'Cupertino', or 'Austin, Texas' when the site gives a state the name lacks."""
    name = (loc.get("name") or "").strip()
    state = (loc.get("stateProvince") or "").strip()
    return f"{name}, {state}" if name and state and state not in name else name


def _team_code(row: dict) -> str | None:
    team = row.get("team")
    return team.get("teamCode") if isinstance(team, dict) else None


def group(rows: list, exclude_teams=()) -> list:
    """One posting per position, in the order the search first lists each."""
    skip = set(exclude_teams or ())
    by_pos: dict[str, list] = {}
    for r in rows:
        if not r.get("positionId") or _team_code(r) in skip:
            continue
        by_pos.setdefault(str(r["positionId"]), []).append(r)
    postings = []
    for pos_rows in by_pos.values():
        first = min(pos_rows, key=lambda r: (str(r.get("postDateInGMT") or ""), str(r.get("reqId") or "")))
        places = sorted({p for r in pos_rows for loc in r.get("locations") or [] if (p := _place(loc))})
        team = first.get("team") if isinstance(first.get("team"), dict) else {}
        postings.append({
            **first,
            "postingTitle": (first.get("postingTitle") or "").strip(),
            LOCATION_KEY: "; ".join(places),
            LOCATIONS_KEY: places,
            TEAM_KEY: team.get("teamName"),
            POSTED_KEY: (str(first.get("postDateInGMT") or "")[:10]) or None,
            REQ_IDS_KEY: sorted({str(r.get("reqId")) for r in pos_rows if r.get("reqId")}),
            ROWS_KEY: sorted(pos_rows, key=lambda r: str(r.get("reqId") or "")),
        })
    return postings


def _lines(text) -> list[str]:
    return [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]


def detail_html(detail: dict) -> str | None:
    """A detail payload as the HTML the description parser reads: each text
    field under the heading the site gives it, then the location's footers
    (pay and benefits), which the site already sends as HTML."""
    out = []
    for key, heading, as_list in _SECTIONS:
        lines = _lines(detail.get(key))
        if not lines:
            continue
        out.append(f"<h3>{heading}</h3>")
        if as_list:
            out.append("<ul>" + "".join(f"<li>{html.escape(ln)}</li>" for ln in lines) + "</ul>")
        else:
            out.extend(f"<p>{html.escape(ln)}</p>" for ln in lines)
    for footer in detail.get("postingFooters") or []:
        locs = footer.get("localizations") or {}
        for part in locs.get("en_US") or next(iter(locs.values()), None) or []:
            if part.get("content"):
                out.append(f"<h3>{html.escape(part.get('name') or '')}</h3>{part['content']}")
    return "".join(out) or None


def _page(s: Session, url: str, body: dict) -> dict:
    """One search page, retried: a timeout is transient here, and the run
    has hundreds of pages to get through."""
    for attempt in range(PAGE_TRIES):
        try:
            return (s.post_json(url, body) or {}).get("res") or {}
        except (OSError, ValueError):
            if attempt == PAGE_TRIES - 1:
                raise
            time.sleep(2 * (attempt + 1))
    return {}


def fetch_all(config: dict, session: Session | None = None) -> tuple[list, int]:
    """Every position the search lists, grouped, and how many rows it reported.

    Fails the run, as any broken source should, when the search answers
    nothing or stops short of its own total."""
    s = session or session_for(config)
    s.start()
    body = dict(config.get("request_body") or {})
    rows, total, page = [], None, 1
    while page <= MAX_PAGES:
        res = _page(s, config["endpoint"], {**body, "page": page})
        if total is None:
            total = int(res.get("totalRecords") or 0)
        batch = res.get("searchResults") or []
        if not batch:
            break
        rows.extend(batch)
        if len(rows) >= total:
            break
        page += 1
    total = total or 0
    if not total:
        # the search answers 0 when the session or the body's format block is
        # wrong, never for a board this size: treat it as a broken source
        raise RuntimeError("apple: search reported 0 postings — session or request body has changed")
    # A posting closed mid-crawl shifts later rows up a page, so a few can be
    # missed; many missing means paging broke.
    if len({r.get("id") for r in rows}) < 0.95 * total:
        raise RuntimeError(f"apple: search reported {total} rows but paging returned {len(rows)}")
    return group(rows, config.get("exclude_teams")), total


def fetch_description(config: dict, posting: dict, session: Session | None = None) -> str | None:
    """One position's description HTML. The detail answers without a session."""
    s = session or session_for(config)
    url = config["detail_endpoint"].replace("{posting_id}", str(posting.get("positionId")))
    data = s.get_json(url, DETAIL_TIMEOUT_S)
    return detail_html(data.get("res") if isinstance(data.get("res"), dict) else data)


def fetch_descriptions(config: dict, postings: list, session: Session | None = None,
                       clock=time.monotonic, stop_at: float | None = None) -> int:
    """Attach DESCRIPTION_KEY to as many of postings as the time budget
    allows, a few at a time, in list order (newest first). stop_at, on the
    same clock, is the run's own deadline and wins when it comes first.
    Returns how many were attached. A failed detail is skipped, never fatal."""
    s = session or session_for(config)
    workers = max(1, int(config.get("detail_workers") or DEFAULT_WORKERS))
    deadline = clock() + float(config.get("detail_budget_s") or DEFAULT_BUDGET_S)
    if stop_at is not None:
        # a batch can run one detail timeout past the check, so stop that much early
        deadline = min(deadline, stop_at - DETAIL_TIMEOUT_S)

    def one(p):
        try:
            return fetch_description(config, p, s)
        except Exception:
            return None

    got = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i in range(0, len(postings), workers):
            if clock() >= deadline:
                break
            batch = postings[i:i + workers]
            for p, h in zip(batch, pool.map(one, batch)):
                if h:
                    p[DESCRIPTION_KEY] = h
                    got += 1
    return got
