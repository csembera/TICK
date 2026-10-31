"""
TICK waitlist — the "Get started" / "Request access" forms post here.

  POST /api/join  {email, name, company}  -> 200 {"ok": true}
                                          -> 400 {"error": ...} on bad input

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

import json
import os
import re
import time

import boto3
from botocore.exceptions import ClientError

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")
SNS = boto3.client("sns") if TOPIC_ARN else None
SENDER = os.environ.get("SENDER", "")              # "TICK <support@...>"
SES = boto3.client("sesv2") if SENDER else None

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
            "Grant access: create the user in TICK's Cognito user pool and add them\n"
            "to the 'tick-viewers' group. List every request: make list-requests\n"
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


def handler(event, context):
    if event.get("requestContext", {}).get("http", {}).get("method") != "POST":
        return respond(405, {"error": "POST only"})

    try:
        body = json.loads(event.get("body") or "{}")
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        return respond(400, {"error": "Body must be a JSON object."})

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
