"""
The one layout every email TICK sends to a person is built from.

    python3 tools/email_layout.py cognito invite          # print a Cognito body
    python3 tools/email_layout.py cognito verification
    python3 tools/email_layout.py cognito --write         # rewrite both in infra/template.yaml

Five emails use it: the welcome invite and the verification code (Cognito
sends both, from bodies in infra/template.yaml), the waitlist confirmation
(the join Lambda, through a symlink to this file), the new-account email
(`tools/tick.py users grant --email`) and the prospect teaser
(tools/teaser.py). docs/email.md says what each one is for and how to add one.

Gmail and Outlook drop <style>, web fonts and SVG, so everything here is
tables and inline styles, the brand is a text wordmark, and Barlow falls back
to Helvetica/Arial. Outlook desktop ignores padding and background on <a>,
which is why button() paints the cell around the link instead.

Cognito cannot import Python, so its two bodies are copied into the template
as text. tests/test_emails.py fails when the copy and cognito_bodies() differ;
`cognito --write` brings the template back in step.

Standard library only: the join Lambda imports this file as it is.
"""

import html
import os
import re
import sys

SITE = "https://tick.sembasolutions.com"
SENDER_LINE = "TICK · Semba Solutions"
# A postal address for commercial mail (the teaser). Empty leaves it out.
POSTAL_ADDRESS = ""
# Keep in step with TemporaryPasswordValidityDays in infra/template.yaml;
# tests/test_emails.py checks it.
PASSWORD_DAYS = 7
WIDTH = 600

INK, SURFACE, PAGE, PANEL = "#080d12", "#ffffff", "#f2f2f3", "#f5f7fa"
TEXT, TEXT2, TEXT3, FAINT = "#17212b", "#4b5866", "#64717f", "#8b97a4"
BORDER, ACCENT, ACCENT_SOFT, ACCENT_LINE = "#e3e8ee", "#3d6a96", "#edf3f9", "#d3e1ee"
LIVE, ON_INK, ON_INK2, ON_INK3 = "#2f8f5b", "#eef2f5", "#c9d4de", "#9aa9b8"

FONT = "font-family:Barlow,'Helvetica Neue',Helvetica,Arial,sans-serif"
FONT_HEAD = "font-family:'Barlow Condensed','Arial Narrow',Arial,sans-serif"
FONT_MONO = "font-family:Menlo,Consolas,'Courier New',monospace"
LINK = f"color:{ACCENT};text-decoration:none"

_TABLE = 'role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"'


def e(value) -> str:
    return html.escape(str(value))


def table(rows: str, style: str = "", full: bool = True) -> str:
    attrs = _TABLE if full else _TABLE.replace(' width="100%"', "")
    return f"<table {attrs}" + (f' style="{style}"' if style else "") + f">{rows}</table>"


def row(content: str, padding: str = "24px 32px 0") -> str:
    """One band of the card, full width."""
    return f'<tr><td style="padding:{padding}">{content}</td></tr>'


def h1(text: str) -> str:
    return (f'<h1 style="margin:0 0 12px;{FONT_HEAD};font-weight:600;font-size:30px;line-height:1.15;'
            f'color:{TEXT}">{text}</h1>')


def para(text: str, size: int = 16, color: str = TEXT2, margin: str = "0") -> str:
    return f'<p style="margin:{margin};font-size:{size}px;line-height:1.6;color:{color}">{text}</p>'


def h2(text: str) -> str:
    """A heading inside an email that already has its h1."""
    return (f'<h2 style="margin:0;{FONT_HEAD};font-weight:600;font-size:24px;line-height:1.2;'
            f'color:{TEXT}">{text}</h2>')


def label(text: str, color: str = TEXT3, margin: str = "0 0 10px") -> str:
    """The small capitals that title every section."""
    return (f'<p style="margin:{margin};font-size:12px;font-weight:600;letter-spacing:.08em;'
            f'text-transform:uppercase;color:{color}">{text}</p>')


