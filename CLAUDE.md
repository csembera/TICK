# TICK

Hiring-signal intelligence: collect a company's job postings daily, diff them
into a time series, and publish a dashboard and weekly brief per account at
https://tick.sembasolutions.com. Why it exists and how it is shaped:
[design-doc.md](design-doc.md). How it is deployed and operated:
[infra/README.md](infra/README.md). Read those before changing anything.

## Map

| Path | What |
|---|---|
| `infra/template.yaml` | The one SAM stack, `tick-collector` (us-east-1): Lambdas, SQS, S3, Cognito, CloudFront, alarms |
| `infra/src/dispatcher/` | Fans the daily run out: one SQS message per `configs/*.json` |
| `infra/src/collector/` | Fetches, normalizes, diffs one company. Also runs locally (`LOCAL_OUTPUT_DIR`). `description.py` parses each description into labelled sections |
| `infra/src/publisher/` | Renders the whole lake to static HTML in the site bucket, then invalidates. `report.py` and `description.py` are symlinks to the renderer and the collector's parser |
| `infra/src/auth/` | Cognito code exchange; mints the HMAC session cookie |
| `infra/src/join/` | `/api/join` waitlist and `/api/account-request` |
| `infra/site-static/` | Hand-authored public pages (`/`, `/docs`, `/demo`), deployed by `make publish-static` |
| `tools/tick_insights_report.py` | The renderer. `infra/src/publisher/report.py` is a **symlink** to it |
| `tools/evaluator.py` | Writes a company's SourceConfig |
| `tests/` | `python3 -m unittest discover tests`; stdlib only, synthetic fixtures (the repo is public) |
| `configs/` | Gitignored local copy of the SourceConfigs. The data bucket's `configs/` prefix is the source of truth |
| `.claude/skills/` | `tick-evaluator` (onboard a company; learn its description format), `tick-brief` (write, check and publish every account's weekly brief; tune its prompt) |

## Invariants. Do not break these without saying so in the plan

- **The collector is dumb and deterministic.** Reasoning lives in the evaluator,
  once per company. The collector only executes a verified config.
- **Lambdas use the standard library and boto3 only.** There are no
  per-function `requirements.txt` files. Adding a dependency means changing
  the build, so it needs a decision first.
- **Edit the renderer in `tools/`, never through the symlink path.** It is one
  file run two ways: by the CLI and by the publisher Lambda. The same goes for
  `description.py`, which lives in `infra/src/collector/`.
- **Read descriptions through the renderer's accessors** (`parsed_description`,
  `role_text_of`, `full_text_of`, `pay_of`), never `description_raw` directly:
  new rows carry only `description`, old rows only `description_raw`.
- **Guarded paths must never serve without a session.** A 200 on `/accounts` or
  `/<slug>/*` without a cookie is a client-data leak, not a cosmetic bug.
- **The two halves of the site bucket never overlap.** The publisher writes only
  `home/` and `<slug>/` keys. `publish-static` uses `--delete` only on
  `docs/` and `demo/`. New keys must keep that separation.
- **Briefs are written by a model, never by code.** The site shows the
  newest brief the tick-brief skill published to the lake's `briefs/` prefix,
  or "No brief yet". A brief reaches the bucket only through `publish.py`,
  after `check.py` passes it; never hand-fix or hand-upload one.
- **One company failing never affects the others.** Each company gets its own
  invocation. Keep it that way.
- **Collected data and configs stay out of git** (`out/`, `site/`, `local-data/`
  and `configs/` are gitignored). They live in the data bucket. The repo is
  public: never commit a SourceConfig or real lake data.
- **The menu bar and tab icon are copied, not shared.** `/demo` is the source
  of truth. Copies live in the static pages (`infra/site-static/demo/**`, and
  the brand and icon on `/` and `/docs`), in the renderer (`_nav`, the `.nav`
  rules in `_STYLE`, `_FAVICON` in `tools/tick_insights_report.py`), and in the
  auth Lambda's error page (`page()` in `infra/src/auth/app.py`). A change to
  any one is a change to all of them, in the same PR. Check with
  `tools/preview.py`, which serves the demo and the app pages side by side.
- **Cognito hosted-UI styling is not a CloudFormation resource.** It belongs to
  `make publish-cognito-ui`. Declaring it in the template makes the deploy fail
  with `AlreadyExists`.
