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

Rows collected before descriptions kept their structure hold only the
flattened text. from_text() rebuilds sections from that text by finding known
headings inside it, and marks the result approximate: tags no longer say where
a heading starts, so a heading the board words unusually stays in the text.

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
    ("company_intro", r"^about (us|the company)\b|company overview|who we are|our (company|culture|mission)"
                      r"|^why\b(?!.*\b(role|position|job|work)\b)"),   # "Why <company>?", not "Why this role matters"
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

# digits with comma groups, never a trailing comma ("$96,000, which includes")
_NUM = r"(\d+(?:,\d+)*(?:\.\d+)?)\s*([kK])?"
_CUR = r"(?:USD|CAD|US|CA)?"
# the second figure's "$" is optional ("$60,000-70,000"), and a currency code
# can stand where the dash would ("$130,000.00 CAD$150,000.00 CAD")
_RANGE_RE = re.compile(r"\$\s?" + _NUM + r"\s*" + _CUR
                       + r"(?:\s*(?:-|–|—|to|and)\s*|\s*(?=\$))" + _CUR + r"\$?\s?" + _NUM)
_ONE_RE = re.compile(r"(up to|maximum of|a maximum|max\.?)?\s*\$\s?" + _NUM, re.I)
_HOURLY = re.compile(r"per hour|an hour|/\s?h(ou)?r\b|hourly", re.I)
# "$2 billion in premiums" is never pay
_SCALE = re.compile(r"\s*(million|billion|mm\b|bn\b|[mb]\b)", re.I)
# a single figure counts as pay only in a sentence about pay
_PAY_WORDS = re.compile(r"salar|compensat|\bpay\b|\bwages?\b|\branges?\b|\bbase\b|hourly|per hour"
                        r"|/\s?h(ou)?r\b|per year|/\s?year|estimate", re.I)
# a figure followed by one of these is the business's, not the hire's
_BUSINESS = re.compile(r"\W{0,3}(annual\s+)?(gross\s+)?(written\s+)?(premiums?|revenue|budget|portfolio|"
                       r"in (sales|assets|claims|losses)|of (business|premium)|book)", re.I)
_CAD = re.compile(r"\bCAD\b|C\$|\bCA\$", re.I)

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


def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", (s or "").casefold())).strip()


def is_heading(text: str, bold: bool, li: bool) -> bool:
    words = len(text.split())
    # recruiter tags (#LI-SB1) are short and all caps, but they head nothing
    if li or _BULLET_RE.match(text) or text.startswith("#") or words > 8 or len(text) > 80:
        return False
    return bold or text.endswith(":") or (text.isupper() and words <= 5)


def _alias_set(fmt: dict | None) -> set[str]:
    return {normalize(a) for kind, aliases in ((fmt or {}).get("aliases") or {}).items()
            if kind in SECTIONS for a in aliases}


def heading_in(text: str, bold: bool, li: bool, fmt: dict | None = None) -> bool:
    """is_heading, plus any non-bullet line that exactly matches one of the
    board's learned aliases: some boards set their template headings ("Job
    Summary", "Work Location") as plain paragraphs, with nothing to mark them."""
    return is_heading(text, bold, li) or (
        not li and bool(fmt) and normalize(text.rstrip(":")) in _alias_set(fmt))


def classify_heading(heading: str, fmt: dict | None = None) -> str | None:
    """The section a heading opens, or None when nothing recognises it."""
    n = normalize(heading)
    for kind, aliases in ((fmt or {}).get("aliases") or {}).items():
        if kind in SECTIONS and n in {normalize(a) for a in aliases}:
            return kind
    for kind, rx in _GENERIC_RE:
        if rx.search(heading):
            return kind
    return None


def boilerplate_kind(text: str, fmt: dict | None) -> str | None:
    """A paragraph the board repeats on every posting, wherever it sits."""
    for rule in (fmt or {}).get("boilerplate") or []:
        kind = rule.get("kind")
        if kind not in BOILERPLATE:
            continue
        if rule.get("starts_with") and normalize(text).startswith(normalize(rule["starts_with"])):
            return kind
        if rule.get("contains") and normalize(rule["contains"]) in normalize(text):
            return kind
    return "eeo" if _EEO_TEXT.search(text) else None


_GROUPED = re.compile(r"^(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")


def _num(v: str, k: str | None) -> int | float | None:
    """A figure's value, or None when its digits are malformed ("$110,00"):
    a typo is not a number to report."""
    if not _GROUPED.match(v):
        return None
    x = float(v.replace(",", ""))
    if k:
        x *= 1000
    return int(x) if x.is_integer() else x


def _sentence(text: str, a: int, b: int) -> str:
    """The sentence holding text[a:b], so a figure is never read out of context."""
    dot = text.rfind(". ", 0, a)
    end = text.find(". ", b)
    return text[dot + 2 if dot >= 0 else 0:end + 1 if end >= 0 else len(text)].strip()


def compensation(text: str) -> dict | None:
    """The pay a text states: {"min", "max", "currency", "period", "text"}.

    A range wins ("$85,000 - $110,000", "$60,000-70,000", "$85K–$110K"). Without
    one, a single figure counts when its sentence is about pay ("the current
    range is $170,000"); "up to $120,000" sets only the max. Implausible ranges
    (a typo like "$150-000") and figures in the millions are not pay."""
    text = text or ""
    for m in _RANGE_RE.finditer(text):
        if _SCALE.match(text, m.end()) or _BUSINESS.match(text, m.end()):
            continue
        lo, hi = _num(m.group(1), m.group(2)), _num(m.group(3), m.group(4))
        if lo is None or hi is None or not (0 < lo <= hi <= 10 * lo):
            continue
        return _pay(text, m.start(), m.end(), lo, hi)
    for m in _ONE_RE.finditer(text):
        if _SCALE.match(text, m.end()) or _BUSINESS.match(text, m.end()):
            continue
        sent = _sentence(text, m.start(), m.end())
        if not _PAY_WORDS.search(sent):
            continue
        x = _num(m.group(2), m.group(3))
        hourly = _HOURLY.search(text[max(0, m.start() - 80):m.end() + 80])
        if x is None or x <= 0 or (x < 1000 and not hourly):   # "$150" alone is no salary
            continue
        return _pay(text, m.start(), m.end(), None if m.group(1) else x, x)
    return None


def _pay(text: str, a: int, b: int, lo, hi) -> dict:
    around = text[max(0, a - 80):b + 80]
    period = "hour" if _HOURLY.search(around) or hi < 500 else "year"
    sent = _sentence(text, a, b)
    return {"min": lo, "max": hi, "currency": "CAD" if _CAD.search(sent) else "USD",
            "period": period, "text": sent}


def find_pay(parsed: dict | None) -> dict | None:
    """The first pay any line of a parsed description states, read afresh, so
    an improved extractor reaches rows stored before it existed."""
    if not parsed:
        return None
    secs = parsed["sections"]
    lines = [t for k in ("compensation", *SECTIONS) for t in secs.get(k, [])]
    lines += [t for o in secs.get("other", []) for t in [o["heading"], *o["lines"]]]
    for line in lines:
        found = compensation(line)
        if found:
            return found
    return None


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
        if heading_in(text, bold, li, fmt):
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
        bp = boilerplate_kind(text, fmt)
        if bp and not li:
            kind = bp
            if bp not in found_bp:
                found_bp.append(bp)
        found_pay = compensation(text)
        if found_pay:
            pay = pay or found_pay
            # a pay range in its own paragraph is compensation wherever it sits
            # (boards tack it onto the last section); a pay bullet stays in its list
            if not li and kind not in ("company_intro", "eeo"):
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


def full_text(parsed: dict | None) -> str:
    """Every line of the description, boilerplate included, as one string."""
    if not parsed:
        return ""
    secs = parsed["sections"]
    lines = [t for k in SECTIONS for t in secs.get(k, [])]
    lines += [t for o in secs.get("other", []) for t in [o["heading"], *o["lines"]]]
    return " ".join(lines)


