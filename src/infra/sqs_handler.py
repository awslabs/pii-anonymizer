# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
SQS Handler Module

Event unwrapping, idempotency locks (via IdempotencyTable), failure context,
and failed doc handling. Reusable module — no direct Lambda dependencies.
"""

import json
import time
import os
import logging

logger = logging.getLogger(__name__)


def extract_s3_event(event):
    """
    Extract S3 event record from either direct S3 trigger or SQS-wrapped event.

    Returns:
        tuple: (s3_event_record, sqs_metadata)
            - s3_event_record: dict with 'bucket' and 'object' keys
            - sqs_metadata: None for direct S3, or dict with message_id/receive_count for SQS
    """
    record = event["Records"][0]

    if record.get("eventSource") == "aws:sqs":
        body = json.loads(record["body"])
        if "Records" not in body:
            logger.info(
                f"Skipping non-S3 SQS message (e.g. s3:TestEvent): {body.get('Event', 'unknown')}"
            )
            return None, None
        s3_event = body["Records"][0]["s3"]
        sqs_metadata = {
            "message_id": record["messageId"],
            "receive_count": int(
                record["attributes"].get("ApproximateReceiveCount", 1)
            ),
        }
        logger.info(
            f"SQS event: message_id={sqs_metadata['message_id']}, attempt={sqs_metadata['receive_count']}"
        )
        return s3_event, sqs_metadata

    return record["s3"], None


def _get_partition_key(table):
    """Get partition key name from table's key schema."""
    schema = table.key_schema
    return next(k["AttributeName"] for k in schema if k["KeyType"] == "HASH")


def acquire_lock(table, doc_key, job_id, ttl_seconds=660):
    """
    Acquire idempotency lock via DynamoDB conditional write.

    Returns True if lock acquired, False if already COMPLETE (skip).
    Stale IN_PROGRESS locks (crashed Lambda) are overwritten after expiry.
    FAILED locks allow retry.
    """
    from botocore.exceptions import ClientError

    pk = _get_partition_key(table)
    now = int(time.time())
    try:
        table.put_item(
            Item={
                pk: doc_key,
                "status": "IN_PROGRESS",
                "lock_expiry": now + ttl_seconds,
                "job_id": job_id,
                "expiration_time": now + (7 * 24 * 60 * 60),
            },
            ConditionExpression=(
                "attribute_not_exists(#pk) "
                "OR #s = :failed "
                "OR (#s = :in_progress AND lock_expiry < :now)"
            ),
            ExpressionAttributeNames={"#s": "status", "#pk": pk},
            ExpressionAttributeValues={
                ":failed": "FAILED",
                ":in_progress": "IN_PROGRESS",
                ":now": now,
            },
        )
        logger.info(f"Lock acquired for {doc_key} (job={job_id})")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info(f"Skipping {doc_key} — already COMPLETE or locked")
            return False
        raise


def release_lock(table, doc_key, status, error_message=None):
    """Update lock record to COMPLETE or FAILED."""
    update_expr = "SET #s = :status, completed_at = :ts"
    expr_values = {":status": status, ":ts": int(time.time())}

    if error_message:
        update_expr += ", error_message = :err"
        expr_values[":err"] = error_message[:1000]

    table.update_item(
        Key={_get_partition_key(table): doc_key},
        UpdateExpression=update_expr,
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=expr_values,
    )
    logger.info(f"Lock released for {doc_key}: {status}")


def build_failure_context(exception, source_key, sqs_metadata, context):
    """Build enriched failure context for logging and DLQ."""
    is_retryable = not isinstance(exception, (ValueError, KeyError, TypeError))
    return {
        "doc_key": source_key,
        "error_type": "retryable" if is_retryable else "non-retryable",
        "error_message": str(exception)[:1000],
        "log_stream": getattr(context, "log_stream_name", "unknown"),
        "attempt_count": sqs_metadata["receive_count"] if sqs_metadata else 1,
    }


def copy_to_failed(s3_client, bucket, source_key):
    """Copy failed document to failed/ prefix in same bucket."""
    filename = os.path.basename(source_key)
    failed_key = f"failed/{filename}"
    try:
        s3_client.copy_object(
            Bucket=bucket,
            CopySource={"Bucket": bucket, "Key": source_key},
            Key=failed_key,
        )
        logger.info(f"Copied failed doc to s3://{bucket}/{failed_key}")
    except Exception as e:
        logger.warning(f"Failed to copy doc to failed/: {e}")
