# Processing Modes: Realtime vs Batch

The pipeline supports two processing modes that control how files enter the system. Both modes use the same Step Functions pipeline, Lambda functions, and redaction logic: the only difference is how files are triggered.

Set the mode during deployment via `ProcessingMode` (CFN) or `processing_mode` (Terraform).

---

## Realtime Mode

Files are processed immediately when uploaded to S3.

```
S3 Upload → S3 Event Notification → SQS → Router Lambda → Step Functions
```

### How It Works

1. You upload a file to the input S3 bucket
2. S3 sends an event notification to the SQS queue
3. SQS triggers the Router Lambda
4. Router checks the concurrency counter, and if a slot is available, it starts a Step Functions execution
5. If the file is inside a folder, Router waits 30 seconds for more files, then lists the entire folder and processes all files as one job

### When to Use

- Processing individual files or small batches as they arrive
- Interactive workflows where users upload and expect results quickly
- Low-to-medium volume (files arrive throughout the day)

### Infrastructure Difference

Realtime mode creates an **S3 event notification** on the input bucket that sends `s3:ObjectCreated:*` events to the SQS queue. This requires the SQS queue policy to allow the S3 bucket to send messages.

If the input bucket uses a customer-managed KMS key, the SQS KMS key policy must allow `s3.amazonaws.com` to call `kms:GenerateDataKey` and `kms:Decrypt`.

---

## Batch Mode

Files are collected in the input bucket and processed on a schedule.

```
Files accumulate in S3 → EventBridge Schedule (every N minutes) → Batch Trigger Lambda → SQS → Router Lambda → Step Functions
```

### How It Works

1. You upload files to the input bucket: nothing happens immediately
2. Every N minutes (default 5), EventBridge triggers the Batch Trigger Lambda
3. Batch Trigger scans the input bucket and groups files by folder:
   - `sponsor1/file1.pdf`, `sponsor1/file2.docx` → one job called `sponsor1`
   - `sponsor2/report.pdf` → one job called `sponsor2`
   - Root-level files → each file is its own job
4. For each job, Batch Trigger checks DynamoDB, skipping jobs that are already `COMPLETE` or currently running
5. New/failed jobs are sent to SQS as one message per job
6. Router picks them up and starts Step Functions executions

### Folder Depth

`JOB_FOLDER_DEPTH` (default 1) controls how files are grouped:

| Depth | File Path                         | Job ID                    |
| ----- | --------------------------------- | ------------------------- |
| 1     | `sponsor1/policy1/claim.pdf`      | `sponsor1`                |
| 2     | `sponsor1/policy1/claim.pdf`      | `sponsor1/policy1`        |
| 3     | `sponsor1/policy1/claim1/doc.pdf` | `sponsor1/policy1/claim1` |

Depth 1 means all files under a top-level folder become one job. Depth 2 groups by the second-level subfolder, useful for hierarchical data like `sponsor/policy/files`.

### Skip Logic

The Batch Trigger checks DynamoDB before queuing each job:

| DDB Status               | Action                       |
| ------------------------ | ---------------------------- |
| No record                | Queue it (new job)           |
| `COMPLETE`               | Skip (already done)          |
| `FAILED`                 | Queue it (automatic retry)   |
| Active + fresh (<30 min) | Skip (still running)         |
| Active + stale (>30 min) | Queue it (reclaim stuck job) |

This means failed jobs are automatically retried on the next scan. No manual intervention needed.

### When to Use

- Large-volume processing where files are uploaded in bulk
- Environments where S3 event notifications are restricted by SCP
- Scheduled processing windows (upload during the day, process overnight)
- When you want automatic retry of failed jobs without re-uploading

### Infrastructure Difference

Batch mode creates an **EventBridge scheduled rule** (`rate(5 minutes)`) that triggers the Batch Trigger Lambda. No S3 event notification is created on the input bucket.

The schedule interval is configurable via `BatchTriggerSchedule` (CFN) or the EventBridge schedule expression (Terraform).

---

## Comparison

|                           | Realtime                             | Batch                                                |
| ------------------------- | ------------------------------------ | ---------------------------------------------------- |
| **Trigger**               | S3 event notification → SQS          | EventBridge schedule → Batch Trigger → SQS           |
| **Latency**               | Seconds after upload                 | Up to N minutes (schedule interval)                  |
| **File grouping**         | Folder detected by Router (30s wait) | Folder grouped by Batch Trigger (configurable depth) |
| **Failed job retry**      | Manual (re-upload file)              | Automatic (next scheduled scan)                      |
| **S3 event notification** | Required on input bucket             | Not needed                                           |
| **SQS KMS key policy**    | Must allow `s3.amazonaws.com`        | Not needed (Batch Trigger sends directly)            |
| **Best for**              | Interactive, low-medium volume       | Bulk processing, restricted environments             |

---

## Switching Modes

To switch between modes, change the parameter and redeploy:

**CloudFormation**: Update `ProcessingMode` in `parameters.json` to `"realtime"` or `"batch"`, then `make cfn-deploy`.

**Terraform**: Update `processing_mode` in `terraform.tfvars` to `"realtime"` or `"batch"`, then `make tf-deploy`.

The deployment handles creating/removing the S3 event notification and EventBridge schedule automatically.