# Headings as boards actually write them, for flattened text, where no tag
# marks where one starts. Generic wording only; a board's own headings come
# from its description_format aliases.
TEXT_HEADINGS = (
    "Key Responsibilities", "Primary Job Responsibilities", "Duties and Responsibilities",
    "Summary of Responsibilities", "Role Responsibilities", "Your Key Responsibilities",
    "Primary Responsibilities", "Other Responsibilities", "Education Requirements",
    "Experience Requirements", "Knowledge Requirements", "Skills & Competencies",
    "Essential Functions", "Responsibilities", "What you'll be doing", "What you'll do",
    "On any given day, you'll", "Your role and impact", "In this role, you'll",
    "Preferred Qualifications", "Minimum Qualifications", "Required Qualifications",
    "Basic Qualifications", "Qualifications", "Requirements", "Skills and Experience Required",
    "Knowledge, Skills, and Abilities", "Preferred Education and Training",
    "Preferred Experience and Skills", "What You Bring", "Here's what we expect",
    "Job Summary", "Position Summary", "Job Description", "Basic Purpose",
    "Work Location", "Work-at-home requirements", "Work Environment",
    "How we value you", "What's in it for you", "Compensation & Benefits", "Benefits",
)
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


def from_text(text: str | None, fmt: dict | None = None, skip_sentences: int = 0) -> dict | None:
    """Approximate sections from flattened description text.

    skip_sentences: how many leading sentences are the board's shared intro
    (the renderer's detect_boilerplate_prefix finds that across a board); they
    are filed as company_intro."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return None
    names = {h for h in TEXT_HEADINGS}
    for aliases in ((fmt or {}).get("aliases") or {}).values():
        names.update(a for a in aliases if a)
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    # a heading starts the text, or follows a sentence end, a bullet or an
    # all-caps label ("SKILLS Role Responsibilities:"), and is followed by a
    # colon, a bullet or a capitalised word — never mid-sentence, where
    # "the Benefits Administration team" is not a heading
    # (case-insensitive for the names only: "QUALIFICATIONS" is a heading, but
    # the lookarounds must still see real capitals)
    heading_re = re.compile(
        rf"(?:^|(?<=[.!?:•]\s)|(?<=[A-Z]{{3}}\s))((?i:{alt}))(?=\s*:|\s*[•▪●]|\s+[A-Z])")

    marks = [(m.start(1), m.end(1), m.group(1)) for m in heading_re.finditer(text)]
    segments = [(None, text[:marks[0][0]] if marks else text)]
    for i, (a, b, head) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        segments.append((head, text[b:end].lstrip(" :")))

    sections: dict[str, list] = {}
    other, unmatched, found_bp = [], [], []
    current = "summary"
    for n_seg, (head, body) in enumerate(segments):
        if head is not None:
            kind = classify_heading(head, fmt)
            current = kind or (current if current in _ROLE_SECTIONS else "other")
            if kind is None:
                unmatched.append(head)
                if current == "other":
                    other.append({"heading": head, "lines": []})
                else:   # a sub-heading inside a role section stays in it, as a line
                    sections.setdefault(current, []).append(head)
            if kind in BOILERPLATE and kind not in found_bp:
                found_bp.append(kind)
        parts = [t.strip() for t in re.split(r"\s*[•▪●]\s*", body) if t.strip()]
        lines = parts if len(parts) > 1 else [t.strip() for t in _SENT_RE.split(body) if t.strip()]
        if n_seg == 0 and skip_sentences:
            sents = [t.strip() for t in _SENT_RE.split(body) if t.strip()]
            intro, lines = sents[:skip_sentences], sents[skip_sentences:]
            if intro:
                sections.setdefault("company_intro", []).extend(intro)
                found_bp.append("company_intro")
        for line in lines:
            kind = current
            bp = boilerplate_kind(line, fmt)
            if bp:
                kind = bp
                if bp not in found_bp:
                    found_bp.append(bp)
            if kind == "other":
                other[-1]["lines"].append(line)
            else:
                sections.setdefault(kind, []).append(line)
    if other:
        sections["other"] = other
    return {"sections": sections, "compensation": compensation(text), "boilerplate": found_bp,
            "unmatched_headings": unmatched, "format": "text", "approximate": True}
