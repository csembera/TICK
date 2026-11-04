# Operating TICK

The runbook: routine tasks first, then what to do when something breaks. Every
task goes through [tools/tick.py](../tools/tick.py), run from the repo root
with an AWS session for the TICK account (`aws sts get-caller-identity` shows
which one you have).

`tools/tick.py` reads freely and asks before it changes anything. A changing
command lists what it will do and waits for a yes; `--dry-run` prints the exact
AWS calls instead of making them, and `--yes` skips the question (the deploy
pipeline uses it). `tools/tick.py <command> --help` documents each command.

## Commands

| Command | What it does | Changes production |
|---|---|---|
| `status` | Schedules, each company's last crawl and latest brief, last publish, queues, the alarm, pending requests. Exits 1 if anything needs attention | no |
| `companies list` | Every config in the bucket, its ATS and last crawl, and any duplicate slug | no |
| `companies push <file>` | Upload a verified config, after checking its slug and showing the diff against the live one | yes |
| `companies pull` | Refresh the local `configs/` from the bucket, matching files by slug | local files only |
| `collect <slug>...` / `--all` | Crawl now. `--wait` waits for the crawls; `--publish` waits, then re-renders | yes |
| `publish pages` | Re-render `/accounts`, `/profile` and every account's pages | yes |
| `publish static` | Upload `/`, `/docs` and `/demo` from `infra/site-static/` | yes |
| `publish login` | Apply the sign-in page's CSS and banner | yes |
| `users list` | Every user, their status and what they can see | no |
| `users invite <email> [--accounts a,b] [--admin]` | Create a user, email a temporary password, set their accounts | yes |
| `users grant` / `revoke <email> <slug>...` | Add or remove accounts, keeping the rest of the list | yes |
| `users admin <email> [--remove]` | Let a user see every account, or take that away | yes |
| `users disable` / `enable` / `resend <email>` | Stop sign-in, allow it again, re-send an expired invite | yes |
| `personas list [--json]` | Each account, the user who tracks it, and who its brief is written for | no |
| `personas set <email> <file>` | Save a user's persona from a JSON file | yes |
| `requests list [--accounts] [--pending]` | The waitlist, or viewers' requests for new companies | no |
| `requests close <email or id> --as granted\|declined\|done` | Record how a request was handled | yes |
| `dlq list` / `dlq redrive` | Crawls that failed three times; send them back to be retried | `redrive` only |
| `logs <function> [--since 6h] [--grep text] [--follow]` | Tail a Lambda's logs: `collector`, `dispatcher`, `publisher`, `auth`, `join` | no |
| `verify` | Public pages return 200; guarded pages redirect to sign-in | no |
| `cognito css` / `cognito banner` | Check the live sign-in CSS against the repo; re-render the banner PNG | no |

## The rhythm

| When (UTC) | What runs | What to check |
|---|---|---|
| 03:00 nightly | The dispatcher queues every company; collectors crawl them, two at a time | A failure retries every 30 minutes, three times, then emails the alert address |
| 03:30 nightly | The publisher re-renders the site | `status` shows the last publish |
| Weekly | You run the tick-brief skill; briefs publish once you approve them | `status` shows each company's latest brief |

`tools/tick.py status` is the morning check. It ends **All good.** or with a
**Needs attention** list (a missed crawl, a failed crawl, the alarm, a
disabled schedule), and exits 1 in the second case, so it can also run from a
scheduler.

## Routine tasks

### Add a company

1. In Claude Code, ask to onboard the company. The
   [tick-evaluator skill](../.claude/skills/tick-evaluator/SKILL.md) finds its
   job board, verifies the endpoint in code, learns its description layout and
   writes `configs/<slug>_config.json`.
2. Upload it:
   ```bash
   tools/tick.py companies push configs/<slug>_config.json
   ```
3. Crawl it now rather than tonight, and render its pages:
   ```bash
   tools/tick.py collect <slug> --publish
   ```
