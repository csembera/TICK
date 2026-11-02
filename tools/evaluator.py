"""
TICK — Evaluator Agent
======================
Reasoning-heavy discovery layer. Runs ONCE per target company (+ on collector
failure). Input: company name (optionally a careers URL hint). Output: a
SourceConfig artifact consumed by the deterministic daily collector.

    Evaluator (LLM, reasoning, expensive)  →  SourceConfig (JSON)  →  Collector (dumb, deterministic)

Tier preference (lowest number wins):
    1  Direct ATS JSON  (Workday CXS, Greenhouse, Lever, Ashby)
    2  Aggregator poll  (Built In / Indeed / LinkedIn)        — date_trust: low
    3  Web search / headless render                            — date_trust: lowest

Usage:
    python3 evaluator.py "Dunder Mifflin"
    python3 evaluator.py "Dunder Mifflin" --careers-url https://dundermifflin.wd1.myworkdayjobs.com/...

Requires:
    pip install strands-agents httpx pydantic
    AWS credentials with Bedrock access (BedrockModel is the default provider).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from typing import Literal, Optional

import httpx
from pydantic import BaseModel, Field

from strands import Agent, tool
from strands.hooks import BeforeModelCallEvent, HookProvider, HookRegistry
from strands.models import BedrockModel, CacheConfig

# --------------------------------------------------------------------------
# 1. The handoff artifact (mirrors the config-band in tick-workflow.html)
# --------------------------------------------------------------------------

class SchemaMap(BaseModel):
    """Maps ATS-native field names → TICK curated schema fields."""
    posting_id: str = Field(description="Source field for the stable posting id. Workday: 'bulletFields[0]' (NOT externalPath — re-posts append a -1 suffix).")
    title: str
    location: str
    url: str = Field(description="Source field or template for the posting URL")
    team: Optional[str] = Field(default=None, description="Department/team field if the source exposes one")
    posted_on: Optional[str] = Field(default=None, description=(
        "Source field for the posting date. Map it only to a field you verified holds a real "
        "date on a row the collector normalizes. Workday: NEVER 'postedOn' — that is a relative "
        "display string ('Posted 30+ Days Ago', 'Posted Today'), not a date: it is unparseable, "
        "and it re-renders every day, so an unchanged posting looks mutated on every diff. "
        "Workday's real ISO date is 'startDate' in the CXS per-posting detail payload, which the "
        "collector fetches for descriptions but does not merge into list rows — so it resolves to "
        "null today. Leave Workday posted_on null until that merge lands. A field that exists "
        "only in an unmerged detail payload is a null, not a date."))


class SourceConfig(BaseModel):
    """The evaluator's only output. Deterministic collector consumes this verbatim."""
    company_name: str = Field(description="Canonical company name after identity resolution")
    company_slug: str = Field(description="Normalized slug used for S3 partitioning, e.g. 'dunder-mifflin'")
    tier: Literal[1, 2, 3]
    ats: Optional[Literal["workday", "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "adp", "other"]] = None
    tenant: Optional[str] = Field(default=None, description="ATS tenant slug, e.g. Workday tenant or Greenhouse board token")
    endpoint: Optional[str] = Field(default=None, description="Fully-qualified collection endpoint verified live by the evaluator")
    http_method: Literal["GET", "POST"] = "GET"
    request_body: Optional[dict] = Field(default=None, description="JSON body template for POST endpoints (e.g. Workday CXS {limit, offset, searchText})")
    request_headers: Optional[dict] = Field(default=None, description="Extra request headers the endpoint requires (e.g. ADP 'orgoid', or a browser Accept/Referer/Sec-Fetch-* set needed to clear a WAF)")
    detail_endpoint: Optional[str] = Field(default=None, description="Per-posting detail URL template with a {posting_id} placeholder. Fetched and merged into each list row when the list payload omits fields (e.g. ADP postingDate/departmentName/jobDescription)")
    pagination: Optional[str] = Field(default=None, description="How to page: 'offset', 'page_param', 'none', etc.")
    schema_map: Optional[SchemaMap] = None
    careers_url: Optional[str] = Field(default=None, description="Human-facing careers page, kept for re-evaluation")
    identity_notes: Optional[str] = Field(default=None, description="Parent company, rebrand, or multi-board notes from identity resolution")
    date_trust: Literal["high", "low", "lowest"] = Field(description=(
        "How far the posting dates can be trusted. Tier sets the ceiling (1 -> high, 2 -> low, "
        "3 -> lowest), but a config that emits no usable date — posted_on null, or mapped to a "
        "relative display string — cannot claim 'high' at any tier."))
    confidence: float = Field(ge=0.0, le=1.0, description="Evaluator confidence the config will collect correctly")
    needs_human_review: bool = Field(description="True when confidence < 0.7 or identity is ambiguous")
    evidence: list[str] = Field(default_factory=list, description="What the evaluator observed: fingerprints found, endpoints probed, HTTP statuses, sample counts")
    evaluated_on: str = Field(default_factory=lambda: date.today().isoformat())


CONFIDENCE_REVIEW_THRESHOLD = 0.7

# --------------------------------------------------------------------------
# 2. Deterministic tools the agent reasons with
#    (the intelligence is in the agent; the tools just observe)
# --------------------------------------------------------------------------

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# ATS fingerprint patterns → (ats name, regex over fetched HTML / redirect URL)
_FINGERPRINTS: list[tuple[str, str]] = [
    ("workday",        r"([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com(?:/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+))?"),
    ("greenhouse",     r"greenhouse\.io/embed/job_board(?:/js)?\?for=([a-z0-9\-_]+)|boards\.greenhouse\.io/(?!embed)([a-z0-9\-_]+)|grnhse\.io"),
    ("lever",          r"jobs\.lever\.co/([A-Za-z0-9\-_]+)"),
    ("ashby",          r"jobs\.ashbyhq\.com/([A-Za-z0-9\-_%]+)"),
    ("workable",       r"apply\.workable\.com/([a-z0-9\-_]+)"),
    ("smartrecruiters", r"careers\.smartrecruiters\.com/([A-Za-z0-9]+)"),
]


@tool
def fetch_page(url: str) -> str:
    """Fetch a web page and return final URL, HTTP status, and the first 8000
    characters of the body. Follows redirects. Use this to inspect a careers
    page, look for ATS fingerprints in its HTML, or check where it redirects.
    """
    try:
        with httpx.Client(headers=_UA, follow_redirects=True, timeout=20) as c:
            r = c.get(url)
        return json.dumps({
            "final_url": str(r.url),
            "status": r.status_code,
            "body_head": r.text[:8000],
        })
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


@tool
def fingerprint_html(html_or_url_text: str) -> str:
    """Scan text (page HTML, a URL, or script src list) for known ATS
    fingerprints. Returns every ATS matched with the captured tenant slug
    candidates. Run this on the body_head returned by fetch_page.
    """
    hits = []
    for ats, pattern in _FINGERPRINTS:
        for m in re.finditer(pattern, html_or_url_text):
            groups = [g for g in m.groups() if g]
            hits.append({"ats": ats, "match": m.group(0), "tenant_candidates": groups})
    return json.dumps({"hits": hits} if hits else {"hits": [], "note": "no known ATS fingerprints found"})


@tool
def probe_ats_endpoint(ats: str, tenant: str, extra: str = "") -> str:
    """Live-test the structured JSON endpoint for a fingerprinted ATS tenant.
    Returns HTTP status, whether the response parsed as JSON, the total job
    count if discoverable, and one sample posting (truncated).

    ats: one of workday|greenhouse|lever|ashby
    tenant: the tenant slug captured by fingerprinting
    extra: ATS-specific —
        workday: 'wd_instance/site' e.g. 'wd1/DunderMifflinCareers' (instance from the
                 myworkdayjobs hostname, site from the URL path)
    """
    try:
        if ats == "workday":
            if "/" not in extra:
                return json.dumps({"error": "workday probe needs extra='wdN/SiteName'"})
            wd, site = extra.split("/", 1)
            url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
            with httpx.Client(headers={**_UA, "Content-Type": "application/json"}, timeout=20) as c:
                r = c.post(url, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
            ok = r.status_code == 200
            data = r.json() if ok else {}
            sample = (data.get("jobPostings") or [None])[0]
            return json.dumps({"endpoint": url, "method": "POST", "status": r.status_code,
                               "total": data.get("total"), "sample": sample})

        urls = {
            "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs",
            "lever":      f"https://api.lever.co/v0/postings/{tenant}?mode=json",
            "ashby":      f"https://api.ashbyhq.com/posting-api/job-board/{tenant}",
        }
        if ats not in urls:
            return json.dumps({"error": f"no probe pattern for ats '{ats}'"})
        url = urls[ats]
        with httpx.Client(headers=_UA, timeout=20) as c:
            r = c.get(url)
        body = r.json() if r.status_code == 200 else None
        if ats == "greenhouse" and body:
            total, sample = len(body.get("jobs", [])), (body.get("jobs") or [None])[0]
        elif ats == "lever" and isinstance(body, list):
            total, sample = len(body), (body or [None])[0]
        elif ats == "ashby" and body:
            total, sample = len(body.get("jobs", [])), (body.get("jobs") or [None])[0]
        else:
            total, sample = None, None
        if isinstance(sample, dict):
            sample = {k: (str(v)[:200]) for k, v in list(sample.items())[:12]}
        return json.dumps({"endpoint": url, "method": "GET", "status": r.status_code,
                           "total": total, "sample": sample})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


# Default sweep for an SPA's API contract: quoted /api/ paths, versioned routes,
# job/requisition literals, and absolute URLs (incl. template-literal segments).
_BUNDLE_PATTERNS: list[str] = [
    r"[\"'`][^\"'`\n]{0,120}/api/[^\"'`\n]{0,120}[\"'`]",
    r"[\"'`][^\"'`\n]{0,120}/v\d+/[^\"'`\n]{0,120}[\"'`]",
    r"[\"'`][^\"'`\n]{0,100}(?:job|requisition|posting|career|staffing)[^\"'`\n]{0,100}[\"'`]",
    r"https?://[a-zA-Z0-9.\-]+/[A-Za-z0-9/_.\-{}$]{0,120}",
]

_BUNDLE_MAX_BYTES = 12_000_000


@tool
def search_bundle(url: str, patterns: Optional[list[str]] = None,
                  window: int = 100, max_hits: int = 30) -> str:
    """Download a JS/JSON asset IN FULL and regex-search it for API endpoints.

    Use this — NOT fetch_page — to find a JS-rendered career site's request
    contract. fetch_page truncates at 8000 characters, which is worthless
    against a 0.5-3MB webpack bundle; this fetches the whole file and searches
    it. Pass the <script src> URL from the SPA shell.

    Omit `patterns` to run the default sweep (/api/ paths, /vN/ routes,
    job/requisition/staffing literals, absolute URLs). Pass your own regexes to
    chase a specific symbol, e.g. the name of a module or query hook you saw
    referenced in another chunk.

    Each hit comes back with surrounding source context so you can see how the
    path is actually assembled — template placeholders, .concat() calls, and
    which ids the call site expects.
    """
    pats = patterns or _BUNDLE_PATTERNS
    try:
        with httpx.Client(headers=_UA, follow_redirects=True, timeout=45) as c:
            r = c.get(url)
        if r.status_code != 200:
            return json.dumps({"url": url, "status": r.status_code,
                               "note": "non-200, nothing to search"})
        text = r.text[:_BUNDLE_MAX_BYTES]
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})

    results, seen, budget = [], set(), max_hits
    for p in pats:
        try:
            rx = re.compile(p)
        except re.error as e:
            results.append({"pattern": p, "error": f"bad regex: {e}"})
            continue
        hits = []
        for m in rx.finditer(text):
            if budget <= 0:
                break
            frag = m.group(0)[:200]
            if frag in seen:
                continue  # same literal already reported under an earlier pattern
            seen.add(frag)
            a, b = max(0, m.start() - window), min(len(text), m.end() + window)
            hits.append({"match": frag, "context": text[a:b]})
            budget -= 1
        results.append({"pattern": p, "hits": hits})
    return json.dumps({
        "url": url, "status": 200, "bytes_searched": len(text),
        "truncated_hits": budget <= 0,
        "results": results,
    })


@tool
def web_search_careers(company_name: str) -> str:
    """Find careers-page candidate URLs for a company when none was provided.
    Uses DuckDuckGo's HTML endpoint (no API key). Returns the top result URLs;
    the agent should fetch_page the most plausible one and fingerprint it.
    """
    try:
        q = f"{company_name} careers jobs"
        with httpx.Client(headers=_UA, follow_redirects=True, timeout=20) as c:
            r = c.get("https://html.duckduckgo.com/html/", params={"q": q})
        links = re.findall(r'class="result__a" href="([^"]+)"', r.text)[:8]
        # ddg wraps URLs; unwrap uddg param when present
        clean = []
        for l in links:
            m = re.search(r"uddg=([^&]+)", l)
            clean.append(httpx.URL(m.group(1)).path if False else (httpx.QueryParams(l.split("?", 1)[1]).get("uddg") if "uddg=" in l else l))
        return json.dumps({"query": q, "results": clean})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


# --------------------------------------------------------------------------
# 3. The agent
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You are TICK's Evaluator Agent — the one-time discovery layer of a
job-posting intelligence pipeline. You run ONCE per target company (plus on
collector failure). Your ONLY output is a SourceConfig artifact. You must VERIFY
everything live before emitting it, never guess an endpoint into the config.

Procedure:
1. FINGERPRINT: Find the company's careers page (use web_search_careers if no
   URL hint was given). Call fetch_page, then fingerprint_html the body AND the
   final_url (many careers pages are bare redirects to an ATS host).
