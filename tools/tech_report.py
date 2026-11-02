"""
TICK tech-stack report — renders a derived tech_stack.json into a readable
account-planning summary, with every claim linked to the postings it came from.

Usage:
    python3 tech_report.py <path-to-tech_stack.json>
    python3 tech_report.py <lake-root-dir> [--company SLUG]   # picks latest dt
    python3 tech_report.py s3://bucket [--company SLUG]       # same, from S3

Pure stdlib for local files; boto3 (runtime-provided / in .venv) only when
reading from S3.
"""

import argparse
import json
import os
import sys

_CAT_LABELS = {
    "cloud_aws": "AWS",
    "cloud_azure": "Azure",
    "cloud_gcp": "Google Cloud",
    "data_platform": "Data platform",
    "infra_devops": "Infrastructure / DevOps",
    "languages": "Languages & frameworks",
    "analytics_tools": "Analytics tools",
    "signal_terms": "Signal terms",
}
_CLOUD_CATS = ("cloud_aws", "cloud_azure", "cloud_gcp")


# ------------------------------------------------------------------ loading

def _latest_under(paths: list[str], company: str | None) -> str:
    """Pick the lexicographically-latest dt= partition (dates sort correctly)."""
    candidates = [p for p in paths if p.endswith("tech_stack.json")
                  and (company is None or f"company={company}/" in p)]
    if not candidates:
        sys.exit("no tech_stack.json found" + (f" for company={company}" if company else ""))
    return max(candidates)  # company=X/dt=YYYY-MM-DD/ sorts by date

def load_summary(source: str, company: str | None) -> dict:
    if source.startswith("s3://"):
        import boto3
        bucket, _, prefix = source[5:].partition("/")
        s3 = boto3.client("s3")
        keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(
                    Bucket=bucket, Prefix=f"{prefix.rstrip('/')}/derived/tech_stack/".lstrip("/"))
                for o in page.get("Contents", [])]
        key = _latest_under(keys, company)
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())

    if os.path.isdir(source):
        paths = [os.path.join(dp, f) for dp, _, files in os.walk(source) for f in files]
        source = _latest_under(paths, company)
    with open(source) as f:
        return json.load(f)


# ---------------------------------------------------------------- rendering

def _job(postings: dict, pid: str) -> str:
    return f"{pid} ({postings.get(pid, {}).get('title', '?')})"

def render(s: dict) -> str:
    stack, postings = s["stack"], s["postings"]
    out = []
    out.append(f"# {s['company']} — Tech Stack Summary")
    out.append(f"*Derived from {s['postings_total']} live postings captured "
               f"{s['captured_dt']} · {len(s['tech_role_ids'])} tech roles · "
               f"{len(postings)} postings with stack signals*\n")

    # Cloud posture: unique postings per vendor, ranked
    vendor_pids = {cat: {pid for pids in stack.get(cat, {}).values() for pid in pids}
                   for cat in _CLOUD_CATS}
    ranked = sorted(((cat, pids) for cat, pids in vendor_pids.items() if pids),
                    key=lambda kv: -len(kv[1]))
    out.append("## Cloud posture")
    if ranked:
        out.append("| Vendor | Postings mentioning | Services/terms seen |")
        out.append("|---|---|---|")
        for cat, pids in ranked:
            terms = ", ".join(sorted(stack[cat], key=lambda t: -len(stack[cat][t])))
            out.append(f"| {_CAT_LABELS[cat]} | {len(pids)} | {terms} |")
        leader, runner = ranked[0], (ranked[1] if len(ranked) > 1 else None)
        posture = f"**Read:** {_CAT_LABELS[leader[0]]}-leaning ({len(leader[1])} postings"
        posture += f" vs {len(runner[1])} {_CAT_LABELS[runner[0]]})." if runner else ")."
        out.append(f"\n{posture}")
    else:
        out.append("*No cloud vendor mentions found.*")
    out.append("")

    # Stack by category, terms ranked by posting count, each with evidence
    out.append("## Stack detail (evidence-linked)")
    for cat in _CAT_LABELS:
        terms = stack.get(cat)
        if not terms:
            continue
        cat_pids = {pid for pids in terms.values() for pid in pids}
        out.append(f"\n### {_CAT_LABELS[cat]} — {len(cat_pids)} postings")
        for term, pids in sorted(terms.items(), key=lambda kv: -len(kv[1])):
            shown = ", ".join(_job(postings, p) for p in pids[:3])
            more = f", +{len(pids) - 3} more" if len(pids) > 3 else ""
            out.append(f"- **{term}** — {len(pids)}: {shown}{more}")

    # Evidence appendix: every tech role with its full term set and URL
    out.append("\n## Tech-role evidence")
    for pid in s["tech_role_ids"]:
        p = postings.get(pid)
        if not p:
            continue
        out.append(f"\n**{pid} — {p['title']}** · {p['location']}")
        if p.get("url"):
            out.append(f"<{p['url']}>")
    out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Render a TICK tech-stack summary")
    ap.add_argument("source", help="tech_stack.json path, lake root dir, or s3://bucket[/prefix]")
    ap.add_argument("--company", default=None, help="company_slug filter when source is a dir/bucket")
    args = ap.parse_args()
    print(render(load_summary(args.source, args.company)))
