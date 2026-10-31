# Module: Job Crawler

# Job Posting Tracker — System Design

*A signal-intelligence system for an AWS account executive. Tracks new job postings from target private companies over time, lands the data in S3, and surfaces trends that indicate cloud buying-readiness.*

---

## 1. Purpose

Job postings are the highest signal-to-noise public data a private company emits. They leak strategy months ahead of announcements: what's being built, which teams are growing, where the company is expanding, and — eventually — what infrastructure they'll need.

The value is not in any single posting. It's in **change over time**: new roles appearing, volume spikes, shifts in where headcount is being added. A snapshot tells you what a company looks like today; a time series tells you where it's going. The whole system is built around capturing that history.

This doc describes the architecture and the operating model. Tech-stack keyword detection (AWS vs. competitor-cloud tells, "migration"/"scale" signal words) is deliberately deferred — it's a later enrichment layer, not part of the core collection loop.

---

## 2. What "good" looks like

A working version of this system answers questions like:

- Which target accounts are *accelerating* hiring right now?
- Which accounts just opened roles in a new function (e.g., first data-engineering hire, first platform/SRE role)?
- Which accounts are opening roles in new locations (expansion signal)?
- How does this month's posting volume compare to the trailing three months for each account?

Each of those is a derivable from a clean, timestamped record of postings. The job of the system is to produce and maintain that record reliably.

---

## 3. Data sources (collection)

Two collection modes, combined:

**A. Company career pages (direct)** Most companies run their careers page on a known applicant-tracking system (ATS). These expose structured, often JSON, endpoints that are far more stable than scraping rendered HTML:

- Greenhouse — predictable board URLs and a public JSON API per company
- Lever — similar public postings endpoint per company
- Ashby, Workday, Workable, SmartRecruiters — each has its own pattern
- Custom/hand-rolled pages — fall back to HTML parsing or a headless browser

Direct collection is the backbone: it's the cleanest data and ties unambiguously to one company.

**B. Job boards (aggregate)** Boards (LinkedIn Jobs, Indeed, etc.) give breadth and catch roles that don't surface cleanly on a career page, but they're noisier, harder to attribute, and more aggressive about anti-scraping. Treat these as a supplement and a cross-check, not the primary source.

> Practical note: collection runs as a separate process (a scheduled script or a no-code scraper such as Apify / Browse AI). The system described here is about where that data **lands, persists, and gets analyzed** — not the scraper itself.
> 

---

## 4. Architecture

```
                ┌─────────────────────────┐
                │   Target company list    │  (curated: name, ATS type,
                │   + source config        │   board URL, careers URL)
                └────────────┬────────────┘
                             │
                ┌────────────▼────────────┐
   scheduled    │       Collector          │  career-page APIs + board scrape
   (daily)      │   (script / no-code)     │  → normalize → one record per posting
                └────────────┬────────────┘
                             │  raw + normalized JSON
                ┌────────────▼────────────┐
                │            S3            │  durable store, partitioned by date
                │   (raw + curated zones)  │
                └────────────┬────────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
        ┌─────▼────┐   ┌─────▼─────┐  ┌─────▼──────┐
        │  Diffing  │   │  Trends   │  │  Alerting  │
        │ (new vs.  │   │ (rollups, │  │ (threshold │
        │  prior)   │   │  history) │  │  triggers) │
        └───────────┘   └───────────┘  └────────────┘
```

### S3 layout

A simple two-zone (or three-zone) lake. Partition by date so every collection run is immutable and history is never overwritten — that's what makes time-series analysis possible.

```
s3://<bucket>/
  raw/                         # exactly what the source returned, untouched
    company=<slug>/
      dt=YYYY-MM-DD/
        postings.json
  curated/                     # normalized, one schema across all sources
    dt=YYYY-MM-DD/
      postings.parquet
  derived/                     # computed views: diffs, rollups, scores
    snapshots/
    trends/
```