2. RESOLVE IDENTITY: Determine the canonical company name and tenant slug.
   Watch for rebrands, parent companies, and multiple boards under one parent.
   Record anything ambiguous in identity_notes.
3. SELECT TIER: Prefer the lowest tier number that you can verify live:
     Tier 1: direct ATS JSON endpoint (probe_ats_endpoint MUST return status
             200 with at least one posting). date_trust = "high".
     Tier 2: aggregator polling (Built In / Indeed / LinkedIn) when no direct
             ATS endpoint works. date_trust = "low".
     Tier 3: hand-rolled careers page requiring render/scrape. date_trust = "lowest".
4. DIG BEFORE YOU DOWNGRADE. Most "this source has no API" conclusions are
   wrong, and dropping to tier 2/3 degrades date_trust permanently. Exhaust all
   of the following before you give up on tier 1:
   a. A JS-rendered SPA shell is NOT a dead end, and guessing API paths under it
      is worthless — every route returns the same HTML shell, so a 200 full of
      HTML proves nothing. Use search_bundle on the <script src> URLs from the
      shell: it downloads the whole asset and regex-searches it, which fetch_page
      (8000-char head only) cannot do. Start with the largest/main bundle and the
      default pattern sweep. If that misses, run search_bundle again with your
      own regex for a specific symbol rather than fetching more chunks blind —
      chunk-by-chunk browsing does not scale and will exhaust your budget.
      Give any one site at most ~6 bundle searches. If the job-search route has
      not surfaced by then, STOP and emit the best tier you can verify, with the
      partial contract (API base path, ids, field names) recorded in evidence.
      A provisional config plus real findings is worth far more than a run that
      never terminates and emits nothing.
   b. Read status codes precisely. Only 404 rules a path out. 400/401/403, a
      redirect to a 'sorry'/captcha/authwall host, or a Cloudflare interstitial
      all mean the route EXISTS and you are missing a param, header or session.
      Read the error body — it usually names exactly what is missing.
   c. Bot blocks are usually header-based, not IP-based. Before calling a host
      unreachable, retry with a full browser header set: real User-Agent,
      Accept: application/json, Referer and Origin of the careers page, and
      Sec-Fetch-Dest/Mode/Site. Record both the blocked and the cleared attempt.
   d. Chain bootstrap responses. A career-site / config / session endpoint often
      returns the tenant, site, org or client ids that the job-search endpoint
      then requires as params or headers. Fetch config first, then jobs.
   e. If the list payload is thin, look for a per-posting detail endpoint — that
      is usually where posting date, department and description live. Record it
      as detail_endpoint with a {posting_id} placeholder; the collector merges
      each detail response into its list row before normalizing.
