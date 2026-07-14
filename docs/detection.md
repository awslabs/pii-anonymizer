# PII Detection (Step 1)

The detection step identifies PII in uploaded documents. It runs as a Map state in Step Functions: one Lambda invocation per file, up to 2 concurrent.

## Pipeline Flow

```
S3 document → Detection Handler → Route by extension → Processor → Bedrock LLM → Detection JSON → S3
                                                                  ↘ Textract (image-based only) ↗
```

DynamoDB status: `IN_PROGRESS` → `DETECTING` → `DETECT_COMPLETE`

## File Routing

The detection handler (`pii_detection_handler.py`) routes each file to the correct processor based on extension:

```python
EXTENSION_MAP = {
    ".txt":  detect_pii_txt,
    ".docx": detect_pii_word,
    ".xlsx": detect_pii_excel,
    ".csv":  detect_pii_csv,
    ".json": detect_pii_json,
    ".jpg":  detect_pii_image,   # + .jpeg, .png, .tiff, .tif, .bmp, .webp
    ".mp3":  detect_pii_audio,   # + .wav: Transcribe → LLM (see docs/audio.md)
}

# PDFs route based on config.yaml → processing.approach
PDF_APPROACHES = {
    "text":  detect_pii_pdf_text,   # pypdf text extraction → text LLM
    "image": detect_pii_pdf_image,  # Page renders → vision LLM + Textract
}
```

> **Audio** (`.mp3`, `.wav`) routes to `detect_pii_audio`, which transcribes the file with Amazon Transcribe before detecting PII in the transcript. It has its own end-to-end guide: [Audio PII Redaction](audio.md).

---

## Per-Format Detection Details

### PDF: Image-Based (`processing.approach: "image"`)

The most complex pipeline. Designed for scanned or image-heavy PDFs.

1. PDF downloaded from S3, validated (size, page count)
2. Pages rendered as images at configurable DPI (default 300) via `pdf_processor.py` using pypdfium2
3. Each page processed concurrently with `ThreadPoolExecutor` (default `max_workers: 5`)
4. Per page:
   - Single Textract call via `get_textract_full()` returns word-level bboxes AND OCR text
   - OCR text passed to vision LLM alongside the page image as context
   - Vision LLM returns PII with approximate bounding boxes
   - `enhance_pii_detections_with_textract()` refines LLM bboxes using Textract's precise word coordinates (see [Textract Refinement](#textract-bounding-box-refinement) below)
