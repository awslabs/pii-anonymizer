# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Router Lambda - SQS consumer that starts Step Functions executions.
Handles concurrency control, folder batching (30s wait), and job status tracking.

SQS → Router → Step Functions
"""

import json
import os
import re
import time
import logging
import urllib.parse
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

from helpers.observability import init_tracing

init_tracing()  # X-Ray: trace AWS SDK calls as subsegments (no-op if SDK absent)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sfn = boto3.client("stepfunctions")
s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")

CONCURRENCY_TABLE = dynamodb.Table(os.environ["CONCURRENCY_TABLE"])
_pk = next(
    k["AttributeName"] for k in CONCURRENCY_TABLE.key_schema if k["KeyType"] == "HASH"
)
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "10"))
OUTPUT_BUCKET = os.environ["S3_OUTPUT_BUCKET_NAME"]
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE_NAME")
FOLDER_WAIT_SECONDS = int(os.environ.get("FOLDER_WAIT_SECONDS", "30"))
COUNTER_ID = "workflow_counter"
SKIP_PREFIXES = ("redacted/", "intermediate/")
SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".docx",
    ".xlsx",
    ".csv",
    ".json",
    ".jpg",
    ".jpeg",
    ".png",
    ".tiff",
    ".tif",
    ".bmp",
    ".webp",
    ".mp3",
    ".wav",
}


def update_counter(increment=True):
    """Atomic concurrency counter. Returns True if update succeeded."""
    try:
        args = {
            "Key": {_pk: COUNTER_ID},
            "UpdateExpression": "ADD active_count :inc",
            "ExpressionAttributeValues": {
                ":inc": 1 if increment else -1,
            },
            "ReturnValues": "UPDATED_NEW",
        }
        if increment:
            args["ExpressionAttributeValues"][":max"] = MAX_CONCURRENT
            args["ConditionExpression"] = (
                "attribute_not_exists(active_count) OR active_count < :max"
            )
        CONCURRENCY_TABLE.update_item(**args)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning("Concurrency limit reached")
            return False
        raise


def update_job_status(job_id, status, timestamp, files=None):
    """Lightweight job status update in mapping table."""
    if not DYNAMODB_TABLE:
        return
    from infra.dynamodb_manager import DynamoDBManager

    mgr = DynamoDBManager(DYNAMODB_TABLE)
    extra = {"failed_files": {"detect": [], "redact": [], "unsupported": []}}
    if files:
        extra["files"] = files
    mgr.update_job_status(job_id, status, extra_attrs=extra, timestamp=timestamp)


def extract_s3_info(record):
    """Extract job info from SQS message. Handles S3 event and batch-trigger formats.
    Returns (bucket, job_id, files_or_none).
    """
    body = json.loads(record["body"])

    if body.get("source") == "batch-trigger":
        return body["bucket"], body["job_id"], body["files"], body.get("timestamp")

    if "Records" not in body:
        return None, None, None, None

    s3_event = body["Records"][0]["s3"]
    bucket = s3_event["bucket"]["name"]
    key = urllib.parse.unquote_plus(s3_event["object"]["key"])
    return bucket, key, None, None


def list_folder_files(bucket, folder_prefix):
    """List all files in a folder prefix."""
    files = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=folder_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/"):
                files.append(key)
    return files


def _get_redaction_mode():
    try:
        from helpers.config_loader import load_config

        config = load_config(use_cache=False)
        return config.get("redaction", {}).get("mode", "synthetic")
    except Exception:
        return "synthetic"


def start_execution(job_id, source_bucket, files, exec_suffix="", timestamp=None):
    """Start SF execution and track job status. Returns True if started, False if already exists."""
    ts = timestamp or datetime.now().isoformat()
    # Clean job_id: no extension for single files, no trailing slash for folders
    clean_id = job_id.rstrip("/")
    if not os.path.dirname(clean_id):
        clean_id = os.path.splitext(clean_id)[0]
    sf_input = {
        "job_id": clean_id,
        "output_prefix": clean_id,
        "timestamp": ts,
        "source_bucket": source_bucket,
        "output_bucket": OUTPUT_BUCKET,
        "redaction_mode": _get_redaction_mode(),
        "files": [
            {
                "source_bucket": source_bucket,
                "source_key": f,
                "output_bucket": OUTPUT_BUCKET,
            }
            for f in files
        ],
    }
    exec_name = re.sub(r"[^a-zA-Z0-9_-]", "_", job_id).strip("_")[:60]
    if exec_suffix:
        exec_name = f"{exec_name}_{exec_suffix[:16]}"
    try:
        execution = sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=exec_name,
            input=json.dumps(sf_input),
        )
        logger.info(f"SF started: {execution['executionArn']}")
        update_job_status(clean_id, "IN_PROGRESS", ts, files=files)
        return True, clean_id, ts
    except sfn.exceptions.ExecutionAlreadyExists:
        logger.info(f"SF execution already exists for {job_id}, skipping")
        return False, clean_id, ts
    except Exception:
        raise


def process_message(record):
    """Process a single SQS message. Returns (success, message_id)."""
    message_id = record["messageId"]
    try:
        source_bucket, source_key, batch_files, batch_ts = extract_s3_info(record)

        if not source_key:
            return True, message_id

        # Batch trigger mode — files list provided, skip wait
        if batch_files:
            job_id = source_key
            files = batch_files
            exec_suffix = message_id
            logger.info(f"Batch trigger: {job_id} with {len(files)} files")
        else:
            # Skip non-document prefixes
            if any(source_key.startswith(p) for p in SKIP_PREFIXES):
                logger.info(f"Skipping {source_key}")
                return True, message_id

            # Folder-level: wait for more files, then list folder
            parts = source_key.split("/")
            if len(parts) > 1:
                # Use top-level folder as job — all nested files become one job
                job_id = parts[0] + "/"
                exec_suffix = datetime.now().strftime("%Y%m%dT%H") + str(
                    datetime.now().minute // 10
                )  # 10-min window for dedup
                logger.info(
                    f"Folder file: {source_key}, waiting {FOLDER_WAIT_SECONDS}s"
                )
                time.sleep(FOLDER_WAIT_SECONDS)
                files = list_folder_files(source_bucket, job_id)
                files = [
                    f for f in files if not any(f.startswith(p) for p in SKIP_PREFIXES)
                ]
            else:
                job_id = source_key
                exec_suffix = message_id  # unique per single file
                files = [source_key]

        # Filter unsupported file types
        unsupported_files = [
            f
            for f in files
            if os.path.splitext(f)[1].lower() not in SUPPORTED_EXTENSIONS
        ]
        files = [
            f for f in files if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS
        ]
        if unsupported_files:
            logger.info(
                f"Filtered {len(unsupported_files)} unsupported files: {unsupported_files}"
            )

        if not files:
            logger.warning(f"No files found for {job_id}")
            return True, message_id

        if not update_counter(increment=True):
            return False, message_id

        try:
            started, clean_id, ts = start_execution(
                job_id,
                source_bucket,
                files,
                exec_suffix=exec_suffix,
                timestamp=batch_ts if batch_files else None,
            )
            if started and unsupported_files and DYNAMODB_TABLE:
                from infra.dynamodb_manager import DynamoDBManager

                mgr = DynamoDBManager(DYNAMODB_TABLE)
                for uf in unsupported_files:
                    ext = os.path.splitext(uf)[1]
                    mgr.append_failed_file(
                        clean_id, ts, "unsupported", uf, f"Unsupported file type: {ext}"
                    )
                logger.info(
                    f"Tracked {len(unsupported_files)} unsupported files in DDB"
                )
            if not started:
                update_counter(increment=False)
            return True, message_id
        except Exception as e:
            logger.error(f"Error processing {job_id}: {e}", exc_info=True)
            try:
                update_counter(increment=False)
            except Exception as ce:
                logger.error(f"Failed to decrement counter: {ce}")
            return False, message_id

    except Exception as e:
        logger.error(f"Unexpected error for message {message_id}: {e}", exc_info=True)
        return False, message_id


def lambda_handler(event, context):
    logger.info(f"Processing batch of {len(event['Records'])} messages")
    failed_ids = []
    for record in event["Records"]:
        success, message_id = process_message(record)
        if not success:
            failed_ids.append(message_id)
    return {"batchItemFailures": [{"itemIdentifier": mid} for mid in failed_ids]}
