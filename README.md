# TICK

Hiring-signal intelligence: TICK watches a company's job postings and turns them
into a weekly brief. Live at [tick.sembasolutions.com](https://tick.sembasolutions.com).

| Path | What |
|---|---|
| `infra/` | The `tick-collector` SAM stack: collectors, publisher, auth, the site and its static pages. Deploy and run instructions are in [infra/README.md](infra/README.md). |
| `tools/` | Local CLIs: `evaluator.py` writes a company's SourceConfig, `tick_insights_report.py` renders the dashboards and the published AI briefs (the publisher Lambda runs this same file through a symlink), `tech_report.py` renders the tech-stack report, `preview.py` serves any branch or PR on localhost. |
| `configs/` | Not in git. Evaluated SourceConfigs live in the data bucket's `configs/` prefix; `aws s3 sync s3://<DataBucketName>/configs/ configs/` pulls a local copy. |
| [design-doc.md](design-doc.md) | The original system design: purpose, architecture and operating model. |

Collected data and rendered output live in the data bucket, not in git.
