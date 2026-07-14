# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Streamlit App for PII Anonymizer

Upload files/folders → S3 → Pipeline processes → Display redacted output + cost
"""

import streamlit as st
import boto3
import boto3.dynamodb.conditions
import time
import os
import json
import yaml
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=True)

# --- Config ---
AWS_REGION = os.getenv("AWS_REGION")
INPUT_BUCKET = os.getenv("INPUT_BUCKET")
INPUT_PREFIX = os.getenv("INPUT_PREFIX", "pii_data/")
OUTPUT_BUCKET = os.getenv("OUTPUT_BUCKET", os.getenv("INPUT_BUCKET"))
OUTPUT_PREFIX = os.getenv("OUTPUT_PREFIX", "redacted/")
DYNAMODB_TABLE = os.getenv("DYNAMODB_TABLE_NAME")
CONFIG_BUCKET = os.getenv("CONFIG_BUCKET", "")
CONFIG_KEY = os.getenv("CONFIG_KEY", "config/config.yaml")

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

# Load pricing
PRICING_PATH = os.path.join(os.path.dirname(__file__), "..", "src", "pricing.yaml")
PRICING = {}
if os.path.exists(PRICING_PATH):
    with open(PRICING_PATH) as f:
        raw = yaml.safe_load(f)
    for entry in raw.get("pricing", []):
        model_name = entry["name"].split("/", 1)[-1]  # strip "bedrock/" prefix
        units = {u["name"]: float(u["price"]) for u in entry.get("units", [])}
        PRICING[model_name] = units

for check, label in [
    (AWS_REGION, "AWS_REGION"),
    (INPUT_BUCKET, "INPUT_BUCKET"),
    (DYNAMODB_TABLE, "DYNAMODB_TABLE_NAME"),
]:
    if not check:
        st.error(f"❌ {label} not set in .env file")
        st.stop()

s3_client = boto3.client("s3", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)


def load_pipeline_config():
    """Load pipeline config from S3. Returns dict or None."""
    if not CONFIG_BUCKET:
        return None
    try:
        resp = s3_client.get_object(Bucket=CONFIG_BUCKET, Key=CONFIG_KEY)
        return yaml.safe_load(resp["Body"].read())
    except Exception:
        return None


def save_pipeline_config(config):
    """Save pipeline config to S3."""
    if not CONFIG_BUCKET:
        return False
    try:
        s3_client.put_object(
            Bucket=CONFIG_BUCKET,
            Key=CONFIG_KEY,
            Body=yaml.dump(config, default_flow_style=False),
            ContentType="application/x-yaml",
        )
        return True
    except Exception as e:
        st.error(f"Failed to save config: {e}")
        return False


st.set_page_config(page_title="PII Anonymizer", page_icon="🔒", layout="wide")

st.markdown(
    """
