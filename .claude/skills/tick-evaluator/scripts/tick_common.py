"""
TICK — shared deterministic helpers.

Imported by BOTH tick_tools.py (observation) and verify.py (the gate). The
posting locator and the field resolver live here for one reason: the gate must
resolve fields exactly the way the collector does, and the probe must locate
postings exactly the way the gate does. Two copies would drift, and the drift
would show up as a config that verifies clean and then collects nothing.

The same reasoning covers descriptions: the sampler fetches a posting's
description the way the collector's enrich() does, and the format report
parses it with the collector's own description.py, imported from
infra/src/collector rather than copied, so a description_format is checked
exactly as the collector will apply it.
"""

from __future__ import annotations

import os
import re
import sys
from collections import Counter
from typing import Any, Callable, Optional

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "infra", "src", "collector")))
import description  # noqa: E402  the collector's parser, not a copy

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# A browser header set clears most bot blocks; a bare client does not.
BROWSER_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

FINGERPRINTS: list[tuple[str, str]] = [
    ("workday",        r"([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com(?:/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+))?"),
    ("greenhouse",     r"greenhouse\.io/embed/job_board(?:/js)?\?for=([a-z0-9\-_]+)|boards\.greenhouse\.io/(?!embed)([a-z0-9\-_]+)|grnhse\.io"),
    ("lever",          r"jobs\.lever\.co/([A-Za-z0-9\-_]+)"),
    ("ashby",          r"jobs\.ashbyhq\.com/([A-Za-z0-9\-_%]+)"),
    ("workable",       r"apply\.workable\.com/([a-z0-9\-_]+)"),
    ("smartrecruiters", r"careers\.smartrecruiters\.com/([A-Za-z0-9]+)"),
    ("adp",            r"myjobs\.adp\.com/([A-Za-z0-9\-_]+)"),
    ("dayforce",       r"jobs\.dayforcehcm\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9\-_]+)/([A-Za-z0-9\-_]+)"),
]

# Default sweep for an SPA's API contract: quoted /api/ paths, versioned routes,
# job/requisition literals, and absolute URLs (incl. template-literal segments).
BUNDLE_PATTERNS: list[str] = [
    r"[\"'`][^\"'`\n]{0,120}/api/[^\"'`\n]{0,120}[\"'`]",
    r"[\"'`][^\"'`\n]{0,120}/v\d+/[^\"'`\n]{0,120}[\"'`]",
    r"[\"'`][^\"'`\n]{0,100}(?:job|requisition|posting|career|staffing)[^\"'`\n]{0,100}[\"'`]",
    r"https?://[a-zA-Z0-9.\-]+/[A-Za-z0-9/_.\-{}$]{0,120}",
]

def collector_headers(cfg: dict) -> dict:
    """The headers the collector sends — keep in step with _http_json in
    infra/src/collector/app.py. Re-executing a config must send exactly these,
    not the browser set: some boards answer a browser's Accept-Language with an
    empty posting list, so a gate using browser headers would fail a config the
    collector runs fine (and the reverse). Discovery tools keep the browser set;
    a config that needs it carries it in request_headers."""
    return {"Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (tick-collector)", **(cfg.get("request_headers") or {})}


# Every container key a supported ATS puts its posting list under.
POSTING_KEYS = ("jobPostings", "jobRequisitions", "jobs", "data", "results",
                "postings", "items")

INDEXED = re.compile(r"^(\w+)\[(\d+)\]$")
URL_TOKEN = re.compile(r"\{(\w+(?:\[\d+\])?(?:\.\w+(?:\[\d+\])?)*)\}")


def resolve_field(posting: dict, expr: Optional[str]):
    """Resolve a schema_map expression. Mirrors the collector's resolver exactly —
    if it cannot resolve here, the collector will not resolve it either."""
    if not expr:
        return None
    cur: Any = posting
    for part in expr.split("."):
        m = INDEXED.match(part)
        if m:
            seq = (cur or {}).get(m.group(1)) if isinstance(cur, dict) else None
            idx = int(m.group(2))
            cur = seq[idx] if isinstance(seq, list) and len(seq) > idx else None
        else:
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur


def locate_postings(data: Any) -> tuple[list, Optional[str]]:
    """Find the posting list in a response body.

    A container that exists but is empty is NOT the same as a body with no
    container: the first means the endpoint works and the board is empty, the
    second means the config is pointed at the wrong shape. Returning the
    container name in the empty case is what lets the caller tell them apart.
    """
    if isinstance(data, list):
        return data, "<root list>"
    if not isinstance(data, dict):
        return [], None
    for k in POSTING_KEYS:
        v = data.get(k)
        if isinstance(v, list) and (not v or isinstance(v[0], dict)):
            return v, k
    for k, v in data.items():  # fall back to any list-of-dicts
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v, k
    return [], None


# ------------------------------------------------------------ descriptions

GetJson = Callable[..., Any]   # (url, method="GET", body=None) -> parsed JSON


def description_html(cfg: dict, posting: dict, get_json: GetJson) -> Optional[str]:
    """A posting's description HTML, fetched the way the collector's enrich() does."""
    if cfg.get("description_field"):
        h = resolve_field(posting, cfg["description_field"])
        return h if isinstance(h, str) else None
    if cfg.get("ats") == "workday":
        base, path = cfg.get("endpoint") or "", posting.get("externalPath")
        if not path or not base.endswith("/jobs"):
            return None
        return (get_json(base[: -len("/jobs")] + path).get("jobPostingInfo") or {}).get("jobDescription")
    if cfg.get("ats") == "adp":
        if posting.get("jobDescription"):
            return posting["jobDescription"]
        rid = resolve_field(posting, ((cfg.get("schema_map") or {}).get("posting_id")) or "reqId")
        if cfg.get("detail_endpoint") and rid:
            row = (get_json(cfg["detail_endpoint"].replace("{posting_id}", str(rid)))
                   .get("jobRequisitions") or [None])[0]
            return (row or {}).get("jobDescription")
    return None


def merge_detail(cfg: dict, posting: dict, get_json: GetJson) -> dict:
    """posting with its per-posting detail merged in, as the collector's
    fetch_all does for ADP before normalize() sees the row: team and
    posted_on live only in the detail payload there, so checking schema_map
    against the bare listing would report them missing on a config that
    collects them every night."""
    if cfg.get("ats") != "adp" or not cfg.get("detail_endpoint"):
        return posting
    rid = resolve_field(posting, ((cfg.get("schema_map") or {}).get("posting_id")) or "reqId")
    if not rid:
        return posting
    try:
        row = (get_json(cfg["detail_endpoint"].replace("{posting_id}", str(rid)))
               .get("jobRequisitions") or [None])[0]
    except Exception:
        return posting   # the collector skips a failed detail the same way
    return {**posting, **row} if isinstance(row, dict) else posting


def spread(items: list, n: int) -> list:
    """n items evenly spaced through the list, so a sample spans teams, not one."""
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def _repeated_blocks(htmls: list[str], min_share: float = 0.4) -> list[dict]:
    """Paragraphs most postings share: the board's boilerplate candidates."""
    seen: Counter = Counter()
    example, where = {}, {}
    for h in htmls:
        bl = [b for b in description.blocks(h) if not description.is_heading(*b) and len(b[0]) >= 40]
        keys = {}
        for i, (text, _, _) in enumerate(bl):
            key = " ".join(description.normalize(text).split()[:12])
            keys.setdefault(key, "first" if i == 0 else "last" if i == len(bl) - 1 else "middle")
            example.setdefault(key, text)
        for key, pos in keys.items():
            seen[key] += 1
            where.setdefault(key, Counter())[pos] += 1
    floor = max(2, int(min_share * len(htmls) + 0.999))
    return [{"text": example[k][:200], "postings": c, "position": where[k].most_common(1)[0][0],
             "generic_kind": description.boilerplate_kind(example[k], None)}
            for k, c in seen.most_common() if c >= floor]


def _heading_candidates(htmls: list[str], fmt: Optional[dict], min_share: float = 0.4) -> list[dict]:
    """Short plain lines most postings repeat that the parser does not treat
    as headings: a board's template headings set as ordinary paragraphs
    ("Job Summary", "Work Location"). Alias the ones that head content."""
    seen: Counter = Counter()
    label = {}
    for h in htmls:
        found = {}
        for text, bold, li in description.blocks(h):
            words = len(text.split())
            if (not li and 1 <= words <= 6 and not text.rstrip().endswith((".", "!", "?"))
                    and not text.startswith("#") and not description.heading_in(text, bold, li, fmt)):
                found.setdefault(description.normalize(text.rstrip(":")), text.rstrip(":").strip())
        for key, text in found.items():
            seen[key] += 1
            label.setdefault(key, text)
    floor = max(2, int(min_share * len(htmls) + 0.999))
    return [{"text": label[k], "postings": c, "generic": description.classify_heading(label[k])}
            for k, c in seen.most_common() if c >= floor]


