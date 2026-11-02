# TICK Collector (SAM)

Deterministic daily collection pipeline. Consumes `SourceConfig` artifacts
produced by the evaluator (`../tools/evaluator.py`).

```
EventBridge Scheduler (daily)
  └─► Dispatcher Lambda ── lists s3://<bucket>/configs/*.json
        └─► SQS collect-queue (1 message per company)
              └─► Collector Lambda (batch size 1, max concurrency 2)
                    ├─ raw/company=<slug>/dt=<date>/postings.json
                    ├─ curated/company=<slug>/dt=<date>/postings.jsonl
                    ├─ derived/snapshots/company=<slug>/dt=<date>/diff.json
                    └─ derived/state/company=<slug>/state.json
              └─► DLQ after 3 failed attempts ── CloudWatch alarm
                     = "re-run the evaluator for this company"

tick-brief skill (weekly, from a laptop: model-written, check.py-verified)
  └─► briefs/company=<slug>/week=<date>/{brief,evidence}.json   in the same lake

EventBridge Scheduler (daily, 30 min later)
  └─► Publisher Lambda ── reads the lake + s3://<bucket>/configs/*.json
        └─► S3 site bucket (private)
              ├─ home/<key>/index.html         an accounts home per viewer scope (/accounts)
              ├─ <slug>/dashboard.html
              ├─ <slug>/brief.html             newest published brief, or "No brief yet"
              └─ <slug>/archive/<pid>/index.html   one per closed req

site-static/ ── make publish-static ──► the same bucket
              ├─ index.html                    marketing page
              ├─ og-image.png
              ├─ docs/index.html
              └─ demo/**/index.html

        └─► CloudFront ── the only way in
              ├─ /, /docs, /demo  ─► public, no session needed
              ├─ /auth/*          ─► Auth Lambda ─► Cognito hosted UI (TICK's own pool)
              └─ everything else  ─► CloudFront Function checks the session cookie
```

## First-time setup

Prerequisites: AWS CLI with working credentials (`aws sts get-caller-identity`),
SAM CLI (`brew install aws-sam-cli`).

```bash
# 1. Deploy the stack
sam build
sam deploy --guided
#    Stack Name:          tick-collector
#    ScheduleState:       DISABLED       (enable after first manual run succeeds)
#    AlertEmail:          you@example.com (optional — DLQ alarm emails)
#    ViewerGroupName:     tick-viewers
#    SessionSecret:       $(openssl rand -hex 32)
#    Allow IAM role creation: y
#    Save to samconfig.toml:  y
#    -> note the DataBucketName and SiteUrl outputs

# 2. Register the first company
aws s3 cp ../dunder-mifflin_config.json s3://<DataBucketName>/configs/dunder-mifflin.json

# 3. First manual run
aws lambda invoke --function-name tick-collector-dispatcher /dev/stdout
#    expect {"dispatched": 1, "skipped": 0, "invalid": 0}

# 4. Verify the lake (~30s later)
aws s3 ls s3://<DataBucketName>/ --recursive
#    expect raw/, curated/, derived/snapshots/, derived/state/ objects for today
#    debug: sam logs -n CollectorFunction --stack-name tick-collector

# 5. Turn on the daily schedule
sam deploy --parameter-overrides ScheduleState=ENABLED
```

