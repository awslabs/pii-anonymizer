# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Batch Trigger Lambda - Scans S3 input bucket for unprocessed files,
groups by folder at configurable depth, sends one SQS message per job.

EventBridge Schedule → Batch Trigger → SQS → Router → SF

Job grouping (controlled by JOB_FOLDER_DEPTH env var):
  - Depth 1: top-level folder = one job (default)
  - Depth 2: second-level subfolder = one job
  - Depth 3: third-level subfolder = one job (e.g. sponsor/policy/claim)
  - Files in root (no folder) → each file is a separate job

Skip logic (DDB as source of truth):
  - No DDB record → process (new)
  - COMPLETE → skip
  - FAILED → process (retry)
  - Active status and not stale → skip
  - Active status and stale (>30min) → process (reclaim, mark old as FAILED)
"""

import json
import os
import logging
from collections import defaultdict
from datetime import datetime

import boto3

from helpers.observability import init_tracing

init_tracing()  # X-Ray: trace AWS SDK calls as subsegments (no-op if SDK absent)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
sqs = boto3.client("sqs")
ddb = boto3.resource("dynamodb")

INPUT_BUCKET = os.environ["S3_INPUT_BUCKET_NAME"]
OUTPUT_BUCKET = os.environ["S3_OUTPUT_BUCKET_NAME"]
QUEUE_URL = os.environ["SQS_QUEUE_URL"]
SCAN_PREFIX = os.environ.get("SCAN_PREFIX", "")
MAX_BATCH_MESSAGES = int(os.environ.get("MAX_BATCH_MESSAGES", "50"))
TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "")
JOB_FOLDER_DEPTH = int(os.environ.get("JOB_FOLDER_DEPTH", "1"))

ACTIVE_STATUSES = {
    "QUEUED",
    "IN_PROGRESS",
    "DETECTING",
    "DETECT_COMPLETE",
    "GENERATING_SYNTHETIC",
    "REDACTING",
    "REDACT_COMPLETE",
}
STALE_SECONDS = 30 * 60


def get_latest_record(job_id):
    """Query DDB for latest record. Returns (status, updated_at, timestamp) or (None, None, None)."""
    if not TABLE_NAME:
        return None, None, None
    try:
        table = ddb.Table(TABLE_NAME)
        resp = table.query(
            KeyConditionExpression="filename = :f",
            ExpressionAttributeValues={":f": job_id},
            ScanIndexForward=False,
            Limit=1,
            ProjectionExpression="#s, updated_at, #ts",
            ExpressionAttributeNames={"#s": "status", "#ts": "timestamp"},
        )
        items = resp.get("Items", [])
        if not items:
            return None, None, None
        return (
            items[0].get("status"),
            items[0].get("updated_at"),
            items[0].get("timestamp"),
        )
    except Exception as e:
        logger.warning(f"DDB lookup failed for {job_id}: {e}")
        return None, None, None


def should_process(job_id):
    """Check DDB to decide if job needs processing."""
    status, updated_at, record_ts = get_latest_record(job_id)

    if status is None:
        logger.info(f"Process {job_id}: new")
        return True

    if status == "COMPLETE":
        logger.info(f"Skip {job_id}: COMPLETE")
        return False

    if status == "FAILED":
        logger.info(f"Process {job_id}: FAILED, retry")
        return True

    if status in ACTIVE_STATUSES:
        if updated_at:
            try:
                age = (
                    datetime.now() - datetime.fromisoformat(updated_at)
                ).total_seconds()
                if age > STALE_SECONDS:
                    logger.info(f"Process {job_id}: stale {status} ({age:.0f}s)")
                    if TABLE_NAME and record_ts:
                        try:
                            ddb.Table(TABLE_NAME).update_item(
                                Key={"filename": job_id, "timestamp": record_ts},
                                UpdateExpression="SET #s = :s",
                                ExpressionAttributeNames={"#s": "status"},
                                ExpressionAttributeValues={":s": "FAILED"},
                            )
                        except Exception:
                            pass
                    return True
            except Exception:
                pass
        logger.info(f"Skip {job_id}: {status}")
        return False

    logger.info(f"Process {job_id}: unknown status={status}")
    return True


def handler(event, context):
    """Scan input bucket, group files by folder depth, send to SQS."""
    prefix = SCAN_PREFIX.rstrip("/") + "/" if SCAN_PREFIX else ""
    logger.info(f"Scanning s3://{INPUT_BUCKET}/{prefix} (depth={JOB_FOLDER_DEPTH})")

    paginator = s3.get_paginator("list_objects_v2")
    jobs = defaultdict(list)
    root_files = []

    for page in paginator.paginate(Bucket=INPUT_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or key.endswith(".DS_Store"):
                continue

            rel = key[len(prefix) :] if prefix else key
            parts = rel.split("/")

            if len(parts) <= JOB_FOLDER_DEPTH:
                # File not deep enough — treat as individual job
                root_files.append(key)
                continue

            job_folder = "/".join(parts[:JOB_FOLDER_DEPTH])
            job_id = f"{prefix}{job_folder}" if prefix else job_folder
            jobs[job_id].append(key)

    # Root files: each is its own job
    for f in root_files:
        name = os.path.splitext(os.path.basename(f))[0]
        jobs[name] = [f]

    if not jobs:
        logger.info("No files found")
        return {"statusCode": 200, "body": "No files found"}

    logger.info(
        f"Found {len(jobs)} jobs with {sum(len(f) for f in jobs.values())} total files"
    )

    sent = 0
    total_files = 0
    skipped = 0
    for job_id, files in sorted(jobs.items()):
        if sent >= MAX_BATCH_MESSAGES:
            logger.info(f"Hit max {MAX_BATCH_MESSAGES}, remaining on next run")
            break
        if not should_process(job_id):
            skipped += 1
            continue
        ts = datetime.now().isoformat()
        if TABLE_NAME:
            try:
                ddb.Table(TABLE_NAME).put_item(
                    Item={
                        "filename": job_id,
                        "timestamp": ts,
                        "status": "QUEUED",
                        "updated_at": ts,
                    }
                )
            except Exception as e:
                logger.error(f"Failed to mark {job_id} as QUEUED: {e}, skipping")
                continue
        try:
            sqs.send_message(
                QueueUrl=QUEUE_URL,
                MessageBody=json.dumps(
                    {
                        "source": "batch-trigger",
                        "bucket": INPUT_BUCKET,
                        "job_id": job_id,
                        "files": files,
                        "timestamp": ts,
                    }
                ),
            )
        except Exception as e:
            logger.error(f"SQS send failed for {job_id}: {e}")
            continue
        sent += 1
        total_files += len(files)
        logger.info(f"Queued {job_id}: {len(files)} files")

    logger.info(f"Sent {total_files} files in {sent} jobs, skipped {skipped}")
    return {
        "statusCode": 200,
        "body": f"{total_files} files, {sent} jobs, {skipped} skipped",
    }
