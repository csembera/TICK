"""
TICK publisher — renders the S3 lake into the static site CloudFront serves.

  S3 lake (private)  ──►  this Lambda  ──►  S3 site bucket  ──►  CloudFront
  derived/ + configs/     renders HTML       login/index.html    (Cognito sign-in)
                                             <slug>/dashboard.html
                                             <slug>/brief.html

The public pages at /, /docs and /demo are NOT written here. They are the
hand-authored marketing pages in ../../site-static/, deployed by `make
publish-static`, and this Lambda must never overwrite them — which is why it
writes the accounts home to login/index.html and never touches index.html.

Runs on its own schedule, half an hour behind the daily dispatcher, so every
company has finished collecting before the site is rebuilt. Nothing is
rendered from the site bucket's previous contents — each publish is a full
re-render of the lake as it stands, so a deleted account or a rolled-back
snapshot cannot leave a stale page behind.

The rendering itself is `tools/tick_insights_report.py`, symlinked here as
`report.py` — the deployed renderer is always the repo's renderer, never a
drifted copy.
"""

import os
import time

import boto3

import report

DATA_BUCKET = os.environ["DATA_BUCKET"]
SITE_BUCKET = os.environ["SITE_BUCKET"]
DISTRIBUTION_ID = os.environ.get("DISTRIBUTION_ID", "")

# Short enough that a viewer reloading after a publish sees fresh numbers even
# if the invalidation is still propagating; long enough to keep CloudFront in
# front of S3 for a page people refresh.
_CACHE_CONTROL = "public, max-age=300"

s3 = boto3.client("s3")


def render_site(source: str) -> dict[str, str]:
    """{key: html} for the guarded half of the site — the accounts home plus a
    dashboard and brief for every account with two snapshots to compare. The
    public pages are deployed separately and are never keys here."""
    lake_io = report.open_lake(source)
    accounts = report.build_accounts(report.load_configs(None, lake_io), lake_io)
    if not accounts:
        raise RuntimeError(f"no SourceConfigs and no snapshots at {source} — nothing to publish")

    pages = {"login/index.html": report.render_home(accounts)}
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