Other parameters at deploy time:
- `ScheduleExpression="cron(0 3 * * ? *)"` — when the daily run fires (UTC;
  default 03:00). Note the inner quotes: the value contains spaces (see
  "Quoting" under [The site](#access)).

## Deploying a change

Merging to `main` deploys through
[../.github/workflows/deploy.yml](../.github/workflows/deploy.yml): it plans a
change set, waits for a reviewer to approve the `production` environment, then
runs the steps below. Run them by hand only when the pipeline is unavailable.

The order matters, for one reason: the site bucket's `index.html` is the public
marketing page, but the edge only treats it as public after the stack update
lands. Deploy the stack while the bucket still holds something private at that
key and you publish it. So static content goes first, always.

```bash
# 0. Recover the existing session key. It is NoEcho, so CloudFormation will not
#    show it, but the auth Lambda holds it — and deploying a *new* value signs
#    every live viewer out.
SECRET=$(aws lambda get-function-configuration \
  --function-name tick-collector-auth \
  --query 'Environment.Variables.SESSION_SECRET' --output text)
[ ${#SECRET} -eq 64 ] || echo "WARNING: expected 64 chars, got ${#SECRET}"

# 1. Static pages first — closes the window described above.
make publish-static

# 2. The stack.
sam build
sam deploy --parameter-overrides "AlertEmail=you@example.com SessionSecret=$SECRET"

# 3. Hosted-UI styling, if cognito-ui.css or the banner changed. Independent of
#    CloudFormation (see the Makefile header) and idempotent, so the position
#    here does not matter.
make publish-cognito-ui

# 4. Re-render the app pages. Until this runs, /accounts 404s for signed-in users.
aws lambda invoke --function-name tick-collector-publisher /dev/stdout
#    expect {"accounts_rendered": N, "homes_rendered": H, "pages": 2N+H, ...}
```

### Verify

Public — every one of these must return 200 with no cookie:

```bash
for p in / /docs /demo /demo/dunder-mifflin/brief; do
  printf "%-32s %s\n" "$p" \
    "$(curl -s -o /dev/null -w '%{http_code}' https://tick.sembasolutions.com$p)"
done
```

Guarded — every one of these must 302 to `/auth/login`. **A 200 here is a
client-data leak, not a cosmetic bug:**

```bash
for p in /accounts /login /dunder-mifflin/dashboard.html /dunder-mifflin/brief.html; do
  printf "%-36s %s\n" "$p" \
    "$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' https://tick.sembasolutions.com$p)"
done
```

Then sign in through a browser: `/` → **Log in** → Cognito (TICK palette, TICK
banner) → lands on `/accounts` showing the accounts home. Then open `/` and
`/login` again: both must 302 straight to `/accounts` while you are signed in.

### Which target owns what

| Path | Deployed by | Guarded |
|---|---|---|
| `/`, `/docs`, `/demo` | `make publish-static` | no |
| Cognito hosted UI | `make publish-cognito-ui` (`cognito-ui.css` + banner) | n/a |
| `/accounts`, `/<slug>/*` | the publisher Lambda | yes |

The hosted-UI customization is deliberately **not** a CloudFormation resource.
`SetUICustomization` and `AWS::Cognito::UserPoolUICustomizationAttachment`
create the same underlying object, so declaring it in the template while also
running the make target fails the deploy with `AlreadyExists` and rolls the
stack back. The template does not declare it; the make target owns it.

`site-static/branding/` is source for the Cognito banner and is deliberately
not synced — `make cognito-banner` re-renders the PNG from it after a copy edit.

## Register a company

The evaluator's output goes in the bucket's `configs/` prefix:

```bash
python3 ../tools/evaluator.py "Dunder Mifflin" --out dunder-mifflin_config.json
aws s3 cp dunder-mifflin_config.json s3://<DataBucketName>/configs/dunder-mifflin.json
```

Configs with `needs_human_review: true` are skipped by the dispatcher.

## Run on demand

```bash
aws lambda invoke --function-name tick-collector-dispatcher /dev/stdout
```

## Test locally (no AWS)

The collector worker doubles as a local script — writes to disk instead of S3:

```bash
cd src/collector
LOCAL_OUTPUT_DIR=../../out python3 app.py ../../../configs/<slug>_config.json
```

Or simulate the full SQS invocation:

```bash
sam build
sam local invoke CollectorFunction -e events/dunder-mifflin_sqs_event.json
```

(The event carries the fictional `dunder-mifflin` config, whose endpoint does
not resolve: swap in a real config's JSON to exercise a live fetch.
`sam local invoke` will also attempt the S3 writes — use real credentials and
`BUCKET_NAME`, or prefer the `LOCAL_OUTPUT_DIR` path above.)

## The site

The lake is rendered into static HTML by the publisher Lambda and served from
CloudFront. Pages are rendered server-side and land in S3 finished — the
browser never reads the lake, and the lake bucket is never public.

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

The site has two halves in one bucket, and they never collide:

| | Written by | Guarded |
|---|---|---|
| `/`, `/docs`, `/demo` | `make publish-static` from `site-static/` | no |
| `/accounts`, `/<slug>/*` | the publisher Lambda, nightly | yes |

The publisher writes `home/<key>/index.html` and per-account keys only, so a nightly
run cannot overwrite the marketing pages; `make publish-static` scopes its
`--delete` to the `docs/` and `demo/` prefixes, so it cannot reach the app.

The public pages are hand-authored and carry no lake data — the demo is built
on a fictional company. Deploy them with:

```bash
cd tick-collector && make publish-static
```

### Domain

`SiteDomainName` (default `tick.sembasolutions.com`) drives the whole chain, so
the pieces cannot drift apart: the CloudFront alias, an ACM certificate
DNS-validated in `SiteHostedZoneId`, the A/AAAA alias records, the Cognito
callback and logout URLs, and the `site_url` the auth Lambda uses as its OIDC
`redirect_uri`. That last one is the subtle one — if it does not match the host
the viewer is actually on, the code exchange fails.

The distribution's own `*.cloudfront.net` domain stays registered and working.
Landing there sends you to the canonical domain after login, because that is the
host the session cookie is set on.

Deploying with `SiteDomainName=""` removes the alias, certificate and DNS
records and falls back to the CloudFront domain — the way out if certificate
validation ever wedges a deploy.

The accounts home at `/accounts` lists every account in `s3://<DataBucketName>/configs/` — the
same list the dispatcher collects from — so an account shows up the moment its
config is uploaded, marked `Not collected` until its first crawl and
`First crawl` until it has the two snapshots the dashboard and brief compare.

### Access

Viewers sign in with Cognito against **TICK's own user pool**, created by this
stack (`UserPoolId` output). It is invite-only — no self sign-up — and mail
(invites, password resets) goes out through Cognito's default sender, capped at
50 emails a day.

| In the pool | Why |
|---|---|
| App client `tick-collector-site` | Confidential client for the code exchange |
| Group `tick-viewers` | Only its members may view the site |
| Group `tick-admins` | Sees every account (and may also sign in without `tick-viewers`) |
| Attribute `custom:accounts` | The accounts a viewer may open: comma-separated company slugs |
| Hosted-UI domain `tick-login-<account>` | Serves the login page (`SiteLoginDomain` output) |

Being in the pool is not enough — membership in `tick-viewers` is what grants
access. A pool user who is not in the group gets an explanatory page, not a
login loop. To invite someone:

```bash
POOL=$(aws cloudformation describe-stacks --stack-name tick-collector \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)

# Sends the invite email with a temporary password
aws cognito-idp admin-create-user --user-pool-id "$POOL" \
  --username you@example.com \
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL

aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" \
  --group-name tick-viewers --username you@example.com

# Which accounts they see — the company slugs, as in configs/ and the page URLs.
# Overwrites the whole list, so pass every account each time.
aws cognito-idp admin-update-user-attributes --user-pool-id "$POOL" \
  --username you@example.com \
  --user-attributes Name=custom:accounts,Value="dunder-mifflin,initech"

# Or let them see everything
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" \
  --group-name tick-admins --username you@example.com

# Then render their accounts home
aws lambda invoke --function-name tick-collector-publisher /dev/stdout
```

A viewer sees only their accounts: the accounts home lists nothing else, and
any other account's `/<slug>/` page sends them back to it. Assignments are
read at sign-in, so a change applies at the viewer's next sign-in — after the
publisher has rendered a home for the new set. A viewer with no
`custom:accounts` and no `tick-admins` sees an empty home. Only the admin API
can write `custom:accounts`; the app client may not, so a viewer cannot grant
themselves an account.

Every accounts home ends with a **Track another company** form. It posts to
`/api/account-request` (the join Lambda, which checks the session cookie
itself because `/api/*` skips the edge check), stores the request in the
`account-requests` table and emails the alert address. To fulfil one: run the
evaluator for the company, add its slug to the requester's `custom:accounts`,
and republish.

```bash
make list-account-requests   # created_at, status, requester, company, careers URL
```

The hosted UI walks a new user through choosing a password on first sign-in.
After a deploy that replaces the app client, re-run `make publish-cognito-ui`:
the sign-in styling is attached per client.

#### How the check works

CloudFront Functions cannot verify an RS256 JWT or make a network call, so the
work is split:

1. The edge function first resolves extensionless URIs (`/docs` →
   `/docs/index.html`), because S3 has no directory semantics and
   `DefaultRootObject` only covers `/`. The two front doors come next: `/`
   and `/login` with a valid session → 302 to `/accounts`, so a signed-in
   viewer never lands on the marketing page. Then it lets the public prefixes
   through. For anything else, no session cookie → 302 to `/auth/login`,
   preserving the path you asked for (`/login` asks for `/accounts`).
2. `/auth/login` → the auth Lambda redirects to the Cognito hosted UI.
3. `/auth/callback` → the Lambda exchanges the code for tokens (client secret,
   server side), checks `aud` / `iss` / `token_use` / `exp` and the group
   claim, then mints an HMAC-signed session cookie good for 12 hours. The
   cookie carries the viewer's scope: `*` for `tick-admins`, else their
   normalised `custom:accounts`.
4. Every later request → one HMAC at the edge. No Lambda, no Cognito call.
   The edge refuses `/<slug>/` pages outside the scope, and serves `/accounts`
   from `home/<sha256(scope)[:16]>/index.html` — the publisher renders one
   such home per distinct scope in the pool.

`SessionSecret` is the HMAC key, shared by the Lambda and the edge function.
It has no default — **every deploy must pass it** — and rotating it signs out
every live session:

```bash
SECRET=$(openssl rand -hex 32)
sam deploy --parameter-overrides "AlertEmail=canyon.work01@gmail.com SessionSecret=$SECRET"
```

> **Quoting.** `--parameter-overrides` takes one string that SAM splits on
> spaces, so any value containing a space needs its own inner quotes —
> `'ScheduleExpression="cron(0 3 * * ? *)"'`, the form `samconfig.toml` uses.
> Without them the cron is truncated to `cron(0` and the schedule update fails
> the whole deploy. The command above sidesteps it: neither value has a space,
> and every parameter left out falls back to a template default that already
> matches (`cron(0 3 * * ? *)`, `ENABLED`, the pool id, `tick-viewers`).
> `AlertEmail` is the one exception worth passing — its default is empty, which
> would delete the DLQ alarm subscription.

Like any CloudFront Function source, it is readable by anyone holding
`cloudfront:GetFunction` on the account: it authenticates viewers, it is not a
secret from account principals. Sign out using the **Sign out** link in the
navigation on any protected app page, or directly at `/auth/logout`.

### Publishing

The publisher runs on `PublishScheduleExpression` (default `cron(30 3 * * ? *)`,
30 minutes after the dispatcher) and follows `ScheduleState`, so disabling the
pipeline disables publishing with it. Each run is a **full re-render** of the
lake as it stands — a removed account or a rolled-back snapshot cannot leave a
stale page behind — followed by a CloudFront invalidation. Pages carry
`max-age=300`, so even a failed invalidation self-corrects within 5 minutes.

### Briefs

No code writes brief prose. Each account's brief is written by a model with
the tick-brief skill (`.claude/skills/tick-brief/`), verified by its
`check.py` (every citation exists, every quote is verbatim, the length limits
hold) and uploaded by its `publish.py` to
`briefs/company=<slug>/week=<window end>/` in the data bucket: `brief.json`
plus the `evidence.json` it was written from, which the page resolves its
citations against. A brief that fails the check is never uploaded.

Each publish renders the newest week holding both files per account; an
account with none gets a "No brief yet" page at the same address. A skipped
week leaves last week's brief up, dated. To see a new brief before the
nightly run, invoke the publisher.

Republish immediately (after uploading a config or a brief, say):

```bash
aws lambda invoke --function-name tick-collector-publisher /dev/stdout
#    expect {"accounts_rendered": N, "homes_rendered": H, "pages": 2N+H, ...}
```

### Comparison window

Both pages compare the latest snapshot against **the most recent one at least 7
days old**, not against the previous crawl, and the chart plots every snapshot
between the two. An overnight delta on a daily board is mostly noise: one tracked board
moves +12/-7 between consecutive days while drifting far less, so a
crawl-to-crawl comparison reported ±1 and a flat line no matter what the week
did.

Change it with `--window-days N` locally. Where a company has less history than
the window — a board collecting for three days — it compares against the oldest
snapshot it has and labels the real dates, so a short window is visible rather
than being claimed as a full one.

### One menu bar, several copies

Every page shares the menu bar and tab icon of `/demo`, but nothing generates
them from one file. They are copied into:

| Copy | Where |
|---|---|
| Source of truth | `site-static/demo/**` (`.nav` CSS and markup, `<link rel="icon">`) |
| Marketing and docs | `site-static/index.html`, `site-static/docs/index.html`: brand and icon (their tabs are their own) |
| App pages | `_nav`, the `.nav` rules in `_STYLE`, and `_FAVICON` in `../tools/tick_insights_report.py` |
| Auth error page | `page()` in `src/auth/app.py`: brand, Docs and icon only |

Change them together. The app's bar differs from the demo's in its links only:
each account's own Dashboard and Brief tabs, and **Sign out** where the demo
has **Log in** / **Get started**.

### Render locally

To see a branch or PR as a site, use
[../tools/preview.py](../tools/preview.py). It syncs the lake into
`local-data/lake`, renders with that version's renderer and serves the result
alongside `site-static/` on localhost: `python3 tools/preview.py <branch|PR#>`
from the repo root.

The renderer is [../tools/tick_insights_report.py](../tools/tick_insights_report.py) — the
same file the Lambda runs, symlinked in as `src/publisher/report.py` so there
is no second copy to drift. It reads S3 or a local lake:

```bash
python3 ../tools/tick_insights_report.py s3://<DataBucketName> --out-dir site
python3 ../tools/tick_insights_report.py . --configs ../configs --out-dir site   # local dry-run lake
open site/index.html
```

Or exercise exactly what the Lambda renders, writing to disk instead of S3:

```bash
cd src/publisher
DATA_BUCKET=<DataBucketName> SITE_BUCKET=- LOCAL_OUTPUT_DIR=../../../site python3 app.py
```

## Failure semantics

- One company failing never affects the others: each is its own invocation.
- A failing message retries up to 3 times (`maxReceiveCount: 3`), then moves
  to the DLQ and raises the `tick-collector-collect-failures` alarm.
- A DLQ message means the SourceConfig has drifted — re-run the evaluator
  for that `company_slug`, upload the new config, and redrive or wait for
  the next daily run.
- The collector also fails deliberately when it collects 0 postings for a
  company whose state shows open ones (drift detection, not layoffs).

## Descriptions & keyword enrichment

For Workday sources the collector fetches each posting's full description
(CXS detail endpoint, one GET per posting, 0.1s politeness gap); ADP and
inline boards carry it in the posting payload. The original HTML is kept in
`raw/` for every ATS (Workday's is attached to the posting before `raw/` is
written), so descriptions can be re-parsed without refetching. Every curated
row gets:

- `description` — the description in labelled sections, parsed by
  [src/collector/description.py](src/collector/description.py):
  `summary`, `responsibilities`, `qualifications`, `preferred_qualifications`,
  `work_arrangement`, `compensation`, `benefits`, `company_intro`, `eeo`, and
  `other` for a heading nothing recognises (kept, never dropped); plus
  `compensation` as `{min, max, currency, period, text}` (a range, a single
  figure in a sentence about pay where `min == max`, or "up to $X" where `min`
  is null; figures in the millions, business figures such as premium or
  revenue, and malformed numbers are never pay), the boilerplate kinds
  found, and the unmatched headings. Headings match the SourceConfig's
  `description_format` (the aliases and boilerplate the evaluator learned for
  that board) first, then generic patterns. A parser error costs the row its
  `description`, never the run.
- `description_raw` — the flat text, written only when parsing failed. The
  sections hold every line, so curated no longer stores each description twice.
  Rows collected before structured descriptions hold only this field.
- `keywords` — `{category: [matched terms]}` from the tech-stack taxonomy in
  [src/collector/app.py](src/collector/app.py) (`cloud_aws` / `cloud_azure` /
  `cloud_gcp` / `data_platform` / `infra_devops` / `languages` /
  `analytics_tools` / `signal_terms`)
- `is_tech_role` — true on any hit outside `signal_terms`/`analytics_tools`

Keywords come from the title and the role text, never the board's intro,
benefits or equal-opportunity text, which repeat on every posting.

**Reading descriptions.** Every reader goes through the renderer's accessors in
[../tools/tick_insights_report.py](../tools/tick_insights_report.py):
`parsed_description()`, `role_text_of()`, `full_text_of()` and `pay_of()`. They
use the collector's sections when a row has them, and otherwise rebuild
approximate sections from the old flat text (`description.from_text()`: known
headings found inside the text, the board's shared opening sentences filed as
its intro). Nothing in the lake is rewritten; the rebuild happens on read and is
marked `approximate`. The publisher Lambda gets the parser the way it gets the
renderer, through a symlink (`src/publisher/description.py`).

Where it shows: archive pages lay a description out in its sections, with the
board's boilerplate left out and a pay range; the dashboard's open-roles table
has a Pay column; and the weekly brief's evidence pack sends each posting's
role text and `pay` (prompt v8).

Each run's summary reports `description_coverage`, the average share of
description text filed under a known section. A drop means the board's layout
moved and its `description_format` needs relearning.

Set `FETCH_DESCRIPTIONS=0` on the collector function to skip detail fetches
(keywords then come from titles only). Keyword matching is word-bounded and
case-insensitive — extend the `_KEYWORDS` lists freely.

Each run also writes a company tech-stack summary to
`derived/tech_stack/company=<slug>/dt=<date>/tech_stack.json` — every matched
term mapped to the posting IDs it appeared in, plus a posting evidence index.
Render it for account planning with [../tools/tech_report.py](../tools/tech_report.py):

```bash
python3 ../tools/tech_report.py out --company dunder-mifflin      # local dry-run output
python3 ../tools/tech_report.py s3://<DataBucketName> --company dunder-mifflin
```
