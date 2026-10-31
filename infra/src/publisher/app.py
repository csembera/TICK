"""
TICK publisher — renders the S3 lake into the static site CloudFront serves.

  S3 lake (private)  ──►  this Lambda  ──►  S3 site bucket  ──►  CloudFront
  derived/ + configs/     renders HTML       home/<key>/index.html (Cognito sign-in)
                                             <slug>/dashboard.html
                                             <slug>/brief.html

Viewers see only the accounts they are assigned, so there is no single
accounts home. Each viewer's session carries a scope — "*" for admins, else
the sorted slugs in their `custom:accounts` Cognito attribute — and the edge
serves /login from home/<key>/index.html, key = first 16 hex of
sha256(scope). This Lambda reads the pool's users and renders one home per
distinct scope, so an assignment change shows after the next publish (and
the viewer's next sign-in, which re-mints the scope).

The public pages at /, /docs and /demo are NOT written here. They are the
hand-authored marketing pages in ../../site-static/, deployed by `make
publish-static`, and this Lambda must never overwrite them — which is why it
writes the accounts homes under home/ and never touches index.html.

Runs on its own schedule, half an hour behind the daily dispatcher, so every
company has finished collecting before the site is rebuilt. Nothing is
rendered from the site bucket's previous contents — each publish is a full
re-render of the lake as it stands, so a deleted account or a rolled-back
snapshot cannot leave a stale page behind.

The rendering itself is `tools/tick_insights_report.py`, symlinked here as
`report.py` — the deployed renderer is always the repo's renderer, never a
drifted copy.
"""

import hashlib
import os
import re
import time

import boto3

import report

DATA_BUCKET = os.environ["DATA_BUCKET"]
SITE_BUCKET = os.environ["SITE_BUCKET"]
DISTRIBUTION_ID = os.environ.get("DISTRIBUTION_ID", "")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
VIEWER_GROUP = os.environ.get("VIEWER_GROUP", "tick-viewers")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "tick-admins")

# Short enough that a viewer reloading after a publish sees fresh numbers even
# if the invalidation is still propagating; long enough to keep CloudFront in
# front of S3 for a page people refresh.
_CACHE_CONTROL = "public, max-age=300"

s3 = boto3.client("s3")


_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


def account_scope(groups: list[str], accounts: str) -> str:
    """A viewer's scope — keep in step with account_scope() in ../auth/app.py,
    which puts the same string in the session cookie."""
    if ADMIN_GROUP in groups:
        return "*"
    slugs = {s.strip().lower() for s in (accounts or "").split(",")}
    return ",".join(sorted(s for s in slugs if _SLUG_RE.match(s)))


def scope_key(scope: str) -> str:
    """The home page's directory name; the CloudFront Function computes the
    same hash from the cookie to find it."""
    return hashlib.sha256(scope.encode()).hexdigest()[:16]


def viewer_scopes() -> set[str]:
    """Every distinct scope a viewer in the pool can sign in with. "*" and ""
    are always included, so the admin home and the no-accounts home exist
    before anyone holds them."""
    scopes = {"*", ""}
    if not USER_POOL_ID:
        return scopes
    idp = boto3.client("cognito-idp")

    def members(group: str) -> list[dict]:
        return [u for page in idp.get_paginator("list_users_in_group").paginate(
                    UserPoolId=USER_POOL_ID, GroupName=group) for u in page["Users"]]

    # list_users_in_group returns each user's attributes too, so two group
    # listings cover everyone who can sign in — no per-user calls.
    admins = {u["Username"] for u in members(ADMIN_GROUP)}
    for user in members(VIEWER_GROUP):
        if user["Username"] in admins:
            continue
        attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
        scopes.add(account_scope([], attrs.get("custom:accounts", "")))
    return scopes


def render_site(source: str, scopes: set[str] | None = None) -> dict[str, str]:
    """{key: html} for the guarded half of the site — one accounts home per
    viewer scope plus a dashboard and brief for every account with two
    snapshots to compare. The public pages are deployed separately and are
    never keys here."""
    lake_io = report.open_lake(source)
    accounts = report.build_accounts(report.load_configs(None, lake_io), lake_io)
    if not accounts:
        raise RuntimeError(f"no SourceConfigs and no snapshots at {source} — nothing to publish")

    pages = {}
    for scope in scopes if scopes is not None else viewer_scopes():
        if scope == "*":
            pages[f"home/{scope_key(scope)}/index.html"] = report.render_home(accounts)
        else:
            allowed = set(scope.split(",")) if scope else set()
            pages[f"home/{scope_key(scope)}/index.html"] = report.render_home(
                [a for a in accounts if a["slug"] in allowed])
    for account in accounts:
        if not account["has_pages"]:
            continue
        slug = account["slug"]
        lake = report.load_lake(source, slug, lake_io)
        pages[f"{slug}/dashboard.html"] = report.render_dashboard(lake, slug)
        pages[f"{slug}/brief.html"] = report.render_brief(lake, slug)
    return pages


def publish(pages: dict[str, str]) -> None:
    for key, html in pages.items():
        s3.put_object(Bucket=SITE_BUCKET, Key=key, Body=html.encode("utf-8"),
                      ContentType="text/html; charset=utf-8", CacheControl=_CACHE_CONTROL)


def invalidate() -> str | None:
    """Best effort — a published site with a stale edge cache is still a
    published site, so a failure here must not fail the run."""
    if not DISTRIBUTION_ID:
        return None
    try:
        resp = boto3.client("cloudfront").create_invalidation(
            DistributionId=DISTRIBUTION_ID,
            InvalidationBatch={"Paths": {"Quantity": 1, "Items": ["/*"]},
                               "CallerReference": str(time.time())})
        return resp["Invalidation"]["Id"]
    except Exception as exc:                      # noqa: BLE001 — reported, not raised
        print(f"invalidation failed (site is published; edges expire in 300s): {exc}")
        return None


def handler(event, context):
    source = f"s3://{DATA_BUCKET}"
    pages = render_site(source)
    publish(pages)
    result = {"accounts_rendered": sum(1 for k in pages if k.endswith("/dashboard.html")),
              "homes_rendered": sum(1 for k in pages if k.startswith("home/")),
              "pages": len(pages),
              "site_bucket": SITE_BUCKET,
              "invalidation": invalidate()}
    print(result)
    return result


if __name__ == "__main__":
    # Local dry run against the real lake: writes to ./out-site instead of S3.
    #   DATA_BUCKET=<bucket> SITE_BUCKET=- python app.py
    import pathlib
    out = pathlib.Path(os.environ.get("LOCAL_OUTPUT_DIR", "out-site"))
    for key, html in render_site(f"s3://{DATA_BUCKET}").items():
        path = out / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html)
        print(f"wrote {path}")
