# Data reference

Every file TICK writes, what writes it and what reads it, field by field. How
the pieces fit together is in [architecture.md](architecture.md).

Nothing here is in git: the repo is public, so collected data and configs live
only in the data bucket (`DataBucketName` output). `python3 tools/preview.py
--refresh` syncs a local copy into `local-data/lake` (gitignored), skipping
`raw/`, and `tools/tick.py companies pull` refreshes the local `configs/`.

## The data bucket

| Key | Writer | Readers |
|---|---|---|
| `configs/<name>.json` | `tools/tick.py companies push` | dispatcher, publisher, tick-brief |
| `raw/company=<slug>/dt=<date>/postings.json` | collector | nobody, routinely: the audit trail and the source for re-parsing |
| `curated/company=<slug>/dt=<date>/postings.jsonl` | collector | publisher, tick-brief, `tech_report.py` |
| `curated/company=<slug>/dt=<date>/departed.jsonl` | collector, on a same-day re-run | publisher (archive), tick-brief |
| `derived/snapshots/company=<slug>/dt=<date>/diff.json` | collector | publisher, tick-brief, `audit_diffs.py` |
| `derived/state/company=<slug>/state.json` | collector, last | collector (next run), publisher, tick-brief, `audit_diffs.py` |
| `derived/tech_stack/company=<slug>/dt=<date>/tech_stack.json` | collector | publisher, tick-brief, `tech_report.py` |
| `briefs/company=<slug>/week=<date>/brief.json`, `evidence.json` | tick-brief `publish.py` | publisher |
| `profiles/<sub>.json` | the join Lambda's `/api/profile` (the persona editor and My terms), `tools/tick.py personas set` | `/api/profile`, `tools/tick.py personas list`, so the tick-brief run |

`<slug>` is the config's `company_slug`; `<date>` is the UTC date of the crawl;
`<week>` is the last day of the brief's 7-day window. A config's file name
does not have to match its slug (`configs/church-mutual.json` holds
`church-mutual-insurance`): every reader uses the slug inside, and so must any
tool. The bucket is versioned, so an overwritten or deleted object can be
recovered.

## SourceConfig

One per company, written by the tick-evaluator skill's `verify.py` and executed
verbatim by the collector.

| Field | Meaning |
|---|---|
| `company_name`, `company_slug` | Display name; the key for every partition and page URL (lowercase letters, digits, hyphens; not a reserved name) |
| `ats` | `workday`, `greenhouse`, `lever`, `ashby`, `adp`, `dayforce`, `taleo` or `other` (a custom JSON board) |
| `tier` | 1 direct ATS JSON, 2 aggregator, 3 web search or headless render |
| `endpoint`, `http_method`, `request_body`, `request_headers` | The verified listing request |
| `pagination` | How to page: `offset`, `page_param`, `none`, ... |
| `postings_key` | Where the posting list sits in the response, for boards that need it |
| `detail_endpoint` | Per-posting URL template with `{posting_id}`, fetched when the list omits fields or the description |
| `description_field` | A field path to a description already in the listing payload (no extra request) |
| `description_format` | What the evaluator learned about the board's descriptions: heading `aliases` per section and `boilerplate` rules (`kind` with `starts_with` or `contains`) |
| `portals` | Taleo only: every career section to crawl |
| `schema_map` | Source field (or `{field}` URL template) for `posting_id`, `title`, `location`, `url`, `team`, `posted_on` |
| `timeout_s` | Per-request timeout for slow boards |
| `date_trust` | `high`, `low` or `lowest`: how far the board's own dates can be believed |
| `confidence`, `needs_human_review` | Set by verification; the dispatcher skips a config needing review |
| `careers_url`, `tenant`, `identity_notes`, `evidence`, `verification`, `evaluated_on` | What the evaluator found and how it checked, kept for re-evaluation |

## A curated row

`postings.jsonl` holds one JSON object per line, one line per posting open at
that crawl. `departed.jsonl` has the same shape.

