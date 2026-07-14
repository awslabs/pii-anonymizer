# Audio PII Redaction

Audio redaction detects and removes PII spoken in audio recordings (`.mp3`, `.wav`). Unlike the document formats, it relies on AWS speech services end to end: **Amazon Transcribe** converts speech to timestamped text, a **Bedrock** model detects PII in that text, **Amazon Polly** synthesizes replacement speech (or silence is inserted), and **ffmpeg** splices the replacements back into the audio at the exact PII timestamps.

The audio path reuses the same 3-step Step Functions pipeline as every other format (Detect → Synthetic → Redact) and the same synthetic-generation engine: only the detection and redaction processors are audio-specific.

## Pipeline Flow

```
S3 audio (.mp3/.wav)
   │
   ▼  Step 1: Detection  (detect_pii_audio)
   ├─ Amazon Transcribe  → word-level timestamps + speaker diarization
   ├─ Bedrock LLM        → detect PII in the transcript text
   ▼
   Detection JSON {detections, transcript, words, speaker_segments}
   │
   ▼  Step 2: Synthetic  (batch_generate_synthetic_pii, shared engine)
   PII → realistic fake replacements
   │
   ▼  Step 3: Redaction  (redact_audio)
   ├─ map PII text → timed word spans
   ├─ synthetic mode: Amazon Polly speech   |  silence mode: muted segment
   ├─ ffmpeg splices replacements at exact timestamps
   ▼
   S3 output: anonymized WAV + speaker-formatted redacted transcript
```

DynamoDB status (identical to all formats): `IN_PROGRESS` → `DETECTING` → `DETECT_COMPLETE` → `GENERATING_SYNTHETIC` → `SYNTHETIC_COMPLETE` → `REDACTING` → `COMPLETE`.

## File Routing

Audio uses the same extension dispatch as the rest of the pipeline:

- **Router** (`router_handler.py`) accepts `.mp3` and `.wav` as processable uploads.
- **Detection** (`pii_detection_handler.py`) maps `.mp3` / `.wav` → `detect_pii_audio`.
- **Redaction** (`redact_handler.py`) routes files whose `file_type == "audio"` → `redact_audio`.

All audio logic lives in `src/processors/audio_processor.py`.

## Step 1: Transcription & Detection

`detect_pii_audio(source_bucket, source_key, config, bedrock_runtime, s3_client)`:

1. **Transcribe** (`_transcribe_from_s3`): starts an Amazon Transcribe job that reads the file **directly from S3** (the bucket the router uploaded to), with:
   - `MediaFileUri = s3://{source_bucket}/{source_key}`
   - `MediaFormat` derived from the file extension
   - **Speaker diarization** enabled (`ShowSpeakerLabels`) for conversational readability
   - `LanguageCode` from `audio.language_code` (default `en-US`)

   > **Region note:** Transcribe must run in the **same region as the input bucket**. The job uses the region from `AWS_DEFAULT_REGION` / `AWS_REGION`. If they differ from the bucket's region you get `BadRequestException: incorrect S3 URI`.

2. **Word extraction** (`_extract_words`): pulls word-level items with `start` / `end` timestamps from the Transcribe result. Speaker segments come from `results.speaker_labels.segments`.

3. **PII detection**: a single Bedrock call over the full transcript text via the shared model router (`converse_or_responses`), so it honours every per-model rule (Claude / Nova / OpenAI GPT-5.x, thinking / reasoning effort). It uses the standard detection inference config (`get_inference_config_from_yaml`), **not** a hardcoded `maxTokens`, so long transcripts don't truncate and `maxTokens` stays above any extended-thinking budget. The response is read with `safe_get_response_content`, which skips reasoning blocks and warns on truncation (the same extractor used by the text and vision paths).

Returns the standard detection payload:

```python
{
    "detections": [{"content", "type", "confidence", "detection_source"}, ...],
    "token_usage": {...},
    "file_type": "audio",
    "transcript": "...",        # full transcript text
    "words": [...],             # word-level items with timestamps
    "speaker_segments": [...],  # diarization segments
}
```

By default the detection model is `model.id` from `config.yaml`; set `audio.detection_model` to override it for audio only.

## Step 2: Synthetic Generation

