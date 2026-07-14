# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Synthetic PII Generation Lambda (Step 2) - Called by SF after Map(Detect).
Reads all detection JSONs for a folder, deduplicates PII across files,
generates one unified synthetic mapping, stores in S3.

Input from SF:  {"job_id": "folder/", "source_bucket": "...", "output_bucket": "...", "detection_results": [...]}
Output to SF:   {"mapping_s3_key": "folder/detections/synthetic_mapping.json", "token_usage": {...}}
"""

import json
import os
import logging
import time
from collections import Counter

import boto3
from botocore.config import Config

from helpers.observability import init_tracing

init_tracing()  # X-Ray: trace AWS SDK calls as subsegments (no-op if SDK absent)
from helpers.throttle_handler import check_and_raise_throttling
from infra.dynamodb_manager import DynamoDBManager

logger = logging.getLogger()
logger.setLevel(logging.INFO)


from helpers.config_loader import load_config


def _get_ddb(job_id):
    table_name = os.environ.get("DYNAMODB_TABLE_NAME")
    if table_name and job_id:
        return DynamoDBManager(table_name)
    return None


def _handle(event, context):
    job_id = event["job_id"]
    output_prefix = event.get("output_prefix", job_id)
    ts = event.get("timestamp", "")
    output_bucket = event["output_bucket"]
    detection_results = event.get("detection_results", [])

    logger.info(
        f"[START] Synthetic generation | job={job_id} | detection_files={len(detection_results)}"
    )

    config = load_config(use_cache=False)
    aws_config = Config(
        retries={"max_attempts": 5, "mode": "adaptive"},
        connect_timeout=60,
        read_timeout=300,
    )
    bedrock_runtime = boto3.client("bedrock-runtime", config=aws_config)
    s3_client = boto3.client("s3", config=aws_config)

    ddb = _get_ddb(job_id)
    if ddb:
        ddb.update_job_status(job_id, "GENERATING_SYNTHETIC", timestamp=ts)

    # --- Read all detection JSONs from S3, aggregate tokens ---
    all_detections = []
    detection_tokens = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    files_read = 0
    files_failed = 0

    for det in detection_results:
        det_key = det.get("detection_s3_key")
        if not det_key:
            continue
        try:
            obj = s3_client.get_object(Bucket=output_bucket, Key=det_key)
            data = json.loads(obj["Body"].read())
            dets = data.get("detections", [])
            all_detections.extend(dets)
            files_read += 1
            logger.info(f"[READ] {os.path.basename(det_key)}: {len(dets)} detections")

            # Aggregate detection token usage
            tok = data.get("token_usage", {})
            detection_tokens["input_tokens"] += tok.get("input_tokens", 0)
            detection_tokens["output_tokens"] += tok.get("output_tokens", 0)
            detection_tokens["requests"] += tok.get("requests", 0)
        except Exception as e:
            files_failed += 1
            logger.error(f"[READ] Failed: {os.path.basename(det_key)}: {e}")

    logger.info(
        f"[READ] files_read={files_read} files_failed={files_failed} total_detections={len(all_detections)}"
    )
    logger.info(
        f"[READ] Detection tokens: input={detection_tokens['input_tokens']} output={detection_tokens['output_tokens']} requests={detection_tokens['requests']}"
    )

    if not all_detections:
        logger.warning("[SKIP] No detections found — nothing to generate")
        return {
            "mapping_s3_key": "",
            "token_usage": {
                "detection": detection_tokens,
                "synthetic": {},
                "total": detection_tokens,
            },
        }

    # --- Deduplicate by content ---
    seen = {}
    unique = []
    for d in all_detections:
        content = d.get("content", "").strip()
        if content and content not in seen:
            seen[content] = True
            unique.append(d)

    duplicates_removed = len(all_detections) - len(unique)
    pii_types = Counter(d.get("type", "unknown") for d in unique)
    logger.info(f"[DEDUP] unique={len(unique)} duplicates_removed={duplicates_removed}")
    logger.info(f"[DEDUP] PII types: {dict(pii_types.most_common())}")

    # --- Generate synthetic mapping ---
    model_id = config.get("model", {}).get("id", "global.anthropic.claude-sonnet-4-6")
    model_provider = config.get("model", {}).get("provider", "anthropic")

    from core.synthetic_pii_generator import batch_generate_synthetic_pii
    from helpers.token_tracker import TokenTracker

    tracker = TokenTracker(model_id)
    logger.info(f"[GENERATE] model={model_id}")

    start = time.time()
    pii_mapping = batch_generate_synthetic_pii(
        unique,
        model_id,
        model_provider,
        bedrock_runtime,
        config=config,
        token_tracker=tracker,
    )
    elapsed = time.time() - start

    mapped = sum(1 for v in pii_mapping.values() if v)
    unmapped = len(unique) - mapped
    synthetic_tokens = {
        "input_tokens": tracker.input_tokens,
        "output_tokens": tracker.output_tokens,
        "requests": tracker.requests,
    }
    total_tokens = {
        "input_tokens": detection_tokens["input_tokens"]
        + synthetic_tokens["input_tokens"],
        "output_tokens": detection_tokens["output_tokens"]
        + synthetic_tokens["output_tokens"],
        "requests": detection_tokens["requests"] + synthetic_tokens["requests"],
    }

    logger.info(
        f"[GENERATE] mapped={mapped}/{len(unique)} unmapped={unmapped} elapsed={elapsed:.1f}s"
    )
    logger.info(
        f"[GENERATE] Synthetic tokens: input={synthetic_tokens['input_tokens']} output={synthetic_tokens['output_tokens']} requests={synthetic_tokens['requests']}"
    )
    logger.info(
        f"[TOKENS] Cumulative: input={total_tokens['input_tokens']} output={total_tokens['output_tokens']} requests={total_tokens['requests']}"
    )

    if unmapped:
        unmapped_types = Counter(
            d.get("type", "unknown")
            for d in unique
            if d.get("content", "").strip() not in pii_mapping
            or not pii_mapping.get(d.get("content", "").strip())
        )
        logger.warning(f"[GENERATE] Unmapped PII by type: {dict(unmapped_types)}")

    # --- Store mapping in S3 ---
    pfx = f"{output_prefix}/" if output_prefix else ""
    if output_prefix:
        mapping_key = f"{pfx}intermediate/synthetic/synthetic_mapping.json"
    else:
        # Single file: use safe_name from source_key to match detection/redaction folders
        src = detection_results[0].get("source_key", "") if detection_results else ""
        safe = os.path.basename(src).replace(".", "_") if src else job_id
        mapping_key = f"intermediate/{safe}/synthetic/synthetic_mapping.json"

    token_usage = {
        "detection": detection_tokens,
        "synthetic": synthetic_tokens,
        "total": total_tokens,
    }

    mapping_data = {
        "job_id": job_id,
        "pii_mapping": pii_mapping,
        "stats": {
            "total_detections": len(all_detections),
            "unique_values": len(unique),
            "duplicates_removed": duplicates_removed,
            "mappings_generated": mapped,
            "unmapped": unmapped,
            "elapsed_seconds": round(elapsed, 1),
        },
        "token_usage": token_usage,
    }
    s3_client.put_object(
        Bucket=output_bucket,
        Key=mapping_key,
        Body=json.dumps(mapping_data, default=str),
        ContentType="application/json",
    )
    logger.info(f"[STORE] s3://{output_bucket}/{mapping_key}")
    logger.info(
        f"[END] Synthetic generation complete | job={job_id} | mapped={mapped} | {elapsed:.1f}s"
    )

    if ddb:
        ddb.update_job_status(
            job_id,
            "SYNTHETIC_COMPLETE",
            timestamp=ts,
            extra_attrs={
                "mapping_s3_key": mapping_key,
            },
        )

    return {"mapping_s3_key": mapping_key, "token_usage": token_usage}


def lambda_handler(event, context):
    try:
        return _handle(event, context)
    except Exception as e:
        check_and_raise_throttling(e)
        job_id = event.get("job_id", "")
        ts = event.get("timestamp", "")
        ddb = _get_ddb(job_id)
        if ddb:
            ddb.update_job_status(
                job_id,
                "FAILED",
                timestamp=ts,
                extra_attrs={
                    "error": f"{type(e).__name__}: {e}",
                    "failed_step": "synthetic",
                },
            )
        raise
