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

A draft's description_format gets the same treatment: the gate samples fresh
descriptions, parses them with and without the format, records the coverage
and which rules and aliases actually matched, and drops boilerplate rules that
matched nothing. Descriptions are enrichment, not collection, so this informs
the reviewer and never moves the tier or the confidence.

    .venv/bin/python .claude/skills/tick-evaluator/scripts/verify.py \
        draft.json --out configs/dunder-mifflin_config.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from typing import Any, Literal, Optional

try:
    import httpx
    from pydantic import BaseModel, Field, ValidationError
except ImportError as e:  # pragma: no cover
    print(f"missing dependency ({e}) — run: .venv/bin/pip install httpx pydantic", file=sys.stderr)
    sys.exit(1)

from tick_common import (URL_TOKEN, collect_dayforce, collect_successfactors, collect_taleo,
                         collector_headers, dayforce, description_html, format_report,
                         locate_postings, merge_detail, prune_format, resolve_field, spread)

CONFIDENCE_REVIEW_THRESHOLD = 0.7

# Optional fields the collector tests for presence, not truthiness: a present
# postings_key of null means "the payload is the posting list". Emitting them
# as null on every config would change what the collector does, so they are
# written only when the draft set them.
PASSTHROUGH_IF_SET = ("postings_key", "timeout_s", "description_field", "description_format",
                      "portals", "synthetic")

ATS = Literal[
    "workday", "greenhouse", "lever", "ashby", "workable",
    "smartrecruiters", "adp", "dayforce", "taleo", "successfactors", "other",
]


class TaleoPortal(BaseModel):
    """One Taleo career section: the portal id its search takes, and the
    section code in its job page URLs (careersection/<section>/jobdetail.ftl)."""
    portal: str
    section: str


class SchemaMap(BaseModel):
    """Maps ATS-native field names → TICK curated schema fields."""
    posting_id: str
    title: str
    location: str
    url: str
    team: Optional[str] = None
    posted_on: Optional[str] = None


SectionKind = Literal["summary", "responsibilities", "qualifications", "preferred_qualifications",
                      "work_arrangement", "compensation", "benefits", "company_intro", "eeo"]


class BoilerplateRule(BaseModel):
    """A paragraph the board repeats on most postings. Match on a distinctive
    prefix or phrase copied from a real description, not a paraphrase."""
    kind: Literal["company_intro", "benefits", "eeo"]
    starts_with: Optional[str] = None
    contains: Optional[str] = None


class DescriptionFormat(BaseModel):
    """How this board lays out a description, for the collector's parser.

    aliases: headings this board uses that the generic patterns miss or misfile,
    per section kind, copied verbatim. boilerplate: the board's repeated intro,
    benefits and equal-opportunity paragraphs."""
    aliases: dict[SectionKind, list[str]] = Field(default_factory=dict)
    boilerplate: list[BoilerplateRule] = Field(default_factory=list)


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
    # read by the collector, so they must survive re-verification
    postings_key: Optional[str] = None
    timeout_s: Optional[float] = None
    description_field: Optional[str] = None
    portals: Optional[list[TaleoPortal]] = None
    schema_map: Optional[SchemaMap] = None
    description_format: Optional[DescriptionFormat] = None
    careers_url: Optional[str] = None
    identity_notes: Optional[str] = None
    date_trust: Literal["high", "low", "lowest"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    synthetic: Optional[bool] = None


class DescriptionCheck(BaseModel):
    """What sampling the board's descriptions showed. Written by code."""
    sampled: int = 0
    coverage_generic: Optional[float] = None
    coverage_with_format: Optional[float] = None
    role_sections: dict[str, float] = Field(default_factory=dict)
    pay_found: int = 0
    rule_hits: list[dict] = Field(default_factory=list)
    alias_hits: dict[str, dict[str, int]] = Field(default_factory=dict)
    unmatched_headings: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)


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
    descriptions: Optional[DescriptionCheck] = None

    @property
    def collects(self) -> bool:
        return self.parsed_json and self.postings_found > 0


class SourceConfig(SourceConfigDraft):
    """The evaluator's only output. Deterministic collector consumes this verbatim."""
    needs_human_review: bool
    verification: Verification
    evaluated_on: str


def verify_draft(draft: SourceConfigDraft, sample: int = 25, desc_sample: int = 12) -> Verification:
    """Re-execute the emitted config and report what it actually collects.

    This is the gate an earlier run needed: that config asserted a
    verified tier 1 at 0.88 confidence on an endpoint that had never once
    returned a posting.
    """
    v = Verification()
    if not draft.endpoint:
        v.findings.append("no endpoint in draft — nothing to verify")
        return v

    # exactly the collector's headers: the gate re-executes the config as it will run
    hdrs = collector_headers(draft.model_dump())
    v.ran = True
    postings = (_collect_taleo(draft, hdrs, v) if draft.ats == "taleo"
                else _collect_dayforce(draft, hdrs, v) if draft.ats == "dayforce"
                else _collect_successfactors(draft, hdrs, v) if draft.ats == "successfactors"
                else _collect_one(draft, hdrs, v))
    v.postings_found = len(postings)
    if not postings:
        return v

    sm = draft.schema_map
    if not sm:
        v.findings.append("no schema_map — collector cannot normalize these rows")
        return v

    rows = postings[:sample]
    if draft.ats == "adp" and draft.detail_endpoint:
        # the collector merges each posting's detail before normalizing; so must the gate
        with httpx.Client(headers=hdrs, follow_redirects=True, timeout=30) as c:
            def get_json(url, method="GET", body=None):
                r = c.request(method, url, json=body)
                r.raise_for_status()
                return r.json()
            merged = []
            for p in rows:
                merged.append(merge_detail(draft.model_dump(), p, get_json))
                time.sleep(0.1)
        rows = merged
        v.findings.append(f"merged the detail payload into {len(rows)} sampled postings, as the collector does")
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

    v.descriptions = verify_descriptions(draft, postings, hdrs, desc_sample)
    return v


def _collect_one(draft: SourceConfigDraft, hdrs: dict, v: Verification) -> list:
    """The posting list from one request to the endpoint, as the collector
    makes it for every ATS without a paging branch."""
    try:
        with httpx.Client(headers=hdrs, follow_redirects=True, timeout=30) as c:
            r = c.request(draft.http_method, draft.endpoint, json=draft.request_body)
    except Exception as e:
        v.findings.append(f"endpoint unreachable: {type(e).__name__}: {e}")
        return []

    v.status = r.status_code
    v.content_type = r.headers.get("content-type")
    if r.status_code != 200:
        v.findings.append(f"endpoint returned HTTP {r.status_code}, not 200")
        return []
    try:
        data = r.json()
    except Exception:
        v.findings.append(
            f"endpoint returned {v.content_type or 'non-JSON'}, not JSON — "
            "the collector calls json.loads on this and will raise")
        return []

    v.parsed_json = True
    postings, container = locate_postings(data)
    if not postings:
        v.findings.append("endpoint returned JSON but no posting list was found in it")
        return []
    v.findings.append(f"{len(postings)} postings under '{container}'")
    return postings


def _collect_taleo(draft: SourceConfigDraft, hdrs: dict, v: Verification) -> list:
    """Every page of every career section, through the collector's taleo.py.
    A failed page, or a section short of its own total, raises there and the
    collector's run fails; here it fails the gate."""
    cfg = draft.model_dump()
    if not cfg.get("portals"):
        v.findings.append("taleo config lists no portals — the collector would collect nothing")
        return []
    try:
        with httpx.Client(headers=hdrs, follow_redirects=True, timeout=30) as c:
            def get_json(url, method="GET", body=None):
                r = c.request(method, url, json=body)
                v.status, v.content_type = r.status_code, r.headers.get("content-type")
                r.raise_for_status()
                return r.json()
            postings, reported = collect_taleo(cfg, get_json)
    except Exception as e:
        v.findings.append(f"taleo collection failed: {type(e).__name__}: {e}")
        return []

    v.parsed_json = True
    if not postings:
        v.findings.append("every career section returned an empty posting list")
        return []
    v.findings.append(f"{len(postings)} postings across {len(cfg['portals'])} career sections "
                      f"(the sections reported {reported})")
    return postings


def _collect_dayforce(draft: SourceConfigDraft, hdrs: dict, v: Verification) -> list:
    """The job feed grouped into requisitions by the collector's dayforce.py:
    the gate checks schema_map against the postings the collector will write,
    not the feed's one-row-per-location list."""
    try:
        with httpx.Client(headers=hdrs, follow_redirects=True, timeout=30) as c:
            def get_json(url, method="GET", body=None):
                r = c.request(method, url, json=body)
                v.status, v.content_type = r.status_code, r.headers.get("content-type")
                r.raise_for_status()
                return r.json()
            postings, _ = collect_dayforce(draft.model_dump(), get_json)
    except Exception as e:
        v.findings.append(f"dayforce job feed failed: {type(e).__name__}: {e}")
        return []

    v.parsed_json = True
    if not postings:
        v.findings.append("the job feed returned an empty list")
        return []
    rows = sum(len(p[dayforce.ROWS_KEY]) for p in postings)
    v.findings.append(f"{len(postings)} requisitions, grouped from {rows} feed rows")
    return postings