- **Every deploy must pass the existing `SessionSecret` and `AlertEmail`.** A
  new secret signs everyone out. An empty `AlertEmail` deletes the DLQ alarm
  subscription.

## Code style

Match the code around you. Python 3.12. Each module opens with a docstring
saying what it does and why. Comments explain *why*, never *what*. Prefer
plain functions and stdlib over abstractions. Commit subjects are short,
sentence case and imperative, with no prefix tags ("Archive closed postings,
each at /<slug>/archive/<pid>"). Commits and PR descriptions carry no AI
attribution: no `Co-Authored-By: Claude` trailer, no "Generated with Claude
Code" line.

## How a feature gets built

### 1. Analyze and brainstorm (before any code)

When asked for a new feature, do not start editing. First:

1. **Read what it touches.** Trace the data path end to end: config →
   dispatcher → collector → lake layout → publisher/renderer → edge/auth →
   page. Name the files and functions involved.
2. **Brainstorm 2–3 approaches.** For each, give the shape, what it touches
   and its trade-offs: cost, blast radius, effect on the invariants above,
   and whether it needs a `template.yaml` change.
3. **Recommend one** and write a plan: files to change, new S3 keys or
   resources, the deploy-order impact, and how it will be tested locally.
4. **Wait for approval** before writing code.

### 2. Branch

Branch off `main`: `git switch main && git pull && git switch -c feature/<short-name>`.
Never commit directly to `main`.

### 3. Build and test locally

Run the tests, then prove the change locally before opening the PR. The suite
is young (the description parser only), so it backs up a local check rather
than replacing one:

```bash
python3 -m unittest discover tests
```

For anything that changes what a page shows, serve the site on
localhost (public pages plus every account, from a local copy of the lake):

```bash
python3 tools/preview.py --refresh   # first run, or for today's data: sync the bucket into local-data/lake
python3 tools/preview.py             # the working tree, uncommitted edits included
python3 tools/preview.py <branch>    # any branch or commit;  python3 tools/preview.py <PR#>  for a PR
```

Not every change needs a preview. The edge, Cognito and per-viewer scoping
only run on AWS. Other checks, as they apply:

```bash
# Collector against a live board, writing to disk instead of S3
cd infra/src/collector && LOCAL_OUTPUT_DIR=../../../out python3 app.py ../../../configs/<slug>_config.json

# Exactly what the publisher Lambda renders, written to disk
cd infra/src/publisher && DATA_BUCKET=<DataBucketName> SITE_BUCKET=- LOCAL_OUTPUT_DIR=../../../site python3 app.py

# Template changes
cd infra && sam validate --lint && sam build
```

### 4. Pull request

Push the branch and open a PR against `main` with `gh pr create`. The PR
description says what changed and why, and says how to check it: `python3 tools/preview.py <PR#>` when pages change,
otherwise the step 3 commands that apply. It also flags any
change to `template.yaml`, the edge function, auth or deploy order.

### 5. Merge → production

Merging to `main` is the release. A push to `main` that touches `infra/**` or
the renderer runs [.github/workflows/deploy.yml](.github/workflows/deploy.yml):

1. **plan:** `sam validate --lint`, `sam build`, `sam package`, then a
   CloudFormation change set. Every existing parameter is reused, so
   `SessionSecret` and `AlertEmail` carry over. The change set goes to the
   run summary, and removals and replacements are flagged.
2. **Human gate:** the `deploy` job waits on the `production` environment's
   required reviewer. That review is the `confirm_changeset` step.
3. **deploy:** after approval it runs the order from
   [infra/README.md § Deploying a change](infra/README.md#deploying-a-change):
   `make publish-static` (static content always goes first), execute the change
   set, `make publish-cognito-ui`, invoke the publisher, then check that public
   paths return 200 and guarded paths 302.

AWS access is OIDC through two roles in
[infra/github-deploy-roles.yaml](infra/github-deploy-roles.yaml). The plan
role can only create change sets. The deploy role can be assumed only by an
approved `production` job.

When a change replaces a stateful resource (the user pool, a bucket, a
table), say so in the PR. The reviewer has to spot it in the change set.

Never run `sam deploy`, `make publish-*`, invoke a production Lambda or
approve a deployment unless the user explicitly asks in this session. The
manual steps in infra/README.md are the fallback when the pipeline is down.
