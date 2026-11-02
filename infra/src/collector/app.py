"""
TICK collector worker — executes ONE SourceConfig verbatim.

Invoked by SQS with batch size 1: each invocation handles exactly one
company, so a failure here only retries (then dead-letters) that company.
All intelligence lives in the evaluator; this worker just executes the
config it's handed and fails loudly when the config stops working — the
DLQ is the "re-run the evaluator" signal.

Per invocation:
    fetch (paginate) -> raw/  -> normalize via schema_map -> diff vs state
    -> curated/ (+first_seen/last_seen) -> derived/tech_stack/
    -> derived/snapshots/ (the day's changes) -> state, written last

Safe to run more than once a day (a manual re-crawl, an SQS retry, a
duplicate delivery). Files are keyed by date, and the day's changes would
otherwise be overwritten by the later run's, which sees only what changed
since the earlier one. So a re-run merges into the day's change record, and
postings the earlier run saw that are gone now are kept in departed.jsonl,
while curated/ stays the board as of the latest run. State is written last:
a run that fails partway leaves it untouched, and the retry recomputes the
same changes.

Descriptions keep their structure: raw/ holds each posting's original HTML
(for Workday, fetched per posting and attached before raw/ is written), and
every curated row carries `description`, its labelled sections (see
description.py). The flat `description_raw` text is written only when parsing
fails; readers go through the renderer's parsed_description()/full_text_of().
Keywords come from the title and the role text, not the board's boilerplate.

Local dry run (no AWS): set LOCAL_OUTPUT_DIR to write files to disk:
    LOCAL_OUTPUT_DIR=./out python3 app.py ../../../configs/<slug>_config.json
"""

import hashlib
import html
import json
import os
import re
import time
import urllib.request
from datetime import date, datetime, timezone

import description

BUCKET = os.environ.get("BUCKET_NAME")
LOCAL_DIR = os.environ.get("LOCAL_OUTPUT_DIR")
FETCH_DESCRIPTIONS = os.environ.get("FETCH_DESCRIPTIONS", "1") != "0"

MAX_PAGES = 100
_UA = "Mozilla/5.0 (tick-collector)"
_DETAIL_DELAY_S = 0.1  # politeness gap between per-posting detail GETs
_TIMEOUT_S = 30  # per-request default; a slow source overrides via timeout_s

# Envelope key holding the posting array, per ATS. None means the payload IS
# the array. A source with no entry here supplies its own postings_key.
_POSTINGS_KEY = {"greenhouse": "jobs", "lever": None, "ashby": "jobs"}

_s3 = None
if not LOCAL_DIR:
    import boto3

    _s3 = boto3.client("s3")


# ------------------------------------------------------------------ storage