5. EMIT CONFIG: Fill every field you verified.
   - Workday: schema_map posting_id MUST be 'bulletFields[0]' (externalPath
     appends a -1 suffix on re-posted roles and creates false duplicates in
     diffing). Include POST body template {"appliedFacets": {}, "limit": 20,
     "offset": 0, "searchText": ""} and pagination = "offset".
   - Workday: schema_map posted_on MUST be null, never 'postedOn'. That field
     is a relative display string ('Posted 30+ Days Ago'), not a date — it
     cannot be parsed, and it re-renders daily, so a diff keyed on it sees
     every unchanged posting mutate. The real ISO date is 'startDate' in the
     per-posting detail payload, which the collector does not yet merge into
     Workday list rows; mapping it today yields null. Set date_trust
     accordingly rather than claiming "high" for a config with no date.
   - Put anything the endpoint requires in request_headers — tenant/org/auth
     ids, and the browser header set if that is what cleared a bot block.
   - Validate schema_map field by field against a REAL response you fetched.
     Copy field names out of that payload; never take them from schema.org, from
     another ATS, or from memory. Note which fields are list-payload vs
     detail-only. Check that each one resolves on EVERY sample posting, not just
     the first — and that it holds the value you think it does (mapping a
     description blob into `location` is a silent, permanent data corruption).
   - schema_map.url may be a field path OR a '{field}' template for sources that
     expose no URL of their own.
   - Prefer a single large page over paging when the total fits. Before trusting
     a paged config, read the total twice and confirm it is stable — unstable
     ordering silently duplicates and drops postings across pages.