def button(href: str, text: str) -> str:
    return table(f'<tr><td style="background:{ACCENT};border-radius:8px">'
                 f'<a href="{e(href)}" style="display:inline-block;padding:13px 26px;{FONT};font-size:16px;'
                 f'font-weight:600;color:#ffffff;text-decoration:none">{text}</a></td></tr>', full=False)


def card(content: str, tint: bool = False, padding: str = "18px 20px") -> str:
    """A bordered panel: grey for details, blue (tint) for the thing to act on."""
    bg, line = (ACCENT_SOFT, ACCENT_LINE) if tint else (PANEL, BORDER)
    return table(f'<tr><td style="padding:{padding};{FONT}">{content}</td></tr>',
                 f"background:{bg};border:1px solid {line};border-radius:8px")


def dark_panel(title: str, text: str, href: str, cta: str) -> str:
    """The closing call to action on an ink panel."""
    return table(f'<tr><td style="padding:20px 22px;{FONT}">'
                 f'<div style="{FONT_HEAD};font-weight:600;font-size:20px;line-height:1.2;color:{ON_INK}">{title}</div>'
                 f'<div style="font-size:14px;line-height:1.55;color:{ON_INK2};margin:4px 0 16px">{text}</div>'
                 f'{button(href, cta)}</td></tr>', f"background:{INK};border-radius:8px")


def document(title: str, preheader: str, rows: list[str], note: str = "", aside: str = "",
             reason: str = "") -> str:
    """A whole email.

    preheader: the line an inbox shows after the subject.
    rows: the card's bands, each from row().
    note: the small print closing the card (how to get help, what to do if
      this was unexpected), already HTML.
    aside: a line in the header's right-hand corner, such as who it was
      prepared for, already HTML.
    reason: why this address received it, below the card, plain text.
    """
    head = (f'<span style="{FONT_HEAD};font-weight:700;font-size:24px;letter-spacing:.16em;color:{ON_INK}">TICK</span>'
            f'<span style="display:inline-block;margin-left:10px;padding:2px 8px;border:1px solid #2b3c4d;'
            f'border-radius:999px;font-size:11px;letter-spacing:.08em;color:#86aace;vertical-align:4px">BETA</span>')
    if aside:
        head += f'<div style="font-size:12px;line-height:1.5;color:{ON_INK3};margin-top:6px">{aside}</div>'
    foot = row(f'<div style="border-top:1px solid {BORDER};padding-top:20px;font-size:13px;line-height:1.6;'
               f'color:{TEXT3}">{note}</div>', "28px 32px 32px") if note else row("", "0 0 32px")
    small = " · ".join(e(x) for x in (SENDER_LINE, POSTAL_ADDRESS) if x)
    if reason:
        small += f"<br>{e(reason)}"
    body = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only"><title>{e(title)}</title></head>
