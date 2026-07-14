# PII Anonymizer Frontend

Streamlit web app for uploading documents, monitoring processing jobs, and reviewing redaction results. Connects to your deployed PII Anonymizer infrastructure (S3, DynamoDB, SQS).

> **This is a demonstration UI, not a production frontend.** It exists only to showcase the pipeline and make it easy to try out. It has no authentication or access control and is not hardened for production. For real use, build your own frontend with the authentication, authorization, and security controls appropriate to your environment.

## Prerequisites

- Python 3.10+
- AWS CLI configured with credentials that can access your deployed resources
- Deployed PII Anonymizer stack (CFN or Terraform)

## Setup

**Option A: Auto-generate `.env` from stack outputs (recommended)**

```bash
# From the project root
make frontend-env-cfn   # if deployed with CloudFormation
make frontend-env-tf    # if deployed with Terraform
```

**Option B: Manual setup**

```bash
cp .env.example .env
# Edit .env with your resource names
```

The `.env` file contains:

| Variable              | Required | What it is                                                  |
| --------------------- | -------- | ----------------------------------------------------------- |
| `AWS_REGION`          | Yes      | Region where the stack is deployed                          |
| `INPUT_BUCKET`        | Yes      | S3 input bucket name                                        |
| `OUTPUT_BUCKET`       | Yes      | S3 output bucket name                                       |
| `INPUT_PREFIX`        | No       | S3 prefix for uploads (default: `pii_data/`)                |
| `OUTPUT_PREFIX`       | No       | S3 prefix for results (default: `redacted/`)                |
| `DYNAMODB_TABLE_NAME` | Yes      | PII tracking table name                                     |
| `CONFIG_BUCKET`       | No       | Artifact bucket: enables the Settings panel in the sidebar |
| `CONFIG_KEY`          | No       | S3 key for config.yaml (default: `config/config.yaml`)      |

## Run

```bash
# From the project root, install all dependencies (includes frontend)
uv sync --all-extras
source .venv/bin/activate

# Start the app
cd frontend
streamlit run app.py
```

## Features

### Upload

- **Single file mode**: upload one or more files, each processed independently
- **Batch mode**: upload multiple files as one job. Auto-generates a folder name (`batch_YYYYMMDD_HHMMSS`) or lets you specify one

Supported formats: PDF, DOCX, XLSX, CSV, TXT, JPG, PNG, TIFF, BMP, WebP.

### Processing Status

After upload, the app polls DynamoDB every few seconds and shows live status updates:

`WAITING` → `IN_PROGRESS` → `DETECTING` → `DETECT_COMPLETE` → `GENERATING_SYNTHETIC` → `SYNTHETIC_COMPLETE` → `REDACTING` → `COMPLETE`

For batch jobs, shows per-file progress and an aggregate summary.

### Results

On completion:

- Download links for each redacted file from S3
- Redaction report showing what was detected and replaced per file
- Token usage and estimated Bedrock cost breakdown

### Processing History

Browse past jobs from DynamoDB. Select any job to load its redaction reports and download results.

### Sidebar: Pipeline Settings

When `CONFIG_BUCKET` is set in `.env`, the sidebar shows live pipeline settings loaded from `config.yaml` in S3:

- **PDF processing**: switch between image and text approach
- **Redaction mode**: synthetic or blackout
- **Visual markers**: toggle bounding box overlay, Word highlights, Excel cell highlights

Changes are saved back to S3 immediately. They take effect on the next Lambda cold start.

## AWS Permissions

The IAM identity running the frontend needs:

| Action                            | Resource                                       |
| --------------------------------- | ---------------------------------------------- |
| `s3:PutObject`                    | Input bucket                                   |
| `s3:GetObject`, `s3:ListBucket`   | Output bucket                                  |
| `dynamodb:Query`, `dynamodb:Scan` | PII tracking table                             |
| `s3:GetObject`, `s3:PutObject`    | Artifact bucket (only if using Settings panel) |