Evidence discipline — this is what makes the artifact auditable:
  - Every evidence line is a literal observation: URL, HTTP status, content-type,
    and what you actually saw in the body. If you did not fetch it, it does not
    go in evidence. Never cite a parsing mechanism you did not confirm is
    present (e.g. "confirmed via ld+json") — go look for that markup first.
  - Sanity-check every job count against a second signal. Aggregators advertise
    inventory-wide totals, not the company's. A count wildly out of line with
    the company's size is a red flag, not a confirmation.
  - Aggregator pages (LinkedIn /jobs/{company}-jobs, Indeed) are SEARCH results,
    not company filters. Before selecting one as a tier-2 endpoint, confirm the
    employer name on the returned cards is actually the target — these pages
    routinely mix in competitors, and collecting them poisons the dataset.
  - identity_notes must be sourced, not inferred. Do not invent expansions of
    legal suffixes (S.I., N.A., plc), and do not assert "no parent company"
    unless you read it somewhere. Holding-company reorganizations are common.
  - On re-evaluation, read the previous config FIRST and re-check every lead it
    recorded. Silently dropping a lead the last run found is a regression; if
    you rule one out, say so in evidence.
  - Do not populate evaluated_on — it is stamped for you.

Confidence rubric:
  0.9+  endpoint probed live, 200, postings returned, identity unambiguous
  0.7–0.9  endpoint works but identity has minor ambiguity, or job count is
           suspiciously low vs. company size
  <0.7  could not verify a tier-1 endpoint, identity unresolved, or you fell
        back to tier 2/3 without live verification → needs_human_review = True
  A tier-2/3 config emitted without working through step 4 never scores above
  0.6 — you have not yet shown that tier 1 is impossible.