- **raw** is your audit trail and reprocessing safety net — never edit it.
- **curated** is the clean, query-ready layer (one schema regardless of source).
- **derived** holds the analysis outputs (what's new, trend rollups, later: scores).

Partitioning by `dt` means Athena/Glue (or even a local DuckDB pull) can query "all postings as of date X" or "postings that first appeared between X and Y" trivially.

---

## 5. Core data record

One row per posting per capture. Keep it minimal now; the schema is designed so enrichment columns (tech keywords, cloud signals, scoring) can be **added later** without disturbing collection.

| Field | Type | Notes |
| --- | --- | --- |
| `posting_id` | string | Stable ID from the source if available; else a hash of company + title + location |
| `company` | string | Canonical company name |
| `company_slug` | string | Normalized key used for partitioning/joins |
| `source` | string | `greenhouse` / `lever` / `linkedin` / `careers_html` … |
| `title` | string | Raw role title |
| `team` | string | Department/team if exposed by the source |
| `location` | string | Raw location string |
| `remote_flag` | bool | Parsed from location/title where possible |
| `url` | string | Link to the posting |
| `first_seen` | date | First capture date this posting appeared |
| `last_seen` | date | Most recent capture date it was still live |
| `captured_dt` | date | Partition key for this run |
| `description_raw` | string | Full text — stored now, parsed later (this is where tech-stack enrichment will read from) |

**`first_seen` / `last_seen` are the heart of the system.** Computing them requires comparing each run against prior state — that's the diffing step. From those two dates you get: when a role opened (a signal event), how long it stayed open (urgency/hiring difficulty), and when it closed (filled or pulled).

---

## 6. The collection & analysis loop

Run daily. Each cycle:

1. **Collect** — pull current live postings for every target company from its configured source(s).
2. **Land raw** — write the untouched response to `raw/company=<slug>/dt=<today>/`.
3. **Normalize** — map each posting to the common schema; write to `curated/dt=<today>/`.
4. **Diff** — compare today's set against the last known state per company:
    - postings present today but not before → **new** (set `first_seen = today`)
    - postings seen before and still present → update `last_seen = today`
    - postings seen before but absent today → **closed**
5. **Roll up** — update per-company trend aggregates (postings by week/month, by team, by location).
6. **Alert** — fire triggers on meaningful changes (see §7).

Because raw is immutable and partitioned by date, you can always rebuild curated/derived from scratch if logic changes.

---

## 7. Trend signals to compute

These are the outputs that actually drive selling motions — derived entirely from the timestamped record, no tech-stack parsing required yet:

1. **Volume trend** — postings per company per week/month vs. trailing average. A spike = expansion or a fresh budget cycle.
2. **Net new openings** — count of postings with `first_seen` in the last N days. The clean "something is happening" metric.
3. **New function appearing** — first-ever posting in a team/function the company hasn't hired for before (e.g., first data-eng, first platform/infra role). Strong leading indicator of new workloads.
4. **Location expansion** — postings in a city/region/country not previously seen → geographic growth (maps later to new AWS regions in the upsell).
5. **Time-to-fill proxy** — postings open a long time → hiring difficulty or aggressive scaling. Many simultaneous senior infra roles is a loud signal.
6. **Seniority mix shift** — sudden cluster of leadership roles (VP Eng, Head of Platform) → reorg or re-architecture, often a vendor re-evaluation window.

### Suggested alert triggers

- Net-new postings for an account exceed its trailing-3-month weekly average by ≥ X%.
- First-ever posting in a target function (infra, data, platform, SRE).
- First posting in a new country/region.
- A cluster (≥ N) of senior engineering roles opening within a short window.

---

## 8. Build sequence (phased)

**Phase 1 — Collect & persist.** Curate the target-company list with each company's ATS type and URL. Stand up the collector for one ATS (Greenhouse is the easiest start), land raw + curated in S3, partitioned by date. Goal: a growing, reliable history.

**Phase 2 — Diff & trend.** Add the diffing step (`first_seen`/`last_seen`) and the per-company rollups. This is where the time series starts paying off. Add the alert triggers from §7.

**Phase 3 — Broaden sources.** Add the remaining ATS patterns (Lever, Ashby, Workday…) and the job-board supplement with attribution/cross-checking.

**Phase 4 — Enrich (deferred tech-stack layer).** Parse `description_raw` for tech-stack and cloud keywords, build the AWS-vs-competitor taxonomy, and fold it into a buying-readiness score per account. *Intentionally last* — the collection history has to exist before enrichment is worth anything.

---

## 9. Open design decisions

A few choices to settle before/while building Phase 1:

- **Collection cadence** — daily is the default; weekly is cheaper but blurs `first_seen`/`last_seen` precision. Daily is recommended given how cheap the storage is.
- **Target list size & curation** — how many accounts, and who maintains the list as territories change.
- **Query layer** — Athena/Glue over S3 (serverless, SQL, scales) vs. pulling Parquet into something lightweight (DuckDB) for ad-hoc analysis. Athena fits the "eventually S3" intent cleanly.
- **Collector hosting** — a scheduled Lambda (fits the AWS-native theme) vs. a no-code scraper writing to S3.
- **Identity resolution** — handling companies that rename, rebrand, or run multiple ATS boards under one parent.

---

*Scope note: this design covers collection, persistence, and trend analysis. Tech-stack / cloud-keyword detection is deferred to Phase 4 by design. Scraping execution is treated as an external process that writes into the S3 raw zone described here.*