# Changelog

All notable changes to the PII Anonymization & Redaction System are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries for releases prior to 3.5.1 were reconstructed from git history.

## [Unreleased]

### Added

- **`CONTRIBUTING.md` and `CODE_OF_CONDUCT.md`** following the awslabs open-source conventions: the Amazon Open Source Code of Conduct, contribution and pull-request process, security issue reporting, and a note to keep real PII/PHI out of bug reports.
- **`NOTICE`** file carrying the Amazon copyright attribution.
- **Hardening guidance** in `docs/SECURITY.md`: a "Hardening Items Not Enforced by Default" section covering the SQS SSL/TLS transport policy, Lambda function-level Dead Letter Queue, container image digest pinning, and Lambda environment-variable customer-managed KMS encryption.

### Changed

- **Relicensed the project to Apache-2.0.** Replaced `LICENSE` with the Apache-2.0 text, flipped every source `SPDX-License-Identifier` header to `Apache-2.0`, and updated the license references in `pyproject.toml`, the README, and the docs. Copyright attribution is retained in `NOTICE` and the source headers.
- **Replaced PyMuPDF (AGPL-3.0) with permissively licensed libraries** to unblock the open-source release. PDF rendering now uses `pypdfium2` (Apache-2.0/BSD); PDF text extraction, validation, and page-type detection use `pypdf`. Detection accuracy verified equivalent (identical PII results vs PyMuPDF on the sample set).
- **Rewrote PDF image-based redaction to a flatten-to-image pipeline.** Each page is rendered (pypdfium2), pixel-redacted with synthetic text via Pillow at Textract-derived coordinates, and re-assembled into an image-based PDF. The output has no text layer, which eliminates the residual-text leak where redacted PII could still be extracted from the searchable layer. `redaction/pdf_redactor.py` shrank from about 2,935 to about 660 lines.
- **Completed the `pyproject.toml` runtime dependencies** so a fresh `uv sync` or `pip install -e .` produces a working environment (added aws-xray-sdk, python-docx, openpyxl, defusedxml, PyYAML, termcolor).
- **Humanized the documentation.** Removed typographic dashes across the README and docs, enlarged the architecture diagram, and verified and completed the Project Structure listing.

### Removed

- **PyMuPDF (`pymupdf`/`fitz`) and `PyPDF2`** dependencies, removed from `pyproject.toml`, `requirements_lambda.txt`, `uv.lock`, the Lambda layer, the notebook, and all documentation. The former font-matched (searchable-text) PDF redaction path is dropped in favor of the leak-proof flatten-to-image approach.
- **`pdf2image`** unused dependency removed from `pyproject.toml` and `uv.lock`.

## [3.5.1] - 2026-06-19

### Added

- **Audio PII redaction (`.mp3`, `.wav`)**: Amazon Transcribe (speaker diarization + word-level timestamps) → PII detection → Amazon Polly synthetic speech or silence → ffmpeg splice; outputs an anonymized WAV plus speaker-formatted redacted and original transcripts.
- **OpenAI GPT-5.4 support** via the Bedrock `bedrock-mantle` Responses API (US regions only); reasoning effort exposed as `none`/`low`.
- **New-style Claude Opus 4.7 / 4.8 support** using adaptive thinking (`thinking.type: adaptive` + `output_config.effort`).
- **Centralized per-model capability registry** (`src/helpers/model_router.py`): single source of truth for each model's sampling support, thinking style, valid effort values, and output-token limit; overridable from `config.yaml` via `model.capabilities` as a sparse delta.
- **Application-level AWS X-Ray tracing** (`src/helpers/observability.py` → `patch_all()`) wired into all six pipeline Lambda handlers: AWS SDK calls (Bedrock, S3, DynamoDB, Textract, Transcribe, Polly, Step Functions) now appear as per-call subsegments. Safe no-op when the SDK is absent.
- **Robust LLM JSON parsing** (`extract_json_dict`) with layered recovery (code-fence strip, balanced-object scan, trailing-comma + truncation repair) for vision and text paths.
- Audio coverage in the live test harness; deterministic test suites (75 tests).
- **Ruff linting enforced in CI**: `.github/workflows/python-lint.yml` runs `ruff check` on changed Python files for every pull request; `pre-commit` added to the dev/test extras; "Code Quality" section added to the README.
- **Dedicated audio documentation**: `docs/audio.md` (full Transcribe → detect → Polly/silence → ffmpeg pipeline), an `audio` section in `docs/config.md`, and Transcribe/Polly entries in `docs/IAM-Permissions.md`.
- Audio walkthrough section in the local Jupyter notebook (`pii_detection_demo.ipynb`).
- `VERSION` and `CHANGELOG.md` files.

### Changed