Budget your calls where they pay off. An already-fingerprinted Workday or
Greenhouse tenant needs about 1 search, 1 fetch and 1 probe — do not pad it.
Spend the extra calls on blocked or SPA-based sources instead: one search_bundle
sweep and a browser-header retry are far cheaper than shipping a permanently
degraded tier-2 config that a human has to come back and fix.

But always terminate. You have a hard cap of ~25 model calls per run and the
process is killed at that point. If you notice yourself repeating a plan you
have already tried, or re-fetching things you have already seen, treat that as a
signal to stop digging and emit — not to rephrase and retry. Never restate the
same next step twice in a row. An unfinished run that emits a low-confidence
config with real evidence is a success; a run that spirals and emits nothing
wastes the entire evaluation."""


class BudgetExhausted(RuntimeError):
    """Phase A hit its model-call cap and was stopped."""


class _ModelCallBudget(HookProvider):
    """Hard cap on model calls for one run.

    Strands exposes no per-Agent iteration limit (only Swarm has max_iterations),
    so we enforce one here. This counts MODEL calls rather than tool calls on
    purpose: the observed failure was a repetition collapse that emitted the
    same paragraph indefinitely while calling no tools at all, so a tool-based
    cap would never have fired. temperature=0.0 makes that state absorbing —
    identical context produces an identical next token forever — so the loop
    cannot break itself and something external has to end it.

    Disarm before Phase B so the structured_output call is always allowed
    through, even when discovery burned the whole budget.
    """

    def __init__(self, max_calls: int):
        self.max_calls = max_calls
        self.calls = 0
        self.armed = True

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeModelCallEvent, self._check)

    def _check(self, event: BeforeModelCallEvent) -> None:
        if not self.armed:
            return
        self.calls += 1
        if self.calls > self.max_calls:
            raise BudgetExhausted(
                f"stopped after {self.max_calls} model calls — discovery did not "
                "converge; emitting an artifact from the partial transcript"
            )

    def disarm(self) -> None:
        self.armed = False


def build_agent(region: str = "us-east-1", model_id: str = "us.anthropic.claude-sonnet-4-6",
                max_model_calls: int = 25) -> tuple[Agent, _ModelCallBudget]:
    # Prompt caching: "auto" keeps a cachePoint on the last user message, so each
    # call in the tool loop re-reads the entire growing prefix (tools + system +
    # conversation) at ~0.1x input price instead of re-paying full price. The
    # Phase B structured_output call hits the same cache. Biggest single cost
    # lever for this agent (~60% of input spend on a typical 6-10 call run).
    model = BedrockModel(
        region_name=region,
        model_id=model_id,
        temperature=0.0,
        cache_config=CacheConfig(strategy="auto"),  # 5m TTL default — runs take ~1-3 min
    )
    budget = _ModelCallBudget(max_model_calls)
    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[fetch_page, fingerprint_html, probe_ats_endpoint, search_bundle,
               web_search_careers],
        hooks=[budget],
    )
    return agent, budget


def evaluate(company_name: str, careers_url: str | None = None,
             region: str = "us-east-1",
             max_model_calls: int = 25) -> SourceConfig:
    """Run the evaluator for one company and return its SourceConfig."""
    agent, budget = build_agent(region=region, max_model_calls=max_model_calls)
    prompt = f"Evaluate target company: {company_name}"
    if careers_url:
        prompt += f"\nCareers URL hint: {careers_url}"

    # Phase A: agentic discovery (tool loop). A run that fails to converge must
    # still yield an artifact — the findings up to that point (tenant slugs, API
    # base paths, ruled-out boards) are the expensive part and re-running from
    # scratch would just rediscover them.
    capped = False
    try:
        agent(prompt)
    except BudgetExhausted as e:
        capped = True
        print(f"⚠ {e}", file=sys.stderr)

    # Phase B: typed extraction of the artifact from the discovery transcript
    budget.disarm()
    task = "Emit the final SourceConfig for this company based on everything you verified above."
    if capped:
        task += (
            " Discovery was cut short by its call budget, so this config is"
            " provisional: emit the best tier you actually verified, keep every"
            " endpoint and id you confirmed in evidence, note in identity_notes"
            " what you were still chasing, and set confidence no higher than 0.5."
        )
    config = agent.structured_output(SourceConfig, task)

    # Hard guards: rubric enforcement is deterministic, not left to the model
    if capped:
        config.confidence = min(config.confidence, 0.5)
    if config.confidence < CONFIDENCE_REVIEW_THRESHOLD:
        config.needs_human_review = True
    return config


# --------------------------------------------------------------------------
# 4. CLI
# --------------------------------------------------------------------------

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="TICK Evaluator — company name → SourceConfig")
    p.add_argument("company", help="Target company name, e.g. 'Dunder Mifflin'")
    p.add_argument("--careers-url", default=None, help="Optional careers page hint")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--out", default=None, help="Write config JSON to this path (default: stdout)")
    args = p.parse_args()

    cfg = evaluate(args.company, careers_url=args.careers_url, region=args.region)
    payload = cfg.model_dump_json(indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(payload)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(payload)

    if cfg.needs_human_review:
        print(f"⚠ confidence {cfg.confidence:.2f} — flagged for human review", file=sys.stderr)
        sys.exit(2)
