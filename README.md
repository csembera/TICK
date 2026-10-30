# TICK

Hiring-signal intelligence: TICK watches a company's job postings and turns them
into a weekly brief. Live at [tick.sembasolutions.com](https://tick.sembasolutions.com).

| Path | What |
|---|---|
| `infra/` | The `tick-collector` SAM stack: collectors, publisher, auth, the site and its static pages. Deploy and run instructions are in [infra/README.md](infra/README.md). |
| `tools/` | Local CLIs: `evaluator.py` writes a company's SourceConfig, `tick_insights_report.py` renders the dashboard and brief (the publisher Lambda runs this same file through a symlink), `tech_report.py` renders the tech-stack report. |
| `configs/` | Evaluated SourceConfigs, one per company. Upload to the data bucket's `configs/` prefix to start collecting. |
| `docs/` | Working journal, design doc, dry-run notes, UI mockups and the early prototypes. |

Collected data and rendered output live in the data bucket, not in git.
