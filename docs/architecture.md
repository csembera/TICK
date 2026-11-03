# How TICK works

TICK collects a company's job postings every night, diffs them into a time
series, and publishes a dashboard and a weekly brief per account at
<https://tick.sembasolutions.com>. This document describes the system as it is
built. [design-doc.md](../design-doc.md) is the original design it grew from,
and [CLAUDE.md](../CLAUDE.md) holds the rules every change must keep.

- Running it: [operations.md](operations.md)
- The files it reads and writes: [data-reference.md](data-reference.md)
- Changing it: [development.md](development.md)
- The AWS stack itself: [infra/README.md](../infra/README.md)

## The shape

```
 once per company              every night                                   weekly, from a laptop
┌───────────────────┐   ┌───────────────────────────────────────────┐   ┌──────────────────────────┐
│ tick-evaluator     │   │ 03:00 UTC  Dispatcher ─► SQS (1 msg/company)│   │ tick-brief skill          │
│ skill: find the    │──►│            ─► Collector ×N (2 at a time)   │   │ evidence.py ─► model ─►   │
│ board, verify it,  │   │            ─► S3 lake  (raw/ curated/       │◄──│ check.py ─► publish.py    │
│ learn its format   │   │                         derived/)           │   │ ─► briefs/ in the lake    │
└───────────────────┘   │ 03:30 UTC  Publisher ─► site bucket (HTML)  │   └──────────────────────────┘
  SourceConfig JSON      └───────────────────────────────────────────┘
  in s3://<data>/configs/                 │
                                          ▼
                CloudFront ─ /, /docs, /demo: public, hand-authored (infra/site-static/)
                           ─ /auth/*: Auth Lambda ─► Cognito (TICK's own pool)
                           ─ /api/*: Join Lambda (waitlist, account requests)
                           ─ everything else: an edge function checks the session cookie
```

Four ideas carry the design:

1. **Reasoning happens once; the nightly run is mechanical.** Finding a
   company's job board, proving the endpoint works and learning how its
   descriptions are laid out is model work, done once per company by the
   evaluator. Its output, a SourceConfig, is executed verbatim every night by a
   collector that makes no decisions. When a board changes, the collector fails
   loudly and the evaluator runs again.
2. **History is the product.** A snapshot says what a company looks like; the
   series of snapshots says where it is going. Every crawl is kept, partitioned
   by date, and each posting's `first_seen`, `last_seen` and `closed_on` are
   tracked across them.
3. **Pages are rendered ahead of time.** The publisher turns the lake into
   finished HTML. The browser never reads the lake, the lake bucket is never
   public, and a request costs CloudFront one HMAC check.
4. **Briefs are written by a model, never by code.** Code builds the evidence
   and verifies every citation; the prose is the model's, and a brief that fails
   verification is never published.

## Onboarding a company: the evaluator

The [tick-evaluator skill](../.claude/skills/tick-evaluator/SKILL.md) is how a
company is onboarded. Working in Claude Code, it fingerprints the company's
careers page to find its applicant-tracking system (Workday, Greenhouse, Lever,
Ashby, ADP, Dayforce, Taleo, or a custom JSON board), probes the endpoint, and
drafts a SourceConfig. Then `verify.py` re-executes that config in code and
overrides the tier and confidence the model claimed with what actually came
back: a config that never returned a posting cannot be emitted as verified.
The skill also learns the board's `description_format`, the heading aliases and
boilerplate it uses, so the collector can file each description into sections.

[tools/evaluator.py](../tools/evaluator.py) is the same discovery as a
standalone Strands agent on Bedrock.

A verified config lands in the local `configs/` and reaches production with
`tools/tick.py companies push`. A config with `needs_human_review: true` is
listed on `/accounts` as **Needs review** and skipped by the nightly run.

## Collection

The dispatcher ([src/dispatcher/app.py](../infra/src/dispatcher/app.py)) runs
at 03:00 UTC. It lists `configs/` in the data bucket and sends one SQS message
per company, `{"config_key": ..., "config": {...}}`, skipping any flagged for
review. The collector ([src/collector/app.py](../infra/src/collector/app.py))
takes one message per invocation, at most two at a time to stay polite to the
boards, and for that one company:

