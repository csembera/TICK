"""
Turn a job description's HTML into labelled sections.

A board's postings repeat the same skeleton: a company intro, a run of
headed sections (responsibilities, qualifications, ...), then benefits and
an equal-opportunity statement. Stripping the tags, as the collector used to,
flattens all of that into one string. This module keeps it:

    parse(html, fmt) -> {
        "sections": {"summary": [...], "responsibilities": [...], ...,
                     "other": [{"heading": "...", "lines": [...]}]},
        "compensation": {"min", "max", "currency", "period", "text"} | None,
        "boilerplate": ["company_intro", "eeo", ...],   # kinds found
        "unmatched_headings": [...],                    # filed under "other"
        "format": "config" | "generic",
    }

Headings are matched first against the SourceConfig's description_format
(the aliases and boilerplate the evaluator learned for this board), then
against the generic patterns below. A heading neither recognises is kept as
a sub-heading when it sits inside a role section ("Credentialing" under
responsibilities), and otherwise opens an "other" block under its own text:
nothing in a description is ever dropped.

Deterministic and stdlib only, like the rest of the collector: the evaluator
does the reasoning, this applies it.
"""

import re
from html.parser import HTMLParser

# Role content first, then the parts every posting on a board repeats.
SECTIONS = ("summary", "responsibilities", "qualifications", "preferred_qualifications",
            "work_arrangement", "compensation", "benefits", "company_intro", "eeo")
BOILERPLATE = ("company_intro", "benefits", "eeo")
_ROLE_SECTIONS = ("responsibilities", "qualifications", "preferred_qualifications")

# Generic heading patterns, tried in order: the first hit wins, so the more
# specific families come first ("Summary of Responsibilities" is
# responsibilities; "Compensation & Benefits" is compensation).
_GENERIC = [
    ("compensation", r"\b(compensation|salary|pay range|pay|wages?)\b"),
    ("eeo", r"equal (employment )?opportunit|\beeo\b|diversity|inclusion|accommodation"),
    ("benefits", r"\bbenefits?\b|how we value you|what.?s in it for you|perks|total rewards|we offer"),
    ("company_intro", r"^about (us|the company)\b|company overview|who we are|^why\b|our (company|culture|mission)"),
    ("work_arrangement", r"\blocation\b|work.at.home|\bremote\b|\bhybrid\b|\btravel\b|\bschedule\b|work environment"),
    ("preferred_qualifications", r"\bpreferred\b|nice to have|\bbonus\b|\bdesired\b"),
    ("responsibilities", r"responsibilit|\bduties\b|what you.?ll (be )?do|your role|role and impact"
                         r"|on any given day|in this role|your work will include|accountabilit"
                         r"|essential functions|key activities"),
    ("qualifications", r"qualification|requirement|\bexperience\b|\bskills?\b|knowledge|abilities"
                       r"|what you bring|\beducation\b|credentials\b|what we.?re looking for"
                       r"|^(required|minimum)\b"
                       r"|what we expect|who you are|competenc|\bcertification"),
    ("summary", r"summary|overview|\bdescription\b|purpose|about the (role|job|position)|the opportunity"),
]
_GENERIC_RE = [(kind, re.compile(rx, re.I)) for kind, rx in _GENERIC]

# Paragraphs that are boilerplate wherever they sit, headed or not.
_EEO_TEXT = re.compile(r"equal (employment )?opportunit|without regard to (race|age|sex|religion)"
                       r"|\be-?verify\b|reasonable accommodation", re.I)

_MONEY = r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*([kK])?"
_PAY_RE = re.compile(_MONEY + r"\s*(?:-|–|—|to)\s*" + _MONEY)
_HOURLY = re.compile(r"per hour|an hour|/\s?h(ou)?r\b|hourly", re.I)

_BLOCK_TAGS = {"p", "div", "li", "br", "tr", "ul", "ol", "table", "section", "article",
               "h1", "h2", "h3", "h4", "h5", "h6"}
_BOLD_TAGS = {"b", "strong", "h1", "h2", "h3", "h4", "h5", "h6", "u"}
_BULLET_RE = re.compile(r"\s*[•·▪◦‣●○■–\-*]\s+")


