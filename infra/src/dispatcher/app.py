"""
TICK dispatcher — fans the daily run out to SQS.

Lists every SourceConfig under s3://<bucket>/configs/, skips any flagged
needs_human_review, and sends one SQS message per company. The collector
Lambda consumes them one at a time (batch size 1), so one bad company can
never fail the rest.
"""

import json
import os

import boto3

s3 = boto3.client("s3")
sqs = boto3.client("sqs")

BUCKET = os.environ["BUCKET_NAME"]
QUEUE_URL = os.environ["QUEUE_URL"]
CONFIG_PREFIX = "configs/"


def handler(event, context):
    dispatched, skipped, invalid = [], [], []

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=CONFIG_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".json"):
                continue
            try:
                body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
                config = json.loads(body)
            except Exception as e:
                invalid.append({"key": key, "error": f"{type(e).__name__}: {e}"})
                continue

            slug = config.get("company_slug") or key
            if config.get("needs_human_review"):
                skipped.append(slug)
                continue

            sqs.send_message(
                QueueUrl=QUEUE_URL,
                MessageBody=json.dumps({"config_key": key, "config": config}),
            )
            dispatched.append(slug)

    summary = {
        "dispatched": dispatched,
        "skipped_for_review": skipped,
        "invalid_configs": invalid,
    }
    print(json.dumps(summary))
    return {
        "dispatched": len(dispatched),
        "skipped": len(skipped),
        "invalid": len(invalid),
    }