| Field | Meaning |
|---|---|
| `posting_id` | The board's stable id, or a hash of company, title and location when it has none |
| `company`, `company_slug`, `source` | Who, and which ATS |
| `title`, `team`, `location`, `remote_flag`, `url` | As the board states them; `remote_flag` is "remote" appearing in the location or title |
| `posted_on`, `date_trust` | The board's own date, if any, and how far to trust it |
| `captured_dt`, `first_seen`, `last_seen` | The crawl date; the first and latest crawl the posting was on |
| `description` | The description in sections (below), or null if it could not be fetched or parsed |
| `description_raw` | Flat text: only on rows whose parse failed, and on rows collected before descriptions kept their structure |
| `keywords` | `{category: [terms]}` matched in the title and role text |
| `is_tech_role` | True on any keyword outside `signal_terms` and `analytics_tools` |

Read descriptions only through the renderer's accessors (`parsed_description`,
`role_text_of`, `full_text_of`, `pay_of`), never `description_raw`
directly: new rows carry only `description`, old rows only `description_raw`.

### `description`

```json
{
  "sections": {
    "summary": ["..."], "responsibilities": ["..."], "qualifications": ["..."],
    "preferred_qualifications": [], "work_arrangement": [], "compensation": [],
    "benefits": [], "company_intro": [], "eeo": [],
    "other": [{"heading": "Credentialing", "lines": ["..."]}]
  },
  "compensation": {"min": 90000, "max": 110000, "currency": "USD", "period": "year", "text": "..."},
  "boilerplate": ["company_intro", "eeo"],
  "unmatched_headings": ["Credentialing"],
  "format": "config"
}
```

`compensation` is null when no pay is stated; `min` is null for "up to $X" and
equals `max` for a single figure. `format` says whether the config's
`description_format` or only the generic patterns matched. The role text is
everything except `company_intro`, `benefits` and `eeo`.

## `diff.json`: the day's changes

```json
{
  "captured_at": "2026-11-02T03:01:12+00:00",
  "company_slug": "geha",
  "runs": ["2026-11-02T03:01:12+00:00"],
  "counts": {"collected": 19, "total_reported": 19, "new": 1, "closed": 0, "reopened": 0},
  "new": ["R123"], "closed": [], "reopened": []
}
```

`new`, `closed` and `reopened` list posting ids. On a day with several runs
they are the union of every run's, and `runs` lists each run's time.
`collected` is what the crawl normalized; `total_reported` is the board's own
count, where it gives one.

## `state.json`: every posting ever seen

```json
{"R123": {"first_seen": "2026-08-11", "last_seen": "2026-11-02", "closed_on": null,
          "title": "Data Engineer", "location": "Kansas City, MO"}}
```

A posting absent from a crawl gets `closed_on` set to that date; if it comes
back, `closed_on` clears and the day's diff lists it as reopened. Written last
in every run, so a run that fails partway leaves it as it was.

## `tech_stack.json`

```json
{
  "company": "GEHA", "company_slug": "geha",
  "captured_dt": "2026-11-02", "captured_at": "2026-11-02T03:01:12+00:00",
  "postings_total": 19,
  "tech_role_ids": ["R123"],
  "stack": {"cloud_aws": {"aws": ["R123", "R130"]}, "languages": {"python": ["R123"]}},
  "postings": {"R123": {"title": "...", "location": "...", "url": "...", "is_tech_role": true}}
}
```

Every term maps to the postings it was found in, so each claim traces back to
the descriptions it came from. `postings` indexes only postings with at least
one match; the full board is the curated file.

## Briefs

`brief.json` is the model's output, in the schema of the brief prompt
([prompt.md](../.claude/skills/tick-brief/prompt.md)):

```json
{
  "prompt_version": 9,
  "company_slug": "geha",
  "persona_ref": "f8b9e6456717",
  "week": {"start": "2026-10-26", "end": "2026-11-01"},
  "quiet_week": false,
  "paragraphs": ["... [[R123]] ..."],
  "action_items": [{"kind": "outreach", "title": "...", "why": "... [[R123]]",
                    "quote": {"text": "...", "source": "R123"}, "steps": ["...", "..."]}]
}
```

`persona_ref` is the `ref` of the pack's persona, or null for the default
reader (briefs before prompt v9 have no such key).