class _Blocks(HTMLParser):
    """HTML -> [(text, is_bold, is_list_item)], one entry per block."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self._parts, self._bold_chars, self._bold, self._li = [], [], 0, 0, False

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._parts)).strip()
        if text:
            # bold when (nearly) all of the block's text sat inside bold tags
            self.blocks.append((text, self._bold_chars >= 0.9 * len(text.replace(" ", "")), self._li))
        self._parts, self._bold_chars, self._li = [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self._flush()
            self._li = tag == "li"
        if tag in _BOLD_TAGS:
            self._bold += 1

    def handle_endtag(self, tag):
        if tag in _BOLD_TAGS and self._bold:
            self._bold -= 1
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        self._parts.append(data)
        if self._bold:
            self._bold_chars += len(data.replace(" ", "").replace("\n", ""))

    def close(self):
        super().close()
        self._flush()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", (s or "").casefold())).strip()


def _is_heading(text: str, bold: bool, li: bool) -> bool:
    words = len(text.split())
    # recruiter tags (#LI-SB1) are short and all caps, but they head nothing
    if li or _BULLET_RE.match(text) or text.startswith("#") or words > 8 or len(text) > 80:
        return False
    return bold or text.endswith(":") or (text.isupper() and words <= 5)


def classify_heading(heading: str, fmt: dict | None = None) -> str | None:
    """The section a heading opens, or None when nothing recognises it."""
    n = _norm(heading)
    for kind, aliases in ((fmt or {}).get("aliases") or {}).items():
        if kind in SECTIONS and n in {_norm(a) for a in aliases}:
            return kind
    for kind, rx in _GENERIC_RE:
        if rx.search(heading):
            return kind
    return None


def _boilerplate_kind(text: str, fmt: dict | None) -> str | None:
    """A paragraph the board repeats on every posting, wherever it sits."""
    for rule in (fmt or {}).get("boilerplate") or []:
        kind = rule.get("kind")
        if kind not in BOILERPLATE:
            continue
        if rule.get("starts_with") and _norm(text).startswith(_norm(rule["starts_with"])):
            return kind
        if rule.get("contains") and _norm(rule["contains"]) in _norm(text):
            return kind
    return "eeo" if _EEO_TEXT.search(text) else None


def compensation(text: str) -> dict | None:
    """The first pay range in text: {"min", "max", "currency", "period", "text"}."""
    m = _PAY_RE.search(text or "")
    if not m:
        return None

    def num(v, k):
        x = float(v.replace(",", ""))
        return int(x * 1000) if k else (int(x) if x.is_integer() else x)

    lo, hi = num(m.group(1), m.group(2)), num(m.group(3), m.group(4))
    around = text[max(0, m.start() - 80):m.end() + 80]
    period = "hour" if _HOURLY.search(around) or max(lo, hi) < 500 else "year"
    # the sentence holding the range, so the figure is never read out of context
    dot = text.rfind(". ", 0, m.start())
    end = text.find(". ", m.end())
    return {"min": min(lo, hi), "max": max(lo, hi), "currency": "USD", "period": period,
            "text": text[dot + 2 if dot >= 0 else 0:end + 1 if end >= 0 else len(text)].strip()}


def blocks(raw_html: str) -> list[tuple[str, bool, bool]]:
    p = _Blocks()
    p.feed(raw_html or "")
    p.close()
    out = []
    for text, bold, li in p.blocks:
        # "• one • two" inside a single block is two bullets
        parts = [t for t in re.split(r"\s*[•▪●]\s*", text) if t.strip()]
        if len(parts) > 1 or text.lstrip().startswith(("•", "▪", "●")):
            out += [(t.strip(), False, True) for t in parts]
        else:
            out.append((_BULLET_RE.sub("", text, count=1) if li else text, bold, li))
    return out


def parse(raw_html: str | None, fmt: dict | None = None) -> dict | None:
    """Labelled sections of one description; None when there is no text."""
    bl = blocks(raw_html) if raw_html else []
    if not bl:
        return None
    sections: dict[str, list] = {}
    other: list[dict] = []
    unmatched, found_bp = [], []
    current, current_other = "summary", None
    pay = None

    for text, bold, li in bl:
        if _is_heading(text, bold, li):
            kind = classify_heading(text.rstrip(":").strip(), fmt)
            if kind:
                current, current_other = kind, None
                if kind in BOILERPLATE and kind not in found_bp:
                    found_bp.append(kind)
            elif current in _ROLE_SECTIONS:
                # a sub-heading inside a role section stays in it, as a line
                sections.setdefault(current, []).append(text)
                unmatched.append(text.rstrip(":").strip())
            else:
                current, current_other = "other", {"heading": text.rstrip(":").strip(), "lines": []}
                other.append(current_other)
                unmatched.append(current_other["heading"])
            continue
        kind = current
        bp = _boilerplate_kind(text, fmt)
        if bp and not li:
            kind = bp
            if bp not in found_bp:
                found_bp.append(bp)
        found_pay = compensation(text)
        if found_pay and pay is None:
            pay = found_pay
            if not li and kind in ("summary", "other", "benefits"):
                kind = "compensation"
        if kind == "other":
            current_other["lines"].append(text)
        else:
            sections.setdefault(kind, []).append(text)

    if other:
        sections["other"] = other
    return {"sections": sections, "compensation": pay, "boilerplate": found_bp,
            "unmatched_headings": unmatched, "format": "config" if fmt else "generic"}


def role_text(parsed: dict | None) -> str:
    """The description without the board's boilerplate, as one string."""
    if not parsed:
        return ""
    secs = parsed["sections"]
    lines = [t for k in SECTIONS if k not in BOILERPLATE for t in secs.get(k, [])]
    lines += [t for o in secs.get("other", []) for t in [o["heading"], *o["lines"]]]
    return " ".join(lines)


def coverage(parsed: dict | None) -> float:
    """Share of the description's characters filed under a known section."""
    if not parsed:
        return 0.0
    secs = parsed["sections"]
    known = sum(len(t) for k in SECTIONS for t in secs.get(k, []))
    unknown = sum(len(t) for o in secs.get("other", []) for t in o["lines"])
    return round(known / (known + unknown), 3) if known + unknown else 0.0
