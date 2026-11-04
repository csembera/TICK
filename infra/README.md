# The tick-collector stack

Every AWS resource TICK runs on is one SAM stack, `tick-collector` in
us-east-1, defined in [template.yaml](template.yaml). This file covers the
stack itself: what is in it, first-time setup, deploys, parameters, the domain
and sign-in.

- How the system works: [docs/architecture.md](../docs/architecture.md)
- Operating it day to day, through `tools/tick.py`: [docs/operations.md](../docs/operations.md)
- File formats: [docs/data-reference.md](../docs/data-reference.md)
- Local development: [docs/development.md](../docs/development.md)

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
      briefs/company=<slug>/overview=<date>/{brief,evidence}.json   a new account's first

EventBridge Scheduler (daily, 30 min later)
  └─► Publisher Lambda ── reads the lake + s3://<bucket>/configs/*.json
        └─► S3 site bucket (private)
              ├─ home/<key>/index.html         an accounts home per viewer scope (/accounts)
              ├─ home/profile/index.html       the profile page, one for everyone (/profile)
              ├─ <slug>/dashboard.html
              ├─ <slug>/brief.html             current brief (newest weekly, else overview), or "No brief yet"
              ├─ <slug>/briefs/index.html      the brief archive, and one page per brief beside it
              └─ <slug>/archive/<pid>/index.html   one per closed req

