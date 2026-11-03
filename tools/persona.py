"""
A TICK persona: who a user is, what they sell, and the terms they watch.

Each user keeps one persona, at profiles/<sub>.json in the data bucket. The
weekly brief for an account is written for the persona of the one user who
tracks it (the account is in their custom:accounts), and the dashboard's My
terms are its focus terms. An account tracked by no one, or by several users,
gets the default reader.

This module is the one definition of what a persona may hold. A persona is
text a user typed that ends up in a model's context, so every field is
bounded and normalised here, and nothing that is not a field below survives.
It runs on write (the join Lambda, through a symlink like the renderer's) and
again on read (the brief skill's evidence.py), so a profile edited by hand in
the bucket is held to the same rules.
"""

from __future__ import annotations

import hashlib
import json
import re

# Fixed lists rather than free text wherever they can be: less room for a
# persona to carry anything but a description of its reader.
ROLES = {
    "account_executive": "Account executive",
    "solutions_engineer": "Solutions engineer or architect",
    "sales_development": "Sales development",
    "customer_success": "Customer success",
    "partner_manager": "Partner or alliance manager",
    "marketing": "Marketing",
    "founder": "Founder or executive",
    "other": "Other",
}
BUYERS = {
    "security": "Security leadership",
    "platform": "Platform, infrastructure and DevOps",
    "engineering": "Engineering leadership",
    "data": "Data and analytics leadership",
    "it": "CIO and IT",
    "finance": "Finance",
    "operations": "Operations",
    "people": "HR and people",
    "procurement": "Procurement",
}
FIELDS = ("role", "sells_for", "offer", "buyers", "focus_terms", "competitor_terms")
NAME_CHARS, OFFER_CHARS, TERM_CHARS = 80, 240, 60
MAX_FOCUS, MAX_COMPETITORS = 30, 20

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# What a tech or business term needs ("c#", "node.js", "ci/cd", "power bi",
# "at&t") and no more: no brackets, quotes or markup.
_TERM_RE = re.compile(r"^[\w .+#/&'-]+$")


def _text(value, limit: int, field: str, problems: list[str]) -> str:
    text = re.sub(r"\s+", " ", _CONTROL.sub(" ", str(value or ""))).strip()
    if len(text) > limit:
        problems.append(f"{field} is {len(text)} characters (limit {limit})")
        text = text[:limit].rsplit(" ", 1)[0]
    return text


def _terms(value, limit: int, field: str, problems: list[str]) -> list[str]:
    items = value if isinstance(value, list) else str(value or "").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        term = re.sub(r"\s+", " ", _CONTROL.sub(" ", str(raw))).strip()
        if not term:
            continue
        if len(term) > TERM_CHARS or not _TERM_RE.match(term):
            problems.append(f"{field}: {term[:40]!r} is not a term (letters, digits and . + # / & ' - only, "
                            f"up to {TERM_CHARS} characters)")
            continue
        if term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    if len(out) > limit:
        problems.append(f"{field}: {len(out)} terms (limit {limit})")
        out = out[:limit]
    return out


def clean(raw: dict | None) -> tuple[dict, list[str]]:
    """(persona, problems). The persona holds only the known fields, each
    bounded; problems say what was dropped or cut. A writer refuses to save
    when there are problems; a reader can use what survived."""
    raw = raw if isinstance(raw, dict) else {}
    problems: list[str] = []
    role = str(raw.get("role") or "")
    if role and role not in ROLES:
        problems.append(f"role {role[:40]!r} is not one of: {', '.join(ROLES)}")
        role = ""
    buyers_in = raw.get("buyers") or []
    buyers = []
    for b in (buyers_in if isinstance(buyers_in, list) else [buyers_in]):
        if b in BUYERS and b not in buyers:
            buyers.append(b)
        elif b:
            problems.append(f"buyer {str(b)[:40]!r} is not one of: {', '.join(BUYERS)}")
    persona = {
        "role": role,
        "sells_for": _text(raw.get("sells_for"), NAME_CHARS, "sells_for", problems),
        "offer": _text(raw.get("offer"), OFFER_CHARS, "offer", problems),
        "buyers": buyers,
        "focus_terms": _terms(raw.get("focus_terms"), MAX_FOCUS, "focus_terms", problems),
        "competitor_terms": _terms(raw.get("competitor_terms"), MAX_COMPETITORS, "competitor_terms", problems),
    }
    return persona, problems


def is_blank(persona: dict | None) -> bool:
    """A persona with nothing in it describes no reader: the default applies."""
    return not persona or not any(persona.get(k) for k in FIELDS)


def ref(persona: dict) -> str:
    """A short fingerprint of a persona's content. A brief records the ref of
    the persona it was written for, so check.py can tell it was written from
    this pack and not an older one."""
    canonical = json.dumps({k: persona.get(k) for k in FIELDS}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def describe(persona: dict | None) -> str:
    """"Account executive, Chainguard", for a page or a table."""
    if is_blank(persona):
        return ""
    parts = [ROLES.get(persona.get("role"), ""), persona.get("sells_for", "")]
    return ", ".join(p for p in parts if p) or "a persona with terms only"
