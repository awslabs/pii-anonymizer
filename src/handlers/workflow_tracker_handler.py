"""
Workflow Tracker Lambda - Triggered by EventBridge on SF completion.
1. Decrements concurrency counter (always)
2. Updates job status to FAILED in mapping table (on failure only)
"""

import os
import json
import logging

import boto3

from helpers.observability import init_tracing

init_tracing()  # X-Ray: trace AWS SDK calls as subsegments (no-op if SDK absent)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ddb = boto3.resource("dynamodb")
sfn = boto3.client("stepfunctions")

CONCURRENCY_TABLE = os.environ.get("CONCURRENCY_TABLE", "")
DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "")


def decrement_counter():
    """Decrement active workflow counter."""
    if not CONCURRENCY_TABLE:
        return None
    table = ddb.Table(CONCURRENCY_TABLE)
    resp = table.update_item(
        Key={"doc_key": "workflow_counter"},
        UpdateExpression="ADD active_count :dec",
        ExpressionAttributeValues={":dec": -1},
        ReturnValues="UPDATED_NEW",
    )
    return resp["Attributes"]["active_count"]


def update_job_failed(job_id, timestamp, error):
    """Non-blocking job status update - logs error but doesn't raise."""
    if not DYNAMODB_TABLE_NAME:
        return
    try:
        table = ddb.Table(DYNAMODB_TABLE_NAME)
        table.update_item(
            Key={"filename": job_id, "timestamp": timestamp},
            UpdateExpression="SET #s = :s, #err = :e",
            ExpressionAttributeNames={"#s": "status", "#err": "error"},
            ExpressionAttributeValues={":s": "FAILED", ":e": str(error)},
        )
    except Exception as e:
        logger.error(f"Failed to update job status: {e}")


def lambda_handler(event, context):
    detail = event.get("detail", {})
    status = detail.get("status", "UNKNOWN")
    execution_arn = detail.get("executionArn", "")

    logger.info(f"SF completed: status={status}, arn={execution_arn}")

    # Always decrement counter
    try:
        count = decrement_counter()
        logger.info(f"Counter decremented, active_count={count}")
    except Exception as e:
        logger.error(f"Failed to decrement counter: {e}")

    # On failure, update DDB
    if status != "SUCCEEDED":
        try:
            resp = sfn.describe_execution(executionArn=execution_arn)
            sf_input = json.loads(resp.get("input", "{}"))
            job_id = sf_input.get("job_id", "")
            timestamp = sf_input.get("timestamp", "")
            cause = resp.get("cause", detail.get("cause", status))
            update_job_failed(job_id, timestamp, cause)
            logger.info(f"Job {job_id} marked as FAILED")
        except Exception as e:
            logger.error(f"Failed to get SF execution details: {e}")
