#!/usr/bin/env python3
"""
TICK — Evaluator observation tools (CLI).

The deterministic half of the evaluator. These five subcommands were extracted
from the SDK-driven evaluator that handed them to an Anthropic tool_runner;
here they are driven by whatever agent is running the skill, so no API key is
involved.

The intelligence is in the caller. These just observe and report JSON on stdout.

    fetch        careers pages and HTML (truncates at 8000 chars)
    bundle       download a JS asset IN FULL and regex it for API endpoints
    probe        live-test a candidate collection endpoint
    fingerprint  scan text/URL for known ATS fingerprints
    search       find careers-page candidate URLs
    descriptions sample a config's descriptions and inventory their layout

Every subcommand exits 0 and prints a JSON object, including on failure — a
transport error is an observation, not a crash. Read the JSON, not the exit code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    print(json.dumps({"error": "httpx is not installed — run: .venv/bin/pip install httpx"}))
    sys.exit(0)

from tick_common import (BROWSER_HEADERS, BUNDLE_PATTERNS, FINGERPRINTS, _UA,
                         description_html, format_report, locate_postings, spread)


def _json_arg(raw: Optional[str], what: str) -> Optional[dict]:
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"--{what} is not valid JSON: {e}"}))
        sys.exit(0)
    if not isinstance(val, dict):
        print(json.dumps({"error": f"--{what} must be a JSON object"}))
        sys.exit(0)
    return val


# --------------------------------------------------------------------------

def cmd_fetch(a) -> dict:
    """Fetch a page: final URL, status, content-type, first 8000 chars of body."""
    hdrs = {"User-Agent": _UA}
    if a.browser_headers:
        hdrs = {**BROWSER_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    hdrs.update(_json_arg(a.headers, "headers") or {})
    try:
        with httpx.Client(headers=hdrs, follow_redirects=True, timeout=25) as c:
            r = c.get(a.url)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return {
        "final_url": str(r.url),
        "status": r.status_code,
        "content_type": r.headers.get("content-type"),
        "bytes": len(r.text),
        "truncated": len(r.text) > 8000,
        "body_head": r.text[:8000],
    }


def cmd_bundle(a) -> dict:
    """Download a JS/JSON asset IN FULL and regex-search it for API endpoints."""
    pats = a.pattern or BUNDLE_PATTERNS
    try:
        with httpx.Client(headers={"User-Agent": _UA}, follow_redirects=True, timeout=60) as c:
            r = c.get(a.url)
        if r.status_code != 200:
            return {"url": a.url, "status": r.status_code, "note": "non-200, nothing to search"}
        text = r.text[:12_000_000]
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    results, seen, budget = [], set(), a.max_hits
    for p in pats:
        try:
            rx = re.compile(p)
        except re.error as e:
            results.append({"pattern": p, "error": f"bad regex: {e}"})
            continue
        hits = []
        for m in rx.finditer(text):
            if budget <= 0:
                break
            frag = m.group(0)[:200]
            if frag in seen:
                continue  # same literal already reported under an earlier pattern
            seen.add(frag)
            lo, hi = max(0, m.start() - a.window), min(len(text), m.end() + a.window)
            hits.append({"match": frag, "context": text[lo:hi]})
            budget -= 1
        results.append({"pattern": p, "hits": hits})
    return {"url": a.url, "status": 200, "bytes_searched": len(text),
            "truncated_hits": budget <= 0, "results": results}


def cmd_probe(a) -> dict:
    """Live-test a candidate collection endpoint and report what came back."""
    hdrs = dict(BROWSER_HEADERS) if not a.no_browser_headers else {"User-Agent": _UA}
    hdrs["Content-Type"] = "application/json"
    hdrs.update(_json_arg(a.headers, "headers") or {})
    body = _json_arg(a.body, "body")
    try:
        with httpx.Client(headers=hdrs, follow_redirects=True, timeout=30) as c:
            r = c.request(a.method.upper(), a.url, json=body)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    out: dict[str, Any] = {
        "url": a.url, "method": a.method.upper(), "status": r.status_code,
        "content_type": r.headers.get("content-type"), "final_url": str(r.url),
    }
    try:
        data = r.json()
    except Exception:
        out["parsed_json"] = False
        out["body_head"] = r.text[:1200]
        return out

    out["parsed_json"] = True
    postings, container = locate_postings(data)
    out["container_keys"] = list(data)[:20] if isinstance(data, dict) else "list"
    out["postings_container"] = container
    out["postings_returned"] = len(postings)
    for k in ("total", "count", "totalCount", "totalResults"):
        if isinstance(data, dict) and k in data:
            out["reported_total"] = data[k]
    if postings:
        sample = postings[0]
        out["sample_keys"] = list(sample) if isinstance(sample, dict) else None
        out["sample"] = json.loads(json.dumps(sample)[:1500])
    return out


def cmd_fingerprint(a) -> dict:
    """Scan text (page HTML, a URL, a script src list) for known ATS fingerprints."""
    text = a.text if a.text is not None else sys.stdin.read()
    hits = []
    for ats, pattern in FINGERPRINTS:
        for m in re.finditer(pattern, text):
            groups = [g for g in m.groups() if g]
            hits.append({"ats": ats, "match": m.group(0), "tenant_candidates": groups})
    return {"hits": hits} if hits else {"hits": [], "note": "no known ATS fingerprints found"}


def cmd_search(a) -> dict:
    """Find careers-page candidate URLs when no URL hint was given."""
    try:
        q = f"{a.company} careers jobs"
        with httpx.Client(headers={"User-Agent": _UA}, follow_redirects=True, timeout=25) as c:
            r = c.get("https://html.duckduckgo.com/html/", params={"q": q})
        links = re.findall(r'class="result__a" href="([^"]+)"', r.text)[:8]
        clean = []
        for l in links:  # ddg wraps URLs; unwrap the uddg param when present
            clean.append(httpx.QueryParams(l.split("?", 1)[1]).get("uddg")
                         if "uddg=" in l else l)
        return {"query": q, "results": clean}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------

def cmd_descriptions(a) -> dict:
    """Sample a config's descriptions the way the collector fetches them and
    report their layout: headings and how each would be filed, paragraphs most
    postings repeat, pay sentences, and — if the config already has a
    description_format — what that format does to them."""
    try:
        cfg = json.load(open(a.config))
    except Exception as e:
        return {"error": f"cannot read {a.config}: {type(e).__name__}: {e}"}
    hdrs = {**BROWSER_HEADERS, "Content-Type": "application/json", **(cfg.get("request_headers") or {})}
    errors: list[str] = []
    with httpx.Client(headers=hdrs, follow_redirects=True, timeout=30) as c:
        def get_json(url, method="GET", body=None):
            r = c.request(method, url, json=body)
            r.raise_for_status()
            return r.json()
        try:
            data = get_json(cfg["endpoint"], cfg.get("http_method") or "GET", cfg.get("request_body"))
        except Exception as e:
            return {"error": f"collection endpoint failed: {type(e).__name__}: {e}"}
        postings = (data.get(cfg["postings_key"]) if cfg.get("postings_key") and isinstance(data, dict)
                    else locate_postings(data)[0]) or []
        htmls = []
        for posting in spread(postings, a.n):
            try:
                h = description_html(cfg, posting, get_json)
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")
                continue
            if h:
                htmls.append(h)
            time.sleep(0.2)
    if a.save:
        os.makedirs(a.save, exist_ok=True)
        for i, h in enumerate(htmls, 1):
            with open(os.path.join(a.save, f"{i:02d}.html"), "w") as f:
                f.write(h)
    report = format_report(htmls, cfg.get("description_format"))
    report["postings_listed"] = len(postings)
    if errors:
        report["fetch_errors"] = errors[:5]
    if a.save:
        report["saved_to"] = a.save
    return report


def main() -> None:
    p = argparse.ArgumentParser(prog="tick_tools", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="fetch a page (truncates body at 8000 chars)")
    f.add_argument("url")
    f.add_argument("--browser-headers", action="store_true",
                   help="send a full browser header set; retry with this before "
                        "concluding a host is unreachable")
    f.add_argument("--headers", help="extra request headers as a JSON object")
    f.set_defaults(fn=cmd_fetch)

    b = sub.add_parser("bundle", help="download a JS asset in full and regex it")
    b.add_argument("url")
    b.add_argument("--pattern", action="append", help="regex (repeatable); omit for the default API sweep")
    b.add_argument("--window", type=int, default=100, help="context chars around each hit")
    b.add_argument("--max-hits", type=int, default=30)
    b.set_defaults(fn=cmd_bundle)

    pr = sub.add_parser("probe", help="live-test a candidate collection endpoint")
    pr.add_argument("url")
    pr.add_argument("--method", default="GET", choices=["GET", "POST", "get", "post"])
    pr.add_argument("--headers", help="auth/tenant/org headers as a JSON object")
    pr.add_argument("--body", help="JSON body for POST")
    pr.add_argument("--no-browser-headers", action="store_true")
    pr.set_defaults(fn=cmd_probe)

    fp = sub.add_parser("fingerprint", help="scan text or a URL for ATS fingerprints")
    fp.add_argument("text", nargs="?", default=None, help="text to scan; omit to read stdin")
    fp.set_defaults(fn=cmd_fingerprint)

    s = sub.add_parser("search", help="find careers-page candidate URLs")
    s.add_argument("company")
    s.set_defaults(fn=cmd_search)

    d = sub.add_parser("descriptions", help="sample a config's descriptions and inventory their layout")
    d.add_argument("config", help="a draft or SourceConfig JSON with a working endpoint")
    d.add_argument("--n", type=int, default=12, help="postings to sample, spread across the board")
    d.add_argument("--save", help="also write each sampled description's HTML into this directory")
    d.set_defaults(fn=cmd_descriptions)

    a = p.parse_args()
    print(json.dumps(a.fn(a), indent=2))


if __name__ == "__main__":
    main()