def _collect_successfactors(draft: SourceConfigDraft, hdrs: dict, v: Verification) -> list:
    """The job feed read by the collector's successfactors.py. The feed is
    XML, so parsed_json here records that the collector parsed it, as for the
    other feed readers."""
    try:
        with httpx.Client(headers=hdrs, follow_redirects=True, timeout=30) as c:
            def get_text(url):
                r = c.get(url)
                v.status, v.content_type = r.status_code, r.headers.get("content-type")
                r.raise_for_status()
                return r.text
            postings, _ = collect_successfactors(draft.model_dump(), get_text)
    except Exception as e:
        v.findings.append(f"successfactors job feed failed: {type(e).__name__}: {e}")
        return []

    v.parsed_json = True
    if not postings:
        v.findings.append("the job feed listed no postings")
        return []
    v.findings.append(f"{len(postings)} postings in the job feed")
    return postings


def verify_descriptions(draft: SourceConfigDraft, postings: list, hdrs: dict, n: int) -> DescriptionCheck:
    """Sample descriptions as the collector fetches them and check the format.

    Prunes, in place, boilerplate rules that matched no sampled posting."""
    d = DescriptionCheck()
    cfg = draft.model_dump()
    htmls, errors = [], 0
    with httpx.Client(headers=hdrs, follow_redirects=True, timeout=30) as c:
        def get_json(url, method="GET", body=None):
            r = c.request(method, url, json=body)
            r.raise_for_status()
            return r.json()

        def get_text(url):
            r = c.get(url)
            r.raise_for_status()
            return r.text
        for posting in spread(postings, n):
            try:
                h = description_html(cfg, posting, get_json, get_text)
            except Exception:
                errors += 1
                continue
            if h:
                htmls.append(h)
            time.sleep(0.2)
    fmt = draft.description_format.model_dump(exclude_none=True) if draft.description_format else None
    rep = format_report(htmls, fmt)
    d.sampled = rep["sampled"]
    if not htmls:
        d.findings.append("no descriptions could be sampled — the collector will store none for this board"
                          + (f" ({errors} detail fetches failed)" if errors else ""))
        return d
    d.coverage_generic = rep["coverage_generic"]
    d.coverage_with_format = rep.get("coverage_with_format")
    d.role_sections = rep["role_sections"]
    d.pay_found = rep["pay_found"]
    d.rule_hits = rep.get("rule_hits", [])
    d.alias_hits = rep.get("alias_hits", {})
    d.unmatched_headings = [h["text"] for h in rep["headings"]
                            if (h.get("with_format", h["generic"])) is None and h["postings"] >= 2][:10]
    d.findings = rep["findings"]
    if not fmt:
        d.findings.append("no description_format: the collector falls back to generic headings")
    elif any(r["postings"] == 0 for r in d.rule_hits):
        draft.description_format = DescriptionFormat.model_validate(prune_format(fmt, rep))
    return d


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
    p.add_argument("--desc-sample", type=int, default=12,
                   help="descriptions to sample when checking the description_format")
    args = p.parse_args()

    raw = sys.stdin.read() if args.draft == "-" else open(args.draft).read()
    try:
        draft = SourceConfigDraft.model_validate_json(raw)
    except ValidationError as e:
        print("draft failed schema validation — fix these fields and re-run:\n", file=sys.stderr)
        print(e, file=sys.stderr)
        sys.exit(1)

    cfg = finalize(draft, verify_draft(draft, sample=args.sample, desc_sample=args.desc_sample))
    data = json.loads(cfg.model_dump_json())
    for k in PASSTHROUGH_IF_SET:
        if k not in draft.model_fields_set:
            data.pop(k, None)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
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
    if v.descriptions:
        d = v.descriptions
        print(f"descriptions: sampled={d.sampled} coverage generic={d.coverage_generic} "
              f"with_format={d.coverage_with_format} role_sections={d.role_sections}", file=sys.stderr)
        for f in d.findings:
            print(f"  - {f}", file=sys.stderr)
    if cfg.needs_human_review:
        print(f"⚠ confidence {cfg.confidence:.2f} — flagged for human review", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