`evidence.json` is the pack it was written from, built by `evidence.py`:
`company`, `window` (start, end, crawl days), `data_quality_flags`, `board`
(id `BOARD`: open counts, movement, open leadership roles, longest open),
`stack` (id `STACK`: terms new, gone, rising, falling, recent and standing),
and `opened` / `closed` / `reopened` postings, each with its id, flags, pay and
role text. When the brief has a reader, `persona` (id `PERSONA`) holds them:
`ref`, `role`, `sells_for`, `offer`, `sells_to`, each focus and competitor
term with `open_roles`, `in_opened`, `in_closed` and `new_to_board`, and
`standing_roles`; every moved posting then also lists its `persona_terms`.
Every `[[id]]` in the brief must name a posting in the pack (standing roles
included), `BOARD`, `STACK` or `PERSONA`; `check.py` enforces that, and the
page resolves each citation against this file.

## A profile

`profiles/<sub>.json`, keyed by the user's Cognito `sub`:

```json
{
  "persona": {
    "role": "account_executive",
    "sells_for": "Initech",
    "offer": "Hardened container images with signed SBOMs",
    "buyers": ["security", "platform"],
    "focus_terms": ["sbom", "kubernetes"],
    "competitor_terms": ["snyk"]
  },
  "updated_at": "2026-11-03T15:00:00Z"
}
```

| Field | Limits |
|---|---|
| `role` | One of `account_executive`, `solutions_engineer`, `sales_development`, `customer_success`, `partner_manager`, `marketing`, `founder`, `other` |
| `sells_for` | Up to 80 characters |
| `offer` | What they sell, up to 240 characters |
| `buyers` | Any of `security`, `platform`, `engineering`, `data`, `it`, `finance`, `operations`, `people`, `procurement` |
| `focus_terms`, `competitor_terms` | Up to 30 and 20 terms, each up to 60 characters of letters, digits, spaces and `. + # / & ' -`; de-duplicated ignoring case |

[tools/persona.py](../tools/persona.py) enforces these on every write and
again when a brief reads the profile; any other field is dropped.

`/api/profile` on the join Lambda is how the pages reach it, for the signed-in
user only (their `sub` comes from the session cookie):

| | |
|---|---|
| `GET` | `{"persona": {...} or null, "updated_at", "tracks": [slugs]}`; `tracks` is the user's `custom:accounts` |
| `POST {"persona": {field: value}}` | Merges the named fields onto the saved persona and returns it; `400 {"error", "problems"}` if a field breaks a limit, and then nothing is saved |

## The site bucket

| Key | Writer | Served at |
|---|---|---|
| `index.html`, `og-image.png`, `docs/**`, `demo/**` | `tools/tick.py publish static` | `/`, `/docs`, `/demo` (public) |
| `home/<sha256(scope)[:16]>/index.html` | publisher, one per viewer scope | `/accounts` (the edge picks the key) |
| `<slug>/dashboard.html`, `<slug>/brief.html` | publisher | the same paths (guarded) |
| `<slug>/archive/<pid>/index.html` | publisher, one per closed posting | `/<slug>/archive/<pid>` (guarded) |

## Users and requests

Cognito pool (`UserPoolId` output):

| | |
|---|---|
| Username | the email address |
| Group `tick-viewers` | may sign in |
| Group `tick-admins` | may sign in, and sees every account |
| `custom:accounts` | comma-separated company slugs the user may open; only the admin API can write it |

The session cookie `tick_session` is `<sub>:<scope>:<expiry>.<hmac>`, where
`<scope>` is `*` for admins or the user's sorted, de-duplicated slugs, and the
HMAC is SHA-256 keyed with `SessionSecret`. It lasts 12 hours.

DynamoDB, both tables retained with point-in-time recovery:

| Table | Key | Fields |
|---|---|---|
| `tick-collector-join-requests` (the public waitlist) | `email` | `name`, `company`, `status`, `created_at`, `closed_at` |
| `tick-collector-account-requests` ("Track another company") | `request_id` | `requester_sub`, `requester_email`, `company`, `careers_url`, `note`, `status`, `created_at`, `closed_at` |

`status` starts `pending`; `tools/tick.py requests close` sets it to
`granted`, `declined` or `done` and stamps `closed_at`, and `users invite`
marks the invitee's waitlist row `granted`.