def _write(key: str, body: str) -> None:
    if LOCAL_DIR:
        path = os.path.join(LOCAL_DIR, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(body)
    else:
        _s3.put_object(Bucket=BUCKET, Key=key, Body=body.encode("utf-8"))


def _read(key: str):
    if LOCAL_DIR:
        path = os.path.join(LOCAL_DIR, key)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return f.read()
    try:
        return _s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
    except _s3.exceptions.NoSuchKey:
        return None


# -------------------------------------------------------------------- fetch

def _http_json(url: str, method: str = "GET", body: dict | None = None,
               headers: dict | None = None, timeout: int | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _UA,
            **(headers or {}),  # config-supplied headers win (auth ids, WAF-clearing UA)
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout or _TIMEOUT_S) as resp:
        return json.loads(resp.read().decode())


def fetch_all(config: dict) -> tuple[list, int]:
    """Pull every live posting for one company, per its SourceConfig."""
    ats = config.get("ats")
    endpoint = config["endpoint"]
    headers = config.get("request_headers") or {}
    timeout = config.get("timeout_s")  # None falls back to _TIMEOUT_S

    if ats == "adp":
        # ADP MyJobs CX. The list payload carries only id/title/location; the
        # per-posting detail adds postingDate, departmentName and jobDescription,
        # so detail_endpoint is merged in before normalize() ever sees the row.
        data = _http_json(endpoint, "GET", None, headers, timeout)
        jobs = list(data.get("jobRequisitions") or [])
        total = data.get("count", len(jobs))
        if total > len(jobs):
            raise RuntimeError(
                f"adp: endpoint reported {total} postings but returned {len(jobs)} — "
                "raise $top in the config endpoint ($skip paging is unstable here)"
            )
        detail_t = config.get("detail_endpoint")
        if detail_t:
            id_expr = (config.get("schema_map") or {}).get("posting_id") or "reqId"
            for j in jobs:
                rid = resolve_field(j, id_expr)
                if not rid:
                    continue
                try:
                    detail = _http_json(
                        detail_t.replace("{posting_id}", str(rid)), "GET", None,
                        headers, timeout)
                except Exception:
                    continue  # one bad detail must not fail the company's run
                row = (detail.get("jobRequisitions") or [None])[0]
                if isinstance(row, dict):
                    j.update(row)
                time.sleep(_DETAIL_DELAY_S)
        return jobs, total

    if ats == "workday":
        body_t = dict(config.get("request_body") or
                      {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""})
        limit = int(body_t.get("limit", 20))
        first = _http_json(endpoint, "POST", {**body_t, "limit": limit, "offset": 0},
                           None, timeout)
        total = first.get("total", 0)
        postings = list(first.get("jobPostings") or [])
        offset, pages = limit, 1
        while offset < total and pages < MAX_PAGES:
            batch = _http_json(endpoint, "POST",
                               {**body_t, "limit": limit, "offset": offset},
                               None, timeout).get("jobPostings") or []
            if not batch:
                break
            postings.extend(batch)
            offset += limit
            pages += 1
        return postings, total

    data = _http_json(endpoint, config.get("http_method", "GET"),
                      config.get("request_body"), headers, timeout)

    # postings_key names the envelope key holding the posting array, so a board
    # with no first-class branch (ats 'other') collects without masquerading as
    # one: normalize() derives the curated `source` column from `ats`, so
    # borrowing another ATS's name to reach its parser would falsify provenance.
    # Explicit null means the payload is a bare array, as Lever's is.
    if "postings_key" in config:
        key = config["postings_key"]
    elif ats in _POSTINGS_KEY:
        key = _POSTINGS_KEY[ats]
    else:
        raise ValueError(
            f"unsupported ats '{ats}' — set postings_key in the config to the "
            "envelope key holding the posting array (null if it is a bare array)")

    if key is None:
        jobs = data if isinstance(data, list) else []
    else:
        jobs = data.get(key) or []
    return jobs, len(jobs)


# ---------------------------------------------------------------- normalize

_INDEXED = re.compile(r"^(\w+)\[(\d+)\]$")
# {placeholder} in a schema_map.url template; the body is a resolve_field expr
_URL_TOKEN = re.compile(r"\{(\w+(?:\[\d+\])?(?:\.\w+(?:\[\d+\])?)*)\}")


def resolve_field(posting: dict, expr):
    """Resolve a schema_map expression like 'title', 'a.b', 'bulletFields[0]'."""
    if not expr:
        return None
    cur = posting
    for part in expr.split("."):
        m = _INDEXED.match(part)
        if m:
            seq = (cur or {}).get(m.group(1)) if isinstance(cur, dict) else None
            idx = int(m.group(2))
            cur = seq[idx] if isinstance(seq, list) and len(seq) > idx else None
        else:
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur


def normalize(posting: dict, config: dict, captured: str) -> dict:
    """Map one source posting to the curated schema (§5 of the design doc)."""
    sm = config.get("schema_map") or {}

    def f(name):
        return resolve_field(posting, sm.get(name))

    title = f("title") or ""
    location = f("location") or ""

    # schema_map.url is either a field path ('externalPath') or a template with
    # {field} placeholders ('https://host/job?reqId={reqId}') for sources that
    # expose no URL field of their own.
    url_expr = sm.get("url")
    if isinstance(url_expr, str) and "{" in url_expr:
        url = _URL_TOKEN.sub(
            lambda m: str(resolve_field(posting, m.group(1)) or ""), url_expr)
    else:
        url = f("url")
    if isinstance(url, str) and url.startswith("/") and config.get("careers_url"):
        url = config["careers_url"].rstrip("/") + url

    posting_id = f("posting_id")
    if not posting_id:  # no stable source id -> hash of company+title+location
        posting_id = hashlib.sha1(
            f"{config['company_slug']}|{title}|{location}".encode()
        ).hexdigest()[:12]

    return {
        "posting_id": str(posting_id),
        "company": config["company_name"],
        "company_slug": config["company_slug"],
        "source": config.get("ats") or f"tier{config.get('tier')}",
        "title": title,
        "team": f("team"),
        "location": location,
        "remote_flag": "remote" in f"{location} {title}".lower(),
        "url": url,
        "posted_on": f("posted_on"),
        "date_trust": config.get("date_trust"),
        "captured_dt": captured,
        "description_raw": None,
        "description": None,
    }


# ------------------------------------------- descriptions + keyword enrichment

_TAG_RE = re.compile(r"<[^>]+>")


def _html_text(raw: str | None) -> str | None:
    """Strip tags/entities from a description blob; None if nothing is left."""
    if not raw:
        return None
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", raw))).strip() or None


def fetch_workday_description(config: dict, external_path) -> str | None:
    """GET the CXS per-posting detail and return the description's HTML.

    Detail endpoint = collection endpoint minus '/jobs', plus the posting's
    externalPath. Returns None on any failure — one bad detail page must not
    fail the company's whole run.
    """
    base = config["endpoint"]
    if not external_path or not base.endswith("/jobs"):
        return None
    try:
        data = _http_json(base[: -len("/jobs")] + external_path, "GET")
    except Exception:
        return None
    return (data.get("jobPostingInfo") or {}).get("jobDescription") or None


# Tech-stack / cloud taxonomy (design doc Phase 4). Categories are additive
# curated columns — extend the lists freely; matching is word-bounded and
# case-insensitive so 'java' won't hit 'javascript', 'spark' won't hit
# 'sparkling'. Use 'golang' not 'go' (false-positive magnet).
_KEYWORDS: dict[str, list[str]] = {
    "cloud_aws": [
        "aws", "amazon web services", "ec2", "s3", "lambda", "dynamodb",
        "redshift", "sagemaker", "eks", "ecs", "cloudformation", "cloudwatch",
        "kinesis", "athena", "glue", "rds", "bedrock",
    ],
    "cloud_azure": [
        "azure", "synapse", "cosmos db", "aks", "azure devops",
    ],
    "cloud_gcp": [
        "gcp", "google cloud", "bigquery", "gke", "dataflow", "vertex ai",
    ],
    # analyst-desk tools — tracked, but excluded from is_tech_role (they show
    # up in FP&A/accounting postings and would flood the tech filter)
    "analytics_tools": [
        "power bi", "looker", "tableau", "qlik", "alteryx",
    ],
    "data_platform": [
        "snowflake", "databricks", "spark", "kafka", "airflow", "dbt", "etl",
        "data lake", "data warehouse", "data engineering", "data pipeline",
    ],
    "infra_devops": [
        "kubernetes", "docker", "terraform", "ansible", "jenkins", "ci/cd",
        "devops", "sre", "site reliability", "microservices", "serverless",
    ],
    "languages": [
        "python", "java", "scala", "golang", "typescript", "javascript",
        "react", "node.js", ".net", "c#", "sql", "rust",
    ],
    "signal_terms": [
        "migration", "modernization", "cloud-native", "digital transformation",
        "greenfield", "re-architect", "scalability", "real-time",
        "machine learning", "generative ai", "genai", "llm",
    ],
}

_KEYWORD_RES: dict[str, list[tuple[str, re.Pattern]]] = {
    cat: [
        (term, re.compile(rf"(?<![\w#./-]){re.escape(term)}(?![\w#-])", re.IGNORECASE))
        for term in terms
    ]
    for cat, terms in _KEYWORDS.items()
}


def extract_keywords(text: str) -> dict[str, list[str]]:
    """Scan text against the taxonomy; return {category: [matched terms]}."""
    if not text:
        return {}
    return {
        cat: hits
        for cat, terms in _KEYWORD_RES.items()
        if (hits := [term for term, rx in terms if rx.search(text)])
    }


def enrich(postings: list[dict], rows: list[dict], config: dict) -> int:
    """Attach description (or, if parsing fails, description_raw) and keyword
    fields to rows in place.

    Returns the number of descriptions fetched. Keywords are extracted from
    title + description, so rows still get title-level keywords when a detail
    fetch fails (or for ATSes without a detail path yet). The description's
    HTML stays on the posting, so raw/ keeps it for every ATS.
    """
    fetched = 0
    ats = config.get("ats")
    workday = ats == "workday" and FETCH_DESCRIPTIONS
    # ADP descriptions arrive with the detail payload fetch_all already merged,
    # so there is nothing extra to fetch (and nothing to sleep between).
    adp = ats == "adp" and FETCH_DESCRIPTIONS
    # description_field is a resolve_field expr naming a description already in
    # the posting payload, for boards that ship it inline with the listing. It
    # costs no extra request, so it wins over the per-ATS defaults.
    desc_expr = config.get("description_field") if FETCH_DESCRIPTIONS else None
    fmt = config.get("description_format")
    for posting, row in zip(postings, rows):
        raw_html = None
        if desc_expr:
            raw_html = resolve_field(posting, desc_expr)
        elif workday:
            raw_html = fetch_workday_description(config, posting.get("externalPath"))
            if raw_html:
                posting["jobDescription"] = raw_html   # so raw/ keeps it
            time.sleep(_DETAIL_DELAY_S)
        elif adp:
            raw_html = posting.get("jobDescription")
        text = _html_text(raw_html) if isinstance(raw_html, str) else None
        if text:
            try:
                row["description"] = description.parse(raw_html, fmt)
            except Exception as exc:   # a parser bug costs the structure, never the run
                print(json.dumps({"description_parse_error": row["posting_id"],
                                  "error": f"{type(exc).__name__}: {exc}"}))
            # the sections hold every line, so the flat copy is kept only when
            # parsing failed: curated stops storing each description twice
            if not row["description"]:
                row["description_raw"] = text
            fetched += 1
        # role text only: a stack term in the board's intro or benefits blurb is
        # on every posting and says nothing about the role
        body = (description.role_text(row["description"]) if row["description"]
                else row["description_raw"] or "")
        kw = extract_keywords(f"{row['title']} {body}")
        row["keywords"] = kw
        row["is_tech_role"] = any(
            cat not in ("signal_terms", "analytics_tools") for cat in kw
        )
    return fetched


def build_tech_summary(rows: list[dict], config: dict, today: str, ts: str) -> dict:
    """Aggregate per-posting keywords into a company tech-stack summary.

    Every term maps to the posting_ids it was found in, and 'postings' is the
    evidence index — so each claim in the summary is traceable to the job
    descriptions it was derived from.
    """
    stack: dict[str, dict[str, list[str]]] = {}
    evidence: dict[str, dict] = {}
    for r in rows:
        if not r.get("keywords"):
            continue
        evidence[r["posting_id"]] = {
            "title": r["title"], "location": r["location"],
            "url": r["url"], "is_tech_role": r["is_tech_role"],
        }
        for cat, terms in r["keywords"].items():
            for term in terms:
                stack.setdefault(cat, {}).setdefault(term, []).append(r["posting_id"])
    return {
        "company": config["company_name"],
        "company_slug": config["company_slug"],
        "captured_dt": today,
        "captured_at": ts,
        "postings_total": len(rows),
        "tech_role_ids": [r["posting_id"] for r in rows if r.get("is_tech_role")],
        "stack": stack,        # {category: {term: [posting_id, ...]}}
        "postings": evidence,  # {posting_id: {title, location, url, is_tech_role}}
    }


# --------------------------------------------------------------------- diff

def merge_day_diff(earlier: dict | None, now: dict) -> dict:
    """One day's changes across every run that day: the union of each run's
    new, closed and reopened, in the order first seen. A re-run that finds
    nothing new leaves the day's record as it was."""
    if not earlier:
        return now
    return {k: list(dict.fromkeys([*(earlier.get(k) or []), *now[k]]))
            for k in ("new", "closed", "reopened")}


def departed_rows(earlier_curated: str | None, earlier_departed: str | None,
                  rows: list[dict]) -> list[dict]:
    """Rows an earlier run today saw that this run did not: a posting that
    opened and closed between two same-day runs. curated/ must stay the board
    as of the latest run (the site reads it as the open roles), so these are
    kept beside it, where the archive and the brief look for a closed req's
    last record."""
    now = {r["posting_id"] for r in rows}
    kept: dict[str, dict] = {}
    for blob in (earlier_departed, earlier_curated):   # the later blob wins
        for line in (blob or "").splitlines():
            if line.strip():
                rec = json.loads(line)
                if rec["posting_id"] not in now:
                    kept[rec["posting_id"]] = rec
    return list(kept.values())


def apply_diff(state: dict, rows: list[dict], today: str) -> dict:
    """Update per-posting state in place; return today's diff events.

    state: {posting_id: {first_seen, last_seen, closed_on, title, location}}
    """
    today_ids = {r["posting_id"] for r in rows}
    new, reopened = [], []

    for r in rows:
        entry = state.get(r["posting_id"])
        if entry is None:
            state[r["posting_id"]] = {
                "first_seen": today,
                "last_seen": today,
                "closed_on": None,
                "title": r["title"],
                "location": r["location"],
            }
            new.append(r["posting_id"])
        else:
            if entry.get("closed_on"):
                entry["closed_on"] = None
                reopened.append(r["posting_id"])
            entry["last_seen"] = today

    closed = []
    for pid, entry in state.items():
        if pid not in today_ids and not entry.get("closed_on"):
            entry["closed_on"] = today
            closed.append(pid)

    return {"new": new, "closed": closed, "reopened": reopened}


# ---------------------------------------------------------------- main flow

def collect_company(config: dict) -> dict:
    slug = config["company_slug"]
    today = date.today().isoformat()
    ts = datetime.now(timezone.utc).isoformat()

    postings, total_reported = fetch_all(config)
    rows = [normalize(p, config, today) for p in postings]
    descriptions_fetched = enrich(postings, rows, config)

    state_key = f"derived/state/company={slug}/state.json"
    state = json.loads(_read(state_key) or "{}")
    open_before = sum(1 for e in state.values() if not e.get("closed_on"))

    # Sanity guard: an empty result against a previously active board means
    # config drift, not mass layoffs. Fail loudly -> retries -> DLQ -> evaluator.
    if not rows and open_before > 0:
        raise RuntimeError(
            f"{slug}: collected 0 postings but {open_before} were open — "
            "endpoint/config drift suspected"
        )

    diff = apply_diff(state, rows, today)
    for r in rows:
        r["first_seen"] = state[r["posting_id"]]["first_seen"]
        r["last_seen"] = today

    curated_key = f"curated/company={slug}/dt={today}/postings.jsonl"
    departed_key = f"curated/company={slug}/dt={today}/departed.jsonl"
    diff_key = f"derived/snapshots/company={slug}/dt={today}/diff.json"
    # A run already happened today if the day's change record exists: merge
    # into it rather than overwrite it (see the module docstring).
    earlier_diff = json.loads(_read(diff_key) or "null")
    earlier_departed = _read(departed_key)
    departed = departed_rows(_read(curated_key) if earlier_diff else None, earlier_departed, rows)
    day = merge_day_diff(earlier_diff, diff)
    runs = [*((earlier_diff or {}).get("runs") or
              ([earlier_diff["captured_at"]] if earlier_diff else [])), ts]

    _write(f"raw/company={slug}/dt={today}/postings.json", json.dumps(
        {"captured_at": ts, "total_reported": total_reported, "postings": postings},
        ensure_ascii=False))
    _write(curated_key, "\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
    if departed or earlier_departed is not None:
        _write(departed_key, "\n".join(json.dumps(r, ensure_ascii=False) for r in departed))
    _write(f"derived/tech_stack/company={slug}/dt={today}/tech_stack.json",
           json.dumps(build_tech_summary(rows, config, today, ts), ensure_ascii=False))
    _write(diff_key, json.dumps(
        {"captured_at": ts, "company_slug": slug, "runs": runs,
         "counts": {"collected": len(rows), "total_reported": total_reported,
                    "new": len(day["new"]), "closed": len(day["closed"]),
                    "reopened": len(day["reopened"])},
         **day}, ensure_ascii=False))
    # last: until state is written, a retry recomputes exactly these changes
    _write(state_key, json.dumps(state, ensure_ascii=False))

    return {"company_slug": slug, "collected": len(rows),
            "total_reported": total_reported,
            "descriptions_fetched": descriptions_fetched,
            # share of description text filed under a known section, averaged;
            # a drop is the sign the board's layout moved and the evaluator's
            # description_format needs relearning
            "description_coverage": round(
                sum(description.coverage(r["description"]) for r in rows if r.get("description"))
                / max(1, sum(1 for r in rows if r.get("description"))), 3),
            "tech_roles": sum(1 for r in rows if r.get("is_tech_role")),
            # this run's changes; the day's record holds every run's
            "new": len(diff["new"]),
            "closed": len(diff["closed"]), "reopened": len(diff["reopened"]),
            "runs_today": len(runs)}


def handler(event, context):
    # BatchSize=1 -> exactly one record; a raise retries only this company.
    for record in event["Records"]:
        msg = json.loads(record["body"])
        summary = collect_company(msg["config"])
        print(json.dumps(summary))
    return {"ok": True}


if __name__ == "__main__":
    # Local dry run: python3 app.py <path-to-source-config.json>
    import sys

    if not LOCAL_DIR:
        sys.exit("set LOCAL_OUTPUT_DIR=<dir> for a local dry run")
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
    print(json.dumps(collect_company(cfg), indent=2))
