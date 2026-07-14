# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Audio PII Processor Module

Integrates audio PII anonymization into the existing pipeline:
- Step 1 (Detection): Transcribe audio → detect PII → return standard detections format
- Step 3 (Redaction): Use synthetic mapping → Polly synthesis → ffmpeg splice

Supports: .mp3, .wav
Requires: ffmpeg, Amazon Transcribe, Amazon Polly
"""

import json
import os
import re
import time
import uuid
import shutil
import logging
import tempfile
import subprocess

import boto3

from helpers.model_router import converse_or_responses

logger = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg") or "/opt/bin/ffmpeg"

PII_SYSTEM_PROMPT = """You are a PII detection expert. Given a transcript, identify all PII entities.
Return ONLY a JSON array of objects with keys: "text" (exact text as it appears), "type" (name/address/phone/email/ssn/dob/other).
If no PII found, return [].
"""

S3_AUDIO_PREFIX = "audio-pii/"


def _get_audio_config(config):
    """Get audio-specific configuration with defaults."""
    audio = config.get("audio", {})
    return {
        "polly_voice": audio.get("polly_voice", "Joanna"),
        "sample_rate": audio.get("sample_rate", "16000"),
        "language_code": audio.get("language_code", "en-US"),
    }


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------


def _transcribe_from_s3(
    source_bucket, source_key, transcribe_client, language_code="en-US"
):
    """Run a Transcribe job on an audio file already in S3. Returns full transcript JSON."""
    job_name = f"pii-audio-{uuid.uuid4().hex[:8]}"
    media_uri = f"s3://{source_bucket}/{source_key}"
    ext = os.path.splitext(source_key)[1].lstrip(".").lower()
    fmt = "mp3" if ext == "mp3" else "wav"

    logger.info(
        f"Starting Transcribe job: {job_name} for s3://{source_bucket}/{source_key}"
    )
    transcribe_client.start_transcription_job(
        TranscriptionJobName=job_name,
        Media={"MediaFileUri": media_uri},
        MediaFormat=fmt,
        LanguageCode=language_code,
        Settings={"ShowSpeakerLabels": True, "MaxSpeakerLabels": 10},
    )

    while True:
        resp = transcribe_client.get_transcription_job(TranscriptionJobName=job_name)
        status = resp["TranscriptionJob"]["TranscriptionJobStatus"]
        if status == "COMPLETED":
            break
        if status == "FAILED":
            raise RuntimeError(f"Transcribe job failed: {resp}")
        logger.info("Waiting for transcription...")
        time.sleep(5)

    result_uri = resp["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
    import urllib.request

    # The transcript URI comes from the Amazon Transcribe API response and is always
    # an HTTPS S3 URL. Validate the scheme so urlopen can never be coerced into
    # reading a local file (file://) or other scheme (defense-in-depth; bandit B310).
    if not result_uri.startswith("https://"):
        raise ValueError(
            f"Unexpected transcript URI scheme (expected https): {result_uri}"
        )

    with urllib.request.urlopen(
        result_uri
    ) as r:  # nosec B310 - https-only validated above
        return json.loads(r.read())


def _extract_words(transcript_json):
    """Return list of {word, start_time, end_time} from Transcribe output."""
    items = transcript_json["results"]["items"]
    words = []
    for item in items:
        if item["type"] == "pronunciation":
            words.append(
                {
                    "word": item["alternatives"][0]["content"],
                    "start": float(item["start_time"]),
                    "end": float(item["end_time"]),
                }
            )
    return words


# ---------------------------------------------------------------------------
# PII Detection (Step 1 interface)
# ---------------------------------------------------------------------------


def detect_pii_audio(source_bucket, source_key, config, bedrock_runtime, s3_client):
    """
    Detect PII in an audio file.
    Transcribes directly from S3 → detects PII → returns standard detection format.

    Returns dict compatible with pii_detection_handler expectations:
    {
        "detections": [...],
        "token_usage": {...},
        "file_type": "audio",
        "transcript": "...",
        "words": [...],
    }
    """
    audio_config = _get_audio_config(config)
    model_id = config.get("audio", {}).get("detection_model", config["model"]["id"])
    language_code = audio_config["language_code"]

    # Transcribe uses the file directly from S3 (same bucket the router uploaded to)
    region = os.environ.get(
        "AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1")
    )
    transcribe_client = boto3.client("transcribe", region_name=region)

    # Transcribe
    transcript_json = _transcribe_from_s3(
        source_bucket, source_key, transcribe_client, language_code
    )
    words = _extract_words(transcript_json)
    transcript_text = transcript_json["results"]["transcripts"][0]["transcript"]
    speaker_segments = (
        transcript_json.get("results", {}).get("speaker_labels", {}).get("segments", [])
    )
    logger.info(
        f"Transcription complete: {len(words)} words, {len(speaker_segments)} speaker segments"
    )

    # Detect PII using Bedrock (routes to OpenAI mantle API if an openai.* model)
    from helpers.model_config_helper import get_inference_config_from_yaml

    prompt = (
        f"Transcript:\n{transcript_text}\n\nIdentify all PII. Return JSON array only."
    )
    kwargs = {
        "modelId": model_id,
        "system": [{"text": PII_SYSTEM_PROMPT}],
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        # Use the standard detection inference config (model-clamped maxTokens),
        # not a tiny hardcoded value — otherwise long transcripts truncate and,
        # with Claude extended thinking, maxTokens can fall below the thinking
        # budget (Bedrock requires maxTokens > thinking.budget_tokens).
        "inferenceConfig": get_inference_config_from_yaml(config),
    }
    response = converse_or_responses(
        bedrock_runtime,
        kwargs,
        region=region,
        config=config,
        step="detection",
    )
    # Use the shared extractor: with extended thinking enabled, the first content
    # block is a reasoning block (no "text" key) — this finds the text block and
    # also warns on truncation. Same helper the text/vision paths use.
    from validation.model_schemas import safe_get_response_content

    content = safe_get_response_content(response).strip()
    usage = response.get("usage", {})

    # Parse PII detections
    match = re.search(r"\[.*\]", content, re.DOTALL)
    raw_detections = json.loads(match.group(0)) if match else []

    # Convert to standard detection format
    detections = []
    for pii in raw_detections:
        detections.append(
            {
                "content": pii["text"],
                "type": pii["type"].lower(),
                "confidence": 0.9,
                "detection_source": "llm",
            }
        )

    token_usage = {
        "input_tokens": usage.get("inputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        "requests": 1,
        "model_id": model_id,
    }

    logger.info(f"PII detected: {len(detections)} entities")

    return {
        "detections": detections,
        "token_usage": token_usage,
        "file_type": "audio",
        "transcript": transcript_text,
        "words": words,
        "speaker_segments": speaker_segments,
    }


# ---------------------------------------------------------------------------
# Audio Redaction (Step 3 interface)
# ---------------------------------------------------------------------------


def _find_pii_spans(detections, words, pii_mapping):
    """
    For each PII detection, find matching word(s) in the timed word list.
    Returns list of {start, end, fake_text} spans to replace.
    """
    spans = []
    for det in detections:
        original = det.get("content", "")
        fake_text = pii_mapping.get(original)
        if not fake_text:
            continue

        pii_words = original.lower().split()
        for i in range(len(words) - len(pii_words) + 1):
            window = [
                w["word"].lower().strip(".,!?;:\"'")
                for w in words[i : i + len(pii_words)]
            ]
            if window == pii_words:
                span_start = words[i]["start"]
                span_end = words[i + len(pii_words) - 1]["end"]
                spans.append(
                    {"start": span_start, "end": span_end, "fake_text": fake_text}
                )

    spans.sort(key=lambda s: s["start"])
    return spans


def _synthesize_replacement(
    text, polly_client, tmpdir, sample_rate="16000", voice_id="Joanna"
):
    """Synthesize text with Polly, return WAV file path."""
    resp = polly_client.synthesize_speech(
        Text=text,
        OutputFormat="pcm",
        VoiceId=voice_id,
        SampleRate=sample_rate,
    )
    pcm_path = os.path.join(tmpdir, f"polly_{uuid.uuid4().hex}.pcm")
    wav_path = pcm_path.replace(".pcm", ".wav")
    with open(pcm_path, "wb") as f:
        f.write(resp["AudioStream"].read())
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-f",
            "s16le",
            "-ar",
            sample_rate,
            "-ac",
            "1",
            "-i",
            pcm_path,
            wav_path,
        ],
        check=True,
        capture_output=True,
    )
    return wav_path


def _generate_silence(duration_sec, tmpdir, sample_rate="16000"):
    """Generate a silence WAV file of given duration."""
    wav_path = os.path.join(tmpdir, f"silence_{uuid.uuid4().hex}.wav")
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={sample_rate}:cl=mono",
            "-t",
            str(duration_sec),
            "-ar",
            sample_rate,
            "-ac",
            "1",
            wav_path,
        ],
        check=True,
        capture_output=True,
    )
    return wav_path


def _splice_audio(
    original_path,
    spans,
    polly_client,
    tmpdir,
    output_path,
    sample_rate="16000",
    redaction_mode="synthetic",
    voice_id="Joanna",
):
    """
    Build output audio by splicing replacements at PII timestamps.
    redaction_mode: "synthetic" (Polly speech) or "silence" (muted segments).
    """
    original_wav = os.path.join(tmpdir, "original.wav")
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-i",
            original_path,
            "-ar",
            sample_rate,
            "-ac",
            "1",
            original_wav,
        ],
        check=True,
        capture_output=True,
    )

    segments = []
    cursor = 0.0

    for span in spans:
        if span["start"] > cursor:
            seg_path = os.path.join(tmpdir, f"seg_{uuid.uuid4().hex}.wav")
            subprocess.run(
                [
                    FFMPEG,
                    "-y",
                    "-i",
                    original_wav,
                    "-ss",
                    str(cursor),
                    "-to",
                    str(span["start"]),
                    "-ar",
                    sample_rate,
                    "-ac",
                    "1",
                    seg_path,
                ],
                check=True,
                capture_output=True,
            )
            segments.append(seg_path)

        if redaction_mode == "silence":
            duration = span["end"] - span["start"]
            silence_wav = _generate_silence(max(duration, 0.1), tmpdir, sample_rate)
            segments.append(silence_wav)
        else:
            polly_wav = _synthesize_replacement(
                span["fake_text"], polly_client, tmpdir, sample_rate, voice_id
            )
            segments.append(polly_wav)
        cursor = span["end"]

    tail_path = os.path.join(tmpdir, f"seg_tail_{uuid.uuid4().hex}.wav")
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-i",
            original_wav,
            "-ss",
            str(cursor),
            "-ar",
            sample_rate,
            "-ac",
            "1",
            tail_path,
        ],
        check=True,
        capture_output=True,
    )
    segments.append(tail_path)

    concat_list = os.path.join(tmpdir, "concat.txt")
    with open(concat_list, "w") as f:
        for seg in segments:
            f.write(f"file '{seg}'\n")

    subprocess.run(
        [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", concat_list, output_path],
        check=True,
        capture_output=True,
    )
    logger.info(f"Spliced audio output: {output_path}")


def redact_audio(
    s3_client,
    source_bucket,
    source_key,
    output_bucket,
    pii_mapping,
    detections,
    detection_data,
    config,
    job_id="",
    output_prefix="",
):
    """
    Redact audio file using synthetic mapping (Step 3).

    Downloads audio → locates PII spans → synthesizes/silences → splices.
    Outputs: anonymized WAV + redacted transcript to S3.

    Returns (output_key, replaced_count, found_originals).
    Also annotates detections with timestamp_start/timestamp_end for reporting.
    """
    audio_config = _get_audio_config(config)
    sample_rate = audio_config["sample_rate"]
    voice_id = audio_config["polly_voice"]
    audio_redaction_mode = config.get("audio", {}).get("redaction_mode", "synthetic")
    region = os.environ.get(
        "AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1")
    )
    polly_client = boto3.client("polly", region_name=region)

    filename = os.path.basename(source_key)
    stem = os.path.splitext(filename)[0]

    # Retrieve words and speaker segments from detection data
    words = detection_data.get("words", [])
    speaker_segments = detection_data.get("speaker_segments", [])

    with tempfile.TemporaryDirectory() as tmpdir:
        # Download original audio
        local_audio = os.path.join(tmpdir, filename)
        s3_client.download_file(source_bucket, source_key, local_audio)

        # Find PII spans in timed words
        spans = _find_pii_spans(detections, words, pii_mapping)
        found_originals = set(
            det["content"] for det in detections if det["content"] in pii_mapping
        )

        # Annotate detections with timestamps for the report
        for det in detections:
            original = det.get("content", "")
            for span in spans:
                if pii_mapping.get(original) == span["fake_text"]:
                    det["timestamp_start"] = span["start"]
                    det["timestamp_end"] = span["end"]
                    break

        # Build output paths
        pfx = f"{output_prefix}/" if output_prefix else ""

        if spans:
            output_wav = os.path.join(tmpdir, f"{stem}_anonymized.wav")
            _splice_audio(
                local_audio,
                spans,
                polly_client,
                tmpdir,
                output_wav,
                sample_rate=sample_rate,
                redaction_mode=audio_redaction_mode,
                voice_id=voice_id,
            )
        else:
            output_wav = os.path.join(tmpdir, f"{stem}_anonymized.wav")
            subprocess.run(
                [
                    FFMPEG,
                    "-y",
                    "-i",
                    local_audio,
                    "-ar",
                    sample_rate,
                    "-ac",
                    "1",
                    output_wav,
                ],
                check=True,
                capture_output=True,
            )

        # Upload anonymized audio → output bucket
        audio_output_key = f"{pfx}redacted/audio/{stem}_anonymized.wav"
        s3_client.upload_file(output_wav, output_bucket, audio_output_key)
        logger.info(f"Anonymized audio: s3://{output_bucket}/{audio_output_key}")

        # Build speaker-formatted transcript
        def _build_speaker_transcript(words_list, segments, pii_map=None):
            """Build a conversational transcript with speaker labels."""
            if not segments or not words_list:
                text = " ".join(w["word"] for w in words_list)
                if pii_map:
                    for orig, fake in sorted(pii_map.items(), key=lambda x: -len(x[0])):
                        text = text.replace(orig, fake)
                return text

            # Group words by speaker segment using time matching
            lines = []
            current_speaker = None
            current_words = []

            for word in words_list:
                word_start = word["start"]
                # Find which speaker segment this word belongs to
                speaker = current_speaker or "spk_0"
                for seg in segments:
                    seg_start = float(seg["start_time"])
                    seg_end = float(seg["end_time"])
                    if seg_start <= word_start <= seg_end:
                        speaker = seg.get("speaker_label", "spk_0")
                        break

                if speaker != current_speaker and current_words:
                    line = " ".join(current_words)
                    if pii_map:
                        for orig, fake in sorted(
                            pii_map.items(), key=lambda x: -len(x[0])
                        ):
                            line = line.replace(orig, fake)
                    lines.append(f"{current_speaker}: {line}")
                    current_words = []

                current_speaker = speaker
                current_words.append(word["word"])

            # Flush remaining words
            if current_words:
                line = " ".join(current_words)
                if pii_map:
                    for orig, fake in sorted(pii_map.items(), key=lambda x: -len(x[0])):
                        line = line.replace(orig, fake)
                lines.append(f"{current_speaker}: {line}")

            return "\n\n".join(lines)

        redacted_pii_map = {
            k: v for k, v in pii_mapping.items() if k in found_originals
        }
        redacted_transcript = _build_speaker_transcript(
            words, speaker_segments, redacted_pii_map
        )
        original_formatted = _build_speaker_transcript(words, speaker_segments)

        transcript_key = f"{pfx}redacted/audio/{stem}_redacted_transcript.txt"
        s3_client.put_object(
            Bucket=output_bucket,
            Key=transcript_key,
            Body=redacted_transcript,
            ContentType="text/plain",
        )
        logger.info(f"Redacted transcript: s3://{output_bucket}/{transcript_key}")

        # Upload original transcript (for review/audit)
        original_transcript_key = (
            f"{pfx}intermediate/audio/{stem}_original_transcript.txt"
        )
        s3_client.put_object(
            Bucket=output_bucket,
            Key=original_transcript_key,
            Body=original_formatted,
            ContentType="text/plain",
        )

    replaced_count = len(set(span["fake_text"] for span in spans))
    return audio_output_key, replaced_count, found_originals