<style>
    .main-header {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 50%, #f093fb 100%);
        padding: 2.5rem; border-radius: 15px; margin-bottom: 2rem;
        box-shadow: 0 8px 16px rgba(102, 126, 234, 0.3);
    }
    .main-header h1 { color: white; margin: 0; font-size: 2.5rem; }
    .main-header p { color: #ffffff; margin: 0.5rem 0 0 0; font-size: 1.2rem; }
    .stButton>button {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white; border: none; padding: 0.75rem 2rem;
        font-size: 1.1rem; font-weight: 600; border-radius: 8px;
    }
</style>
""",
    unsafe_allow_html=True,
)

st.markdown(
    """
<div class="main-header">
    <h1>🔒 PII Anonymizer</h1>
    <p>Document & Audio PII Detection & Redaction Pipeline</p>
</div>
""",
    unsafe_allow_html=True,
)


def calc_cost(token_usage):
    """Calculate cost from token usage dict using pricing.yaml."""
    if not token_usage:
        return None
    model_id = token_usage.get("model_id", "")
    input_tokens = int(token_usage.get("input_tokens", 0))
    output_tokens = int(token_usage.get("output_tokens", 0))
    rates = PRICING.get(model_id)
    if not rates:
        # Try partial match
        for key, val in PRICING.items():
            if key in model_id or model_id in key:
                rates = val
                break
    if not rates:
        return None
    cost = input_tokens * rates.get("inputTokens", 0) + output_tokens * rates.get(
        "outputTokens", 0
    )
    return cost


def fetch_s3_json(key):
    """Fetch and parse a JSON file from S3 output bucket."""
    try:
        resp = s3_client.get_object(Bucket=OUTPUT_BUCKET, Key=key)
        return json.loads(resp["Body"].read())
    except Exception:
        return None


def fetch_redaction_reports(job_id, filenames, mapping_s3_key=""):
    """Fetch redactions.json for each file in a job."""
    # Derive base path from mapping_s3_key if available
    # mapping_s3_key: "{prefix}/intermediate/synthetic/synthetic_mapping.json"
    # redaction key:  "{prefix}/intermediate/redactions/{safe_name}/redactions.json"
    base = ""
    if mapping_s3_key:
        base = mapping_s3_key.rsplit("/intermediate/synthetic/", 1)[0]

    reports = []
    for fname in filenames:
        safe_name = fname.replace(".", "_")
        candidates = []
        if base:
            candidates.append(
                f"{base}/intermediate/redactions/{safe_name}/redactions.json"
            )
        candidates += [
            f"{job_id}/intermediate/redactions/{safe_name}/redactions.json",
            f"intermediate/{safe_name}/redactions/redactions.json",
        ]
        for key in candidates:
            report = fetch_s3_json(key)
            if report:
                reports.append({"filename": fname, "report": report})
                break
    return reports


def fetch_job_from_ddb(job_id, after_ts=None):
    """Query DDB for the latest entry for a job ID, optionally after a timestamp."""
    try:
        table = dynamodb.Table(DYNAMODB_TABLE)
        key_cond = boto3.dynamodb.conditions.Key("filename").eq(job_id)
        if after_ts:
            key_cond = key_cond & boto3.dynamodb.conditions.Key("timestamp").gte(
                after_ts
            )
        resp = table.query(
            KeyConditionExpression=key_cond,
            ScanIndexForward=False,
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0] if items else None
    except Exception:
        return None


# --- Upload Section ---
upload_mode = st.radio(
    "Upload mode",
    ["Single files", "Batch (folder)"],
    captions=["Each file processed independently", "All files processed as one job"],
    horizontal=True,
)

if upload_mode == "Single files":
    uploaded_files = st.file_uploader(
        "Choose file(s)",
        type=[ext.lstrip(".") for ext in SUPPORTED_EXTENSIONS],
        accept_multiple_files=True,
    )
else:
    uploaded_files = st.file_uploader(
        "Choose files for batch",
        type=[ext.lstrip(".") for ext in SUPPORTED_EXTENSIONS],
        accept_multiple_files=True,
    )

if uploaded_files and uploaded_files[0] is not None:
    # Filter unsupported
    valid_files = [
        f
        for f in uploaded_files
        if os.path.splitext(f.name)[1].lower() in SUPPORTED_EXTENSIONS
    ]
    if len(valid_files) < len(uploaded_files):
        skipped = len(uploaded_files) - len(valid_files)
        st.warning(f"⚠️ Skipped {skipped} unsupported file(s)")

    if valid_files:
        st.write(f"**{len(valid_files)} file(s) selected:**")

        # Per-file S3 path (batch mode) or single files to root
        file_paths = {}
        if upload_mode == "Single files":
            for f in valid_files:
                st.write(f"  - {f.name} ({f.size / 1024:.1f} KB)")
        else:
            auto_folder = datetime.now().strftime("batch_%Y%m%d_%H%M%S")
            folder_name = st.text_input(
                "S3 folder path (e.g. `claim-123` or `client-a/loan-package`)",
                key="folder_name_input",
                help=f"Enter folder name without trailing slash. Leave empty to use: {auto_folder}",
            )
            folder_name = folder_name.strip().strip("/")
            if not folder_name:
                folder_name = auto_folder
            for f in valid_files:
                st.write(
                    f"  - {f.name} ({f.size / 1024:.1f} KB) → `{folder_name}/{f.name}`"
                )

        if st.button("🚀 Upload & Process", type="primary"):
            s3_keys = []

            content_types = {
                ".pdf": "application/pdf",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".csv": "text/csv",
                ".json": "application/json",
                ".txt": "text/plain",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".tiff": "image/tiff",
                ".tif": "image/tiff",
                ".bmp": "image/bmp",
                ".webp": "image/webp",
                ".mp3": "audio/mpeg",
                ".wav": "audio/wav",
            }

            with st.spinner(f"Uploading {len(valid_files)} file(s) to S3..."):
                for f in valid_files:
                    ext = os.path.splitext(f.name)[1].lower()
                    if upload_mode == "Single files":
                        s3_key = f.name
                    else:
                        s3_key = f"{folder_name}/{f.name}"
                    try:
                        s3_client.put_object(
                            Bucket=INPUT_BUCKET,
                            Key=s3_key,
                            Body=f.getvalue(),
                            ContentType=content_types.get(
                                ext, "application/octet-stream"
                            ),
                        )
                        s3_keys.append(s3_key)
                    except Exception as e:
                        st.error(f"❌ Failed to upload {f.name}: {e}")

            if s3_keys:
                for k in s3_keys:
                    st.success(f"✅ `s3://{INPUT_BUCKET}/{k}`")

                if upload_mode == "Single files":
                    # Each file is an independent job — track all job IDs
                    job_ids = [os.path.splitext(f.name)[0] for f in valid_files]
                    st.session_state["job_ids"] = job_ids
                    st.session_state["upload_mode"] = "single"
                else:
                    st.session_state["job_ids"] = [folder_name.strip("/").split("/")[0]]
                    st.session_state["upload_mode"] = "batch"

                st.session_state["uploaded_filenames"] = [f.name for f in valid_files]
                st.session_state["upload_time"] = time.time()
                st.session_state["results"] = []
                st.session_state["highest_status"] = {}
                st.session_state["processing"] = True
                st.rerun()

# --- Processing Status & Results ---
if st.session_state.get("processing"):
    st.markdown("---")
    st.subheader("📊 Processing Status")

    job_ids = st.session_state.get("job_ids", [])
    filenames = st.session_state.get("uploaded_filenames", [])
    upload_time = st.session_state.get("upload_time", 0)
    elapsed = time.time() - upload_time
    mode = st.session_state.get("upload_mode", "batch")
    # Convert epoch to UTC ISO for DDB sort key filter (30s buffer for clock skew)
    after_ts = (
        datetime.utcfromtimestamp(upload_time - 30).isoformat() if upload_time else None
    )

    status_map = {
        "WAITING": "⏳ Waiting (files batching ~30s)...",
        "IN_PROGRESS": "🔄 Started",
        "DETECTING": "🔍 Detecting",
        "DETECT_COMPLETE": "✅ Detected",
        "GENERATING_SYNTHETIC": "🧪 Generating",
        "SYNTHETIC_COMPLETE": "✅ Generated",
        "REDACTING": "✏️ Redacting",
        "COMPLETE": "✅ Complete",
        "SUCCESS": "✅ Complete",
        "FAILED": "❌ Failed",
    }

    progress_order = [
        "WAITING",
        "IN_PROGRESS",
        "DETECTING",
        "DETECT_COMPLETE",
        "GENERATING_SYNTHETIC",
        "SYNTHETIC_COMPLETE",
        "REDACTING",
        "COMPLETE",
        "SUCCESS",
        "FAILED",
    ]

    # Track highest status seen per job to prevent backwards jumps
    if "highest_status" not in st.session_state:
        st.session_state["highest_status"] = {}

    all_complete = True
    all_results = []

    for job_id in job_ids:
        ddb_item = fetch_job_from_ddb(job_id, after_ts=after_ts)
        raw_status = ddb_item.get("status", "WAITING") if ddb_item else "WAITING"

        # Only allow forward progress
        prev = st.session_state["highest_status"].get(job_id, "WAITING")
        prev_idx = progress_order.index(prev) if prev in progress_order else 0
        cur_idx = (
            progress_order.index(raw_status) if raw_status in progress_order else 0
        )
        if cur_idx >= prev_idx:
            status = raw_status
            st.session_state["highest_status"][job_id] = status
        else:
            status = prev

        display = status_map.get(status, status)

        if mode == "single":
            st.write(f"**{job_id}**: {display}")
        else:
            st.write(f"**Job `{job_id}`**: {display}")

        if status in ("COMPLETE", "SUCCESS"):
            job_files = (
                [f for f in filenames]
                if mode == "batch"
                else [f for f in filenames if os.path.splitext(f)[0] == job_id]
            )
            reports = fetch_redaction_reports(
                job_id, job_files, ddb_item.get("mapping_s3_key", "")
            )
            all_results.extend(reports or [])
        elif status == "FAILED":
            st.error(f"❌ `{job_id}` failed: {ddb_item.get('error', 'Unknown')}")
        else:
            all_complete = False

    # Progress bar
    progress_order = [
        "WAITING",
        "IN_PROGRESS",
        "DETECTING",
        "DETECT_COMPLETE",
        "GENERATING_SYNTHETIC",
        "SYNTHETIC_COMPLETE",
        "REDACTING",
        "COMPLETE",
    ]
    if not all_complete:
        statuses = [
            st.session_state["highest_status"].get(jid, "WAITING") for jid in job_ids
        ]
        avg_progress = sum(
            (progress_order.index(s) + 1) if s in progress_order else 0
            for s in statuses
        ) / (len(statuses) * len(progress_order))
        st.progress(min(avg_progress, 0.95))
        st.caption(f"{elapsed:.0f}s elapsed")
        time.sleep(10)
        st.rerun()
    else:
        st.progress(1.0)
        if all_results:
            st.session_state["results"] = all_results
            st.session_state["processing"] = False
            st.success(
                f"✅ All done! {len(all_results)} file(s) processed ({elapsed:.0f}s)"
            )
            st.rerun()
        else:
            st.session_state["processing"] = False
            st.warning("Jobs complete but no reports found.")

# --- Results Display ---
elif st.session_state.get("results"):
    st.markdown("---")
    st.subheader("🔐 Redaction Results")

    results = st.session_state["results"]
    total_cost = 0

    # Aggregate summary for batch jobs
    if len(results) > 1:
        agg_detections = sum(r["report"].get("total_detections", 0) for r in results)
        agg_replaced = sum(r["report"].get("replaced_detections", 0) for r in results)
        agg_not_redacted = sum(r["report"].get("not_redacted", 0) for r in results)
        agg_unique = sum(r["report"].get("unique_pii_values", 0) for r in results)
        sc1, sc2, sc3, sc4 = st.columns(4)
        sc1.metric("📁 Files Processed", len(results))
        sc2.metric("Detections", agg_detections)
        sc3.metric("Replaced", agg_replaced)
        sc4.metric("Not Redacted", agg_not_redacted)

        # Aggregate PII types across all files
        all_pii_types = {}
        for r in results:
            for m in r["report"].get("mappings", []):
                t = m.get("type", "unknown")
                all_pii_types[t] = all_pii_types.get(t, 0) + 1
        if all_pii_types:
            st.markdown("**🏷️ PII Types (all files):**")
            type_cols = st.columns(min(len(all_pii_types), 6))
            for i, (pt, cnt) in enumerate(
                sorted(all_pii_types.items(), key=lambda x: -x[1])
            ):
                type_cols[i % len(type_cols)].metric(pt.replace("_", " ").title(), cnt)
        st.markdown("---")

    for res in results:
        report = res["report"]
        fname = res["filename"]
        mappings = report.get("mappings", [])
        token_usage = report.get("token_usage", {})
        detection_tokens = token_usage.get("detection", {})

        with st.expander(f"📄 {fname}", expanded=len(results) == 1):
            # Summary metrics
            c1, c2, c3, c4, c5, c6 = st.columns(6)
            c1.metric("Detections", report.get("total_detections", 0))
            c2.metric("Unique PII", report.get("unique_pii_values", 0))
            c3.metric("Replaced", report.get("replaced_detections", 0))
            c4.metric("Not Redacted", report.get("not_redacted", 0))
            c5.metric("Rasterized", report.get("rasterized", 0))
            c6.metric("File Type", report.get("file_type", "N/A"))

            # Token usage
            if detection_tokens:
                st.markdown("**🔢 Token Usage:**")
                tc1, tc2, tc3, tc4 = st.columns(4)
                input_tok = int(detection_tokens.get("input_tokens", 0))
                output_tok = int(detection_tokens.get("output_tokens", 0))
                requests = int(detection_tokens.get("requests", 0))
                tc1.metric("Input Tokens", f"{input_tok:,}")
                tc2.metric("Output Tokens", f"{output_tok:,}")
                tc3.metric("API Calls", requests)

                cost = calc_cost(detection_tokens)
                if cost is not None:
                    tc4.metric("Est. Cost", f"${cost:.4f}")
                    total_cost += cost

            # PII type breakdown
            if mappings:
                pii_types = {}
                for m in mappings:
                    t = m.get("type", "unknown")
                    pii_types[t] = pii_types.get(t, 0) + 1

                st.markdown("**🏷️ PII Types:**")
                type_cols = st.columns(min(len(pii_types), 6))
                for i, (pt, cnt) in enumerate(
                    sorted(pii_types.items(), key=lambda x: -x[1])
                ):
                    type_cols[i % len(type_cols)].metric(
                        pt.replace("_", " ").title(), cnt
                    )

                # Mappings table
                table_data = []
                is_audio = report.get("file_type") == "audio"
                has_pages = any(m.get("page_num") for m in mappings)
                has_timestamps = any(m.get("timestamp_start") for m in mappings)
                for m in mappings:
                    row = {
                        "Type": m.get("type", "N/A"),
                        "Original": m.get("original", "N/A"),
                        "Replacement": m.get("synthetic", m.get("replacement", "N/A")),
                        "Confidence": f"{int(float(m.get('confidence', 0)) * 100) if float(m.get('confidence', 0)) <= 1 else int(m.get('confidence', 0))}%",
                    }
                    status = m.get("replacement_status", "")
                    reason = m.get("not_redacted_reason", "")
                    STATUS_MAP = {
                        "text_replaced": "✅ Redacted",
                        "replaced": "✅ Redacted",
                        "rasterized": "✅ Redacted",
                        "fallback_rasterized": "✅ Redacted",
                        "not_found_in_text": "⚠️ Not found in document",
                        "no_synthetic": "⚠️ No replacement generated",
                        "not_redacted": "❌ Not redacted",
                    }
                    if reason:
                        row["Status"] = f"❌ Not redacted ({reason.replace('_', ' ')})"
                    else:
                        row["Status"] = STATUS_MAP.get(status, status or "N/A")
                    if has_pages:
                        row["Page"] = m.get("page_num", "—")
                        row["BBox"] = "✅" if m.get("bounding_box") else "—"
                    if is_audio or has_timestamps:
                        ts_start = m.get("timestamp_start", "")
                        ts_end = m.get("timestamp_end", "")
                        if ts_start:
                            row["Timestamp"] = (
                                f"{float(ts_start):.1f}s – {float(ts_end):.1f}s"
                            )
                        else:
                            row["Timestamp"] = "—"
                    table_data.append(row)
                df = pd.DataFrame(table_data)
                st.dataframe(df, use_container_width=True, hide_index=True)

                with st.expander("Status legend"):
                    st.markdown(
                        "| Status | Meaning |\n|---|---|\n"
                        "| ✅ Redacted | PII was successfully redacted |\n"
                        "| ⚠️ Not found in document | Detected PII whose exact text wasn't found in the document (text / Word / Excel) |\n"
                        "| ⚠️ No replacement generated | PII was found but a fake replacement couldn't be generated (Faker fallback also failed) |\n"
                        "| ❌ Not redacted (no bounding box) | A **visual** element (e.g. signature, face/iris photo, stamp) was detected but couldn't be located on the page, so it can't be auto-redacted — **review and redact it manually** |\n"
                        "| ❌ Not redacted (…) | PII was found but couldn't be redacted; the specific reason is shown in parentheses |"
                    )

            # Download redacted file
            redacted_key = report.get("redacted_s3_key")
            if redacted_key:
                try:
                    resp = s3_client.get_object(Bucket=OUTPUT_BUCKET, Key=redacted_key)
                    redacted_bytes = resp["Body"].read()
                    ext = os.path.splitext(redacted_key)[1] or ".pdf"
                    mime_map = {
                        ".pdf": "application/pdf",
                        ".txt": "text/plain",
                        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        ".csv": "text/csv",
                        ".json": "application/json",
                        ".png": "image/png",
                        ".jpg": "image/jpeg",
                        ".tiff": "image/tiff",
                        ".wav": "audio/wav",
                        ".mp3": "audio/mpeg",
                    }
                    # Audio: play button + transcript + download button
                    if ext in (".wav", ".mp3"):
                        st.markdown("**🔊 Redacted Audio Playback:**")
                        st.audio(redacted_bytes, format=mime_map[ext])

                        # Fetch and display redacted transcript
                        stem = os.path.splitext(os.path.basename(redacted_key))[
                            0
                        ].replace("_anonymized", "")
                        transcript_key = (
                            redacted_key.rsplit("/", 1)[0]
                            + f"/{stem}_redacted_transcript.txt"
                        )
                        try:
                            t_resp = s3_client.get_object(
                                Bucket=OUTPUT_BUCKET, Key=transcript_key
                            )
                            transcript_text = t_resp["Body"].read().decode("utf-8")
                            st.markdown("**📝 Redacted Transcript:**")
                            # Format speaker-labeled lines for readability
                            lines = transcript_text.strip().split("\n")
                            formatted = []
                            for line in lines:
                                line = line.strip()
                                if not line:
                                    continue
                                if ":" in line and line.split(":")[0].startswith(
                                    "spk_"
                                ):
                                    speaker_id, text = line.split(":", 1)
                                    speaker_num = (
                                        int(speaker_id.replace("spk_", "")) + 1
                                    )
                                    formatted.append(
                                        f"**Speaker {speaker_num}:** {text.strip()}"
                                    )
                                else:
                                    formatted.append(line)
                            transcript_html = (
                                "<br><br>".join(
                                    f.replace("**", "<b>", 1).replace("**", "</b>", 1)
                                    for f in formatted
                                )
                                if formatted
                                else transcript_text
                            )
                            st.markdown(
                                f'<div style="max-height: 300px; overflow-y: auto; padding: 1rem; '
                                f"border: 1px solid #e0e0e0; border-radius: 8px; "
                                f'background: #f9f9f9; resize: vertical; min-height: 100px;">'
                                f"{transcript_html}</div>",
                                unsafe_allow_html=True,
                            )
                        except Exception:
                            pass

                        st.download_button(
                            "📥 Download Redacted Audio",
                            data=redacted_bytes,
                            file_name=f"redacted_{os.path.splitext(fname)[0]}.wav",
                            mime=mime_map.get(ext, "audio/wav"),
                        )
                    else:
                        st.download_button(
                            f"📥 Download Redacted {fname}",
                            data=redacted_bytes,
                            file_name=f"redacted_{fname}",
                            mime=mime_map.get(ext, "application/octet-stream"),
                        )
                except Exception as e:
                    st.warning(f"Could not download redacted file: {e}")

    # Total cost across all files
    if total_cost > 0 and len(results) > 1:
        st.markdown("---")
        st.metric("💰 Total Estimated Cost", f"${total_cost:.4f}")

# --- Processing History ---
st.markdown("---")
st.subheader("📚 Processing History")

if st.button("🔄 Load History"):
    with st.spinner("Fetching from DynamoDB..."):
        try:
            table = dynamodb.Table(DYNAMODB_TABLE)
            response = table.scan(Limit=50)
            items = response.get("Items", [])

            if items:
                history = []
                for item in sorted(
                    items, key=lambda x: x.get("timestamp", ""), reverse=True
                ):
                    files_list = item.get("files", [])
                    file_count = len(files_list) if isinstance(files_list, list) else 0
                    failed_detect = len(item.get("failed_files", {}).get("detect", []))
                    failed_redact = len(item.get("failed_files", {}).get("redact", []))
                    # Parse redaction_results summary if available
                    total_detections = 0
                    redaction_results_raw = item.get("redaction_results", "")
                    if redaction_results_raw:
                        try:
                            rr = (
                                json.loads(redaction_results_raw)
                                if isinstance(redaction_results_raw, str)
                                else redaction_results_raw
                            )
                            total_detections = sum(
                                r.get("total_detections", 0) for r in rr
                            )
                        except Exception:
                            pass
                    history.append(
                        {
                            "Job ID": item.get("filename", "N/A"),
                            "Status": item.get("status", "N/A"),
                            "Files": file_count,
                            "Detections": total_detections,
                            "Failed (Detect)": failed_detect,
                            "Failed (Redact)": failed_redact,
                            "Processed At": (
                                item.get("timestamp", "N/A")[:19]
                                if item.get("timestamp")
                                else "N/A"
                            ),
                        }
                    )
                st.dataframe(
                    pd.DataFrame(history), use_container_width=True, hide_index=True
                )
                st.session_state["history_items"] = items

                # Failed job details
                failed = [
                    i for i in items if i.get("status") == "FAILED" and i.get("error")
                ]
                if failed:
                    st.markdown("**❌ Failed Jobs:**")
                    for item in failed:
                        with st.expander(
                            f"{item.get('filename')} — {item.get('timestamp', '')[:19]}"
                        ):
                            st.error(item.get("error", "Unknown error"))
            else:
                st.info("No processing history found")
        except Exception as e:
            st.error(f"❌ Error: {e}")

# Load results from a past job
if st.session_state.get("history_items"):
    completed = [
        i
        for i in st.session_state["history_items"]
        if i.get("status") in ("COMPLETE", "SUCCESS")
    ]
    if completed:
        job_ids = [
            f"{i['filename']} ({i.get('timestamp', '')[:19]})" for i in completed
        ]
        selected = st.selectbox("View results for a completed job:", [""] + job_ids)
        if selected and st.button("📄 Load Job Results"):
            idx = job_ids.index(selected)
            item = completed[idx]
            job_id = item["filename"]
            filenames = item.get("files", [])
            reports = fetch_redaction_reports(
                job_id, filenames, item.get("mapping_s3_key", "")
            )
            if reports:
                st.session_state["results"] = reports
                st.session_state["ddb_item"] = item
                st.success(f"Loaded {len(reports)} report(s) for job '{job_id}'")
            else:
                st.warning("Redaction reports not found in S3")

# --- Sidebar ---
with st.sidebar:
    # --- Pipeline Settings ---
    if CONFIG_BUCKET:
        st.subheader("Pipeline Settings")
        # Persistent confirmation after a save (survives the rerun)
        _saved = st.session_state.pop("settings_saved", None)
        if _saved:
            st.success(
                f"✅ Settings saved to S3.\n\n"
                f"Model: `{_saved['model']}` · Redaction: `{_saved['mode']}`\n\n"
                f"Applies to the next file processed."
            )
        cfg = load_pipeline_config()
        if cfg:
            redaction = cfg.get("redaction", {})
            markers = redaction.get("markers", {})
            processing = cfg.get("processing", {})
            audio_cfg = cfg.get("audio", {})
            model_cfg = cfg.get("model", {})
            detection_cfg = cfg.get("detection", {})
            synthetic_cfg = cfg.get("synthetic", {})

            # Initialize all values from current config so the hidden group
            # keeps its existing values when Save is pressed.
            new_approach = processing.get("approach", "image")
            new_mode = redaction.get("mode", "synthetic")
            new_bbox = markers.get("image", False)
            new_mark_word = markers.get("word", False)
            new_mark_tabular = markers.get("tabular", False)
            new_mark_text = markers.get("text", False)
            new_audio_mode = audio_cfg.get("redaction_mode", "synthetic")
            new_polly_voice = audio_cfg.get("polly_voice", "Joanna")
            new_model_id = model_cfg.get("id", "global.anthropic.claude-sonnet-4-6")
            new_reasoning_effort = detection_cfg.get("reasoning_effort", "low")
            new_synthetic_effort = synthetic_cfg.get("reasoning_effort", "low")
            new_enable_thinking = detection_cfg.get("enable_thinking", False)

            # --- Model selection (common to document + audio) ---
            # label → model_id. Provider is auto-derived on save.
            MODEL_CHOICES = {
                "Claude Sonnet 4.6 (Anthropic, global)": "global.anthropic.claude-sonnet-4-6",
                "Claude Opus 4.8 (Anthropic, highest quality)": "us.anthropic.claude-opus-4-8",
                "Claude Opus 4.7 (Anthropic)": "us.anthropic.claude-opus-4-7",
                "Claude Haiku 4.5 (Anthropic, fastest)": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "Nova Pro (Amazon)": "us.amazon.nova-pro-v1:0",
                "Nova Lite (Amazon)": "us.amazon.nova-lite-v1:0",
                "GPT-5.4 (OpenAI, US only)": "openai.gpt-5.4",
                # GPT-5.5 is intentionally NOT offered yet: it is us-east-2-only
                # and that region currently has AWS-side mantle capacity/stability
                # issues (intermittent "Engine not found" / HTTP 500). Re-add once
                # us-east-2 mantle is stable and 5.5 invokes reliably.
                # "GPT-5.5 (OpenAI, us-east-2 only)": "openai.gpt-5.5",
            }
            id_to_label = {v: k for k, v in MODEL_CHOICES.items()}
            labels = list(MODEL_CHOICES.keys())
            # Keep the current config model selectable even if it's not in the list
            if new_model_id in id_to_label:
                current_label = id_to_label[new_model_id]
            else:
                current_label = f"(config) {new_model_id}"
                labels = [current_label] + labels
                MODEL_CHOICES[current_label] = new_model_id

            chosen_label = st.selectbox(
                "Model",
                labels,
                index=labels.index(current_label),
                help="Model used for both PII detection (Step 1) and synthetic "
                "generation (Step 2). Must be enabled in your Bedrock console.",
            )
            new_model_id = MODEL_CHOICES[chosen_label]
            is_openai = new_model_id.startswith("openai.")

            if is_openai:
                st.warning(
                    "⚠️ OpenAI GPT-5.x runs in US regions only (data stays in AWS). "
                    "If your stack isn't in a supported US region, requests cross-region "
                    "to a US region — avoid for EU / data-residency-restricted PII."
                )
                # Reasoning efforts VERIFIED per model against bedrock-mantle
                # (source of truth: model_router.supported_reasoning_efforts /
                # tests/probe_reasoning_efforts.py). GPT-5.4/5.5 reject 'minimal',
                # so it is intentionally not offered. Only valid values per the
                # selected model are shown, so an unsupported combo can't be picked.
                MODEL_EFFORTS = {
                    "openai.gpt-5.4": ["none", "low"],
                    "openai.gpt-5.5": ["none", "low"],
                }
                EFFORT_OPTIONS = MODEL_EFFORTS.get(new_model_id, ["none", "low"])

                def _effort_index(value, default="low"):
                    v = value if value in EFFORT_OPTIONS else default
                    return EFFORT_OPTIONS.index(v)

                new_reasoning_effort = st.selectbox(
                    "Detection reasoning effort",
                    EFFORT_OPTIONS,
                    index=_effort_index(new_reasoning_effort, "low"),
                    help="Depth of reasoning for PII DETECTION (Step 1). "
                    "Higher = more thorough but slower. Avoid 'none' here — "
                    "detection accuracy matters most (missed PII = leak).",
                )
                new_synthetic_effort = st.selectbox(
                    "Synthetic reasoning effort",
                    EFFORT_OPTIONS,
                    index=_effort_index(
                        synthetic_cfg.get("reasoning_effort", "low"), "low"
                    ),
                    help="Depth of reasoning for SYNTHETIC generation (Step 2). "
                    "'none' is usually safe here and much faster — generating "
                    "fake replacements needs little reasoning.",
                )
            elif "anthropic" in new_model_id:
                # Extended thinking is an Anthropic Claude feature (Nova doesn't
                # support it; OpenAI uses reasoning_effort above). Applies to
                # detection + synthetic.
                new_enable_thinking = st.checkbox(
                    "Enable extended thinking (Claude)",
                    value=bool(new_enable_thinking),
                    help="Claude 'thinking' mode — the model reasons before "
                    "answering. May improve accuracy on complex documents, but "
                    "adds latency and cost. Off by default. Claude models only.",
                )
                # Opus 4.7/4.8 use ADAPTIVE thinking + an effort level (not a
                # token budget). Show an effort selector when thinking is on.
                is_new_claude = "opus-4-7" in new_model_id or "opus-4-8" in new_model_id
                if is_new_claude and new_enable_thinking:
                    CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh"]
                    cur = (
                        new_reasoning_effort
                        if new_reasoning_effort in CLAUDE_EFFORTS
                        else "high"
                    )
                    new_reasoning_effort = st.selectbox(
                        "Thinking effort (Opus 4.7/4.8)",
                        CLAUDE_EFFORTS,
                        index=CLAUDE_EFFORTS.index(cur),
                        help="How much the model thinks (adaptive thinking). "
                        "Higher = more thorough, slower, costlier. Opus 4.7/4.8.",
                    )
                    new_synthetic_effort = new_reasoning_effort

            st.divider()

            # Top-level choice: show only the relevant settings group
            proc_type = st.radio(
                "Processing type",
                ["Document", "Audio"],
                horizontal=True,
                help="**Document**: PDF, Word, Excel, CSV, images, text.  \n**Audio**: MP3, WAV.",
            )

            if proc_type == "Document":
                new_approach = st.selectbox(
                    "PDF processing",
                    ["image", "text"],
                    index=0 if processing.get("approach", "image") == "image" else 1,
                    help="**Image**: PDF → redacted PDF (preserves layout, uses OCR)  \n**Text**: PDF → redacted TXT (faster, text-only output)",
                )
                new_mode = st.selectbox(
                    "Redaction mode",
                    ["synthetic", "blackout"],
                    index=0 if redaction.get("mode", "synthetic") == "synthetic" else 1,
                    help="**Synthetic**: replaces PII with realistic fake data  \n**Blackout**: covers PII with solid black fill or [REDACTED]",
                )

                if new_mode == "synthetic":
                    st.caption("Visual markers")
                    new_bbox = st.checkbox(
                        "Bounding boxes (PDF, images)",
                        value=new_bbox,
                        help="Red borders around detected PII regions",
                    )
                    new_mark_word = st.checkbox(
                        "Highlight replacements (DOCX)",
                        value=new_mark_word,
                        help="Yellow text highlight on replaced PII",
                    )
                    new_mark_tabular = st.checkbox(
                        "Highlight cells (XLSX, CSV)",
                        value=new_mark_tabular,
                        help="Yellow cell background on replaced PII",
                    )
                    new_mark_text = st.checkbox(
                        "Mark replacements (TXT, JSON)",
                        value=new_mark_text,
                        help="Wraps replaced PII with ***markers***",
                    )
            else:  # Audio
                new_audio_mode = st.selectbox(
                    "Audio redaction mode",
                    ["synthetic", "silence"],
                    index=(
                        0
                        if audio_cfg.get("redaction_mode", "synthetic") == "synthetic"
                        else 1
                    ),
                    help="**Synthetic**: replaces PII audio with Polly-generated fake speech  \n**Silence**: mutes PII segments with silence",
                )
                voices = ["Joanna", "Matthew", "Amy", "Brian", "Ivy", "Ruth"]
                new_polly_voice = st.selectbox(
                    "Polly voice",
                    voices,
                    index=(
                        voices.index(audio_cfg.get("polly_voice", "Joanna"))
                        if audio_cfg.get("polly_voice", "Joanna") in voices
                        else 0
                    ),
                    help="Amazon Polly voice for synthesizing replacement speech",
                    disabled=(new_audio_mode == "silence"),
                )

            if st.button("Save Settings", type="primary", use_container_width=True):
                # Model + provider (auto-derived from the model ID)
                cfg.setdefault("model", {})
                cfg["model"]["id"] = new_model_id
                if new_model_id.startswith("openai."):
                    cfg["model"]["provider"] = "openai"
                elif "amazon" in new_model_id or "nova" in new_model_id:
                    cfg["model"]["provider"] = "amazon"
                else:
                    cfg["model"]["provider"] = "anthropic"
                # Reasoning effort (OpenAI only; harmless for other models).
                # Detection and synthetic are persisted separately so each step
                # can be tuned independently (e.g. detection=low, synthetic=none).
                cfg.setdefault("detection", {})
                cfg["detection"]["reasoning_effort"] = new_reasoning_effort
                cfg.setdefault("synthetic", {})
                cfg["synthetic"]["reasoning_effort"] = new_synthetic_effort
                # Extended thinking (Claude only; ignored by Nova/OpenAI). Applied
                # to both detection and synthetic steps.
                cfg["detection"]["enable_thinking"] = bool(new_enable_thinking)
                cfg["synthetic"]["enable_thinking"] = bool(new_enable_thinking)

                cfg["processing"]["approach"] = new_approach
                cfg["redaction"]["mode"] = new_mode
                cfg.setdefault("redaction", {}).setdefault("markers", {})
                cfg["redaction"]["markers"]["image"] = new_bbox
                cfg["redaction"]["markers"]["word"] = new_mark_word
                cfg["redaction"]["markers"]["tabular"] = new_mark_tabular
                cfg["redaction"]["markers"]["text"] = new_mark_text
                cfg.setdefault("audio", {})
                cfg["audio"]["redaction_mode"] = new_audio_mode
                cfg["audio"]["polly_voice"] = new_polly_voice
                if save_pipeline_config(cfg):
                    st.session_state["settings_saved"] = {
                        "model": new_model_id,
                        "mode": new_mode,
                    }
                    st.rerun()
        else:
            st.warning("Could not load config from S3")

    st.divider()

    # Environment info
    st.subheader("Environment")
    st.caption(f"Region: `{AWS_REGION}`")
    st.caption(f"Input: `{INPUT_BUCKET}`")
    st.caption(f"Output: `{OUTPUT_BUCKET}`")

if "processing" not in st.session_state:
    st.session_state["processing"] = False