- All per-model request shaping centralized in `apply_model_capabilities()` (clamps `maxTokens`, applies thinking/effort format, strips `temperature`/`top_p`/`top_k` where unsupported).
- GPT-5.x reasoning effort limited to `none`/`low` this release (higher tiers can exceed the 300s per-call timeout on dense docs; deferred pending a detection-chunking refactor).
- Bounding-box overlay unified under `redaction.markers.image` for both PDF and image output; legacy `validation.show_bounding_boxes` removed.
- Detection fails loud on truncated responses or failed chunks (no silent PII drop); chunk size / `maxTokens` clamped to each model's output limit; Excel/CSV sub-split at row boundaries.
- `create_layer.sh` strips the runtime-provided boto stack so the X-Ray SDK does not bloat the layer.
- Updated the architecture diagram (`images/Architecture.png`) to include the audio pipeline (Amazon Transcribe → detection → Amazon Polly / silence → ffmpeg splice).

### Fixed

- Claude Opus 4.7/4.8 `temperature is deprecated` error: sampling params stripped for no-sampling models; capability resolution made order-independent (robust to S3 alphabetizing `config.yaml` keys).
- Audio + Claude extended thinking: `maxTokens` could fall below the thinking budget (centralized guard added); response parser now skips the leading reasoning block via `safe_get_response_content()`.
- CloudFormation `RedactFunction` duplicate-layer error (pii layer was added via both Globals and the function).
- `import re` shadowing in `text_replacer.py` and `pdf_redactor.py`.
- Frontend env region override and missing Terraform artifact-bucket output.
- GPT region map corrected (`gpt-5.4` includes `us-east-1`).
- Latent `NameError` in `process_pdf_for_pii_redaction()` (missing `config` parameter), plus `F401`/`F841` lint cleanups: code now passes `ruff check`.
- Local Jupyter notebook accuracy: updated stale call signatures (`get_textract_full` now returns 3 values; `replace_pii_in_text` returns 5), switched the bounding-box read to `redaction.markers.image`, hardened the AWS credential cell (explicit profile + cleared cached session to avoid `ExpiredToken` loops), and made local ffmpeg discovery robust for the audio section.
- README project structure now lists `model_router.py`, `observability.py`, and `docs/IAM-Permissions.md`.

### Known limitations

- GPT-5.5 is `us-east-2`-only and subject to AWS-side capacity/stability issues; not offered in the UI this release.
- Amazon Nova has a 10,000 output-token limit and fails loud on very PII-dense documents; use Claude or GPT for dense documents.

## [3.5.0] - 2026-03-29

### Added

- AWS X-Ray tracing (infrastructure-level `Active` tracing on Lambdas and Step Functions).
- Copyright headers across source files.

### Changed

- Persist Amazon Textract data as intermediate artifacts.
- Frontend overhaul; expanded README and documentation.

### Fixed

- Word (DOCX) highlight rendering.
- Image bounding-box placement.

## [3.0.0] - 2026-03-10

### Added

- Step Functions orchestration pipeline with 6 Lambda handlers (router, detection, synthetic, redact, batch-trigger, workflow-tracker).
- SQS backpressure with a dead-letter queue and DynamoDB-based concurrency control.
- Batch processing mode (EventBridge scheduled bucket scan) alongside realtime mode.
- Multi-format support: Word (`.docx`), Excel (`.xlsx`/`.csv`), images, and plain text.
- Output folder-structure preservation and per-format summary JSON.
- Dynamic frontend resource discovery and Makefile targets for frontend config generation.

### Changed

- Reorganized `src/` into modular subdirectories (core, handlers, processors, helpers, redaction, validation, infra).
- Enabled SSE on SQS queues, execution-history logging on Step Functions, and a 1-year minimum log retention.
- Lambda layer auto-builds on deploy.

### Security

- Security remediation: static imports, `defusedxml` for XXE protection, explicit temp directories.
- Updated Pillow to 12.1.1.

### Fixed

- PII detection prompt refinements (exclude non-DOB dates and salary amounts; add employer and policyholder/role-based name detection).
- Bounding-box matching fixes.

## [2.0.0] - 2026-02-05

### Added

- Blackout/masking mode and Word/Excel support (with demo notebooks).
- Name-consistency rules in the vision/LLM prompts.
- Multi-stage Textract bounding-box matching (exact → spatial → fuzzy).

### Changed

- Removed PII from logs; documentation updates.

### Fixed

- `bbox_source` filter normalization; font-size range; lint/line-length cleanups.

## [1.0.0] - 2026-01-02

### Added

- Initial release: core PII detection and redaction for PDFs and images using Amazon Bedrock and Amazon Textract.
- Streamlit frontend and architecture diagram.
- Apache-2.0 license.