4. Let viewers see it: `tools/tick.py users grant <email> <slug>`.

The dashboard appears after the first crawl; change figures and weekly
briefs start with the second. Once it has crawled, write its overview (below)
so its Brief tab is not empty for the first week.

### Re-crawl a company

```bash
tools/tick.py collect <slug> --publish
```

A second crawl on the same day is safe: it merges into that day's record. After
any re-crawl, check that no day's record lost an opening or closure:

```bash
python3 tools/audit_diffs.py s3://<DataBucketName>
```

### Update a company's config

When a board moves, or its descriptions start filing badly, re-run the
tick-evaluator skill for the company (it has a shorter path for relearning just
the description format). Then push the new config: `companies push` shows
exactly what changed against the live one before it uploads. Crawl with
`collect <slug>` to see the effect today.

### Stop tracking a company

There is no command for this, on purpose: it is rare and hard to undo by
accident. Find the config's key with `tools/tick.py companies list`, then:

```bash
aws s3 rm s3://<DataBucketName>/configs/<key>.json
```

Crawling stops tonight. The account's pages stay up, frozen at the last crawl,
because the publisher also lists every company it finds in the lake. Revoke it
from its viewers (`users revoke`). Taking it off the site too means deleting
its prefixes from the data bucket (versioned, so recoverable) and its
`<slug>/` pages from the site bucket. Decide that deliberately.

### Invite someone

```bash
tools/tick.py users invite pam@example.com --accounts geha,sysco
tools/tick.py users invite jan@example.com --admin            # sees every account
```

They get an email from `TICK <support@sembasolutions.com>` with a temporary
password, valid 7 days, and choose their own at `/login`. The CLI refuses an
account slug that is not tracked, renders their accounts home if no viewer had
that set of accounts before, and marks their waitlist request granted if they
had one. If the invite expires: `tools/tick.py users resend <email>`.

### Change what someone sees

```bash
tools/tick.py users grant  pam@example.com valero      # adds valero, keeps the rest
tools/tick.py users revoke pam@example.com sysco       # removes only sysco
tools/tick.py users admin  pam@example.com             # every account (--remove to undo)
tools/tick.py users list                               # who sees what
```