<body style="margin:0;padding:0;background:{PAGE}">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{e(preheader)}</div>
<table {_TABLE} style="background:{PAGE}">
<tr><td align="center" style="padding:32px 16px">
<table {_TABLE} style="max-width:{WIDTH}px;background:{SURFACE};border:1px solid {BORDER};border-radius:12px;overflow:hidden;{FONT};color:{TEXT}">
<tr><td style="background:{INK};padding:22px 32px">{head}</td></tr>
<tr><td style="height:3px;background:{ACCENT};line-height:3px;font-size:0">&nbsp;</td></tr>
{body}
{foot}
</table>
<p style="margin:16px 0 0;max-width:{WIDTH}px;font-family:Arial,sans-serif;font-size:12px;line-height:1.6;color:{FAINT}">{small}</p>
</td></tr></table>
</body></html>"""


def text_document(lines: list[str], reason: str = "") -> str:
    """The plain-text part sent beside the HTML, for clients that show no HTML."""
    out = "\n\n".join(x.strip() for x in lines if x.strip())
    out += "\n\n— " + ", ".join(x for x in (SENDER_LINE.replace(" · ", ", "), POSTAL_ADDRESS) if x)
    return out + (f"\n{reason}" if reason else "") + "\n"


# ------------------------------------------------------------------- the emails

INVITE_SUBJECT = "Welcome to TICK: your sign-in details"   # also the template's EmailSubject (tested)
_INVITE_LEDE = ("TICK watches the job boards of the companies you sell to and turns what they post into a "
                "dashboard and a weekly brief, so a new program reaches you while it is still being staffed.")
_INVITE_DAYS = f"It works for {PASSWORD_DAYS} days. You choose your own password the first time you sign in."
_INVITE_NOTE = ("Password expired, or not expecting this? Just reply and we'll sort it out.<br>"
                f'<a href="{SITE}/docs" style="{LINK}">How TICK works</a>')
_FOUND = [("Your accounts.", "Each company's open roles, what opened and closed this week, and how its "
           "hiring is trending."),
          ("The weekly brief.", "Written every Sunday night: what moved, what the company is staffing "
           "for, and what to do about it."),
          ("Your profile.", "Tell TICK what you sell and the brief is written for you.")]


def _invite_parts(username: str = "{username}") -> dict[str, str]:
    """The welcome's pieces: its greeting, the sign-in details with the days
    note, the button, and what you'll find. {####} is left for the password."""
    details = table(
        f'<tr><td style="padding:18px 20px 6px">{label("Email", margin="0")}</td></tr>'
        f'<tr><td style="padding:0 20px 14px;font-size:16px;color:{TEXT}">{username}</td></tr>'
        f'<tr><td style="padding:0 20px"><div style="border-top:1px solid {BORDER};font-size:0;line-height:0">&nbsp;</div></td></tr>'
        f'<tr><td style="padding:14px 20px 6px">{label("Temporary password", margin="0")}</td></tr>'
        f'<tr><td style="padding:0 20px 18px;{FONT_MONO};font-size:20px;letter-spacing:.06em;color:{TEXT}">{{####}}</td></tr>',
        f"background:{PANEL};border:1px solid {BORDER};border-radius:8px")
    item = lambda n, head, text, pad: (
        f'<tr><td valign="top" style="width:28px;padding:0 0 {pad}px;color:{ACCENT};font-weight:700">{n}</td>'
        f'<td style="padding:0 0 {pad}px"><b>{head}</b> <span style="color:{TEXT2}">{text}</span></td></tr>')
    found = table("".join(item(n, head, text, 4 if n == len(_FOUND) else 12)
                          for n, (head, text) in enumerate(_FOUND, 1)),
                  f"font-size:15px;line-height:1.55;color:{TEXT}")
    return {"details": details + para(_INVITE_DAYS, 13, TEXT3, "10px 0 0"),
            "button": button(f"{SITE}/login", "Sign in to TICK"),
            "found": label("What you'll find", margin="0 0 14px") + found}


def invite() -> str:
    """The welcome email Cognito sends with a temporary password. Cognito
    fills {username} and {####} unescaped."""
    p = _invite_parts()
    return document("Welcome to TICK", f"Your temporary password is inside. It works for {PASSWORD_DAYS} days.",
                    [row(h1("You're in.") + para(_INVITE_LEDE), "36px 32px 8px"),
                     row(p["details"], "24px 32px 4px"),
                     row(p["button"], "24px 32px 8px"),
                     row(p["found"], "28px 32px 8px")], note=_INVITE_NOTE)


