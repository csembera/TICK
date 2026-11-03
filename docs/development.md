# Developing TICK

How to set up, run the pieces locally, and ship a change. The rules every change
must keep are in [CLAUDE.md](../CLAUDE.md) (written for Claude Code, and binding
on anyone); how the system fits together is in
[architecture.md](architecture.md).

## Setup

| Tool | For |
|---|---|
| Python 3.12 or later | Everything. The Lambdas run 3.12; the local tools and tests use the standard library only |
| AWS CLI v2, with a session for the TICK account | `tools/tick.py`, `preview.py --refresh`, the brief skill's uploads |
| SAM CLI (`brew install aws-sam-cli`) | Building and validating `infra/template.yaml` |
| GitHub CLI (`gh`) | Pull requests, and `preview.py <PR#>` |
| Google Chrome | `tools/tick.py cognito banner`, which renders the sign-in banner headless |
| Claude Code | The tick-evaluator and tick-brief skills in `.claude/skills/` |

The evaluator's standalone agent ([tools/evaluator.py](../tools/evaluator.py))
is the only part with third-party dependencies:

```bash
python3 -m venv .venv && .venv/bin/pip install -r tools/requirements.txt
```

The skills run their scripts with `.venv/bin/python` when it exists.

Local copies of production data, all gitignored:

```bash
python3 tools/preview.py --refresh     # the lake, into local-data/lake (raw/ skipped), then serves it
tools/tick.py companies pull           # the SourceConfigs, into configs/
```

The repo is public. Collected data, configs and rendered pages stay out of git
(`configs/`, `local-data/`, `out/` and `site/` are ignored), and tests use
synthetic fixtures built on fictional companies (Dunder Mifflin, Initech).

## Tests

```bash
python3 -m unittest discover tests
```

Standard library only, no network, no AWS. They cover the description parser,
the ATS modes, same-day re-runs, first crawls, the dashboard's table data and
the operations CLI (against a fake AWS CLI that records every call). The suite
backs up a local check of the change; it does not replace one.

## Running the pieces locally

**The site, from any version of the code.** For anything that changes what a
page shows:

```bash
python3 tools/preview.py               # the working tree, uncommitted edits included
python3 tools/preview.py my-branch     # any branch, tag or commit
python3 tools/preview.py 12            # pull request #12
python3 tools/preview.py --briefs local-data/briefs   # also show draft briefs that pass check.py
```

It renders the local lake with that version's renderer and serves it beside
`infra/site-static/` on <http://127.0.0.1:8000>. The edge, Cognito and
per-viewer scoping are not previewed: `/accounts` lists every account.
`/api/profile` answers from memory, checked by `tools/persona.py` like the
real one, so the account menu, `/profile` and My terms work: one persona for
everyone, signed in as `viewer@example.com`, forgotten when the server stops.

**The renderer directly.** [tools/tick_insights_report.py](../tools/tick_insights_report.py)
is the file the publisher Lambda runs (symlinked as `report.py`), and reads S3
or a local lake:

```bash
python3 tools/tick_insights_report.py local-data/lake --configs configs --out-dir site
python3 tools/tick_insights_report.py s3://<DataBucketName> --out-dir site
```

**Exactly what the publisher Lambda renders**, written to disk instead of S3:

```bash
cd infra/src/publisher
DATA_BUCKET=<DataBucketName> SITE_BUCKET=- LOCAL_OUTPUT_DIR=../../../site python3 app.py
```

**The collector against a live board**, writing to disk:

```bash
cd infra/src/collector
LOCAL_OUTPUT_DIR=../../../out python3 app.py ../../../configs/<slug>_config.json
```

Or the full SQS invocation, with `sam build` then
`sam local invoke CollectorFunction -e events/dunder-mifflin_sqs_event.json`
from `infra/`. The event carries the fictional `dunder-mifflin` config, whose
endpoint does not resolve: swap in a real config to exercise a fetch.
`sam local invoke` also attempts the S3 writes, so prefer `LOCAL_OUTPUT_DIR`.

**Checks on lake data:**

```bash
python3 tools/audit_diffs.py local-data/lake                 # no day's record lost a change
python3 tools/tech_report.py local-data/lake --company geha  # one company's tech-stack report
```

**Template changes:**

```bash
cd infra && sam validate --lint && sam build
```

**The operations CLI**: any changing command takes `--dry-run`, which prints
the AWS calls it would make against production without making them.

## Shipping a change

1. **Plan first.** For a feature: trace the data path it touches (config →
   dispatcher → collector → lake → publisher/renderer → edge/auth → page),
   weigh two or three approaches against the rules in CLAUDE.md, and agree a
   plan before writing code.
2. **Branch** off `main`: `git switch main && git pull && git switch -c feature/<short-name>`.
   Nothing is committed to `main` directly.
3. **Build and test locally**: the tests, plus the checks above that apply.
4. **Open a pull request** with `gh pr create`. Say what changed and why, and
   how to check it (`python3 tools/preview.py <PR#>` when pages change). Flag
   any change to `template.yaml`, the edge function, auth, the deploy pipeline
   or deploy order, and any replacement of a stateful resource (the user pool,
   a bucket, a table): the reviewer has to spot it in the change set.
5. **Merge to release.** A push to `main` that touches `infra/**`, the renderer
   or the workflow runs [deploy.yml](../.github/workflows/deploy.yml). Its plan
   job writes the CloudFormation change set to the run's summary, warning
   about anything removed or replaced. Approving the `production` environment
   runs the deploy; see
   [infra/README.md § Deploying a change](../infra/README.md#deploying-a-change).

Commit subjects are short, sentence case and imperative, with no prefix tags
("Archive closed postings, each at /<slug>/archive/<pid>"). Commits and pull
requests carry no AI attribution.

## Conventions

- **Code style.** Match the code around you. Each module opens with a docstring
  saying what it does and why; comments explain why, never what. Plain
  functions and the standard library over abstractions.
- **Lambdas use the standard library and boto3 only.** There are no
  per-function `requirements.txt` files; adding a dependency changes the build
  and needs a decision first.
- **One file, run two ways.** Edit the renderer in `tools/`, never through
  `infra/src/publisher/report.py`, which is a symlink; likewise
  `description.py`, which lives in `infra/src/collector/`.
- **Operate through `tools/tick.py`.** If a task is not covered, add it there,
  with a test, rather than documenting raw AWS CLI steps.
- **Keep the menu bar's copies in step.** `/demo` is the source of truth; see
  [architecture.md § One menu bar, several copies](architecture.md#one-menu-bar-several-copies).