Audio uses the **same** synthetic generator as every other format: `batch_generate_synthetic_pii` on the detected entities. See [Synthetic Generation](synthetic.md) for clustering, batching, the repair pipeline, and the Faker fallback. No audio-specific logic here; the detected PII strings are replaced with realistic fakes (e.g. a phone number → a different valid-looking phone number) so the redacted audio still sounds natural.

## Step 3: Redaction & Splicing

`redact_audio(s3_client, source_bucket, source_key, output_bucket, pii_mapping, detections, detection_data, config, job_id, output_prefix)`:

1. **Locate PII spans** (`_find_pii_spans`): matches each detected PII string to the timed words from Transcribe, producing `{start, end, fake_text}` spans. Each detection is also annotated with `timestamp_start` / `timestamp_end` for the redaction report.

2. **Build replacement segments** per `audio.redaction_mode`:
   - **`synthetic`** (default): Amazon Polly (`_synthesize_replacement`) synthesizes speech for the fake text using `audio.polly_voice` (default `Joanna`).
   - **`silence`**: a silent segment (`_generate_silence`) the length of the PII span.

3. **Splice** (`_splice_audio`): **ffmpeg** cuts the original audio at the PII timestamps and concatenates the surrounding audio with the replacement/silence segments, producing a clean anonymized track. If no spans are found, the original is simply transcoded to a normalized mono WAV.

4. **Speaker-formatted transcript** (`_build_speaker_transcript`): groups words by diarization segment into a readable `spk_0: ... / spk_1: ...` conversation, with PII replaced by the synthetic values.

Returns `(output_key, replaced_count, found_originals)`.

### ffmpeg

Audio splicing shells out to the **ffmpeg binary** (not a Python package). The path resolves as:

```python
FFMPEG = shutil.which("ffmpeg") or "/opt/bin/ffmpeg"
```

- **In Lambda:** provided by the **ffmpeg Lambda layer**, which mounts at `/opt/bin/ffmpeg`.
- **Locally** (notebook / testing): install via `brew install ffmpeg` (macOS) or your package manager, and make sure its directory is on `PATH` so `shutil.which` finds it (otherwise it falls back to the Lambda path and fails with `No such file or directory: '/opt/bin/ffmpeg'`).

## Output Files

For an input `dialog.mp3`, audio redaction writes (under the job's output prefix):

| File                                                | Purpose                                                |
| --------------------------------------------------- | ------------------------------------------------------ |
| `redacted/audio/dialog_anonymized.wav`              | The PII-removed audio (WAV, mono, `audio.sample_rate`) |
| `redacted/audio/dialog_redacted_transcript.txt`     | Speaker-formatted transcript with PII replaced         |
| `intermediate/audio/dialog_original_transcript.txt` | Original speaker-formatted transcript (review/audit)   |

Output is always **WAV** regardless of input format.

## Configuration

The `audio:` section of `src/config.yaml`:

```yaml
audio:
  redaction_mode: "synthetic" # "synthetic" (Polly speech) or "silence" (mute PII)
  polly_voice: "Joanna" # Amazon Polly voice for synthetic replacements
  sample_rate: "16000" # output WAV sample rate (Hz)
  language_code: "en-US" # Amazon Transcribe language
  # detection_model: "..."     # optional: override the PII detection model for audio only
```

See [Configuration](config.md) for the full reference.

## AWS Services & IAM

The audio path requires permissions for, in addition to S3 and Bedrock:

- **Amazon Transcribe**: `transcribe:StartTranscriptionJob`, `transcribe:GetTranscriptionJob`
- **Amazon Polly**: `polly:SynthesizeSpeech`

These are granted to the detection and redaction roles in both the CloudFormation and Terraform deployments. See [Security](SECURITY.md) and [IAM Permissions](IAM-Permissions.md).

## Limitations & Notes

- **Output is WAV**, even for `.mp3` input.
- **English-tuned by default**: set `audio.language_code` for other Transcribe-supported languages (the detection prompt is still English-oriented; adjust prompts for non-English content).
- **Detection accuracy depends on transcription accuracy**: heavy accents, overlapping speech, or poor audio quality reduce both transcription and PII-detection quality. Always validate redacted output (see the disclaimer in the main [README](../README.md)).
- **Span matching is text-based**: PII is removed where the transcribed words match a detection; mis-transcribed PII may not be located. Use `silence` mode when maximum certainty is required.
- **Region**: Transcribe and the input bucket must be in the same region.
