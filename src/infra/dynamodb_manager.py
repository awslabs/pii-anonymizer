# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
DynamoDB Manager Module

This module provides functionality for storing PII mapping data in DynamoDB.
"""

import json
import time
import logging
from datetime import datetime
from decimal import Decimal

import boto3

# Configure logger
logger = logging.getLogger(__name__)


class DynamoDBManager:
    """
    Class to manage interactions with DynamoDB for PII mapping storage.
    """

    def __init__(self, table_name):
        """
        Initialize the DynamoDB manager.

        Args:
            table_name: Name of the DynamoDB table to use
        """
        self.table_name = table_name
        self.dynamodb = boto3.resource("dynamodb")
        self.table = self.dynamodb.Table(table_name)
        schema = self.table.key_schema
        self.pk = next(k["AttributeName"] for k in schema if k["KeyType"] == "HASH")
        self.sk = next(
            (k["AttributeName"] for k in schema if k["KeyType"] == "RANGE"), None
        )

    def update_job_status(self, job_id, status, extra_attrs=None, timestamp=None):
        """Lightweight status update for job tracking."""
        expr = "SET #s = :s, updated_at = :t, expiration_time = :ttl"
        values = {
            ":s": status,
            ":t": datetime.now().isoformat(),
            ":ttl": int(time.time()) + (90 * 24 * 60 * 60),
        }
        names = {"#s": "status"}
        if extra_attrs:
            for k, v in extra_attrs.items():
                expr += f", #{k} = :{k}"
                names[f"#{k}"] = k
                values[f":{k}"] = v
        key = {self.pk: job_id}
        if self.sk:
            key[self.sk] = timestamp or datetime.now().isoformat()
        values = json.loads(json.dumps(values), parse_float=Decimal)
        try:
            self.table.update_item(
                Key=key,
                UpdateExpression=expr,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            logger.info(f"Job {job_id} status → {status}")
            return True
        except Exception as e:
            logger.error(f"Failed to update job status: {e}")
            return False

    def append_failed_file(self, job_id, timestamp, step, file_key, error):
        """Append a failed file entry to failed_files.<step> list. Concurrent-safe via list_append."""
        key = {self.pk: job_id}
        if self.sk:
            key[self.sk] = timestamp or datetime.now().isoformat()
        try:
            self.table.update_item(
                Key=key,
                UpdateExpression="SET failed_files.#step = list_append(failed_files.#step, :entry)",
                ExpressionAttributeNames={"#step": step},
                ExpressionAttributeValues={
                    ":entry": [{"file": file_key, "error": str(error)}]
                },
            )
            logger.info(f"Recorded failed file: {file_key} in {step}")
        except Exception as e:
            logger.error(f"Failed to record failed file {file_key}: {e}")

    def update_file_status(self, job_id, timestamp, file_key, file_attrs):
        """Update per-file nested map inside the job record. Concurrent-safe."""
        # Ensure files map exists, then set files.<file_key> = {...}
        safe_key = file_key.replace("/", "_").replace(".", "_")
        key = {self.pk: job_id}
        if self.sk:
            key[self.sk] = timestamp or datetime.now().isoformat()
        file_attrs = json.loads(json.dumps(file_attrs), parse_float=Decimal)
        try:
            self.table.update_item(
                Key=key,
                UpdateExpression="SET files = if_not_exists(files, :empty), files.#fk = :fv",
                ExpressionAttributeNames={"#fk": safe_key},
                ExpressionAttributeValues={
                    ":empty": {},
                    ":fv": {"source_key": file_key, **file_attrs},
                },
            )
            return True
        except Exception as e:
            logger.error(f"Failed to update file status for {file_key}: {e}")
            return False

    def store_pii_mapping(
        self,
        pii_mapping,
        filename,
        status="SUCCESS",
        error_message=None,
        token_usage=None,
    ):
        """
        Store PII mapping data in DynamoDB as a single item with 90-day TTL

        Args:
            pii_mapping: List of PII mapping dictionaries
            filename: The filename to use as the partition key
            status: Processing status (SUCCESS or FAILED)
            error_message: Error message if status is FAILED

        Returns:
            Boolean indicating success or failure
        """
        # Handle None value for pii_mapping
        entity_count = 0
        if pii_mapping is not None:
            entity_count = len(pii_mapping)

        logger.info(
            f"Storing {status} record for file: {filename} with {entity_count} entities"
        )
        start_time = time.time()

        # Get current timestamp
        timestamp = datetime.now().isoformat()

        # Calculate expiration time (90 days from now)
        expiration_time = int(time.time()) + (90 * 24 * 60 * 60)

        try:
            # Create a single item containing all PII mappings
            item = {
                self.pk: filename,  # partition key
                "status": status,
                "entity_count": entity_count,
                "expiration_time": expiration_time,  # TTL attribute
            }
            if self.sk:
                item[self.sk] = timestamp

            # Add mappings only if successful and mappings exist
            if status == "SUCCESS" and pii_mapping:
                item["mappings"] = (
                    pii_mapping  # Store the entire mapping list as a single attribute
                )

            # Add error message if status is FAILED
            if status == "FAILED" and error_message:
                item["error_message"] = error_message

            # Add token usage if provided
            if token_usage:
                item["token_usage"] = token_usage

            # Convert to DynamoDB format
            item = json.loads(json.dumps(item), parse_float=Decimal)
            self.table.put_item(Item=item)

            elapsed_time = time.time() - start_time
            logger.info(
                f"Data insertion complete in {elapsed_time:.2f} seconds for file: {filename} (expires in 90 days)"
            )
            return True
        except Exception as e:
            elapsed_time = time.time() - start_time
            logger.error(
                f"Error inserting PII mapping: {str(e)} (file: {filename}, elapsed: {elapsed_time:.2f} seconds)",
                exc_info=True,
            )
            # Not raising here as this is not critical - we can proceed without storing mappings
            return False
