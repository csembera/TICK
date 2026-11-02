---
name: tick-evaluator
description: Discover and verify a company's job-posting collection endpoint, and learn how its job descriptions are laid out, emitting a verified TICK SourceConfig JSON for the deterministic daily collector. Use when asked to evaluate, onboard, or re-evaluate a target company for TICK, to find a company's ATS or careers API (Workday, Greenhouse, Lever, Ashby, ADP, Dayforce, Taleo), to learn or relearn a board's description format, or when a collector run fails and its config must be rebuilt.
---

# TICK Evaluator

The one-time discovery layer of a job-posting intelligence pipeline. Runs ONCE
per target company, plus on collector failure. The only output is a verified
SourceConfig artifact: where the postings are, and how this board lays out a
description, so the collector can file each one into labelled sections.

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
    $PY $S/tick_tools.py descriptions <draft.json> [--n 12] [--save DIR]

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
  "description_format": {
    "aliases": {"responsibilities": ["SKILLS"]},
    "boilerplate": [
      {"kind": "company_intro", "contains": "is a regional paper company that serves"},
      {"kind": "eeo", "starts_with": "Dunder Mifflin is an equal opportunity employer"}
    ]
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
- **Taleo** (`<tenant>.taleo.net/careersection/...`): `ats: "taleo"`. Endpoint
  `https://<tenant>.taleo.net/careersection/rest/jobboard/searchjobs?lang=en`,
  `POST`, with no `portal` in the URL. Each career section's portal id is the
  `portalNo` in its `jobsearch.ftl` page; list **every** section under
  `portals` as `{"portal": "<portalNo>", "section": "<code in its URL>"}`.
  The collector pages them all and tags each posting `careerSection`. The
  search answers 500 without `request_headers`
  `{"tz": "GMT-05:00", "tzname": "America/Chicago"}`. `posting_id` is
  `contestNo` (the requisition number in `jobdetail.ftl?job=`), not `jobId`.
  `column` holds the list columns in order (check which is which on a real
  response); the location column is a JSON array in a string, which the
  collector unwraps. Set `detail_endpoint` and `schema_map.url` to
  `https://<tenant>.taleo.net/careersection/{careerSection}/jobdetail.ftl?job={contestNo}&lang=en`:
  descriptions are read from that page.
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

### Description format — how this board lays out a description

Once the endpoint works, learn the description layout. The collector parses
every description into sections (`summary`, `responsibilities`,
`qualifications`, `preferred_qualifications`, `work_arrangement`,
`compensation`, `benefits`, `company_intro`, `eeo`, plus `other`) using
generic heading patterns. `description_format` tells it where this board
differs.

    $PY $S/tick_tools.py descriptions draft.json --n 12

This samples descriptions the way the collector fetches them (same headers,
same detail merge) and prints: each heading with how many postings use it and
how the generic patterns file it; `heading_candidates`, short plain lines most
postings repeat that the parser does not see as headings; `repeated_blocks`,
the paragraphs most postings share (boilerplate candidates); pay sentences;
and `role_sections`, how often summary, responsibilities and qualifications
were found. `--save DIR` writes the sampled HTML if you need to read one.

Write only what the generic patterns get wrong:

- **aliases** — a heading this board uses that `generic` misfiles or leaves
  `null`, under the section it really opens. Copy the heading verbatim. Read
  what follows it in a saved sample before deciding: a board's "SKILLS" label
  may head the duties. Only alias headings at least a quarter of the sample
  uses; a heading one posting invented is that posting's, not the board's.
- **heading_candidates** — some boards set their template headings ("Job
  Summary", "Work Location") as plain paragraphs. Once aliased, a line that
  exactly matches is treated as a heading, so alias every candidate that heads
  the content under it, even where `generic` already names the right section:
  without the alias the parser never sees it as a heading. Candidates that
  head nothing (a benefits list's items) are not headings; leave them.
- **boilerplate** — the board's repeated company intro, benefits blurb and
  equal-opportunity text, from `repeated_blocks` (a block on most postings).
  Kinds are `company_intro`, `benefits`, `eeo`. Match with `starts_with` or
  `contains`, and **copy the words verbatim from the inventory, never
  paraphrase**: one changed word ("health" for "medical") and the rule
  matches nothing. When postings word the same paragraph two ways, use
  `contains` with the phrase the variants share, at least six words long.
  Skip `eeo` blocks the inventory already marks `generic_kind: eeo`.
- Leave out what already works. An empty `description_format` is fine for a
  board the generic patterns read correctly; the gate reports it either way.

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

The gate re-executes the config exactly as the collector will: the collector's
headers, not the browser set the discovery tools send (some boards answer a
browser `Accept-Language` with an empty posting list), for ADP each
posting's detail merged in before `schema_map` is checked, and for Taleo
every page of every career section, with the collector's own code. A Taleo
section that returns fewer postings than it reports fails the gate, as it
would fail the collector's run.

The gate also samples fresh descriptions and writes
`verification.descriptions`: coverage with and without your format, how often
each role section was found, which boilerplate rules and aliases actually
matched (`rule_hits`, `alias_hits`), and headings still unrecognised. It drops
boilerplate rules that matched no sampled posting. Report the `descriptions:`
line and its findings too. Aim for responsibilities and qualifications on most
postings and every boilerplate rule matching most of the sample; a rule that
matches a few postings is role text, so remove it. Descriptions never change
the tier or the confidence: a board with an imperfect format still collects.

## Relearning one board's description format

When a collector run reports `description_coverage` dropping, or a board's
descriptions file badly, relearn just the format. There is no need to rediscover
the endpoint:

    cp configs/<slug>_config.json /tmp/draft.json         # the current config is a valid draft
    $PY $S/tick_tools.py descriptions /tmp/draft.json --n 12
    # edit description_format in /tmp/draft.json per the rules above
    $PY $S/verify.py /tmp/draft.json --out configs/<slug>_config.json

The new config reaches the collector only once it is uploaded to the data
bucket's `configs/` prefix. That is production: ask before uploading.
