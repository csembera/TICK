# TICK

Hiring-signal intelligence. TICK watches a company's job board every night,
keeps every crawl, and turns the movement into a dashboard and a weekly brief
for the people who sell to that company. Live at
[tick.sembasolutions.com](https://tick.sembasolutions.com); the public
[demo](https://tick.sembasolutions.com/demo) runs on a fictional company.

Job postings leak strategy months ahead of announcements: which teams are
growing, what is being built, what systems a company runs. One snapshot shows
what a company looks like today; a time series shows where it is going. TICK
is built around keeping that history.

## How it works

```
evaluator ──► SourceConfig ──► nightly crawl ──► S3 lake ──► publisher ──► site
(once per      (verified,       (03:00 UTC,      (raw,        (03:30 UTC,    (dashboard and
 company)       in the bucket)   one company      curated,     static HTML)   brief per account,
                                 per Lambda)      derived)                    behind sign-in)
                                                     ▲
                              weekly brief: evidence ─► model ─► citation check ─► published
```

- **Once per company**, a model finds the company's job board, and code
  verifies the endpoint and learns how its descriptions are laid out. The
  result is a SourceConfig.
- **Every night**, a deterministic collector executes each config: fetch,
  normalize, file each description into sections, match a tech-stack taxonomy,
  and diff against everything seen before. A board that changes fails loudly
  instead of collecting bad data.
- **Every night**, the publisher renders the whole lake into static pages
  behind Cognito sign-in. Each viewer sees only the accounts they hold.
- **Every week**, a model writes each account's brief from an evidence pack
  built by code, and code checks every citation and quote before it is
  published.

The full picture is in [docs/architecture.md](docs/architecture.md).

## Quick start

```bash
python3 -m unittest discover tests     # the test suite: stdlib only, no network
python3 tools/preview.py --refresh     # sync the lake and serve the whole site on localhost
tools/tick.py status                   # the live system at a glance (needs an AWS session)
```

## Documentation

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | How TICK works: collection, the lake, publishing, briefs, sign-in and the edge |
| [docs/operations.md](docs/operations.md) | The runbook: every routine task and every failure, through `tools/tick.py` |
| [docs/data-reference.md](docs/data-reference.md) | Every file and table TICK writes, field by field |
| [docs/development.md](docs/development.md) | Setup, running the pieces locally, and shipping a change |
| [infra/README.md](infra/README.md) | The AWS stack: resources, first-time setup, deploys, parameters |
| [CLAUDE.md](CLAUDE.md) | The rules every change keeps, and how a feature gets built |
| [design-doc.md](design-doc.md) | The original design TICK grew from |

## Layout

| Path | What |
|---|---|
| `infra/` | The `tick-collector` SAM stack: dispatcher, collector, publisher, auth and join Lambdas, the site and its hand-authored public pages (`infra/site-static/`) |
| `tools/tick.py` | The operations CLI: status, publishing, crawls, companies, users, requests, failed crawls, logs. `tools/tick.py --help` |
| `tools/tick_insights_report.py` | The renderer, run by the CLI and, through a symlink, by the publisher Lambda |
| `tools/preview.py` | Serve any branch, commit or PR as a site on localhost |
| `tools/audit_diffs.py`, `tools/tech_report.py` | Check the change records; render a company's tech-stack report |
| `tools/evaluator.py` | The evaluator as a standalone Strands agent |
| `.claude/skills/` | `tick-evaluator` (onboard a company) and `tick-brief` (write, check and publish the weekly briefs), run in Claude Code |
| `tests/` | Unit tests on synthetic fixtures |
| `configs/` | Not in git: local copies of the SourceConfigs (`tools/tick.py companies pull`) |

Collected data, configs and rendered pages live in the data bucket, never in
git: the repo is public.
