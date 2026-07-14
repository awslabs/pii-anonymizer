# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
PII Detection Lambda (Step 1) - Called by SF Map state per file.
Routes to correct detect_pii_* function based on file extension.
Stores detection results in S3 (output bucket, same folder structure).

Input from SF:  {"source_bucket": "...", "source_key": "...", "output_bucket": "..."}
Output to SF:   {"source_key": "...", "detection_s3_key": "..."}
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
from processors.txt_processor import detect_pii_txt
from processors.word_processor import detect_pii_word
from processors.tabular_processor import (
    detect_pii_excel,
    detect_pii_csv,
    detect_pii_json,
)
from processors.image_processor import detect_pii_image
from processors.pdf_text_processor import detect_pii_pdf_text
from processors.pdf_image_processor import detect_pii_pdf_image
from processors.audio_processor import detect_pii_audio

logger = logging.getLogger()
logger.setLevel(logging.INFO)

EXTENSION_MAP = {
    ".txt": detect_pii_txt,
    ".docx": detect_pii_word,
    ".xlsx": detect_pii_excel,
    ".csv": detect_pii_csv,
    ".json": detect_pii_json,
    ".jpg": detect_pii_image,
    ".jpeg": detect_pii_image,
    ".png": detect_pii_image,
    ".tiff": detect_pii_image,
    ".tif": detect_pii_image,
    ".bmp": detect_pii_image,
    ".webp": detect_pii_image,
    ".mp3": detect_pii_audio,
    ".wav": detect_pii_audio,
}

PDF_APPROACHES = {
    "text": detect_pii_pdf_text,
    "image": detect_pii_pdf_image,
}


from helpers.config_loader import load_config


def _get_detect_func(file_ext, config):
    if file_ext == ".pdf":
        approach = config.get("processing", {}).get("approach", "image").lower()
        func = PDF_APPROACHES.get(approach)
        if not func:
            raise ValueError(f"Unsupported PDF approach: {approach}")
        return func
    func = EXTENSION_MAP.get(file_ext)
    if not func:
        raise ValueError(f"Unsupported file type: {file_ext}")
    return func


def _get_ddb(job_id):
    table_name = os.environ.get("DYNAMODB_TABLE_NAME")
    if table_name and job_id:
        return DynamoDBManager(table_name)
    return None


def _handle(event, context):
    source_bucket = event["source_bucket"]
    source_key = event["source_key"]
    output_bucket = event["output_bucket"]
    job_id = event.get("job_id", "")
    output_prefix = event.get("output_prefix", job_id)
    ts = event.get("timestamp", "")
    file_ext = os.path.splitext(source_key)[1].lower()
    logger.info(f"PII Detection: {source_key} (ext={file_ext})")

    ddb = _get_ddb(job_id)
    if ddb:
        ddb.update_job_status(job_id, "DETECTING", timestamp=ts)

    config = load_config(use_cache=False)
    aws_config = Config(
        retries={"max_attempts": 5, "mode": "adaptive"},
        connect_timeout=60,
        read_timeout=300,
    )
    bedrock_runtime = boto3.client("bedrock-runtime", config=aws_config)
    s3_client = boto3.client("s3", config=aws_config)

    start = time.time()
    result = _get_detect_func(file_ext, config)(
        source_bucket, source_key, config, bedrock_runtime, s3_client
    )
    elapsed = time.time() - start

    # --- Summary logging ---
    dets = result.get("detections", [])
    failed = result.get("failed_chunks", [])
    tokens = result.get("token_usage", {})
    pii_types = Counter(d.get("type", "unknown") for d in dets)
    pages = Counter(d.get("page_num") for d in dets if d.get("page_num"))
    bbox_sources = Counter(
        d.get("bbox_source", "none") for d in dets if d.get("bbox_source")
    )

    logger.info(
        f"Detection complete: {source_key} | {len(dets)} PII items | {len(failed)} failed chunks | {elapsed:.1f}s"
    )
    if pages:
        logger.info(f"Pages: {len(pages)} | Per-page: {dict(sorted(pages.items()))}")
    logger.info(f"PII types: {dict(pii_types.most_common())}")
    if bbox_sources:
        logger.info(f"Bounding boxes: {dict(bbox_sources)}")
    if tokens:
        logger.info(
            f"Tokens: input={tokens.get('input_tokens', 0)} output={tokens.get('output_tokens', 0)} requests={tokens.get('requests', 0)}"
        )
    if failed:
        logger.warning(f"Failed chunks: {failed}")
        # Fail the job instead of marking it complete. A detection failure means
        # some content was never analyzed for PII — proceeding would redact an
        # incomplete set and silently leak the undetected PII. Better to fail
        # loud so the run is retried than to emit a falsely-"clean" document.
        raise RuntimeError(
            f"Detection failed for {len(failed)} chunk(s)/page(s) of {source_key}; "
            f"failing job to avoid silently missing PII. Details: {failed}"
        )

    # Store result in S3 — use job_id for shared intermediate folder
    filename = os.path.basename(source_key)
    safe_name = filename.replace(".", "_")
    pfx = f"{output_prefix}/" if output_prefix else ""
    if output_prefix:
        detection_key = f"{pfx}intermediate/detections/{safe_name}/detections.json"
    else:
        detection_key = f"intermediate/{safe_name}/detections/detections.json"

    s3_client.put_object(
        Bucket=output_bucket,
        Key=detection_key,
        Body=json.dumps(result, default=str),
        ContentType="application/json",
    )
    logger.info(f"Detection stored: s3://{output_bucket}/{detection_key}")

    # Save raw Textract data per page (image-based processors only)
    textract_pages = result.pop("textract_pages", [])
    for tp in textract_pages:
        page_num = tp["page"]
        tx_prefix = f"{pfx}intermediate/textract/{safe_name}"
        # Raw JSON response
        s3_client.put_object(
            Bucket=output_bucket,
            Key=f"{tx_prefix}/page_{page_num}.json",
            Body=json.dumps(tp["raw"], default=str),
            ContentType="application/json",
        )
        # OCR text
        s3_client.put_object(
            Bucket=output_bucket,
            Key=f"{tx_prefix}/page_{page_num}.txt",
            Body=tp["ocr_text"],
            ContentType="text/plain",
        )
    if textract_pages:
        logger.info(
            f"Textract data saved: {len(textract_pages)} pages to s3://{output_bucket}/{pfx}intermediate/textract/{safe_name}/"
        )

    if ddb:
        ddb.update_job_status(job_id, "DETECT_COMPLETE", timestamp=ts)

    return {"source_key": source_key, "detection_s3_key": detection_key}


def lambda_handler(event, context):
    try:
        return _handle(event, context)
    except Exception as e:
        check_and_raise_throttling(e)
        job_id = event.get("job_id", "")
        ts = event.get("timestamp", "")
        source_key = event.get("source_key", "")
        ddb = _get_ddb(job_id)
        if ddb:
            ddb.append_failed_file(
                job_id, ts, "detect", source_key, f"{type(e).__name__}: {e}"
            )
        raise
