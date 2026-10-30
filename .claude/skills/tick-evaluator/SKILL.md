---
name: tick-evaluator
description: Discover and verify a company's job-posting collection endpoint, emitting a verified TICK SourceConfig JSON for the deterministic daily collector. Use when asked to evaluate, onboard, or re-evaluate a target company for TICK, to find a company's ATS or careers API (Workday, Greenhouse, Lever, Ashby, ADP, Dayforce), or when a collector run fails and its config must be rebuilt.
---

# TICK Evaluator

The one-time discovery layer of a job-posting intelligence pipeline. Runs ONCE
per target company, plus on collector failure. The only output is a verified
SourceConfig artifact.

    Evaluator (you, reasoning)  →  SourceConfig (JSON)  →  Collector (dumb, deterministic)

Three phases, and only the first is your opinion:

- **A. Discovery** — you drive the observation tools below.
- **B. Draft** — you write a typed SourceConfigDraft from what you verified.
- **C. Verification** — `verify.py` RE-EXECUTES your endpoint in code and
  overrides the tier and confidence you claim with what actually came back.

**You do not write the final artifact.** Phase C writes it. A config that never
returned a posting cannot be emitted as a verified tier 1 no matter what you
concluded — that failure is why phase C exists.

Run every command from the project root. `$PY` below means `.venv/bin/python`
(fall back to `python3` only if the venv is missing).

## Phase A — the observation tools

    S=.claude/skills/tick-evaluator/scripts

    $PY $S/tick_tools.py search "Dunder Mifflin"
    $PY $S/tick_tools.py fetch <url> [--browser-headers] [--headers '{"K":"V"}']
    $PY $S/tick_tools.py bundle <js-url> [--pattern REGEX]... [--max-hits 30]
    $PY $S/tick_tools.py probe <url> [--method POST] [--body '{...}'] [--headers '{...}']
    $PY $S/tick_tools.py fingerprint "<html-or-url>"     # or pipe text on stdin

Each prints a JSON object and exits 0 even on a transport error — read the JSON,
not the exit code. `fetch` truncates the body at 8000 chars, so it cannot search
a JS bundle; use `bundle`, which downloads the asset in full.

### Procedure

1. **FINGERPRINT.** Find the careers page (`search` if no URL hint was given).
   `fetch` it, then `fingerprint` **both** the body and the `final_url` — many
   careers pages are bare redirects to an ATS host.
2. **RESOLVE IDENTITY.** Determine the canonical company name and tenant slug.
   Watch for rebrands, parent companies, and multiple boards under one parent.
3. **SELECT TIER** — the lowest number you can verify live:
   - Tier 1: direct ATS JSON, probed live, 200, at least one posting. `date_trust: high`
   - Tier 2: aggregator polling, when no direct endpoint works. `date_trust: low`
   - Tier 3: needs render/scrape. `date_trust: lowest`
4. **DIG BEFORE YOU DOWNGRADE.** Most "this source has no API" conclusions are
   wrong, and dropping a tier costs `date_trust` permanently.
   - A JS-rendered SPA shell is not a dead end, and guessing paths under it is
     worthless — every route returns the same HTML shell, so a 200 full of HTML
     proves nothing. Run `bundle` on the `<script src>` URLs from the shell.
     Give one site about 6 bundle searches; if the job-search route has not
     surfaced by then, emit the best tier you can verify and record the partial
     contract in `evidence`.
   - Only 404 rules a path out. 400/401/403, a redirect to a `sorry`/captcha/
     authwall host, or a Cloudflare interstitial mean the route exists and you
     are missing a param, header or session. Read the error body — it usually
     names what is missing.
   - Bot blocks are usually header-based. Retry with `--browser-headers` before
     calling a host unreachable, and record both attempts.
   - Chain bootstrap responses. A career-site/config/session endpoint often
     returns the tenant, org or client ids the job-search endpoint then requires.
     Fetch config first, then jobs.
   - If the list payload is thin, look for a per-posting detail endpoint — that
     is usually where posting date, department and description live. Record it
     as `detail_endpoint` with a `{posting_id}` placeholder.
   - Distinguish an edge block from the application. If a request to a path that
     does not exist returns the same error as a real path, you are being blocked
     upstream and no amount of body-tuning will help.

**Budget.** Be economical on easy targets: a fingerprinted Workday or Greenhouse
tenant needs about one search, one fetch and one probe. Spend the extra calls on
blocked or SPA-based sources instead. If you notice yourself repeating a plan you
already tried, stop digging and emit. Past roughly 30 tool calls, emit the best
tier you actually verified, note in `identity_notes` what you were still chasing,
and keep `confidence` at or below 0.5.

