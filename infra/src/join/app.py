"""
TICK's forms post here: the public waitlist, and signed-in viewers asking for
a company to be tracked.

  POST /api/join             {email, name, company}  -> 200 {"ok": true}
                                                     -> 400 {"error": ...} on bad input
  POST /api/account-request  {company, careers_url?, note?}
                                                     -> 200 {"ok": true}
                                                     -> 401 without a valid session

Served through the site's own CloudFront as /api/join, so the forms post
same-origin: no CORS, and the response is readable, so a failure is shown as a
failure instead of the old no-cors "success either way". The function URL only
accepts CloudFront's OAC-signed requests; for a POST body that signature needs
the payload hash, which is why the forms send x-amz-content-sha256.

One row per email. A repeat submission is accepted but not rewritten, so the
first request (and its timestamp) is what's on record, and only the first one
notifies — the alerts topic, and a confirmation to the requester.

The confirmation goes to an address anyone can type into a public form, so it
never echoes what they submitted (name, company): it can't be turned into a way
to send arbitrary text from the company domain, and each address gets at most
one, ever.
"""

import hashlib
import hmac
import json
import os
import re
import time
import uuid

import boto3
from botocore.exceptions import ClientError

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")
SNS = boto3.client("sns") if TOPIC_ARN else None
SENDER = os.environ.get("SENDER", "")              # "TICK <support@...>"
SES = boto3.client("sesv2") if SENDER else None

# Account requests — only for signed-in viewers, so this side verifies the same
# session cookie the edge does (see ../auth/app.py for its format).
ACCOUNT_TABLE = (boto3.resource("dynamodb").Table(os.environ["ACCOUNT_TABLE_NAME"])
                 if os.environ.get("ACCOUNT_TABLE_NAME") else None)
SESSION_SECRET = os.environ.get("SESSION_SECRET", "").encode()
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
SUB_RE = re.compile(r"^[0-9a-f-]{36}$")
URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)

EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,190}\.[^\s@]{2,63}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def respond(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body),
    }


def clean(value, limit: int = 120) -> str:
    return CONTROL.sub("", str(value or "")).strip()[:limit]


def notify(item: dict) -> None:
    SNS.publish(
        TopicArn=TOPIC_ARN,
        Subject="TICK: new access request",   # fixed — never built from input
        Message=(
            "TICK access request received.\n\n"
            f"Email:   {item['email']}\n"
            f"Name:    {item['name']}\n"
            f"Company: {item['company']}\n"
            f"At:      {item['created_at']}\n\n"
            "Grant access: tools/tick.py users invite <email> --accounts <slugs>\n"
            "Every request: tools/tick.py requests list\n"
        ),
    )


CONFIRM_SUBJECT = "You're on the TICK list"
CONFIRM_TEXT = (
    "Thanks for your interest in TICK.\n\n"
    "We've received your request for access. TICK is in beta and we're adding "
    "accounts by hand, so we'll email you when yours is ready.\n\n"
    "In the meantime, the demo is open: https://tick.sembasolutions.com/demo\n\n"
    "— TICK, Semba Solutions\n"
    "You're receiving this because this address was entered at "
    "tick.sembasolutions.com. If that wasn't you, ignore it — you won't hear from us again.\n"
)


def confirm(email: str) -> None:
    SES.send_email(
        FromEmailAddress=SENDER,
        Destination={"ToAddresses": [email]},
        Content={"Simple": {
            "Subject": {"Data": CONFIRM_SUBJECT},
            "Body": {"Text": {"Data": CONFIRM_TEXT}},
        }},
    )


def session_sub(event) -> str | None:
    """The Cognito sub from a valid tick_session cookie, or None. Function URLs
    hand cookies over as a list of "name=value" strings."""
    if not SESSION_SECRET:
        return None
    for raw in event.get("cookies") or []:
        name, _, value = raw.partition("=")
        if name.strip() != "tick_session":
            continue
        payload, _, signature = value.strip().rpartition(".")
        expected = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
        parts = payload.split(":")
        if (payload and hmac.compare_digest(expected, signature) and len(parts) == 3
                and parts[2].isdigit() and int(parts[2]) > time.time() and SUB_RE.match(parts[0])):
            return parts[0]
    return None