A change applies at their next sign-in. A revoked account stays visible to a
session they already have for up to 12 hours; see
[Cut off someone's access now](#cut-off-someones-access-now) if that matters.

### Remove someone

```bash
tools/tick.py users disable pam@example.com
```

Disabling is reversible (`users enable`) and keeps their account list. To
delete the user outright, which the CLI leaves to the console or
`aws cognito-idp admin-delete-user --user-pool-id <UserPoolId> --username <email>`,
disable first, then delete once you are sure.

### Handle requests

The public "Get started" form fills the waitlist; the **Track another company**
card on `/accounts` files account requests. Each new one emails the alert
address.

```bash
tools/tick.py requests list --pending                  # the waitlist
tools/tick.py users invite <email> --accounts <slugs>  # grants it (marks the request granted)
tools/tick.py requests close <email> --as declined     # or declines it

tools/tick.py requests list --accounts --pending       # account requests
#   onboard the company (above), then:
tools/tick.py users grant <requester email> <slug>
tools/tick.py requests close <id> --as done            # the first characters of the id are enough
```

### Give an account's brief a persona

An account's weekly brief is written for the persona of the one user who
tracks it. Users describe themselves on `/profile` (**Edit profile** in the
account menu); its
focus terms are their My terms on every dashboard. What remains is yours:
say which accounts each user tracks.

```bash
tools/tick.py users grant pam@example.com geha starr-companies   # the accounts she works
tools/tick.py personas list                                      # check: who each brief is for
tools/tick.py personas set pam@example.com persona.json          # or write a persona for someone
```

`persona.json` holds the fields in [data-reference.md § A profile](data-reference.md#a-profile).
For an admin, `users grant` changes nothing they can see; it marks the
accounts they track. An account tracked by no one, or by two users, gets the
default reader, and `users grant` warns before you make it two. The persona
applies from the next weekly run.

### Write the weekly briefs

In Claude Code, ask to run the weekly briefs. The
[tick-brief skill](../.claude/skills/tick-brief/SKILL.md) syncs the lake,
finds each account's reader with `personas list`, reads every persona before
it reaches a writer, builds each account's evidence, has a model write each
brief in a clean context, verifies every citation with `check.py`, scores
each against its rubric and shows the drafts in the real page design. Nothing is uploaded until
you agree. Its `publish.py` uploads the ones you approve; they appear at the
next nightly publish, or now with `tools/tick.py publish pages`.

### Write a new account's overview

In Claude Code, ask for overview briefs for new accounts. The tick-brief
skill's "Overview run" lists every account with a crawl and no brief yet
(`accounts.py --overview`), builds each one's overview pack, has a model
write it from `prompt-overview.md`, checks and previews it like a weekly
brief, and `publish.py --overview` uploads the ones you approve. It shows on
the account's Brief tab until the first weekly brief replaces it; `status`
shows it as "overview, <date>".

### Find an earlier brief

Every published brief stays at `/<slug>/briefs`, linked from the top of
each brief page as "Brief archive (N)". Nothing is ever removed from it by a
publish; a brief leaves only if its files are deleted from the data bucket's
`briefs/` prefix, which the next nightly publish then reflects (the old page
object stays in the site bucket until deleted by hand).

### Change the public pages or the sign-in page

Edit `infra/site-static/` (the marketing page, `/docs`, `/demo`) or
`infra/cognito-ui.css` and `infra/site-static/branding/cognito-banner.html`,
open a PR, and merge it: the deploy publishes both. After editing the banner's
HTML, re-render the PNG with `tools/tick.py cognito banner` and commit it.
`tools/tick.py cognito css` tells you whether the live sign-in page matches the
repo. By hand, `publish static` and `publish login` do the same as the
pipeline; use them only when the pipeline is down.

### Change the schedule or another stack parameter

The pipeline always reuses the deployed parameter values, so changing one (the
crawl time, the alert address, the domain) is a manual deploy. See
[infra/README.md § Parameters](../infra/README.md#parameters).

## When something breaks

### A crawl failed (the DLQ alarm)

The alarm email means a company's crawl failed three times in a row.

```bash
tools/tick.py dlq list                                  # which company, when
tools/tick.py logs collector --since 1d --grep <slug>   # why
```

Almost always the board changed: a new URL, a new ATS, a changed response.
Re-run the tick-evaluator skill for the company, push the new config and crawl
it:

```bash
tools/tick.py companies push configs/<slug>_config.json
tools/tick.py collect <slug> --publish
```

The message in the DLQ still carries the old config. If the failure was
transient instead (the board was down), `tools/tick.py dlq redrive` retries it
as it was. Messages expire from the DLQ after 14 days; either way, the company
is crawled again tonight.

### A company shows STALE in `status`

Its last crawl is more than 26 hours old. Check, in order:

- the **Schedules** lines: a `DISABLED` collect schedule stops every crawl;
- **Queue**: a crawl still in flight is retrying, every 30 minutes;
- **DLQ**: it failed three times (above);
- `tools/tick.py logs collector --since 1d --grep <slug>` for anything else.

### A crawl reports "collected 0 postings but N were open"

The collector refuses an empty board when the company had open roles: that is
almost always a broken endpoint, not a company that stopped hiring overnight.
Treat it as a failed crawl (above).

### Descriptions are filing badly

Each collector run logs `description_coverage`, the share of description text
filed under a known section (`tools/tick.py logs collector --grep coverage`).
A drop means the board's layout changed. Relearn its `description_format` with
the tick-evaluator skill, push the config, and re-crawl.

### Publishing failed

`tools/tick.py publish pages` prints the publisher's error, and
`tools/tick.py logs publisher --since 1h` shows the rest. The publisher renders
every page before writing any, so a failed run leaves the site exactly as the
last good run left it.

### A deploy failed

On the run's page in GitHub Actions:

- **plan failed:** the change set could not be created. The annotation gives
  CloudFormation's reason; fix the template and push again.
- **"Change set ... is obsolete":** the stack changed while the run waited for
  approval. Re-run all jobs to plan again.
- **Execute change set failed:** CloudFormation rolled the stack back; the
  failing resources are annotated. Nothing else in the deploy ran after it.
- **Verify the edge failed:** see
  [verify reports a guarded page served without sign-in](#verify-reports-a-guarded-page-served-without-sign-in)
  and treat it as urgent.

When the pipeline itself is unavailable, the manual steps are in
[infra/README.md § Deploying a change](../infra/README.md#deploying-a-change).

### Someone can't see an account

`tools/tick.py users list` shows what they hold. If the account is missing,
`users grant` it. If it is listed, they signed in before it was granted: the
list is read at sign-in, so they need to sign out (**Sign out** in the account menu) and
in again.

### Someone is told "No access to TICK"

They are in the user pool but not in `tick-viewers` or `tick-admins`. `users
list` shows "none: not in tick-viewers". `users invite` refuses an existing
user, so add them to the group with
`aws cognito-idp admin-add-user-to-group --user-pool-id <UserPoolId> --group-name tick-viewers --username <email>`,
then `users grant` their accounts.

### Sign-in fails or lands on the wrong host

The session cookie is set on the canonical host, `tick.sembasolutions.com`.
Someone arriving on the distribution's `*.cloudfront.net` domain is carried to
it by the sign-in round trip, by design.

A "Sign-in failed" page comes from the auth Lambda.
`tools/tick.py logs auth --since 1h` shows the exception behind it. The usual
cause after an infrastructure change is a mismatch between the host and the
`site_url` the Lambda sends to Cognito as its redirect URI, which fails the code
exchange; see [infra/README.md § Domain](../infra/README.md#domain). After a
deploy that replaced the app client, the sign-in page also loses its styling
until `tools/tick.py publish login` runs (the pipeline does).

### Cut off someone's access now

`users disable` stops their next sign-in, but a session they already hold stays
valid for up to 12 hours: the edge checks the cookie's signature and never asks
Cognito. To end every session at once, deploy a new `SessionSecret`. Everyone,
you included, signs in again.

```bash
tools/tick.py users disable <email>
cd infra
ALERT=$(aws cloudformation describe-stacks --stack-name tick-collector \
  --query "Stacks[0].Parameters[?ParameterKey=='AlertEmail'].ParameterValue" --output text)
sam build
sam deploy --parameter-overrides "AlertEmail=$ALERT SessionSecret=$(openssl rand -hex 32)"
```

Later pipeline deploys reuse the new secret.

### `verify` reports a guarded page served without sign-in

A 200 on `/accounts` or an account's pages without a cookie means client data
is public. Revert the change that caused it (usually the edge function in
`infra/template.yaml`, or a new key outside `home/` and `<slug>/`) and deploy
the revert at once. If the revert will take a while, disabling the distribution
in the CloudFront console takes the whole site offline, which is the safe
failure.

### Alert emails stopped arriving

SNS delivers to an email address only once its subscription is confirmed, and
drops an unconfirmed one after three days. Check:

```bash
aws sns list-subscriptions-by-topic --topic-arn \
  "$(aws sns list-topics --query "Topics[?ends_with(TopicArn, ':tick-collector-alerts')].TopicArn" --output text)"
```

`PendingConfirmation` means the confirmation email is waiting to be clicked. If
the subscription is gone, a manual deploy that omitted `AlertEmail` deleted it.
Redeploy with the address (see
[Change the schedule or another stack parameter](#change-the-schedule-or-another-stack-parameter))
and click the confirmation email.