## Phase B — write the draft

Write a draft JSON to a scratch path (not into `configs/` — that is phase C's
job). Fill every field you verified:

```json
{
  "company_name": "Canonical name after identity resolution",
  "company_slug": "dunder-mifflin",
  "tier": 1,
  "ats": "workday",
  "tenant": "ATS tenant slug — the ATS's own, not an aggregator's",
  "endpoint": "https://fully-qualified-collection-endpoint",
  "http_method": "POST",
  "request_body": {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""},
  "request_headers": {"...": "auth/tenant/org ids, and the browser header set if that is what cleared a block"},
  "detail_endpoint": "https://.../{posting_id}",
  "pagination": "offset",
  "schema_map": {
    "posting_id": "bulletFields[0]",
    "title": "title",
    "location": "locationsText",
    "url": "field path, or a template with {field} placeholders",
    "team": null,
    "posted_on": null
  },
  "careers_url": "https://...",
  "identity_notes": "Parent company, rebrand, or multi-board notes. Sourced, not inferred.",
  "date_trust": "high",
  "confidence": 0.9,
  "evidence": ["URL, status, content-type, what was in the body — one line each"]
}
```

Field rules that cost real data when broken:

- **Workday**: `posting_id` is `bulletFields[0]`, never `externalPath` — re-posts
  append a `-1` suffix and create false duplicates. Body
  `{"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}`,
  pagination `offset`.
- **Dayforce**: `jobReqId`, not `jobPostingId` — every save mints a new one.
- `posting_id` must be the STABLE requisition identity. Where a source exposes
  both a requisition id and a per-revision id, take the requisition id. A
  revision id turns one edited opening into N postings and makes every edit diff
  as a new hire plus a closure.
- Validate `schema_map` field by field against a real response you fetched. Copy
  names out of that payload; never take them from schema.org, another ATS, or
  memory. Check each resolves on every sample posting, not just the first, and
  that it holds the value you think it does — mapping a description into
  `location` is silent, permanent corruption.
- Prefer one large page over paging when the total fits. Before trusting a paged
  config, read the total twice and confirm it is stable.

### Evidence discipline — this is what makes the artifact auditable

- Every `evidence` line is a literal observation: URL, status, content-type, and
  what you actually saw in the body. If you did not fetch it, it does not go in
  evidence. Never cite a parsing mechanism you have not confirmed is present
  (e.g. "confirmed via ld+json") — go look for that markup first.
- Sanity-check job counts against a second signal. Aggregators advertise
  inventory-wide totals, not the company's. A count wildly out of line with the
  company's size is a red flag, not a confirmation.
- Aggregator pages (LinkedIn `/jobs/{company}-jobs`, Indeed) are SEARCH results,
  not company filters. Before selecting one, confirm the employer name on the
  returned cards is actually the target — these pages routinely mix in
  competitors, and collecting them poisons the dataset.
- `identity_notes` must be sourced, not inferred. Do not invent expansions of
  legal suffixes (S.I., N.A., plc) or assert "no parent company" unless you read
  it. Holding-company reorganizations are common.
- On re-evaluation, read the previous config in `configs/` first and re-check
  every lead it recorded. Silently dropping a lead the last run found is a
  regression.

### Confidence rubric

| | |
|---|---|
| 0.9+ | endpoint probed live, 200, postings returned, identity unambiguous |
| 0.7–0.9 | endpoint works but identity has minor ambiguity, or the count looks low for the company's size |
| <0.7 | no verified tier-1 endpoint, identity unresolved, or you fell back to tier 2/3 |

A tier-2/3 config emitted without working through step 4 does not exceed 0.6.

## Phase C — the gate

    $PY $S/verify.py <draft.json> --out configs/<company_slug>_config.json

This re-executes your endpoint, resolves every `schema_map` expression the way
the collector will, and stamps `tier`, `confidence`, `date_trust`,
`needs_human_review`, `verification` and `evaluated_on` in code. Exit 2 means the
config was flagged for human review; that is information, not a failure to
retry around.

**Report what phase C observed, not what you claimed.** Show the user the
`verification` line and its findings verbatim. If it downgraded your tier or
capped your confidence, say so plainly and say why. Claiming a tier you did not
verify does not make it true — it produces a flagged artifact and a wasted
evaluation.

If the gate reports a fixable defect (a `schema_map` path resolving on 40% of
rows, a revision-id warning, an unresolved URL template), fix the draft and
re-run phase C. If it reports the endpoint returning no postings at all, go back
to phase A — the endpoint is wrong, not the draft.
