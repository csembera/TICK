#!/usr/bin/env python3
"""
TICK — Verification gate (phase C).

Takes the SourceConfigDraft the agent wrote, RE-EXECUTES its endpoint here in
code, and overrides the tier and confidence the draft claims with what actually
came back. A config that never returned a posting cannot be emitted as a
verified tier 1 no matter what the agent concluded — that failure is why this
phase exists.

Nothing the agent says about its own endpoint is taken on trust. The agent does
not write the final artifact; this script does.

    .venv/bin/python .claude/skills/tick-evaluator/scripts/verify.py \
        draft.json --out configs/dunder-mifflin_config.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any, Literal, Optional

try:
    import httpx
    from pydantic import BaseModel, Field, ValidationError
except ImportError as e:  # pragma: no cover
    print(f"missing dependency ({e}) — run: .venv/bin/pip install httpx pydantic", file=sys.stderr)
    sys.exit(1)

from tick_common import BROWSER_HEADERS, URL_TOKEN, locate_postings, resolve_field

CONFIDENCE_REVIEW_THRESHOLD = 0.7

ATS = Literal[
    "workday", "greenhouse", "lever", "ashby", "workable",
    "smartrecruiters", "adp", "dayforce", "other",
]


class SchemaMap(BaseModel):
    """Maps ATS-native field names → TICK curated schema fields."""
    posting_id: str
    title: str
    location: str
    url: str
    team: Optional[str] = None
    posted_on: Optional[str] = None


class SourceConfigDraft(BaseModel):
    """What the agent emits. Provenance and rubric fields are NOT in here —
    they are stamped below so the agent cannot assert its own verification."""
    company_name: str
    company_slug: str
    tier: Literal[1, 2, 3]
    ats: Optional[ATS] = None
    tenant: Optional[str] = None
    endpoint: Optional[str] = None
    http_method: Literal["GET", "POST"] = "GET"
    request_body: Optional[dict] = None
    request_headers: Optional[dict] = None
    detail_endpoint: Optional[str] = None
    pagination: Optional[str] = None
    schema_map: Optional[SchemaMap] = None
    careers_url: Optional[str] = None
    identity_notes: Optional[str] = None
    date_trust: Literal["high", "low", "lowest"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class Verification(BaseModel):
    """What this script actually observed. Written by code, never by the agent."""
    ran: bool = False
    status: Optional[int] = None
    content_type: Optional[str] = None
    parsed_json: bool = False
    postings_found: int = 0
    field_coverage: dict[str, float] = Field(default_factory=dict)
    posting_id_unique: Optional[bool] = None
    findings: list[str] = Field(default_factory=list)

    @property
    def collects(self) -> bool:
        return self.parsed_json and self.postings_found > 0


class SourceConfig(SourceConfigDraft):
    """The evaluator's only output. Deterministic collector consumes this verbatim."""
    needs_human_review: bool
    verification: Verification
    evaluated_on: str