5. Cross-page sweep searches all unique PII on every page via Textract (see [Cross-Page Sweep](#cross-page-sweep-duplicate-detection) below)
6. Detection JSON + raw Textract data (per page) stored in S3

### PDF: Text-Based (`processing.approach: "text"`)

Designed for digitally generated PDFs with extractable text.

1. PDF downloaded from S3, validated
2. Text extracted via pypdf (`PdfReader`)
3. If no extractable text found, raises error suggesting image-based approach
4. If embedded images found, logs warning: `"PII in images will NOT be detected. Use image-based approach for full coverage."`
5. Text chunked by lines via `text_chunker.py` (`max_txt_chunk_tokens: 20000`, clamped down to the selected model's output limit, e.g. Nova → 10000)
6. Chunks detected concurrently via `threaded_detector.py` (default `max_workers: 6`)
7. Results deduplicated across chunks

### TXT

1. File downloaded from S3, validated (max 10 MB)
2. Text chunked by lines (same chunker as PDF-text)
3. Chunks detected concurrently via threaded detector
4. Results deduplicated

### DOCX (Word)

1. File downloaded from S3, validated (max 50 MB)
2. Text extracted via python-docx (paragraphs + table cells)
3. Chunked and detected concurrently via threaded detector
4. Results deduplicated

### XLSX (Excel)

1. File downloaded from S3, validated (max 50 MB, max 100 sheets)
2. Each sheet's cell values extracted via openpyxl as a separate text chunk
3. A sheet larger than the model's output limit is **sub-split at row boundaries** (so its detection output can't exceed the model's cap, e.g. Nova's 10K)
4. All chunks detected concurrently via threaded detector (label: "Sheet")
5. Results deduplicated across all sheets. Synthetic generation later runs once for cross-sheet consistency

### CSV

1. File downloaded from S3
2. All cell values extracted via `csv.reader`, structured like a single Excel sheet
3. Chunked at row boundaries to stay within the model's output limit (sub-split if large)

### JSON

1. File downloaded from S3
2. All string values recursively extracted (traverses nested dicts and lists)
3. Detected as a single chunk
4. Detections mapped back to JSON paths (e.g., `"patients[0].name"`)

### Images (JPG, JPEG, PNG, BMP, WebP)

1. Image downloaded from S3, validated (max 100 MB, dimension limits 10 to 10000px)
2. Single Textract call for word bboxes + OCR text
3. Vision LLM detection with OCR context
4. Textract refinement of bounding boxes

### TIFF (multi-page)

1. Image downloaded from S3, validated (max 100 MB, max 50 frames)
2. Multi-page TIFFs split into individual frames via PIL
3. Each frame processed as a separate image (Textract + vision LLM)
4. Frames processed concurrently

---

## Textract Bounding Box Refinement

`enhance_pii_detections_with_textract()` in `textract_helper.py` refines the LLM's approximate bounding boxes using Textract's precise word-level coordinates. Three-stage matching:

```
Exact Match → Spatial Match (multi-column) → Fuzzy Match (80% threshold)
```

- **Exact match** (`bbox_source: "textract"`): LLM text matches Textract word exactly
- **Spatial match** (`bbox_source: "textract_spatial"`): finds Textract words near the LLM's approximate coordinates. Handles multi-column layouts where the same text appears in different columns.
- **Fuzzy match** (`bbox_source: "textract_fuzzy"`): 80% similarity threshold catches OCR errors (e.g., "Sm1th" vs "Smith")

If none of the three stages match, the detection keeps the LLM's original approximate bbox (`bbox_source: "llm_vision"`).

### Longest Match First Sorting

Detections are sorted by word count (longest first) before Textract matching. This prevents shorter detections from claiming Textract words needed by longer ones.

Example problem without this: "Learning Works" claims the Textract words "Learning" and "Works", leaving the full address "Learning Works, 1 Main St, Adger, AL, 35006" unable to find its first two words.

### bbox_segments Serialization

Multi-word detections produce `bbox_segments`: individual bounding boxes per word rather than one merged rectangle. These are serialized in the detection output JSON so they survive the S3 round-trip between Step 1 (detect) and Step 3 (redact).

Without this, multi-column address redactions (where words span different columns) render as one giant white box instead of precise per-word rectangles.

---

## Cross-Page Sweep (Duplicate Detection)

After per-page detection completes, the image-based processor runs a cross-page sweep to find PII occurrences the LLM missed:

1. Collect all unique PII values detected across all pages
2. For each page's Textract words:
   - First "claim" word indices for PII the LLM already detected on that page (avoids double-counting)
   - Then search for additional occurrences of ALL unique PII values
3. Extra occurrences added with `detection_source: "textract"`
4. Values ≤3 chars skipped to avoid false positives
5. Excluded values: `Unknown`, `None`, `N/A`, `-`, `Male`, `Female`

This catches:

- A name appearing 3 times on a page but the LLM only reported it once
- A name on page 5 that was only detected on page 1
- Any PII value that appears on pages the LLM didn't flag

### Duplicate Detection in Text-Based Formats

For text-based PDFs and other text formats, duplicate detection happens during redaction (Step 3) instead:

- `text_replacer.py`'s `str.count` finds ALL occurrences of each PII value
- If more are found than the LLM reported, they're tracked as `_extra_occurrences`
- Included in the redaction report with `detection_source: "text_search"`

---

## Embedded Images

For PDFs with embedded images (forms with scanned inserts, photos), there is no separate extract/re-embed step. Image-based redaction renders each whole page to a raster and flattens it, so any PII baked into an embedded image is redacted along with the rest of the page at its Textract-derived coordinates. The embedded image is just part of the rendered page pixels.

---

## Concurrency Model

| Component                    | Default Workers | Configurable Via          |
| ---------------------------- | --------------- | ------------------------- |
| Page detection (image-based) | 5               | `performance.max_workers` |
| Chunk detection (text-based) | 6               | `concurrency.max_workers` |
| Step Functions Map state     | 2               | `MaxConcurrency` in ASL   |

Text-based detection uses `threaded_detector.py` which wraps `ThreadPoolExecutor`. Each thread makes an independent Bedrock API call. Results are collected with a thread lock and deduplicated.

Image-based detection also uses `ThreadPoolExecutor` but each thread makes both a Textract call and a Bedrock call per page.

---

## Detection Config Reference

```yaml
# config.yaml
processing:
  approach: "image" # "text" or "image" (PDF only)

model:
  id: "global.anthropic.claude-sonnet-4-6"
  provider: "anthropic" # "amazon", "anthropic", or "openai"
  capabilities: # Per-model inference registry (see docs/config.md)
    "opus-4-8":
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
    "nova": { sampling: true, thinking: null, max_output_tokens: 10000 } # caps maxTokens + chunk size

detection:
  temperature: 0 # Deterministic, consistent detection (ignored by OpenAI)
  top_k: 5 # Narrow token sampling (ignored by OpenAI; dropped when thinking on)
  reasoning_effort: "none" # OpenAI GPT-5.x only: none|low|medium|high|xhigh. Ignored by Claude/Nova
  max_tokens: 64000 # Text-based output limit (clamped per model, e.g. Nova → 10000)
  image_max_tokens: 16000 # Vision-based output limit (clamped per model)
  enable_thinking: false # CLAUDE ONLY: extended thinking. Ignored by Nova/OpenAI
  thinking_budget_tokens: 4000

concurrency:
  max_workers: 6 # Threads for text chunk detection
  chars_per_token: 4 # Token estimation for chunking
  max_txt_chunk_tokens: 20000 # Max tokens per text chunk (clamped to model output limit)

performance:
  dpi: 300 # PDF page render quality
  max_workers: 5 # Threads for image page detection
```

---

## Prompt Customization

Detection prompts are in `src/core/prompts.py`. Default prompts target financial and healthcare documents.

Key prompt behaviors:

- **Address splitting**: explicitly instructs the LLM to detect Street, City, State, Zip as separate items when they're in separate form fields
- **Work state**: vision prompt includes "work state/province codes" in demographics list
- **Male/Female exclusion**: excludes "Male"/"Female" words (not just single letters M/F)

Prompt constants:

- `VISION_SYSTEM_PROMPT` / `VISION_TASK_PROMPT`: image-based detection (uses `<PAGE_IMAGE>` placeholder for multimodal content)
- `SYSTEM_PROMPT` / `PII_DETECTION_PROMPT`: text-based detection

Review and customize these for your domain before deploying.

---

## Detection Output Format

Stored at `{output_prefix}/intermediate/detections/{safe_name}/detections.json`:

```json
{
  "detections": [
    {
      "content": "John Smith",
      "type": "name",
      "confidence": 0.95,
      "page_num": 1,
      "bounding_box": {"left": 100, "top": 200, "width": 150, "height": 20},
      "bbox_segments": [
        {"left": 100, "top": 200, "width": 60, "height": 20},
        {"left": 170, "top": 200, "width": 80, "height": 20}
      ],
      "bbox_source": "textract_exact",
      "detection_source": "llm"
    }
  ],
  "file_type": "pdf_image",
  "token_usage": {"input_tokens": 5000, "output_tokens": 2000, "requests": 3},
  "failed_chunks": [],
  "textract_pages": [
    {"page": 1, "ocr_text": "...", "raw": {...}}
  ]
}
```

| Field              | Values                                                                       | Description                                                                 |
| ------------------ | ---------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `file_type`        | `pdf_image`, `pdf_text`, `txt`, `xlsx`, `csv`, `json`, `image`               | Determines redaction strategy in Step 3                                     |
| `bbox_source`      | `textract`, `textract_spatial`, `textract_fuzzy`, `llm_vision`, `form_field` | How the bounding box was obtained                                           |
| `detection_source` | `llm`, `textract`                                                            | LLM detected it vs cross-page sweep found it                                |
| `bbox_segments`    | Array of `{left, top, width, height}`                                        | Per-word bboxes for multi-word detections (image-based only)                |
| `textract_pages`   | Array of `{page, ocr_text, raw}`                                             | Raw Textract data per page (stored separately in S3 for debugging)          |
| `json_paths`       | Array of strings                                                             | JSON-only: paths where the PII value was found (e.g., `"patients[0].name"`) |

---

## Validation Guards

| Check            | PDF   | Image     | Word  | Excel      | TXT   |
| ---------------- | ----- | --------- | ----- | ---------- | ----- |
| Max file size    | 50 MB | 100 MB    | 50 MB | 50 MB      | 10 MB |
| Max pages/frames | 1000  | 50 (TIFF) | N/A   | 100 sheets | N/A   |
| Min dimension    | N/A   | 10px      | N/A   | N/A        | N/A   |
| Max dimension    | N/A   | 10000px   | N/A   | N/A        | N/A   |

All limits configurable in `config.yaml` under `validation`. Customer can increase for their use case.

---

## Error Handling

- Failed files recorded in DynamoDB via `append_failed_file(job_id, ts, "detect", source_key, error)`
- Step Functions Map state: `ToleratedFailurePercentage: 50`, up to half the files can fail without aborting the job
- Throttling errors (Bedrock rate limits) caught by `throttle_handler.py`, re-raised as `ThrottlingException` for Step Functions retry
- Retry config: 5 attempts, 10s initial interval, 2.5x exponential backoff
- Retried errors: `Sandbox.Timedout`, `Lambda.TooManyRequestsException`, `ThrottlingException`, `ServiceQuotaExceededException`, `ProvisionedThroughputExceededException`, `RequestLimitExceeded`, `ServiceUnavailableException`
- LLM response validation via `model_schemas.py` dataclasses (`TextBasedOutput`/`ImageBasedOutput`). JSON is parsed by `extract_json_dict()`, a layered, repair-tolerant parser (handles control characters, code fences, preamble, trailing commas, and truncated output)
- **Fail-loud on truncation:** if a detection response hits the model's output-token limit (`stopReason=max_tokens`), the chunk is raised as a **failure** (job fails) rather than silently returning a partial PII list. Use a larger-output model (Claude/GPT) or smaller input for very dense documents
- A detection API failure (after retries, or a non-retryable error) is **raised**, never swallowed into an empty result, so a failed detection fails the job instead of producing a falsely-"clean" document

---

## S3 Access

| Operation   | Bucket | Purpose                                          |
| ----------- | ------ | ------------------------------------------------ |
| `GetObject` | Input  | Read source document                             |
| `PutObject` | Output | Write detection JSON, Textract raw data per page |

---

## Key Files

| File                                | Purpose                                                                                                 |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `handlers/pii_detection_handler.py` | Lambda entry point, routes to processor by extension                                                    |
| `core/pii_detector.py`              | Vision LLM invocation (`invoke_model`), text LLM invocation (`invoke_model_for_text`), response parsing |
| `processors/pdf_image_processor.py` | PDF image pipeline: render → detect → Textract refine → cross-page sweep                                |
| `processors/pdf_text_processor.py`  | PDF text pipeline: pypdf extract → chunk by lines → threaded detect                                   |
| `processors/txt_processor.py`       | Plain text: chunk → threaded detect                                                                     |
| `processors/word_processor.py`      | Word: python-docx extract → chunk → threaded detect                                                     |
| `processors/tabular_processor.py`   | Excel (per-sheet chunks), CSV (single chunk), JSON (recursive extract, single chunk)                    |
| `processors/image_processor.py`     | Standalone images: Textract + vision LLM. Multi-page TIFF support.                                      |
| `helpers/textract_helper.py`        | Textract OCR + 3-stage bbox refinement (exact → spatial → fuzzy)                                        |
| `helpers/threaded_detector.py`      | `ThreadPoolExecutor` wrapper for concurrent chunk detection                                             |
| `helpers/text_chunker.py`           | Text splitting by lines to stay under LLM context limits                                                |
| `helpers/pdf_processor.py`          | PDF page rendering to images (supports S3 URIs)                                                         |
| `helpers/page_type_checker.py`      | Detect text vs scanned pages in PDFs                                                                    |
| `helpers/token_tracker.py`          | Token usage tracking across LLM calls                                                                   |
| `helpers/model_config_helper.py`    | Bedrock model config (inference params, concurrency settings)                                           |
| `core/prompts.py`                   | All detection and generation prompts                                                                    |
| `validation/pdf_validator.py`       | PDF size/page count validation                                                                          |
| `validation/document_validator.py`  | TXT/Word/Excel file validation                                                                          |
| `validation/model_schemas.py`       | Dataclass-based I/O validation for LLM responses                                                        |