def invite_with_teaser(email: str, company: str, teaser_rows: list[str], teaser_text: list[str],
                       cite_note: str = "") -> tuple[str, str, str]:
    """(subject, html, text) of the welcome email with a first look at one
    account, which `tools/tick.py users invite --teaser` sends in place of
    Cognito's. The account comes first; the welcome and the sign-in details
    close it, so the button is the last thing read. teaser_rows and
    teaser_text come from tools/teaser.py (email_rows(c, nested=True),
    email_text(c)). {####} is left in both for the temporary password, which
    is only made when the email is sent."""
    p = _invite_parts(e(email))
    rule = row(f'<div style="border-top:1px solid {BORDER};font-size:0;line-height:0">&nbsp;</div>', "32px 32px 0")
    subject = f"{INVITE_SUBJECT} and a first look at {company}"
    note = (f"{cite_note}<br><br>" if cite_note else "") + _INVITE_NOTE
    body = document("Welcome to TICK",
                    f"A first look at {company}, and your sign-in details for TICK.",
                    teaser_rows + [rule,
                                   row(h2("You're in.") + para(_INVITE_LEDE, margin="8px 0 0"), "28px 32px 0"),
                                   row(p["found"], "24px 32px 0"),
                                   row(label("Your sign-in", margin="0 0 10px") + p["details"], "28px 32px 4px"),
                                   row(p["button"], "20px 32px 8px")], note=note)
    text = text_document(
        [f"A FIRST LOOK AT {company.upper()}", *teaser_text, "You're in.", _INVITE_LEDE,
         "What you'll find:\n" + "\n".join(f"- {h} {t}" for h, t in _FOUND),
         f"Your sign-in:\nEmail: {email}\nTemporary password: {{####}}\n{_INVITE_DAYS}",
         f"Sign in: {SITE}/login",
         "Password expired, or not expecting this? Just reply and we'll sort it out."])
    return subject, body, text


def verification() -> str:
    """The code Cognito sends to reset a password or confirm an email address."""
    code = card(f'<div style="{FONT_MONO};font-size:24px;letter-spacing:.12em;color:{TEXT}">{{####}}</div>')
    return document(
        "Your TICK code", "Your TICK code is inside.",
        [row(h1("Your code") + para("Enter this code on the TICK sign-in page to reset your password "
                                    "or confirm your email address."), "36px 32px 8px"),
         row(code, "16px 32px 8px")],
        note=("Didn't ask for a code? You can ignore this email; your password hasn't changed.<br>"
              f'<a href="{SITE}/login" style="{LINK}">Sign in to TICK</a>'))


CONFIRM_SUBJECT = "You're on the TICK list"
_CONFIRM_REASON = ("You're receiving this because this address was entered at tick.sembasolutions.com. "
                   "If that wasn't you, ignore it — you won't hear from us again.")


def waitlist_confirmation() -> tuple[str, str, str]:
    """(subject, html, text) for a new waitlist request. It goes to an
    address anyone can type into a public form, so it never echoes what was
    submitted."""
    lede = ("We've received your request for access. TICK is in beta and we're adding accounts by hand, "
            "so we'll email you when yours is ready.")
    body = document(
        CONFIRM_SUBJECT, "Thanks for your interest in TICK. The demo is open while you wait.",
        [row(h1("You're on the list.") + para(lede), "36px 32px 8px"),
         row(para("In the meantime, the demo is open: a sample account, its dashboard and its weekly brief.",
                  15, TEXT2, "0 0 16px") + button(f"{SITE}/demo", "Open the demo"), "20px 32px 8px")],
        reason=_CONFIRM_REASON)
    text = text_document(["Thanks for your interest in TICK.", lede,
                          f"In the meantime, the demo is open: {SITE}/demo"], _CONFIRM_REASON)
    return CONFIRM_SUBJECT, body, text