```
fetch (paginate) -> raw/  -> normalize via schema_map -> enrich (descriptions, keywords)
-> diff against state -> curated/ (+first_seen/last_seen) -> derived/tech_stack/
-> derived/snapshots/ (the day's changes) -> state, written last
```

### Failure semantics

- One company failing never affects the others: each is its own invocation.
- A failing message is retried after the queue's 30-minute visibility timeout,
  three attempts in all (`maxReceiveCount: 3`). Then it moves to the DLQ, kept
  14 days, and raises the `tick-collector-collect-failures` alarm, which emails
  `AlertEmail`.
- A DLQ message means the SourceConfig has drifted: the board moved. The fix is
  to re-run the evaluator for that company; see
  [operations.md § A crawl failed](operations.md#a-crawl-failed-the-dlq-alarm).
- The collector also fails deliberately when it collects 0 postings for a
  company whose state shows open ones. That is drift detection, not a reading
  of layoffs.

### Running a company more than once a day

A manual re-crawl, an SQS retry or a duplicate delivery can run a company
twice on one date, and every file the collector writes is keyed by that date.
The day's change record (`derived/snapshots/.../dt=<day>/diff.json`) is what the
dashboard's "this window" counts and the weekly brief add up, and a later run
only sees what changed since the earlier one. So the collector:

- **merges** a re-run into the day's change record: new, closed and reopened
  are the union of every run that day, and `runs` lists each run's time. A
  re-run that finds nothing leaves the record as it was.
- keeps `curated/.../postings.jsonl` as the board as of the **latest** run (the
  site lists it as open roles), and puts postings an earlier run saw that are
  gone now in `departed.jsonl` beside it, where the archive and the brief find
  a closed req's last record.
- writes `state.json` **last**: a run that fails partway leaves state as it
  was, so the retry recomputes the same changes instead of finding none.

`python3 tools/audit_diffs.py s3://<DataBucketName>` (or a synced local lake)
compares each posting's `first_seen` and `closed_on` in state with that day's
change record, and exits 1 listing any opening or closure a record lost. Run it
after any re-crawl.

### Descriptions and keywords

For Workday sources the collector fetches each posting's full description
(the CXS detail endpoint, one GET per posting, 0.1s apart). For Taleo it reads
each posting's job page (`jobdetail.ftl`), which embeds the description, with
the same gap ([taleo.py](../infra/src/collector/taleo.py)). ADP and inline
boards carry it in the posting payload. Dayforce's job feed carries it as plain
text, which [dayforce.py](../infra/src/collector/dayforce.py) rewrites as
paragraphs and lists before parsing. The original HTML is kept in `raw/` for
every ATS, so descriptions can be re-parsed without refetching.

[description.py](../infra/src/collector/description.py) files each description
into labelled sections: summary, responsibilities, qualifications, preferred
qualifications, work arrangement, compensation, and the boilerplate every
posting on a board repeats (company intro, benefits, equal-opportunity text).
A heading nothing recognises is kept under `other`; nothing is dropped. Headings
match the config's `description_format` first, then generic patterns. A parser
error costs the row its structure, never the run. Pay is read into
`{min, max, currency, period}`; figures in the millions, business figures such
as premium or revenue, and malformed numbers are never pay.

Keywords come from a fixed tech-stack taxonomy (`_KEYWORDS` in the collector:
AWS, Azure and Google Cloud services, data platform, infrastructure and DevOps,
languages, analytics tools, and signal terms such as "migration"), matched as
whole words in the title and the role text, never the board's boilerplate. A
posting with any match outside signal terms and analytics tools is a **tech
role**. Each run's summary reports `description_coverage`, the share of
description text filed under a known section; a drop means the board's layout
moved and its `description_format` needs relearning. Setting
`FETCH_DESCRIPTIONS=0` on the collector function skips the per-posting detail
fetches; keywords then come from titles only.

**Reading descriptions.** Every reader goes through the renderer's accessors in
[tick_insights_report.py](../tools/tick_insights_report.py):
`parsed_description()`, `role_text_of()`, `full_text_of()` and `pay_of()`. New
rows carry only `description`, old rows only `description_raw`; the accessors
use the sections when a row has them and otherwise rebuild approximate
sections from the flat text, on read. Nothing in the lake is rewritten.

## The lake

One versioned S3 bucket, retained if the stack is ever deleted. Every key is
partitioned by company and, for anything a crawl writes, by date:

| Prefix | Written by | Holds |
|---|---|---|
| `configs/` | `tools/tick.py companies push` | One SourceConfig per company |
| `raw/company=<slug>/dt=<date>/` | collector | Exactly what the board returned, never edited |
| `curated/company=<slug>/dt=<date>/` | collector | One normalized row per open posting; `departed.jsonl` for postings a same-day re-run no longer saw |
| `derived/snapshots/company=<slug>/dt=<date>/` | collector | `diff.json`: the day's openings, closures and reopenings |
| `derived/state/company=<slug>/` | collector | `state.json`: every posting ever seen, with its first, last and closing dates |
| `derived/tech_stack/company=<slug>/dt=<date>/` | collector | `tech_stack.json`: each matched term and the postings it came from |
| `briefs/company=<slug>/week=<date>/` | the tick-brief skill's `publish.py` | `brief.json` and the `evidence.json` it was written from |
| `profiles/<sub>.json` | the persona editor on `/profile` (the join Lambda's `/api/profile`), or `tools/tick.py personas set` | One user's persona: who they are, what they sell, the terms they watch |

Field-by-field formats are in [data-reference.md](data-reference.md).

## Publishing

The publisher ([src/publisher/app.py](../infra/src/publisher/app.py)) runs at
03:30 UTC, half an hour behind the dispatcher so every company has finished,
and follows the same `ScheduleState`. It renders with
[tools/tick_insights_report.py](../tools/tick_insights_report.py), the same file
the local CLI runs, symlinked into the Lambda as `report.py` so there is no
second copy to drift.

Each run is a **full re-render** of the lake as it stands, so a rolled-back
snapshot cannot leave a stale page behind, followed by a CloudFront
invalidation. Pages carry `max-age=300`, so even a failed invalidation corrects
itself within 5 minutes. For each account it writes:

- `<slug>/dashboard.html`: the board's trend, the stack terms (Adopting and
  Established, split on whether a term first appeared in the last six months),
  the roles opened this window, and every open and closed role in tables that
  search descriptions, sort, filter by stack term and tag a viewer's own
  "My terms" (saved to their profile).
- `<slug>/brief.html`: the newest published brief, or "No brief yet".
- `<slug>/archive/<pid>/index.html`: one page per closed req, the stable
  address briefs cite once the board stops serving the posting.

It also writes one accounts home per distinct viewer scope (see
[Sign-in](#sign-in-and-the-edge)), and `home/profile/index.html`, the one
`/profile` page every viewer shares. An account appears on `/accounts` as soon as
its config is uploaded, marked **Not collected** until its first crawl and
**Reporting** from then on. A company that is in the lake without a config is
still listed, so removing a config stops the crawls but not the pages. A first
crawl's dashboard shows the open board without change figures; those, and
briefs, start with the second crawl.

### The comparison window

Both pages compare the latest snapshot against **the most recent one at least 7
days old**, not against the previous crawl, and the chart plots every snapshot
between the two. An overnight delta on a daily board is mostly noise: one
tracked board moves +12/-7 between consecutive days while drifting far less, so
a crawl-to-crawl comparison reported ±1 and a flat line no matter what the week
did. Where a company has less history than the window, the page compares
against the oldest snapshot it has and labels the real dates, so a short window
is visible rather than claimed as a full one.

## Briefs

No code writes brief prose. Once a week the
[tick-brief skill](../.claude/skills/tick-brief/SKILL.md) builds each account's
evidence pack with `evidence.py`: every posting that opened, closed or
reopened in the 7 days, with its role text and pay, plus the board and stack
context, each item carrying an id. A model writes the brief from that pack and
nothing else, citing ids as `[[id]]`. `check.py` then re-verifies in code that
every citation exists, every quote is verbatim, and the length and
action-item limits hold. Only a brief that passes is uploaded, by `publish.py`,
with its evidence, which the page resolves its citations against. Every cited
role links, by its status when the page is rendered rather than in the pack:
an open role to its posting on the board, a closed one to its archive page.
So a role that closes after its brief stops pointing at a dead board link.

The site shows each account's newest week holding both files. A skipped week
leaves last week's brief up, dated. A new brief appears at the next nightly
publish, or at once with `tools/tick.py publish pages`.

### Who a brief is written for

Each user can keep a **persona**: their role, the company they sell for, what
they sell, the teams they sell to, and the terms they watch (focus terms, and
competitor terms for products that compete with theirs). They edit it on
`/profile`, opened with **Edit profile** in the account menu at the right of
the menu bar, and the focus terms are the dashboard's **My terms**, so adding
a term on a dashboard adds it to the persona. Both pages are static and
shared, so neither holds anyone's data: their scripts load and save it
through `/api/profile` on the join Lambda,
which reads the user from the session cookie, never from the request, so a
user can only ever reach their own profile. A save names only the fields it
changes and is merged onto what is stored. Where the API is out of reach (a
local preview without the stub, say), My terms fall back to the browser.
[tools/persona.py](../tools/persona.py) is the one definition of what a
persona may hold. It is text a user typed that ends up in a model's context,
so every field is bounded and normalised on write and again on read.

An account's brief is written for the persona of **the one user who tracks
it**: the account is in their `custom:accounts`. For a viewer that list is
also what they may open; an admin opens every account, and the list marks the
ones they work. An account that no one tracks, or that several users track,
gets the default reader, an account executive at a technology services firm.
So there is still one brief per account, at the same address, and the page
says whom it was written for.

With a persona, `evidence.py` adds a `PERSONA` section: the persona, and for
each of its terms the open roles that mention it, this week's openings and
closures that do, and whether it is new to the board, all counted in code.
It also lists `standing_roles`, open roles that did not move this week but
mention the reader's terms, so the writer can cite a long-open seat that
matters to them. The prompt has the persona decide what ranks first, never
what is true: when the week holds nothing for the reader, the brief says so
rather than stretching a posting to fit what they sell. The persona is data,
never an instruction; the writer runs as an agent that can only read and write
files, and `check.py` refuses a brief whose `persona_ref` does not match its
pack.

## Sign-in and the edge

The site bucket holds two halves that never collide:

| | Written by | Guarded |
|---|---|---|
| `/`, `/docs`, `/demo` | `tools/tick.py publish static` from `infra/site-static/` | no |
| `/accounts`, `/<slug>/*` | the publisher Lambda, nightly | yes |

The publisher writes only `home/` and `<slug>/` keys, so it cannot overwrite
the public pages; `publish static` deletes only under `docs/` and `demo/`, so it
cannot reach the app. The public pages are hand-authored and carry no lake
data; the demo is built on a fictional company.

```
https://tick.sembasolutions.com/                          marketing page  (public)
https://tick.sembasolutions.com/docs                      documentation   (public)
https://tick.sembasolutions.com/demo                      demo            (public)
https://tick.sembasolutions.com/login                     the way in: signs you in, then /accounts
https://tick.sembasolutions.com/accounts                  accounts home
https://tick.sembasolutions.com/<company-slug>/dashboard.html
https://tick.sembasolutions.com/<company-slug>/brief.html
https://tick.sembasolutions.com/<company-slug>/archive/<posting-id>   a closed req
```

### How the check works

CloudFront Functions cannot verify an RS256 JWT or make a network call, so the
work is split:

1. The edge function first resolves extensionless URIs (`/docs` →
   `/docs/index.html`), because S3 has no directory semantics and
   `DefaultRootObject` only covers `/`. The two front doors come next: `/`
   and `/login` with a valid session → 302 to `/accounts`, so a signed-in
   viewer never lands on the marketing page. Then it lets the public prefixes
   through. For anything else, no session cookie → 302 to `/auth/login`,
   preserving the path asked for (`/login` asks for `/accounts`).
2. `/auth/login` → the auth Lambda redirects to the Cognito hosted UI.
3. `/auth/callback` → the Lambda exchanges the code for tokens (client secret,
   server side), checks `aud` / `iss` / `token_use` / `exp` and the group
   claim, then mints an HMAC-signed session cookie good for 12 hours. The
   cookie carries the viewer's scope: `*` for `tick-admins`, else their
   normalised `custom:accounts`.
4. Every later request → one HMAC at the edge. No Lambda, no Cognito call.
   The edge refuses `/<slug>/` pages outside the scope, and serves `/accounts`
   from `home/<sha256(scope)[:16]>/index.html`; the publisher renders one such
   home per distinct scope in the pool. `/profile` is one page for any
   session, `home/profile/index.html`; its script fetches the viewer's own
   profile.

Two consequences follow, and both matter when access changes:

- **A change of accounts applies at the viewer's next sign-in**, because the
  scope is fixed in the cookie, and only once the publisher has rendered a home
  for the new scope. `tools/tick.py users grant` renders it when it is new.
- **Nothing ends a session early except a new `SessionSecret`.** Disabling a
  user or revoking an account stops their next sign-in; a cookie they already
  hold stays valid for up to 12 hours, since the edge never asks Cognito.
  Deploying a new secret signs everyone out at once.

A pool user who is not in `tick-viewers` (or `tick-admins`) gets an
explanatory "No access" page, not a login loop. Only the Cognito admin API can
write `custom:accounts`; the app client may not, so a viewer cannot grant
themselves an account. `/api/*` skips the edge check, so the join Lambda
verifies the session cookie itself for `/api/account-request` and
`/api/profile`. Signing out
(**Sign out** in the account menu on every app page, or `/auth/logout`) clears the cookie
and ends the Cognito session too. The edge
function's source, signing key included, is readable by anyone holding
`cloudfront:GetFunction` on the account: it authenticates viewers, it is not a
secret from account principals.

A company slug is the first segment of its pages' URLs, which is why
`accounts`, `profile`, `home`, `login`, `docs`, `demo`, `auth` and `api` can
never be slugs: the first four are app pages, `docs` and `demo` are public (a company
there would be served without a session), and the last two are routed to
Lambdas. `tools/tick.py companies push` refuses them.

### One menu bar, several copies

Every page shares the menu bar and tab icon of `/demo`, but nothing generates
them from one file. They are copied into:

| Copy | Where |
|---|---|
| Source of truth | `infra/site-static/demo/**` (`.nav` CSS and markup, `<link rel="icon">`) |
| Marketing and docs | `infra/site-static/index.html`, `infra/site-static/docs/index.html`: brand and icon (their tabs are their own) |
| App pages | `_nav`, the `.nav` rules in `_STYLE`, and `_FAVICON` in `tools/tick_insights_report.py` |
| Auth error page | `page()` in `infra/src/auth/app.py`: brand, Docs and icon only |

Change them together. The app's bar differs from the demo's in its links and
its right-hand corner: each account's own Dashboard and Brief tabs, and the
account menu (the viewer's avatar and name, then **Edit profile** and **Sign
out**) where the demo has **Log in** / **Get started**. The demo has no
signed-in corner, so the account menu's `.nav-menu` rules live only in
`_STYLE`. The menu is a `<details>` disclosure that works without script; its
script names the viewer from `/api/profile` and keeps the name for the tab,
so only the first page of a visit waits on the API. `python3 tools/preview.py` serves the demo and
the app pages side by side to check.

## Deploying

Merging to `main` is the release. A push that touches `infra/**`, the renderer
or the workflow runs [deploy.yml](../.github/workflows/deploy.yml): a **plan**
job builds a CloudFormation change set (every deployed parameter value reused)
and summarises it, flagging any resource that would be removed or replaced; a
reviewer approves the `production` environment; then the **deploy** job
publishes the static pages first, executes the change set, applies the sign-in
page, republishes the site and verifies the edge, each a `tools/tick.py`
command. Details, and the manual fallback, are in
[infra/README.md § Deploying a change](../infra/README.md#deploying-a-change).
