# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
PII Redaction Lambda (Step 3) - Called by SF Map state per file.
Reads detection JSON (Step 1) + synthetic mapping (Step 2),
delegates to core redactor for file-type-specific replacement.

Input from SF:  {"source_bucket", "source_key", "output_bucket",
                 "detection_s3_key", "mapping_s3_key", "redaction_mode"}
Output to SF:   {"source_key", "redacted_s3_key", "replacements"}
"""

import json
import os
import logging
import time

import boto3
from botocore.config import Config

from helpers.observability import init_tracing

init_tracing()  # X-Ray: trace AWS SDK calls as subsegments (no-op if SDK absent)
from helpers.throttle_handler import check_and_raise_throttling
from infra.dynamodb_manager import DynamoDBManager

logger = logging.getLogger()
logger.setLevel(logging.INFO)


from helpers.config_loader import load_config
from helpers.model_config_helper import get_show_bounding_boxes


def _get_ddb(job_id):
    table_name = os.environ.get("DYNAMODB_TABLE_NAME")
    if table_name and job_id:
        return DynamoDBManager(table_name)
    return None


def _handle(event, context):
    source_bucket = event["source_bucket"]
    source_key = event["source_key"]
    output_bucket = event["output_bucket"]
    detection_s3_key = event["detection_s3_key"]
    mapping_s3_key = event.get("mapping_s3_key", "")
    redaction_mode = event.get("redaction_mode", "synthetic")
    job_id = event.get("job_id", "")
    output_prefix = event.get("output_prefix", job_id)
    ts = event.get("timestamp", "")

    filename = os.path.basename(source_key)
    folder_path = os.path.dirname(source_key)
    if folder_path:
        folder_path = folder_path.rstrip("/") + "/"
    else:
        folder_path = ""

    logger.info(f"[START] Redact | file={filename} mode={redaction_mode}")

    ddb = _get_ddb(job_id)
    if ddb:
        ddb.update_job_status(job_id, "REDACTING", timestamp=ts)

    config = load_config(use_cache=False)
    aws_config = Config(
        retries={"max_attempts": 5, "mode": "adaptive"},
        connect_timeout=60,
        read_timeout=300,
    )
    s3_client = boto3.client("s3", config=aws_config)

    start = time.time()

    # Read detection JSON from Step 1
    det_obj = s3_client.get_object(Bucket=output_bucket, Key=detection_s3_key)
    det_data = json.loads(det_obj["Body"].read())
    detections = det_data.get("detections", [])
    file_type = det_data.get("file_type", "")

    logger.info(f"[READ] detections={len(detections)} file_type={file_type}")

    if not detections:
        logger.info(f"[SKIP] No detections for {filename}")
        # Overwrite any stale redaction report from a PRIOR run of the same file.
        # Without this, re-running a file that previously had detections leaves
        # the old report in place and the UI shows the prior run's numbers.
        pfx = f"{output_prefix}/" if output_prefix else ""
        safe_name = filename.replace(".", "_")
        if output_prefix:
            report_key = f"{pfx}intermediate/redactions/{safe_name}/redactions.json"
        else:
            report_key = f"intermediate/{safe_name}/redactions/redactions.json"
        skip_report = {
            "source_key": source_key,
            "redacted_s3_key": None,
            "file_type": file_type,
            "redaction_mode": redaction_mode,
            "total_detections": 0,
            "replaced_detections": 0,
            "unique_pii_values": 0,
            "unique_replaced": 0,
            "status": "skip",
            "mappings": [],
        }
        try:
            s3_client.put_object(
                Bucket=output_bucket,
                Key=report_key,
                Body=json.dumps(skip_report),
                ContentType="application/json",
            )
            logger.info(f"[REPORT] (skip) overwrote s3://{output_bucket}/{report_key}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not overwrite stale report: {e}")
        if ddb:
            ddb.update_job_status(job_id, "REDACT_COMPLETE", timestamp=ts)
        return {
            "source_key": source_key,
            "status": "skip",
            "replaced": 0,
        }

    # Build PII mapping
    from core.redactor import build_file_mapping, build_blackout_mapping, redact_file

    # Per-file detection tokens (from Step 1 output)
    detection_tokens = det_data.get("token_usage", {})

    if redaction_mode == "blackout":
        pii_mapping = build_blackout_mapping(detections)
        logger.info(f"[MAP] blackout entries={len(pii_mapping)}")
    else:
        map_obj = s3_client.get_object(Bucket=output_bucket, Key=mapping_s3_key)
        map_data = json.loads(map_obj["Body"].read())
        full_mapping = map_data.get("pii_mapping", {})
        pii_mapping = build_file_mapping(detections, full_mapping)
        logger.info(
            f"[MAP] unique_pii={len(pii_mapping)} from {len(detections)} detections"
        )

    # Audio files use a specialized redaction path (Polly + ffmpeg splice)
    if file_type == "audio":
        from processors.audio_processor import redact_audio

        output_key, unique_replaced, found_originals = redact_audio(
            s3_client,
            source_bucket,
            source_key,
            output_bucket,
            pii_mapping,
            detections,
            det_data,
            config,
            job_id=job_id,
            output_prefix=output_prefix,
        )
    else:
        # Bedrock client only needed for pdf_image
        bedrock_runtime = None
        if file_type == "pdf_image":
            bedrock_runtime = boto3.client("bedrock-runtime", config=aws_config)

        # Redact
        output_key, unique_replaced, found_originals = redact_file(
            s3_client,
            source_bucket,
            source_key,
            output_bucket,
            folder_path,
            pii_mapping,
            detections,
            file_type,
            config,
            bedrock_runtime,
            job_id=job_id,
            output_prefix=output_prefix,
        )

    elapsed = time.time() - start
    logger.info(f"[REDACT] {file_type} | unique_replaced={unique_replaced}")
    logger.info(f"[STORE] s3://{output_bucket}/{output_key}")

    # Build per-detection mappings with accurate replacement_status
    markers = config.get("redaction", {}).get("markers", {})
    mappings = []
    for d in detections:
        content = d.get("content", "")
        if content in found_originals and (
            d.get("bounding_box") or file_type != "pdf_image"
        ):
            status = "text_replaced"
        elif d.get("_redact_method") == "rasterized":
            status = d["_redact_method"]
        elif content in pii_mapping:
            status = "not_redacted"
        else:
            status = "no_synthetic"
        entry = {
            "original": content,
            "synthetic": pii_mapping.get(content, ""),
            "type": d.get("type", "UNKNOWN"),
            "confidence": d.get("confidence", 0),
            "replacement_status": status,
        }
        if status == "not_redacted":
            reason = d.get("_not_redacted_reason")
            if not reason:
                reason = (
                    "signature"
                    if "signature" in content.lower() or d.get("type") == "biometric"
                    else "no_match_in_document"
                )
            entry["not_redacted_reason"] = reason
        elif status == "no_synthetic":
            entry["not_redacted_reason"] = "no_synthetic_replacement"
        if d.get("bounding_box"):
            entry["bounding_box"] = d["bounding_box"]
        if d.get("page_num"):
            entry["page_num"] = d["page_num"]
        if d.get("detection_source"):
            entry["detection_source"] = d["detection_source"]
        if d.get("timestamp_start") is not None:
            entry["timestamp_start"] = d["timestamp_start"]
            entry["timestamp_end"] = d.get("timestamp_end", "")
        mappings.append(entry)
        # Add entries for extra occurrences found by text-engine occurrence counting
        for _ in range(d.get("_extra_occurrences", 0)):
            mappings.append({**entry, "detection_source": "text_search"})

    replaced_count = sum(
        1
        for m in mappings
        if m["replacement_status"]
        in ("text_replaced", "rasterized")
    )
    rasterized_count = sum(
        1
        for m in mappings
        if m["replacement_status"] == "rasterized"
    )
    not_redacted_count = sum(
        1 for m in mappings if m["replacement_status"] == "not_redacted"
    )
    no_synthetic_count = sum(
        1 for m in mappings if m["replacement_status"] == "no_synthetic"
    )

    text_search_count = sum(
        1 for m in mappings if m.get("detection_source") in ("text_search", "textract")
    )
    llm_detection_count = len(detections) - sum(
        1 for d in detections if d.get("detection_source") == "textract"
    )

    # Per-file redaction report → S3
    report = {
        "source_key": source_key,
        "redacted_s3_key": output_key,
        "file_type": file_type,
        "redaction_mode": redaction_mode,
        "total_detections": len(mappings),
        "llm_detections": llm_detection_count,
        "text_search_detections": text_search_count,
        "unique_pii_values": len(
            {d.get("content", "") for d in detections if d.get("content")}
        ),
        "unique_replaced": len(
            {
                m.get("original", "")
                for m in mappings
                if m["replacement_status"] not in ("not_redacted", "no_synthetic")
            }
        ),
        "replaced_detections": replaced_count,
        "rasterized": rasterized_count,
        "not_redacted": not_redacted_count,
        "no_synthetic": no_synthetic_count,
        "options": {
            "bounding_boxes": bool(get_show_bounding_boxes(config)),
            "markers_text": bool(markers.get("text", False)),
            "highlight_tabular": bool(markers.get("tabular", False)),
            "highlight_word": bool(markers.get("word", False)),
        },
        "token_usage": {"detection": detection_tokens},
        "mappings": mappings,
    }
    pfx = f"{output_prefix}/" if output_prefix else ""
    safe_name = filename.replace(".", "_")
    if output_prefix:
        report_key = f"{pfx}intermediate/redactions/{safe_name}/redactions.json"
    else:
        report_key = f"intermediate/{safe_name}/redactions/redactions.json"
    s3_client.put_object(
        Bucket=output_bucket,
        Key=report_key,
        Body=json.dumps(report),
        ContentType="application/json",
    )
    logger.info(f"[REPORT] s3://{output_bucket}/{report_key}")
    logger.info(
        f"[END] Redact | file={filename} replaced={replaced_count} rasterized={rasterized_count} not_redacted={not_redacted_count} no_synthetic={no_synthetic_count} elapsed={elapsed:.1f}s"
    )

    if ddb:
        ddb.update_job_status(job_id, "REDACT_COMPLETE", timestamp=ts)

    return {
        "source_key": source_key,
        "status": "ok",
        "replaced": replaced_count,
    }


def lambda_handler(event, context):
    try:
        return _handle(event, context)
    except Exception as e:
        check_and_raise_throttling(e)
        job_id = event.get("job_id", "")
        ts = event.get("timestamp", "")
        source_key = event.get("source_key", "")
        filename = os.path.basename(source_key)
        ddb = _get_ddb(job_id)
        if ddb:
            ddb.append_failed_file(
                job_id, ts, "redact", source_key, f"{type(e).__name__}: {e}"
            )
        logger.error(f"[SKIP] Redaction failed for {filename}: {e}")
        return {
            "source_key": source_key,
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }
