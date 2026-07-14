# Configuration Reference

All processing behavior is controlled by `src/config.yaml`. This file is loaded at Lambda startup via the config loader, which checks three sources in order:

1. **S3**: `CONFIG_BUCKET`/`CONFIG_KEY` environment variables (set by the deployment)
2. **Local file**: `/var/task/config.yaml` (bundled with Lambda code)
3. **Hardcoded defaults**: fallback values in `config_loader.py`

Both `make cfn-deploy` and `make tf-deploy` automatically upload `src/config.yaml` to your artifact bucket at `config/config.yaml`. The Lambda environment variables point to this S3 location, so the S3 copy always takes priority over the bundled file.

There are two ways to change behavior:

- **Edit `src/config.yaml` and redeploy**: bakes new defaults into the deployment and uploads them to S3.
- **Use the Streamlit frontend's Pipeline Settings panel**: writes directly to `config.yaml` in S3 with no redeploy.

The Lambda handlers read the config from S3 on every invocation (no caching), so changes from either method take effect on the next file processed.

> **Who owns the S3 config (last writer wins).** Both methods write the same S3 object (`config/config.yaml`):
>
> - `make cfn-deploy` / `make tf-deploy` overwrite it with `src/config.yaml` (the repo defaults).
> - The Streamlit Pipeline Settings panel overwrites it with the current UI values when you save or process.
>
> Whichever ran most recently wins. In practice this means **once deployed, the frontend is the live source of truth**. A `terraform apply` will revert any UI edits back to the `src/config.yaml` defaults. Treat `src/config.yaml` as the deploy-time default and the UI as the runtime control, and keep the two in sync if you rely on redeploys.

---

## `processing`: Controls how PDF files are analyzed

Determines whether PDFs go through the image-based pipeline (vision model + Textract OCR) or the text-based pipeline (text extraction + text model). This setting only affects PDFs. Word, Excel, CSV, TXT, and images always use their native pipeline.

```yaml
processing:
  approach: "image" # "image" = vision model + bounding boxes, "text" = text extraction
  process_embedded_images: true # When true, images inside text PDFs are extracted and scanned for PII
```

| Value     | PDF Pipeline                                                                                  | Output Format              |
| --------- | --------------------------------------------------------------------------------------------- | -------------------------- |
| `"image"` | Pages rendered to images → vision model detects PII with coordinates → bounding box redaction | PDF (layout preserved)     |
| `"text"`  | Text extracted via pypdf → text model detects PII → string replacement                      | TXT (layout not preserved) |

---

## `model`: Which Bedrock model to use for detection and synthetic generation

Both Step 1 (detection) and Step 2 (synthetic generation) use this model. The model must be enabled in your Bedrock console.

```yaml
model:
  id: "global.anthropic.claude-sonnet-4-6"
  provider: "anthropic" # "anthropic" | "amazon" | "openai": affects API parameter format
```

Supported models:

| Model                 | Example ID                                    | Provider  | API                | Notes                              |
| --------------------- | --------------------------------------------- | --------- | ------------------ | ---------------------------------- |
| Claude Sonnet 4.6     | `global.anthropic.claude-sonnet-4-6`          | anthropic | Converse           | Recommended default; text + vision |
| Claude Opus 4.8 / 4.7 | `us.anthropic.claude-opus-4-8`                | anthropic | Converse           | Highest quality                    |
| Claude Haiku 4.5      | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | anthropic | Converse           | Fastest / cheapest                 |
| Nova Pro / Lite       | `us.amazon.nova-pro-v1:0`                     | amazon    | Converse           | Text + vision                      |
| GPT-5.4               | `openai.gpt-5.4`                              | openai    | Responses (mantle) | US regions only                    |
| GPT-5.5               | `openai.gpt-5.5`                              | openai    | Responses (mantle) | us-east-2 only                     |

**OpenAI GPT-5.x notes:**

- Served via the `bedrock-mantle` Responses API (not Converse). The tool routes these automatically (no caller change). See `src/helpers/model_router.py`.
- They are **reasoning models**: `temperature` and `top_k` are ignored. Use `reasoning_effort` (see below).
- **US regions only.** If your stack runs outside a supported US region, the request cross-regions to a US region. The data stays within AWS but moves across regions: **avoid for EU or data-residency-restricted PII**. Use Claude or Nova for those cases.
- Requires the `bedrock-mantle:CreateInference` IAM permission on the detection and synthetic Lambda roles (added automatically by both deployments).

**Model output limits (choosing a model for large or PII-dense documents):**

Models differ in how many **output** tokens they can return in one response:

| Model                                | Max output tokens |
| ------------------------------------ | ----------------- |
| Claude (Sonnet / Opus / Haiku)       | 64,000            |
| GPT-5.x                              | high              |
| **Amazon Nova (Pro / Lite / Micro)** | **10,000**        |

Detection returns the list of PII it found as JSON. On a **PII-dense document** (a large spreadsheet, a record dump), that list can be large, and on a structured file each compact row can expand into far more output than input. If the response exceeds the model's output limit it is **truncated**.

The tool guards against this:

- `model.capabilities` (below) sets each model's `max_output_tokens`, which clamps the per-request `max_tokens` **and** the detection chunk size; tabular (Excel/CSV) sheets are sub-split at the limit.
- If a detection response **still** truncates, the job **fails loudly** rather than silently redacting an incomplete set of PII.

**Recommendation:** for **large or PII-dense documents, use Claude or GPT** (high output limits: no truncation). **Nova is excellent for normal-sized documents, speed, and cost**, but on very dense content its 10,000-token output limit can be hit (you'll get a clear "truncated: use a larger-output model" failure, never a silent partial redaction).

### `model.capabilities`: the per-model inference registry

This is the single place describing **how each model's request is shaped**. Keys are matched as a substring of the model id, **most-specific first** (list `opus-4-8` before `anthropic`). Built-in defaults in `model_router.py` cover the known models; this config block **overrides/extends** them, so adding a model is one entry here (no code change).

```yaml
model:
  capabilities:
    # sampling: accepts temperature/top_p/top_k?  thinking: which thinking/effort style.
    # efforts: valid effort values.  max_output_tokens: output cap (clamps maxTokens + chunks).
    "opus-4-8":
      {
        sampling: false,
        thinking: "adaptive_effort",
        efforts: [low, medium, high, max, xhigh],
        max_output_tokens: 64000,
      }
    "opus-4-7":
      {
        sampling: false,
        thinking: "adaptive_effort",
        efforts: [low, medium, high, max, xhigh],
        max_output_tokens: 64000,
      }
    "openai.gpt-5":
      {
        sampling: false,
        thinking: "reasoning_effort",
        efforts: [none, low, medium, high, xhigh],
      }
    "nova": { sampling: true, thinking: null, max_output_tokens: 10000 }
    "anthropic":
      { sampling: true, thinking: "enabled_budget", max_output_tokens: 64000 }
```

**The two Claude API generations** (handled automatically by the registry):

- **Legacy Claude** (Sonnet 4.6, Haiku 4.5, Opus 4.5/4.6): accept `temperature`/`top_k`; thinking via `enabled_budget` (`enable_thinking` + `thinking_budget_tokens`).
- **New Claude** (Opus 4.7/4.8): **reject** `temperature`/`top_p`/`top_k`; thinking via `adaptive_effort` (adaptive thinking + an `effort` level low/medium/high/max/xhigh). The old budget format is rejected by these models. The registry sends the right one.

`thinking` values: `"enabled_budget"` (legacy Claude), `"adaptive_effort"` (Opus 4.7/4.8), `"reasoning_effort"` (OpenAI GPT-5.x), or `null` (Nova: no thinking).

---

## `detection`: Inference parameters for Step 1 (PII Detection)

Controls how the LLM behaves when detecting PII. Low temperature and low top_k make detection deterministic: the same document produces the same results every time.

```yaml
detection:
  temperature: 0 # 0 = fully deterministic (ignored by OpenAI reasoning models)
  top_k: 5 # Narrow token choices (ignored by OpenAI; dropped when thinking is on)
  reasoning_effort: "none" # OpenAI GPT-5.x only: none | low | medium | high | xhigh. Ignored by Claude/Nova.
  max_tokens: 64000 # Max output tokens for text-based detection (clamped per model, e.g. Nova → 10000)
  image_max_tokens: 16000 # Max output tokens for image-based detection (clamped per model)
  enable_thinking: false # CLAUDE ONLY: extended thinking. Ignored by Nova/OpenAI.
  thinking_budget_tokens: 4000
```

`image_max_tokens` is lower because vision model responses are shorter (bounding box coordinates + entity text vs full document analysis).

**Per-model controls (each ignored by the others):**

- **`reasoning_effort`**: used by OpenAI GPT-5.x (`reasoning.effort`) **and** new-style Claude Opus 4.7/4.8 (the adaptive-thinking `effort`). `none` (default) for OpenAI = no reasoning; for Opus 4.7/4.8 (which has no `none`) it maps to the lowest valid level. GPT rejects `minimal`; Opus efforts are `low/medium/high/max/xhigh`. The UI offers only values the selected model supports.
- **`enable_thinking`**: Claude (Anthropic) only; gated to Claude in code, so it's never sent to Nova/OpenAI even if set. Off by default. **Legacy Claude** (Sonnet 4.6, Haiku 4.5, Opus 4.5/4.6) uses `enabled_budget` (`thinking_budget_tokens`); **new Claude** (Opus 4.7/4.8) uses `adaptive_effort` (adaptive thinking + the `reasoning_effort` level above). The registry picks the right format per model.
- **Nova** uses neither: standard `temperature`/`top_k`, no thinking.

---

## `synthetic`: Inference parameters for Step 2 (Synthetic PII Generation)

Controls how the LLM behaves when generating fake replacement data. Higher temperature and top_k produce more varied synthetic values: you don't want every "John Smith" across different jobs to become the same fake name.

```yaml
synthetic:
  temperature: 0.8 # 0.8 = creative, diverse fake data (ignored by OpenAI reasoning models)
  top_k: 200 # Wide token choices for variety (ignored by OpenAI)
  reasoning_effort: "none" # OpenAI GPT-5.x only: none | low | medium | high | xhigh
  max_tokens: 64000 # Max output tokens per LLM call (clamped per model, e.g. Nova → 10000)
  enable_thinking: false # CLAUDE ONLY: ignored by Nova/OpenAI
  thinking_budget_tokens: 4000
```

Synthetic generation is a creative step, so `reasoning_effort: none` is recommended (thinking/reasoning add little value here). Batches are sized to `max_synthetic_batch_tokens` so output stays within model limits; truncated batches are repaired (missed values filled by Faker).

---

## `redaction`: Controls how PII is replaced in Step 3 (Redaction)

Determines whether PII is replaced with realistic fake data or blacked out, and whether visual markers are added to help reviewers identify replacements.

```yaml
redaction:
  mode: "synthetic" # "synthetic" = fake data replacement, "blackout" = [REDACTED] / black rectangles
  markers:
    text: true # TXT/JSON: wraps synthetic values with ***value***
    tabular: true # CSV/XLSX: yellow cell background on replaced cells
    word: true # DOCX: yellow text highlight on replaced text only
    image: false # Images/PDF: colored bounding box border around redacted areas
```

Blackout mode skips the entire synthetic generation step (Step 2): no LLM calls for fake data.

Markers are only active in synthetic mode. They help reviewers spot where replacements were made.

---

## `audio`: Settings for audio (.mp3, .wav) PII redaction

Controls transcription, the detection model, and how spoken PII is replaced. Only applies to audio files. See [Audio PII Redaction](audio.md) for the full pipeline.

```yaml
audio:
  redaction_mode: "synthetic" # "synthetic" = Amazon Polly speech, "silence" = mute the PII segment
  polly_voice: "Joanna" # Amazon Polly voice used for synthetic replacements
  sample_rate: "16000" # output WAV sample rate in Hz
  language_code: "en-US" # Amazon Transcribe language code
  # detection_model: "..."    # optional: override the PII detection model for audio only (defaults to model.id)
```

| Key               | Effect                                                                                                                                                         |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `redaction_mode`  | `synthetic` synthesizes replacement speech via Polly; `silence` inserts a silent segment over each PII span. Use `silence` when maximum certainty is required. |
| `polly_voice`     | Any [Amazon Polly voice](https://docs.aws.amazon.com/polly/latest/dg/voicelist.html) (e.g. `Joanna`, `Matthew`). Used only in `synthetic` mode.                |
| `sample_rate`     | Output WAV sample rate in Hz. Output is always mono WAV regardless of input format.                                                                            |
| `language_code`   | Amazon Transcribe language for transcription + diarization.                                                                                                    |
| `detection_model` | Optional override of the Bedrock detection model for audio only. Falls back to `model.id`.                                                                     |

> Transcribe must run in the **same region as the input bucket**, and audio splicing requires the **ffmpeg binary** (Lambda layer in production, `brew install ffmpeg` locally).

---

## `concurrency`: Thread pool sizes and token limits for batching

Controls parallelism within a single Lambda invocation and when to split large workloads into multiple LLM calls.

```yaml
concurrency:
  max_workers: 6 # Threads for parallel page/sheet detection AND concurrent synthetic batches
  chars_per_token: 4 # Ratio for estimating token counts from character counts (4 = English text)
  max_txt_chunk_tokens: 20000 # Large TXT files are split into chunks of this size for detection
  max_synthetic_batch_tokens: 10000 # All PII exceeding this triggers multiple synthetic LLM calls instead of one
```

---

## `validation`: File size limits, page limits, and bounding box color

Files exceeding these limits are rejected before processing starts. The error is logged and the file is tracked as failed in DynamoDB.

```yaml
validation:
  max_file_size_mb:
    pdf: 50 # Max PDF file size in MB
    image: 100 # Max image file size in MB
    word: 50 # Max DOCX file size in MB
    excel: 50 # Max XLSX file size in MB
    txt: 10 # Max TXT file size in MB

  max_pages:
    pdf: 1000 # Max pages in a PDF
    image: 50 # Max pages in a multi-page TIFF

  max_sheets:
    excel: 100 # Max sheets in an Excel workbook

  image_min_dimension: 10 # Min width/height in pixels (rejects tiny images)
  image_max_dimension: 10000 # Max width/height in pixels (rejects oversized images)

  bounding_box_color: [0.8, 0.2, 0.2] # RGB 0-1 range: soft red. Border color when the overlay is on
```

> **Bounding box overlay** (the colored border drawn around redacted areas, for review/audit) is controlled by **`redaction.markers.image`**, the "Bounding boxes (PDF, images)" UI toggle, which governs **both PDF and image** output. `validation.bounding_box_color` only sets the border color. (The old `validation.show_bounding_boxes` key was removed; `redaction.markers.image` is now the single source of truth.)

Set `redaction.markers.image: false` for clean production output. Set `true` for review and audit.

---

## `performance`: Render quality and AWS API timeouts

Controls the DPI for rendering PDF pages to images and AWS API call behavior.

```yaml
performance:
  dpi: 300 # Resolution for PDF-to-image rendering. Higher = better OCR but slower and more memory
  max_retries: 3 # AWS API call retries (separate from Step Functions retries)
  timeout_seconds: 300 # AWS API call timeout in seconds
```

300 DPI is a good balance between OCR accuracy and Lambda memory usage. Increase to 400+ for very small text; decrease to 200 for faster processing of large PDFs.

---

## `clustering`: How PII values are grouped for synthetic generation

Controls how detected PII values are categorized and clustered before being sent to the LLM for synthetic replacement. Related values (e.g., "John Smith" and "J. Smith") are grouped together so they get consistent synthetic replacements.

```yaml
clustering:
  max_cluster_size: 10 # Max items per cluster: prevents unrelated values from merging together
  type_hint_map: # Maps LLM entity type labels → internal categories for clustering
    person_name: [name, first_name, last_name, patient_name, borrower, attorney]
    address: [address, street, city, state, zip, location]
    phone: [phone, fax, telephone]
    ssn: [ssn, social]
    date: [dob, birth, expiration_date]
    org_name: [institution, hospital, clinic, company, bank, employer, court]
    id_generic:
      [
        id,
        record,
        account,
        policy,
        reference,
        case,
        npi,
        license,
        passport,
        vin,
        credit_card,
        ein,
        tax_id,
        docket,
        credit_score,
      ]
```

`type_hint_map` is the fallback when value-based structural patterns (regex for emails, phones, SSNs, etc.) don't match. The LLM's entity type label is looked up in these lists to determine the category.

**To handle a new PII type**: add its LLM label to the appropriate category list. No code changes needed.

---

## `output`: Where results are stored (primarily for local/notebook use)

Controls result storage destinations. In the deployed Lambda pipeline, both S3 and DynamoDB are always used regardless of these settings. These are primarily for the local Jupyter notebook workflow.

```yaml
output:
  store_in_s3: true # Write results to S3
  store_in_dynamodb: true # Write results to DynamoDB
  output_prefix: "redacted/" # Prefix for output files in S3
  organize_by_approach: true # Organize output by processing approach (text/, image/)
```
