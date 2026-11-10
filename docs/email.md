# Email

Every email TICK sends to a person is built from one layout,
[tools/email_layout.py](../tools/email_layout.py), so they all look and read
alike. The operator alerts (the SNS topic) are plain text for us, and are not
covered here.

| Email | Sent by | When | Body lives in |
|---|---|---|---|
| Welcome (`Welcome to TICK: your sign-in details`) | Cognito | `users invite`, `users resend` | `invite()`, copied into `InviteMessageTemplate` in `infra/template.yaml` |
| Code (`Your TICK verification code`) | Cognito | "Forgot your password?", email confirmation | `verification()`, copied into `VerificationMessageTemplate` |
| Waitlist (`You're on the TICK list`) | join Lambda, SES | the first time an address joins the waitlist | `waitlist_confirmation()`, through the symlink `infra/src/join/email_layout.py` |
| New account (`<Company> is now in your TICK`) | `tools/tick.py`, SES | only when you send a draft from `users grant --email` | `new_accounts()` |
| Teaser | you, pasted into a message | `tools/teaser.py` | `render_email()` in `tools/teaser.py`, on the shared layout |

## The layout

- **Frame:** a 600px card on a grey page. The header is the teaser's
  original: ink, the `TICK` wordmark alone, and a 3px accent rule as its
  bottom border. Under the wordmark, an optional line names who a teaser
  is for. Then come the bands of the body.
- **Preheader:** every email sets the hidden line an inbox shows after the
  subject.
- **Type:** a 30px condensed `h1`, 16px body text (15px in the teaser),
  and the small uppercase `label()` to title every section.
- **Buttons:** `button()` only. The colour goes on the table cell, not on the
  link, because Outlook desktop ignores padding and background on `<a>`.
- **Small print:** inside the card, `note` says how to get help or what to do
  if the email was unexpected. Below the card come `TICK · Semba Solutions`,
  `POSTAL_ADDRESS` once it is set, and `reason`, which says why this address
  got the email. The waitlist and teaser emails need a reason; the others go
  to people who already have an account.
- **Plain text:** anything sent through SES also gets a text part from
  `text_document()`.
- **Never:** no `<style>` blocks, web fonts, SVG or images. Gmail and Outlook
  drop them, so the fonts fall back to Helvetica/Arial.
- **Subjects:** sentence case, naming TICK and what the email is about. The
  `TICK:` prefix is for operator alerts only.

`tests/test_emails.py` checks all of this for every email: tables close,
there is no `<style>`, no `<a>` is painted, there is a sender line and a
preheader.

## Changing an email

Edit `tools/email_layout.py` (or `render_email` in the teaser), then:

```bash
python3 tools/email_layout.py cognito --write   # the invite or code changed: copy them into the template
python3 -m unittest discover tests
```

The Cognito bodies reach users only when the stack deploys; the waitlist
email when the join Lambda does. Both happen when the change merges.
`tests/test_emails.py` fails if the template's copy and the generator
disagree, so you can't change one and forget the other. Never edit the
`EmailMessage` blocks in the template by hand.

`{username}` and `{####}` are Cognito's placeholders, filled unescaped. The
invite needs each exactly once and the code needs `{####}`; each body must be
under 20,000 characters. The invite's password lifetime is `PASSWORD_DAYS`,
which must equal `TemporaryPasswordValidityDays` in the template (tested).

To look at the emails, write them to files and open them in a browser at
600px and at phone width. For the Cognito bodies, replace the placeholders
first.

## The new-account email

Not every grant deserves an email: a grant can be a correction, a trial, or
an admin's bookkeeping. So `users grant` sends nothing unless you ask, and
even then it only drafts:

```bash
tools/tick.py users grant pam@example.com valero --email
#   Drafted "Valero is now in your TICK" to pam@example.com. Nothing is sent yet.
#     Review: local-data/emails/<when>-pam-example-com-accounts/email.html
#     Send:   tools/tick.py users send-email local-data/emails/<when>-pam-example-com-accounts
```

For each account named, the draft says how many roles and tech roles were
open at the newest crawl and links to the dashboard. Before the first crawl,
it says the crawl runs tonight. It reminds the user to sign out and back in,
because the account list is read at sign-in. It holds facts only; what TICK
makes of an account is the brief's job.

`users send-email` sends `email.html` and `email.txt` as they are on disk,
so an edit made in review goes out. It asks first, honours `--dry-run`, and
writes `sent.json` so a draft can't be sent twice. It sends as
`TICK <SenderAddress>` (a stack output) with the operator's AWS session.
If the user already has the account, `--email` still drafts, so a forgotten
flag can be made up for. Drafts are gitignored under `local-data/`, because
they hold a user's address and real numbers.
