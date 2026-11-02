"""
TICK site auth — the OIDC half of the hosted site's front door.

  /auth/login     302 to the Cognito Hosted UI
  /auth/callback  code -> tokens -> group check -> session cookie -> 302 home
  /auth/logout    clears the cookie, 302 to the Hosted UI logout

Signing in lands on HOME_PATH ("/accounts"); signing out lands on "/", the
public marketing page, which is what a viewer without a session is meant to
see. "/login" is the way in, not a page: the edge sends a signed-in viewer on
to /accounts and a signed-out one here.

The app's pages are guarded at the edge instead: the CloudFront
Function checks a short HMAC-signed session cookie this Lambda mints, which is
all an edge function can do (it cannot verify an RS256 JWT or call a network).
So the expensive, once-per-session work happens here, and the per-request check
costs a single HMAC at the edge.

The cookie is `<sub>:<scope>:<exp>.<hmac-sha256 hex>` over `<sub>:<scope>:<exp>`,
signed with SESSION_SECRET — the same secret compiled into the CloudFront
Function. <scope> is the accounts this viewer may open, fixed at sign-in: "*"
for members of ADMIN_GROUP, otherwise the user's `custom:accounts` attribute
(comma-separated company slugs, set by an admin) normalised to sorted,
de-duplicated slugs — possibly empty. The edge refuses any /<slug>/ page
outside it and serves /accounts from the home page the publisher rendered for
exactly that scope, so a change of accounts takes effect at the viewer's next
sign-in.

Config that depends on the CloudFront distribution (this Lambda's own origin)
is read from SSM at cold start rather than passed as an environment variable:
the distribution cannot be built until this function exists, so a build-time
reference would be circular.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
import urllib.request

import boto3

USER_POOL_ID = os.environ["USER_POOL_ID"]
COGNITO_DOMAIN = os.environ["COGNITO_DOMAIN"]        # prefix only
AWS_REGION = os.environ["AWS_REGION"]
VIEWER_GROUP = os.environ["VIEWER_GROUP"]
ADMIN_GROUP = os.environ["ADMIN_GROUP"]
SESSION_SECRET = os.environ["SESSION_SECRET"].encode()
CONFIG_PARAM = os.environ["CONFIG_PARAM"]

SESSION_TTL = 12 * 3600      # a working day; expiry re-runs the silent Cognito redirect
STATE_TTL = 600              # a login round trip
HOME_PATH = "/accounts"      # the app's front page; "/" is the public marketing page

_ISSUER = f"https://cognito-idp.{AWS_REGION}.amazonaws.com/{USER_POOL_ID}"
_HOSTED_UI = f"https://{COGNITO_DOMAIN}.auth.{AWS_REGION}.amazoncognito.com"

_config: dict | None = None


def config() -> dict:
    """{client_id, client_secret, site_url}, resolved once per container."""
    global _config
    if _config is None:
        raw = boto3.client("ssm").get_parameter(Name=CONFIG_PARAM)["Parameter"]["Value"]
        cfg = json.loads(raw)
        described = boto3.client("cognito-idp").describe_user_pool_client(
            UserPoolId=USER_POOL_ID, ClientId=cfg["client_id"])["UserPoolClient"]
        cfg["client_secret"] = described["ClientSecret"]
        _config = cfg
    return _config


# ------------------------------------------------------------------ signing

def _sign(payload: str) -> str:
    return hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def _verify(payload: str, signature: str) -> bool:
    return hmac.compare_digest(_sign(payload), signature)


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def make_state(next_path: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"n": next_path, "t": int(time.time())}).encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload)}"


def read_state(state: str) -> str:
    """The path to land on after login. Anything unsigned, stale or pointing
    off-site falls back to the app's home page rather than being followed.

    Never "/" — that is the public marketing page, so a viewer who has just
    signed in would be dropped back on the page they signed in from — and
    never "/login", the way in, which only bounces on to /accounts."""
    payload, _, signature = (state or "").rpartition(".")
    if not payload or not _verify(payload, signature):
        return HOME_PATH
    try:
        data = json.loads(_b64url_decode(payload))
    except ValueError:
        return HOME_PATH
    if int(time.time()) - data.get("t", 0) > STATE_TTL:
        return HOME_PATH
    nxt = data.get("n", HOME_PATH)
    # only same-site absolute paths — never "//evil.com" or "https://evil.com"
    if not nxt.startswith("/") or nxt.startswith("//"):
        return HOME_PATH
    # The edge serves the public pages without ever redirecting here, so a
    # "next" pointing at one is a path this flow should never have been given.
    return HOME_PATH if nxt in ("/", "/index.html", "/login", "/login/") else nxt


_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


def account_scope(groups: list[str], accounts: str) -> str:
    """The cookie's <scope>. The publisher derives the same string from the
    same user to name each home page, so the two must never disagree — keep
    this in step with account_scope() in ../publisher/app.py. Anything that
    is not a plain slug is dropped, so the attribute can never smuggle a ':'
    or ',' into the signed payload."""
    if ADMIN_GROUP in groups:
        return "*"
    slugs = {s.strip().lower() for s in (accounts or "").split(",")}
    return ",".join(sorted(s for s in slugs if _SLUG_RE.match(s)))


# ------------------------------------------------------------------ responses

def redirect(location: str, cookies: list[str] | None = None) -> dict:
    resp = {"statusCode": 302, "headers": {"location": location, "cache-control": "no-store"}}
    if cookies:
        resp["cookies"] = cookies
    return resp


# The menu bar and tab icon of infra/site-static/demo/, cut down to the brand
# and Docs: which way out is right depends on the error, so the page's own link
# carries it. A third copy, with tools/tick_insights_report.py; see CLAUDE.md.
_FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Cstyle%3E.p%7Bstroke:%23456e99%7D@media(prefers-color-scheme:dark)%7B.p%7Bstroke:%2386aace%7D%7D%3C/style%3E"
    "%3Cpath class='p' d='M2 19h6.5l4-12 4.5 17 3.5-10H30' fill='none' stroke-width='3'"
    " stroke-linejoin='miter' stroke-linecap='square'/%3E%3C/svg%3E"
)
_NAV = ('<div class="nav"><a class="nav-brand" href="/"><svg viewBox="0 0 32 32" aria-hidden="true" '
        'focusable="false"><path d="M2 19h6.5l4-12 4.5 17 3.5-10H30" fill="none" stroke="currentColor" '
        'stroke-width="3" stroke-linejoin="miter" stroke-linecap="square"/></svg><span>TICK</span></a>'
        '<div class="nav-tabs"><a href="/docs">Docs</a></div></div>')


def page(status: int, title: str, message: str, link: tuple[str, str] | None = None) -> dict:
    href = f'<p><a href="{link[0]}">{link[1]}</a></p>' if link else ""
    return {
        "statusCode": status,
        "headers": {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"},
        "body": f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TICK — {title}</title>
<meta name="theme-color" content="#080d12"><link rel="icon" href="{_FAVICON}">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;700&amp;family=Barlow+Condensed:wght@400;600;700&amp;display=swap">
<style>body{{margin:0;min-height:100vh;display:flex;flex-direction:column;
background:#f2f2f3;color:#1d1f20;font-family:"Barlow",system-ui,sans-serif;line-height:1.55}}
.nav{{display:flex;align-items:center;gap:28px;padding:16px 32px;background:#080d12;
border-bottom:1px solid rgba(158,186,214,.14)}}
.nav-brand{{display:inline-flex;align-items:center;gap:10px;font-family:"Barlow Condensed",system-ui,sans-serif;
font-weight:700;font-size:25px;letter-spacing:.14em;color:#eef2f5;text-decoration:none}}
.nav-brand svg{{display:block;width:26px;height:26px;flex:none;color:#86aace}}
.nav-tabs{{display:flex;align-items:center;gap:28px;min-height:48px}}.nav-tabs a{{color:#9aa9b8;text-decoration:none;font-size:18px}}
.nav-tabs a:hover{{color:#eef2f5}}
.center{{flex:1;display:flex;align-items:center;justify-content:center}}
main{{max-width:34rem;margin:2rem;padding:2rem;border:1px solid rgba(29,31,32,.16)}}
h1{{font-size:1.4rem;margin:0 0 .6rem}}p{{margin:.6rem 0 0;font-size:.95rem}}
main a{{color:#5980a6}}</style></head><body>{_NAV}<div class="center"><main><h1>{title}</h1>
<p>{message}</p>{href}</main></div></body></html>""",
    }


