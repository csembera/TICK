---
name: tick-brief
description: Test and tune the prompt for TICK's weekly AI account brief against local lake data, before it becomes a Strands agent. Use when asked to generate, test, compare, or improve a weekly brief, to iterate on the brief prompt, or to evaluate brief quality for a TICK company.
---

# TICK weekly brief — prompt workbench

The weekly brief will be written by a Strands agent every Sunday night from
one company's last 7 days of job-board movement. This skill is the bench for
getting its prompt right first. It runs the agent's exact pipeline, with a
subagent standing in for the model:

    evidence.py (code)  →  evidence.json  →  prompt.md + model  →  brief.json  →  check.py (code)

- `prompt.md` is **the** system prompt. The Strands agent will load this file
  verbatim, so it is the only thing being tuned. Every brief records its
  `prompt_version`.
- `scripts/evidence.py` is the agent's only input. Code counts; the model
  interprets.
- `scripts/check.py` re-verifies the model's citations and quotes in code
  and renders a markdown preview.

Run everything from the project root. The lake is a local backup, e.g.
`local-data/tick-data-backup-2026-10-30`; `s3://<bucket>` works too.

## One run

    L=local-data/tick-data-backup-2026-10-30
    S=.claude/skills/tick-brief/scripts
    OUT=local-data/briefs/<slug>/<week-end>          # week-end = window.end

1. **Evidence.**
   `python3 $S/evidence.py $L <slug> [--week-end YYYY-MM-DD] --out $OUT/evidence.json`
   It prints a one-line summary (window, opened/closed counts, flags, size).
   Don't read the whole pack into this conversation — the writer reads it.
   Default week-end is the latest crawl; for a Sunday-night run pass that
   Sunday.

2. **Write the brief in a clean context.** The test is only honest if the
   writer sees what the agent will see and nothing else — not this repo, not
   this conversation. Launch a `general-purpose` subagent with exactly:

   > Read `.claude/skills/tick-brief/prompt.md`; it is your system prompt
   > and you follow it exactly. Your only input is the evidence pack at
   > `$OUT/evidence.json`. Read no other file, use no web or other tools
   > beyond reading those two files and writing your output. Write the
   > JSON the prompt asks for — and nothing else — to `$OUT/brief.json`.

   For several companies, launch the subagents in parallel.

3. **Check.**
   `python3 $S/check.py $OUT/brief.json $OUT/evidence.json --md $OUT/brief.md`
   Never hand-fix a brief that fails. The failure is a finding about the
   prompt.

4. **Review** `brief.md` against the evidence, using the rubric below, and
   write the findings to `$OUT/review.md` (prompt version, scores, what to
   change). Read the cited postings in `evidence.json` to verify claims —
   check.py proves citations exist, not that they support the sentence.

5. **Report** to the user: the rendered brief, the check result, the rubric
   scores, and specific proposed edits to `prompt.md` (quote the lines).
   Edit `prompt.md` only once the user agrees, bump its `prompt-version`
   comment and the `"prompt_version"` in its schema, and regenerate the same
   weeks so versions compare like for like.

6. **Preview** in the real page design:
   `python3 $S/preview.py $L --serve 8765` → http://127.0.0.1:8765/login/
   Renders the whole site from the local lake into `local-data/preview/`,
   using each company's newest brief that passes check.py and the
   rule-based brief otherwise. Re-run it after regenerating a brief.

## Rubric (score 1–5 each)

| | What 5 looks like |
|---|---|
| **Lead** | Opens on the week's most consequential move, not on counts. |
| **Insight** | Says something the dashboard can't: clusters read as programmes, descriptions mined for systems, goals and reporting lines. |
| **Accuracy** | Every sentence is supported by the rows it cites; reposts aren't called growth; closures aren't called hires. |
| **Calibration** | Confidence matches evidence; a quiet week is called quiet. |
| **Actionability** | Each action item is something a seller could do this week, tied to this account's evidence. No generic plays. |
| **Concision** | No repeated arithmetic, no filler, inside the word limits. |

Also flag any outside knowledge (facts not in the pack) or named people —
both are hard-rule violations even if true.

Outputs under `local-data/briefs/` hold real company data and stay out of
git.