def verify_draft(draft: SourceConfigDraft, sample: int = 25) -> Verification:
    """Re-execute the emitted config and report what it actually collects.

    This is the gate an earlier run needed: that config asserted a
    verified tier 1 at 0.88 confidence on an endpoint that had never once
    returned a posting.
    """
    v = Verification()
    if not draft.endpoint:
        v.findings.append("no endpoint in draft — nothing to verify")
        return v

    hdrs = {**BROWSER_HEADERS, "Content-Type": "application/json",
            **(draft.request_headers or {})}
    v.ran = True
    try:
        with httpx.Client(headers=hdrs, follow_redirects=True, timeout=30) as c:
            r = c.request(draft.http_method, draft.endpoint, json=draft.request_body)
    except Exception as e:
        v.findings.append(f"endpoint unreachable: {type(e).__name__}: {e}")
        return v

    v.status = r.status_code
    v.content_type = r.headers.get("content-type")
    if r.status_code != 200:
        v.findings.append(f"endpoint returned HTTP {r.status_code}, not 200")
        return v
    try:
        data = r.json()
    except Exception:
        v.findings.append(
            f"endpoint returned {v.content_type or 'non-JSON'}, not JSON — "
            "the collector calls json.loads on this and will raise")
        return v

    v.parsed_json = True
    postings, container = locate_postings(data)
    v.postings_found = len(postings)
    if not postings:
        v.findings.append("endpoint returned JSON but no posting list was found in it")
        return v
    v.findings.append(f"{len(postings)} postings under '{container}'")

    sm = draft.schema_map
    if not sm:
        v.findings.append("no schema_map — collector cannot normalize these rows")
        return v

    rows = postings[:sample]
    for name in ("posting_id", "title", "location", "team", "posted_on"):
        expr = getattr(sm, name)
        if not expr:
            continue
        resolved = [resolve_field(p, expr) for p in rows]
        hit = [x for x in resolved if x not in (None, "")]
        cov = len(hit) / len(rows)
        v.field_coverage[name] = round(cov, 3)
        if cov < 0.9:
            v.findings.append(
                f"schema_map.{name}='{expr}' resolves on only {cov:.0%} of sampled "
                "postings (detail-only field, or wrong path)")

        # A description blob mapped into `location` is silent, permanent corruption.
        if name == "location" and hit:
            longest = max(len(str(x)) for x in hit)
            if longest > 120:
                v.findings.append(
                    f"schema_map.location resolves to values up to {longest} chars — "
                    "this looks like a description, not a location")

        if name == "posting_id":
            ids = [str(x) for x in hit]
            v.posting_id_unique = len(set(ids)) == len(ids)
            if not v.posting_id_unique:
                v.findings.append("posting_id is not unique across sampled postings")

    # Many ids collapsing onto one title+location is the revision-id signature:
    # Dayforce mints a new jobPostingId on every save, so one opening reports as N.
    if sm.posting_id and sm.title:
        keys = [(resolve_field(p, sm.title), resolve_field(p, sm.location)) for p in rows]
        distinct = len(set(keys))
        if len(rows) >= 6 and distinct <= len(rows) / 2:
            v.findings.append(
                f"{len(rows)} postings collapse to {distinct} distinct title+location "
                f"pairs — posting_id='{sm.posting_id}' may be a revision id rather than "
                "a stable requisition id; every edit would diff as new+closed")

    if sm.url and "{" in sm.url:
        built = URL_TOKEN.sub(lambda m: str(resolve_field(rows[0], m.group(1)) or ""), sm.url)
        if "{" in built or built.endswith("="):
            v.findings.append(f"schema_map.url template did not fully resolve: {built!r}")
    return v


def finalize(draft: SourceConfigDraft, v: Verification) -> SourceConfig:
    """Stamp provenance and enforce the rubric in code.

    The agent proposes a tier and a confidence; what survives is what the
    verification supports.
    """
    confidence, tier, date_trust = draft.confidence, draft.tier, draft.date_trust

    if tier == 1 and not v.collects:
        # Tier 1 means "direct ATS JSON, verified live". Unverified, it is a lead.
        tier = 3
        date_trust = "lowest"
        confidence = min(confidence, 0.4)
        v.findings.append(
            "tier 1 claimed but the endpoint returned no postings — downgraded to "
            "tier 3 and flagged; re-run once the endpoint is actually collecting")
    elif not v.collects and v.ran:
        confidence = min(confidence, 0.5)

    # Any coverage gap or corruption finding caps confidence below the review bar.
    if any("schema_map" in f for f in v.findings):
        confidence = min(confidence, 0.6)

    return SourceConfig(
        **draft.model_dump(exclude={"tier", "confidence", "date_trust"}),
        tier=tier,
        confidence=round(confidence, 2),
        date_trust=date_trust,
        needs_human_review=confidence < CONFIDENCE_REVIEW_THRESHOLD or not v.collects,
        verification=v,
        evaluated_on=date.today().isoformat(),
    )


def main() -> None:
    p = argparse.ArgumentParser(description="TICK verification gate — draft JSON → verified SourceConfig")
    p.add_argument("draft", nargs="?", default="-", help="path to the draft JSON, or '-' for stdin")
    p.add_argument("--out", default=None, help="write the verified config here (default: stdout)")
    p.add_argument("--sample", type=int, default=25, help="postings to sample for coverage")
    args = p.parse_args()

    raw = sys.stdin.read() if args.draft == "-" else open(args.draft).read()
    try:
        draft = SourceConfigDraft.model_validate_json(raw)
    except ValidationError as e:
        print("draft failed schema validation — fix these fields and re-run:\n", file=sys.stderr)
        print(e, file=sys.stderr)
        sys.exit(1)

    cfg = finalize(draft, verify_draft(draft, sample=args.sample))
    payload = cfg.model_dump_json(indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(payload)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(payload)

    v = cfg.verification
    print(f"verification: tier={cfg.tier} collects={v.collects} "
          f"postings={v.postings_found} coverage={v.field_coverage}", file=sys.stderr)
    for f in v.findings:
        print(f"  - {f}", file=sys.stderr)
    if cfg.needs_human_review:
        print(f"⚠ confidence {cfg.confidence:.2f} — flagged for human review", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
