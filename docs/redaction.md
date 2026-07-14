# Redaction (Step 3)

Step 3 takes the synthetic mapping from Step 2 and applies it to every file, replacing each original PII value with its synthetic replacement. Each file is processed independently as a separate Lambda invocation (up to 2 concurrent via Step Functions Map state).

DynamoDB status: `SYNTHETIC_COMPLETE` → `REDACTING` → `REDACT_COMPLETE`

> **Audio** (`.mp3`, `.wav`) redaction works differently: it splices Amazon Polly speech (or silence) into the original audio at the PII timestamps using ffmpeg, rather than text replacement. It is documented separately: [Audio PII Redaction](audio.md).

---

## How Replacement Works

Every format uses the same core replacement strategy:

1. **Longest match first**: PII values are sorted by length (descending) before replacement. This prevents `"Sarah"` from being replaced inside `"Sarah Elizabeth Johnson"` before the full name is handled.

2. **Two-pass with placeholders**: Original PII is first replaced with invisible Unicode placeholders (Private Use Area characters), then placeholders are swapped with synthetic values. This prevents cascading corruption when one synthetic value happens to match another PII value.

   ```
   Pass 1: "Sarah Johnson" → "\ue000\ue001\ue001\ue000"    (placeholder)
   Pass 2: "\ue000\ue001\ue001\ue000" → "Laura Bennett"     (synthetic)
   ```

3. **Word boundary for short PII**: Values ≤3 characters use regex word boundary matching (`\bPII\b`) to avoid replacing inside longer words.

4. **Normalized fallback**: If exact match fails, tries with collapsed whitespace and canonicalized punctuation.

---

## Per-Format Redaction

### Text Files (`.txt`, `.json`)

Straightforward string replacement on the full file content.

- **Output**: Same format (`.txt` or `.json`)
- **Markers**: When `markers.text: true`, synthetic values are wrapped: `***Laura Bennett***`

### CSV (`.csv`)

Cell-by-cell replacement.

- **Output**: `.csv` (no highlight) or `.xlsx` (with highlight)
- **Markers**: When `markers.tabular: true`, the CSV is converted to Excel and cells containing PII get yellow background fill

### Excel (`.xlsx`)

Cell-by-cell replacement using openpyxl.

- **Output**: `.xlsx` (preserves original formatting)
- **Markers**: When `markers.tabular: true`, replaced cells get yellow background fill

### Word (`.docx`)

Run-level replacement: Word internally splits text into "runs" (segments with the same formatting). The replacer reconstructs PII across run boundaries and replaces at that level, preserving all formatting (bold, italic, font, size).

- **Output**: `.docx` (preserves original formatting)
- **Markers**: When `markers.word: true`, only the synthetic replacement text gets yellow highlight (the run is split so surrounding text stays unhighlighted)

### PDF: Text-Based

Text is extracted via pypdf, replaced using the text engine, and saved as plain text.

- **Output**: `.txt` (layout not preserved; use image-based for layout fidelity)
- **When to use**: When text content matters more than visual layout

### PDF: Image-Based

Every page is flattened to a redacted image, so the output PDF has **no text layer**: the original PII cannot leak through a hidden/searchable text layer (a known failure mode of annotation-based redaction).

```
Render page to image at 300 DPI (pypdfium2)
  → Build mask from all PII bounding boxes (Textract-derived, with padding + dilation)
  → White-out (synthetic) or black-out (blackout) the masked regions
  → Draw synthetic text sized/wrapped to fit each box (synthetic mode only)
  → Assemble the redacted page images into an image-based PDF (Pillow)
```

Bounding boxes come from Textract (refined in Step 1), so redaction placement is independent of the PDF renderer. Embedded images are redacted automatically: flattening the whole page rasterizes them along with the text, so PII baked into embedded images is covered without a separate extract/re-embed step.

> **Why image-based?** pypdfium2 (Apache-2.0/BSD) renders pages but does not edit text in place. Flattening to an image is both the permissively licensed path and the leak-proof one: there is no residual text layer for PII to hide in.

- **Output**: `.pdf` (image-based; layout preserved, not text-searchable)
- **Markers**: When `markers.image: true` (the "Bounding boxes (PDF, images)" UI toggle), colored borders are drawn around redacted areas on both PDF and image output