def requester_email(sub: str) -> str:
    """Best effort — the request is still worth keeping without it."""
    if not USER_POOL_ID:
        return ""
    try:
        users = boto3.client("cognito-idp").list_users(
            UserPoolId=USER_POOL_ID, Filter=f'sub = "{sub}"', Limit=1)["Users"]
    except ClientError as exc:
        print(f"account-request: email lookup failed for {sub}: {exc!r}")
        return ""
    attrs = {a["Name"]: a["Value"] for a in (users[0]["Attributes"] if users else [])}
    return attrs.get("email", "")


def notify_account(item: dict) -> None:
    SNS.publish(
        TopicArn=TOPIC_ARN,
        Subject="TICK: new account request",  # fixed — never built from input
        Message=(
            "A TICK viewer asked for a company to be tracked.\n\n"
            f"From:        {item['requester_email'] or item['requester_sub']}\n"
            f"Company:     {item['company']}\n"
            f"Careers URL: {item['careers_url'] or '-'}\n"
            f"Note:        {item['note'] or '-'}\n"
            f"At:          {item['created_at']}\n\n"
            "Onboard it with the tick-evaluator skill and tools/tick.py companies push,\n"
            "then tools/tick.py users grant <email> <slug>.\n"
            "Every request: tools/tick.py requests list --accounts\n"
        ),
    )


def account_request(event, body: dict) -> dict:
    if ACCOUNT_TABLE is None:
        return respond(404, {"error": "Not found."})
    sub = session_sub(event)
    if not sub:
        return respond(401, {"error": "Sign in again to send a request."})

    company = clean(body.get("company"))
    if not company:
        return respond(400, {"error": "Name the company you want tracked."})
    careers_url = clean(body.get("careers_url"), 500)
    if careers_url and not URL_RE.match(careers_url):
        return respond(400, {"error": "The careers URL must start with http:// or https://."})

    item = {
        "request_id": str(uuid.uuid4()),
        "requester_sub": sub,
        "requester_email": requester_email(sub),
        "company": company,
        "careers_url": careers_url,
        "note": clean(body.get("note"), 1000),
        "status": "pending",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        ACCOUNT_TABLE.put_item(Item=item)
    except ClientError as exc:
        print(f"account-request: write failed for {sub}: {exc!r}")
        return respond(500, {"error": "Couldn't save the request."})

    if SNS:
        try:
            notify_account(item)
        except ClientError as exc:
            print(f"account-request: notify failed for {sub}: {exc!r}")
    return respond(200, {"ok": True})


def handler(event, context):
    if event.get("requestContext", {}).get("http", {}).get("method") != "POST":
        return respond(405, {"error": "POST only"})

    try:
        body = json.loads(event.get("body") or "{}")
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        return respond(400, {"error": "Body must be a JSON object."})

    if event.get("rawPath") == "/api/account-request":
        return account_request(event, body)

    email = clean(body.get("email"), 254).lower()
    if not EMAIL_RE.match(email):
        return respond(400, {"error": "Enter a valid email address."})

    item = {
        "email": email,
        "name": clean(body.get("name")),
        "company": clean(body.get("company")),
        "status": "pending",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    try:
        TABLE.put_item(Item=item, ConditionExpression="attribute_not_exists(email)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return respond(200, {"ok": True})     # already on the list
        print(f"join: write failed for {email}: {exc!r}")
        return respond(500, {"error": "Couldn't save the request."})

    # The request is stored; a missed email shouldn't fail the form.
    if SNS:
        try:
            notify(item)
        except ClientError as exc:
            print(f"join: notify failed for {email}: {exc!r}")
    if SES:
        try:
            confirm(email)
        except ClientError as exc:
            print(f"join: confirmation failed for {email}: {exc!r}")

    return respond(200, {"ok": True})