def _roles(n: int, word: str = "role") -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def new_accounts(accounts: list[dict], site: str = SITE) -> tuple[str, str, str]:
    """(subject, html, text) telling a user accounts were added for them.

    Each account is {name, slug, open_roles, tech_roles, crawled_on}; the
    last three are None before its first crawl. Only facts from the lake go
    in: what TICK thinks of the account is the brief's job.
    """
    names = [a["name"] for a in accounts]
    if len(names) == 1:
        subject = f"{names[0]} is now in your TICK"
        head = f"{names[0]} is in your TICK."
    else:
        subject = f"{len(names)} accounts added to your TICK"
        head = f"{len(names)} new accounts in your TICK."
    cards, lines = [], []
    for a in accounts:
        url = f"{site}/{a['slug']}/dashboard.html"
        if a.get("open_roles") is None:
            fact = "Its first crawl runs tonight. The dashboard fills in tomorrow, and its brief follows."
            cta = ""
        else:
            fact = (f"{_roles(a['open_roles'])} open on its careers site, "
                    f"{_roles(a['tech_roles'], 'tech role')} among them, as of the last crawl.")
            cta = f'<div style="margin-top:14px">{button(url, "Open the dashboard")}</div>'
        cards.append(row(card(
            f'<div style="{FONT_HEAD};font-size:20px;font-weight:600;color:{TEXT}">{e(a["name"])}</div>'
            f'<div style="font-size:14px;line-height:1.55;color:{TEXT2};margin-top:4px">{e(fact)}</div>' + cta),
            "16px 32px 0"))
        lines.append(f"{a['name']}: {fact}" + ("" if a.get("open_roles") is None else f"\n{url}"))
    lede = ("TICK reads its careers site every night: the dashboard shows what opened and closed, "
            "and the weekly brief says what it means for what you sell.")
    if len(names) > 1:
        lede = lede.replace("its careers site", "their careers sites")
    signin = "Already signed in? Sign out and back in to see it." if len(names) == 1 else \
        "Already signed in? Sign out and back in to see them."
    body = document(
        subject, subject + ".",
        [row(h1(e(head)) + para(lede), "36px 32px 0")] + cards
        + [row(para(signin, 13, TEXT3), "16px 32px 0")],
        note=("Questions, or a company you'd like added? Just reply.<br>"
              f'<a href="{site}/accounts" style="{LINK}">All your accounts</a>'))
    text = text_document([head, lede, *lines, signin,
                          f"All your accounts: {site}/accounts",
                          "Questions, or a company you'd like added? Just reply."])
    return subject, body, text


# ------------------------------------------------------- the Cognito template copy

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "infra", "template.yaml")
_COGNITO = {"invite": "InviteMessageTemplate", "verification": "VerificationMessageTemplate"}


def cognito_bodies() -> dict[str, str]:
    return {"invite": invite(), "verification": verification()}


def _block(lines: list[str], section: str) -> tuple[int, int, int]:
    """Where the EmailMessage literal block under `section` sits:
    (first line of its body, line after it, the key's indent)."""
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{section}:")
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == "EmailMessage: |-":
            indent = len(lines[i]) - len(lines[i].lstrip())
            j = i + 1
            # a literal block runs until the first non-blank line indented no
            # deeper than its key
            while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > indent):
                j += 1
            while j > i + 1 and not lines[j - 1].strip():
                j -= 1
            return i + 1, j, indent
    raise ValueError(f"{section} has no `EmailMessage: |-` block")


def template_body(text: str, which: str) -> str:
    """One Cognito body as template.yaml holds it, as Cognito receives it."""
    lines = text.splitlines()
    a, b, _ = _block(lines, _COGNITO[which])
    depth = min(len(x) - len(x.lstrip()) for x in lines[a:b] if x.strip())
    return "\n".join(x[depth:] for x in lines[a:b])


def template_subject(text: str, which: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{_COGNITO[which]}:")
    for line in lines[start + 1:]:
        if line.strip().startswith("EmailSubject:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    raise ValueError(f"{_COGNITO[which]} has no EmailSubject")


def write_template(path: str = TEMPLATE) -> list[str]:
    """Rewrite each Cognito body in the template from cognito_bodies(); the
    names of those that changed."""
    with open(path) as f:
        text = f.read()
    changed = []
    for which, body in cognito_bodies().items():
        if template_body(text, which) == body:
            continue
        lines = text.splitlines()
        a, b, indent = _block(lines, _COGNITO[which])
        pad = " " * (indent + 2)
        lines[a:b] = [pad + x if x else "" for x in body.splitlines()]
        text = "\n".join(lines) + "\n"
        changed.append(which)
    if changed:
        with open(path, "w") as f:
            f.write(text)
    return changed


def main(argv: list[str]) -> int:
    if argv[:1] != ["cognito"] or len(argv) != 2 or argv[1] not in ("invite", "verification", "--write"):
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    if argv[1] == "--write":
        changed = write_template()
        print(f"Rewrote {', '.join(changed)} in infra/template.yaml" if changed else "Already in step.")
        return 0
    print(cognito_bodies()[argv[1]])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