def format_report(htmls: list[str], fmt: Optional[dict] = None) -> dict:
    """What a board's descriptions look like, and what a description_format does to them.

    The evaluator reads this to write the format; the gate re-runs it to check
    the format against fresh samples. Pure: no network, no clock.
    """
    n = len(htmls)
    if not n:
        return {"sampled": 0, "findings": ["no descriptions could be sampled"]}
    generic = [description.parse(h) for h in htmls]
    learned = [description.parse(h, fmt) for h in htmls] if fmt else generic

    heads: Counter = Counter()
    label = {}
    for h in htmls:
        found = {}
        for text, bold, li in description.blocks(h):
            if description.heading_in(text, bold, li, fmt):
                found.setdefault(description.normalize(text.rstrip(":")), text.rstrip(":").strip())
        for key, text in found.items():
            heads[key] += 1
            label.setdefault(key, text)
    headings = [{"text": label[k], "postings": c,
                 "generic": description.classify_heading(label[k]),
                 **({"with_format": description.classify_heading(label[k], fmt)} if fmt else {})}
                for k, c in heads.most_common()]

    def share(parsed, kind):
        return round(sum(1 for p in parsed if p and p["sections"].get(kind)) / n, 2)

    pays = [p["compensation"]["text"] for p in generic if p and p["compensation"]]
    out = {
        "sampled": n,
        "coverage_generic": round(sum(description.coverage(p) for p in generic) / n, 3),
        "headings": headings,
        "repeated_blocks": _repeated_blocks(htmls),
        "heading_candidates": _heading_candidates(htmls, fmt),
        "pay_found": len(pays),
        "pay_examples": pays[:3],
        "role_sections": {k: share(learned, k) for k in ("summary", "responsibilities", "qualifications")},
        "boilerplate_found": dict(Counter(k for p in learned if p for k in p["boilerplate"])),
        "findings": [],
    }
    if not fmt:
        return out

    out["coverage_with_format"] = round(sum(description.coverage(p) for p in learned) / n, 3)
    rule_hits = []
    for rule in fmt.get("boilerplate") or []:
        one = {"boilerplate": [rule]}
        hits = sum(1 for h in htmls if any(
            description.boilerplate_kind(t, one) == rule.get("kind") and not li
            for t, _, li in description.blocks(h)))
        rule_hits.append({**rule, "postings": hits})
        if hits == 0:
            out["findings"].append(f"boilerplate rule {rule} matched no sampled posting — dropped")
        elif hits < 0.3 * n:
            out["findings"].append(
                f"boilerplate rule {rule} matched only {hits}/{n} postings — boilerplate repeats on "
                "most postings, so this is likely role text")
    alias_hits = {kind: {a: heads.get(description.normalize(a), 0) for a in aliases}
                  for kind, aliases in (fmt.get("aliases") or {}).items()}
    for kind, hits in alias_hits.items():
        for a, c in hits.items():
            if c == 0:
                out["findings"].append(f"alias {a!r} -> {kind} heads no sampled posting (kept: it may "
                                       "still appear on postings outside the sample)")
    out["rule_hits"], out["alias_hits"] = rule_hits, alias_hits
    if out["coverage_with_format"] < out["coverage_generic"]:
        out["findings"].append(f"the format lowers coverage ({out['coverage_with_format']:.0%} vs "
                               f"{out['coverage_generic']:.0%} generic)")
    if out["role_sections"]["responsibilities"] < 0.7:
        out["findings"].append(f"responsibilities found on only {out['role_sections']['responsibilities']:.0%} "
                               "of sampled postings — a heading the board uses for duties is not mapped")
    return out


def prune_format(fmt: dict, report: dict) -> dict:
    """The format minus boilerplate rules that matched no sampled posting."""
    dead = [r for r in report.get("rule_hits", []) if r["postings"] == 0]
    keep = [r for r in fmt.get("boilerplate") or []
            if not any(all(r.get(k) == d.get(k) for k in ("kind", "starts_with", "contains")) for d in dead)]
    return {**fmt, "boilerplate": keep}