# ------------------------------------------------------------------ oidc flow

def login(next_path: str) -> dict:
    cfg = config()
    query = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": f'{cfg["site_url"]}/auth/callback',
        "state": make_state(next_path),
    })
    return redirect(f"{_HOSTED_UI}/oauth2/authorize?{query}")


def exchange_code(code: str) -> dict:
    cfg = config()
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": cfg["client_id"],
        "code": code,
        "redirect_uri": f'{cfg["site_url"]}/auth/callback',
    }).encode()
    basic = base64.b64encode(f'{cfg["client_id"]}:{cfg["client_secret"]}'.encode()).decode()
    request = urllib.request.Request(f"{_HOSTED_UI}/oauth2/token", data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("Authorization", f"Basic {basic}")
    with urllib.request.urlopen(request, timeout=10) as resp:
        return json.loads(resp.read())


def id_claims(id_token: str) -> dict:
    """Claims from an ID token this function just fetched itself, over TLS, from
    the Cognito token endpoint, authenticated with the client secret. Per OIDC
    Core 3.1.3.7, a token obtained that way needs no signature check — the
    channel is the proof. The claims below are still checked, because the
    channel says nothing about who the token was minted for."""
    cfg = config()
    claims = json.loads(_b64url_decode(id_token.split(".")[1]))
    if claims.get("aud") != cfg["client_id"]:
        raise ValueError("id token minted for another client")
    if claims.get("iss") != _ISSUER:
        raise ValueError("id token from another issuer")
    if claims.get("token_use") != "id":
        raise ValueError("not an id token")
    if claims.get("exp", 0) <= time.time():
        raise ValueError("id token already expired")
    return claims


def callback(params: dict) -> dict:
    if params.get("error"):
        return page(400, "Sign-in failed",
                    f'Cognito returned <code>{params["error"]}</code>.', ("/auth/login", "Try again"))
    code = params.get("code")
    if not code:
        return redirect("/auth/login")

    claims = id_claims(exchange_code(code)["id_token"])

    groups = claims.get("cognito:groups") or []
    if VIEWER_GROUP not in groups and ADMIN_GROUP not in groups:
        # Authenticated but not entitled: a real pool user who has not been
        # granted TICK. Say so plainly rather than bouncing them to a login
        # page they would pass again and again.
        return page(403, "No access to TICK",
                    f'You are signed in as <strong>{claims.get("email", "this account")}</strong>, '
                    f'but you are not a member of the <code>{VIEWER_GROUP}</code> group. '
                    "Ask an administrator to add you.", ("/auth/logout", "Sign out"))

    expires = int(time.time()) + SESSION_TTL
    scope = account_scope(groups, claims.get("custom:accounts", ""))
    payload = f'{claims["sub"]}:{scope}:{expires}'
    cookie = (f"tick_session={payload}.{_sign(payload)}; Path=/; Secure; HttpOnly; "
              f"SameSite=Lax; Max-Age={SESSION_TTL}")
    return redirect(read_state(params.get("state", "")), cookies=[cookie])


def logout() -> dict:
    cfg = config()
    cleared = "tick_session=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0"
    query = urllib.parse.urlencode({"client_id": cfg["client_id"], "logout_uri": f'{cfg["site_url"]}/'})
    return redirect(f"{_HOSTED_UI}/logout?{query}", cookies=[cleared])


def handler(event, context):
    path = event.get("rawPath", "/")
    params = event.get("queryStringParameters") or {}
    try:
        if path == "/auth/login":
            return login(params.get("next", HOME_PATH))
        if path == "/auth/callback":
            return callback(params)
        if path == "/auth/logout":
            return logout()
    except Exception as exc:                          # noqa: BLE001
        print(f"auth error on {path}: {exc!r}")
        return page(502, "Sign-in failed",
                    "Something went wrong talking to Cognito.", ("/auth/login", "Try again"))
    return page(404, "Not found", "No such page.", ("/", "Back to TICK"))