### Standalone Images (`.jpg`, `.png`, `.tiff`, `.bmp`, `.webp`)

PIL-based pixel redaction using bounding boxes from detection.

- White rectangle over PII area → synthetic text overlay
- Blackout mode: black rectangle instead
- Multi-page TIFF: each page processed independently
- **Output**: Same image format

---

## Redaction Modes

| Mode                    | Text Formats              | PDF / Images                        |
| ----------------------- | ------------------------- | ----------------------------------- |
| **Synthetic** (default) | PII → realistic fake data | White fill + synthetic text overlay |
| **Blackout**            | PII → `[REDACTED]`        | Solid black rectangle (no text)     |

---

## Redaction Report

Each file gets a per-file report at `{job}/intermediate/redactions/{filename}/redactions.json`:

```json
{
  "source_key": "job-123/document.pdf",
  "redacted_s3_key": "job-123/document.pdf",
  "file_type": "pdf_image",
  "redaction_mode": "synthetic",
  "total_detections": 45,
  "unique_pii_values": 20,
  "unique_replaced": 18,
  "replaced_detections": 40,
  "not_redacted": 3,
  "mappings": [
    {
      "original": "John Smith",
      "synthetic": "Robert Chen",
      "type": "person_name",
      "confidence": 0.95,
      "replacement_status": "rasterized",
      "page_num": 1
    }
  ]
}
```

### Replacement Status

Each detection in the report has a `replacement_status` that tells you exactly what happened:

| Status                | What It Means                                                                                 |
| --------------------- | --------------------------------------------------------------------------------------------- |
| `text_replaced`       | Successfully replaced: the PII was found in the document and swapped with synthetic data     |
| `rasterized`          | Successfully replaced: the page was converted to an image and PII was pixel-redacted         |
| `not_redacted`        | PII was detected but could NOT be redacted: see `not_redacted_reason` for why                |
| `no_synthetic`        | No synthetic replacement was generated for this PII value                                     |

When status is `not_redacted`, the `not_redacted_reason` field explains why:

| Reason                   | What It Means                                                                                 |
| ------------------------ | --------------------------------------------------------------------------------------------- |
| `text_not_found_on_page` | The PII text couldn't be located on the PDF page (OCR mismatch, text in a different encoding) |
| `no_bounding_box`        | Image-based detection didn't return coordinates for this PII                                  |
| `signature`              | Detected as a signature or biometric, intentionally skipped                                  |
| `no_match_in_document`   | PII wasn't found anywhere in the document (likely an LLM hallucination)                       |

---

## Extra Occurrence Detection

The LLM in Step 1 might report `"John Smith"` once, but it could appear 5 times in the document. For text formats the redaction step catches all of them:

- **Text formats** (Word, Excel, CSV, TXT, text-based PDF): the replacement engine counts actual occurrences and reports the difference, with extra finds flagged as `detection_source: "text_search"` for an accurate total count.
- **Image-based PDF / images**: every detected PII instance is redacted at its Textract-derived bounding box; coverage comes from detection (Step 1), not a post-hoc text search.

---

## S3 Access

| Operation   | Bucket | Purpose                                                   |
| ----------- | ------ | --------------------------------------------------------- |
| `GetObject` | Input  | Read original file                                        |
| `GetObject` | Output | Read detection JSON (Step 1) + synthetic mapping (Step 2) |
| `PutObject` | Output | Write redacted file + redaction report                    |

---

## Key Files

| File                           | Purpose                                                                             |
| ------------------------------ | ----------------------------------------------------------------------------------- |
| `handlers/redact_handler.py`   | Lambda entry point: reads inputs, routes to redactor, builds report                |
| `core/redactor.py`             | Format router: dispatches to format-specific redaction functions                   |
| `core/text_replacer.py`        | Shared replacement engine: longest-first, placeholders, word boundary              |
| `redaction/pdf_redactor.py`    | PDF image redaction: render (pypdfium2), flatten-to-image pixel redaction, PDF assembly |
| `processors/word_processor.py` | DOCX run-level replacement                                                          |
| `helpers/font_config.py`       | Consistent font loading across Lambda and local environments                        |