site-static/ ── tools/tick.py publish static ──► the same bucket
              ├─ index.html                    marketing page
              ├─ og-image.png
              ├─ docs/index.html
              └─ demo/**/index.html

        └─► CloudFront ── the only way in
              ├─ /, /docs, /demo  ─► public, no session needed
              ├─ /auth/*          ─► Auth Lambda ─► Cognito hosted UI (TICK's own pool)
              ├─ /api/*           ─► Join Lambda (waitlist, account requests)
              └─ everything else  ─► CloudFront Function checks the session cookie
```

## What is in it

| Resource | What it is |
|---|---|
| `DataBucket` | The lake and the SourceConfigs. Versioned, and **retained** if the stack is deleted |
| `DispatcherFunction` (`tick-collector-dispatcher`) | Queues one crawl per config, on `ScheduleExpression` |
| `CollectQueue`, `CollectDLQ`, `DLQAlarm`, `AlertTopic` | The crawl queue (30-minute visibility, 3 attempts), the dead-letter queue (14 days), the alarm on it, and the email topic for `AlertEmail` |
| `CollectorFunction` (`tick-collector-collector`) | Crawls one company per invocation, at most two at a time |
| `PublisherFunction` (`tick-collector-publisher`) | Renders the lake into the site bucket, on `PublishScheduleExpression` |
| `SiteBucket`, `SiteDistribution`, `SiteAuthFunction` | The private site bucket, CloudFront in front of it, and the edge function that checks sessions |
| `AuthFunction` (`tick-collector-auth`) | `/auth/*`: the Cognito code exchange; mints the session cookie |
| `JoinFunction` (`tick-collector-join`), `JoinRequestsTable`, `AccountRequestsTable` | `/api/*`: the public waitlist, viewers' account requests, and each viewer's persona (`/api/profile`, read and write on the data bucket's `profiles/*` only). Both tables **retained**, with point-in-time recovery |
| `SiteUserPool`, its domain, client and the `tick-viewers` / `tick-admins` groups | TICK's own invite-only user pool |
| `SiteCertificate`, `SiteDnsRecords` | The custom domain, when `SiteDomainName` is set |
| `AuthConfigParameter` | SSM parameter that breaks the build-time cycle between the auth Lambda and the distribution |

The roles GitHub Actions deploys with are a separate stack,
`tick-github-deploy`, from [github-deploy-roles.yaml](github-deploy-roles.yaml),
deployed once by hand so that a bad deploy of this stack cannot break the roles
that deploy it. `tick-github-plan` can only create change sets;
`tick-github-deploy` can be assumed only by an approved `production` job.

## First-time setup

Prerequisites: AWS CLI with working credentials (`aws sts get-caller-identity`),
SAM CLI (`brew install aws-sam-cli`).

```bash
# 1. Deploy the stack (from infra/). Keep SECRET: every later deploy passes it.
SECRET=$(openssl rand -hex 32); echo "$SECRET"
sam build
sam deploy --guided
#    Stack Name:          tick-collector
#    ScheduleState:       DISABLED       (enable after first manual run succeeds)
#    AlertEmail:          you@example.com (optional — DLQ alarm emails)
#    ViewerGroupName:     tick-viewers
#    SessionSecret:       the value of $SECRET
#    Allow IAM role creation: y
#    Save to samconfig.toml:  y
#    -> note the DataBucketName and SiteUrl outputs

# 2. Register the first company (from the repo root)
tools/tick.py companies push configs/dunder-mifflin_config.json

# 3. First manual run: crawl it, wait for the crawl, render the site
tools/tick.py collect dunder-mifflin --publish
#    debug: tools/tick.py logs collector --since 30m

# 4. Check everything: the company should show a last crawl from just now
tools/tick.py status

# 5. Turn on the daily schedule (from infra/). The overrides replace the saved
#    ones, so pass the secret and the alert address again (see Parameters).
sam deploy --parameter-overrides "ScheduleState=ENABLED AlertEmail=you@example.com SessionSecret=$SECRET"

# 6. Invite yourself
tools/tick.py users invite you@example.com --admin
```

Every later operation, from inviting a viewer to redriving a failed crawl, is
a `tools/tick.py` command; see [docs/operations.md](../docs/operations.md).

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
tools/tick.py publish static

# 2. The stack (from infra/).
sam build
sam deploy --parameter-overrides "AlertEmail=you@example.com SessionSecret=$SECRET"

# 3. Hosted-UI styling, if cognito-ui.css or the banner changed. Independent of
#    CloudFormation (see "Which target owns what" below) and idempotent, so the
#    position here does not matter.
tools/tick.py publish login

# 4. Re-render the app pages. Until this runs, /accounts and /profile 404 for signed-in users.
tools/tick.py publish pages
```

The `tools/tick.py` commands run from the repo root.

### Verify

```bash
tools/tick.py verify
```

It requests each public page (`/`, `/docs`, `/demo`, a demo brief) with no
cookie and expects 200, then each guarded one (`/accounts`, `/profile`, `/login`, an
account's dashboard, brief and brief archive) and expects a 302 to `/auth/login`. **A 200 on a
guarded page is a client-data leak, not a cosmetic bug.** The pipeline runs the
same command as its last step.

Then sign in through a browser: `/` → **Log in** → Cognito (TICK palette, TICK
banner) → lands on `/accounts` showing the accounts home. Then open `/` and
`/login` again: both must 302 straight to `/accounts` while you are signed in.

### Which target owns what

| Path | Deployed by | Guarded |
|---|---|---|
| `/`, `/docs`, `/demo` | `tools/tick.py publish static` | no |
| Cognito hosted UI | `tools/tick.py publish login` (`cognito-ui.css` + banner) | n/a |
| `/accounts`, `/profile`, `/<slug>/*` | the publisher Lambda | yes |

The hosted-UI customization is deliberately **not** a CloudFormation resource.
`SetUICustomization` and `AWS::Cognito::UserPoolUICustomizationAttachment`
create the same underlying object, so declaring it in the template while also
running `tools/tick.py publish login` fails the deploy with `AlreadyExists` and
rolls the stack back. The template does not declare it; the CLI owns it.
`tools/tick.py cognito css` checks the live sign-in CSS against the repo's.

`site-static/branding/` is source for the Cognito banner and is deliberately
not synced — `tools/tick.py cognito banner` re-renders the PNG from it after a copy edit.

## Parameters

| Parameter | Default | |
|---|---|---|
| `ScheduleExpression` | `cron(0 3 * * ? *)` | When the dispatcher fires (UTC) |
| `PublishScheduleExpression` | `cron(30 3 * * ? *)` | When the publisher fires: keep it after the crawls |
| `ScheduleState` | `ENABLED` | `DISABLED` stops both schedules |
| `AlertEmail` | empty | Where the DLQ alarm and request notifications go. **Empty deletes the subscription** |
| `SessionSecret` | none | The session cookie's HMAC key, shared with the edge function. **Required on every deploy** |
| `SiteDomainName` | `tick.sembasolutions.com` | The custom domain; empty serves on the CloudFront domain only |
| `SiteHostedZoneId` | the sembasolutions.com zone | Route 53 zone holding the domain |
| `SenderDomain`, `SenderAddress` | `sembasolutions.com`, `support@sembasolutions.com` | The SES identity invites and confirmations are sent from (owned by the Semba platform stack) |
| `ViewerGroupName`, `AdminGroupName` | `tick-viewers`, `tick-admins` | The two groups that may sign in |

The pipeline reuses every deployed value, so it never changes a parameter.
**Changing one is a manual deploy**, as in step 2 of
[Deploying a change](#deploying-a-change), with the new value added to
`--parameter-overrides`. Pass `SessionSecret` (the recovered one, unless you
mean to sign everyone out) and `AlertEmail` (readable with
`aws cloudformation describe-stacks --stack-name tick-collector --query "Stacks[0].Parameters"`)
every time.

> **Quoting.** `--parameter-overrides` takes one string that SAM splits on
> spaces, so any value containing a space needs its own inner quotes —
> `'ScheduleExpression="cron(0 3 * * ? *)"'`, the form `samconfig.toml` uses.
> Without them the cron is truncated to `cron(0` and the schedule update fails
> the whole deploy. A parameter left out falls back to its template default
> (`cron(0 3 * * ? *)`, `ENABLED`, `tick-viewers`), which is harmless for all of
> them except `AlertEmail`, whose empty default deletes the alarm subscription.

**Rotating `SessionSecret`** signs every live session out; it is also the only
way to end a session before its 12 hours are up. See
[docs/operations.md § Cut off someone's access now](../docs/operations.md#cut-off-someones-access-now).

## Domain

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

## Access

Viewers sign in with Cognito against **TICK's own user pool**, created by this
stack (`UserPoolId` output). It is invite-only, with no self sign-up. Mail
(invites, password resets) goes out through SES as `TICK <SenderAddress>`,
DKIM-signed from the company domain, rather than from Cognito's no-reply
address and its 50-a-day cap.

| In the pool | Why |
|---|---|
| App client `tick-collector-site` | Confidential client for the code exchange |
| Group `tick-viewers` | Only its members may view the site |
| Group `tick-admins` | Sees every account (and may also sign in without `tick-viewers`) |
| Attribute `custom:accounts` | The accounts a viewer may open: comma-separated company slugs |
| Hosted-UI domain `tick-login-<account>` | Serves the login page (`SiteLoginDomain` output) |

Users are managed with `tools/tick.py users`, described in
[docs/operations.md](../docs/operations.md#invite-someone); how a session is
checked at the edge is in
[docs/architecture.md](../docs/architecture.md#how-the-check-works). Custom
attributes can be added to a live pool but never changed or removed, so think
twice before adding one.

The hosted UI walks a new user through choosing a password on first sign-in.
After a deploy that replaces the app client, re-run `tools/tick.py publish login`:
the sign-in styling is attached per client.
