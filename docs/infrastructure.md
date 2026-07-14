# Infrastructure

This document covers the AWS services that orchestrate the pipeline at runtime: how files flow from upload to completion, how concurrency is controlled, how failures are handled, and how jobs are tracked.

---

## Step Functions Pipeline

The state machine (`pii_processing.asl.json`) orchestrates the 3-step pipeline:

```
DetectPII (Map) → DropFiles (Pass) → CheckRedactionMode (Choice)
                                        ├─ blackout → SetEmptyMapping → RedactPII (Map)
                                        └─ synthetic → GenerateSynthetic → RedactPII (Map)
                                                                              ↓
                                                                    PrepareComplete → WorkflowComplete (DDB update)
```

### Map States

Both `DetectPII` and `RedactPII` are Map states that process files in parallel:

- **MaxConcurrency**: 2 (two files processed simultaneously)
- **ToleratedFailurePercentage**: 50 (the pipeline continues even if up to half the files fail: failed files are tracked in DDB)

### Retry Configuration

Every Lambda task retries on transient errors:

```
IntervalSeconds: 10
MaxAttempts: 5
BackoffRate: 2.5
```

Retried errors: `Sandbox.Timedout`, `Lambda.ServiceException`, `Lambda.TooManyRequestsException`, `ThrottlingException`, `ServiceQuotaExceededException`, and other AWS SDK transient errors.

This means a throttled Lambda call waits 10s → 25s → 62s → 156s → 390s before giving up (~10 minutes total).

### DropFiles Optimization

After detection completes, the `DropFiles` Pass state strips `$.files` from the state payload. At 540 files, this saves ~127KB of state data that would otherwise be carried through the remaining steps.

### Blackout Mode Shortcut

`CheckRedactionMode` is a Choice state. If `redaction_mode == "blackout"`, it skips `GenerateSynthetic` entirely (no LLM calls needed) and goes straight to `RedactPII` with an empty mapping key.

### WorkflowComplete

The final state is a direct DynamoDB `updateItem` (not a Lambda) that sets the job status to `COMPLETE` and stores the redaction results summary.

---

## SQS

### Main Queue

- **Visibility timeout**: 90 seconds (configurable via `SQSVisibilityTimeout`)
- **Message retention**: 24 hours
- **Max receive count**: 1000 (messages retry many times before going to DLQ)
- **Batch size**: 1 (Router processes one message at a time)

### Dead Letter Queue (DLQ)

- **Retention**: 14 days
- Messages land here after 1000 failed processing attempts
- Monitor this queue: messages here indicate persistent failures

### Partial Batch Failures

The Router Lambda returns `batchItemFailures`: if a message fails (e.g., concurrency limit reached), only that message goes back to the queue. Successfully processed messages in the same batch are not retried.

```python
return {"batchItemFailures": [{"itemIdentifier": mid} for mid in failed_ids]}
```

---

## DynamoDB

Two tables serve different purposes:

### PII Tracking Table (MappingRegistry)

Tracks job status and stores redaction results. Schema:

| Attribute           | Type   | Description                                              |
| ------------------- | ------ | -------------------------------------------------------- |
| `filename` (PK)     | String | Job ID: folder name for batch, filename for single file |
| `timestamp` (SK)    | String | ISO timestamp, allows multiple runs of the same job     |
| `status`            | String | Current job status (see lifecycle below)                 |
| `updated_at`        | String | Last update timestamp                                    |
| `files`             | List   | File keys in the job                                     |
| `failed_files`      | Map    | `{detect: [...], redact: [...], unsupported: [...]}`     |
| `mapping_s3_key`    | String | S3 path to synthetic_mapping.json                        |
| `redaction_results` | String | JSON summary of per-file redaction results               |
| `error`             | String | Error message (if FAILED)                                |
| `expiration_time`   | Number | TTL: auto-deleted after 90 days                         |

**Job status lifecycle**:

```
QUEUED → IN_PROGRESS → DETECTING → DETECT_COMPLETE → GENERATING_SYNTHETIC → SYNTHETIC_COMPLETE → REDACTING → REDACT_COMPLETE → COMPLETE
                                                                                                                                    ↓
                                                                                                                                  FAILED
```

Status updates are forward-only: a job never goes backward (e.g., from `REDACTING` back to `DETECTING`).

### Idempotency Table

Controls concurrency and prevents duplicate processing. Schema:

| Attribute         | Type   | Description                                                                          |
| ----------------- | ------ | ------------------------------------------------------------------------------------ |
| `doc_key` (PK)    | String | Document identifier or `workflow_counter`                                            |
| `active_count`    | Number | Current number of running Step Functions executions (on the `workflow_counter` item) |
| `status`          | String | `IN_PROGRESS`, `COMPLETE`, or `FAILED`                                               |
| `lock_expiry`     | Number | Unix timestamp: stale locks are overwritten after expiry                            |
| `expiration_time` | Number | TTL: auto-deleted after 7 days                                                      |

**Concurrency counter**: The `workflow_counter` item uses DynamoDB atomic `ADD` with a `ConditionExpression` to enforce the max concurrent workflows limit:

```python
# Router increments (+1) before starting SF
ConditionExpression = "attribute_not_exists(active_count) OR active_count < :max"
UpdateExpression = "ADD active_count :inc"

# Workflow Tracker decrements (-1) when SF completes
UpdateExpression = "ADD active_count :dec"
```

If the counter is at the max, the condition fails and the SQS message goes back to the queue (visibility timeout retry).

Both tables use:

- **PAY_PER_REQUEST** billing
- **Point-in-time recovery** enabled
- **TTL** for automatic cleanup
- Optional **KMS encryption**

---

## EventBridge

Two EventBridge rules:

### Step Functions Completion

Triggers the Workflow Tracker Lambda when any Step Functions execution finishes:

```json
{
  "source": ["aws.states"],
  "detail-type": ["Step Functions Execution Status Change"],
  "detail": {
    "stateMachineArn": ["<state-machine-arn>"],
    "status": ["SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"]
  }
}
```

This ensures the concurrency counter is always decremented, even if the execution fails or times out.

### Batch Trigger Schedule (batch mode only)

Runs the Batch Trigger Lambda on a schedule:

```
rate(5 minutes)    # configurable via BatchTriggerSchedule parameter
```

Only created when `ProcessingMode = "batch"`.

---

## Router Lambda

The Router is the entry point: it receives SQS messages and decides whether to start a Step Functions execution.

### What It Does

1. **Parse message**: handles both S3 event notifications (realtime) and batch-trigger messages
2. **Folder batching**: if a file is inside a folder, waits 30 seconds (`FOLDER_WAIT_SECONDS`) then lists all files in that folder to process them as one job
3. **Filter unsupported files**: removes files with unsupported extensions, tracks them in DDB as unsupported
4. **Check concurrency**: atomically increments the counter in IdempotencyTable. If at max, returns the message to SQS (partial batch failure)
5. **Start execution**: starts Step Functions with the file list, creates DDB tracking record with status `IN_PROGRESS`

### Folder Batching

When a file lands in `s3://bucket/job-123/file1.pdf`:

- Router sees it's in a folder (`job-123/`)
- Waits 30 seconds for more files to arrive
- Lists all files under `job-123/`
- Starts one SF execution for the entire folder

This means uploading 10 files to a folder results in one job, not 10 separate jobs. The 30-second wait is configurable via `FolderWaitSeconds`.

### Concurrency Control

The counter prevents too many Step Functions from running simultaneously (each execution uses multiple Lambda invocations). Default max: 10 concurrent workflows.

If the limit is reached, the SQS message stays in the queue and retries after the visibility timeout (90s). Once a running workflow completes and the counter decrements, the next message succeeds.

---

## Batch Trigger Lambda

In batch mode, there are no S3 event notifications. Instead, the Batch Trigger Lambda runs on a schedule and scans the input bucket for unprocessed files.

### What It Does

1. **Scan**: lists all files in the input bucket (under optional `SCAN_PREFIX`)
2. **Group by folder**: groups files by folder at configurable depth (`JOB_FOLDER_DEPTH`):
   - Depth 1 (default): `sponsor1/file.pdf` → job = `sponsor1`
   - Depth 2: `sponsor1/policy1/file.pdf` → job = `sponsor1/policy1`
   - Root files (no folder): each file is its own job
3. **Check DDB**: for each job, queries the tracking table to decide whether to process:

   | DDB Status               | Action                                         |
   | ------------------------ | ---------------------------------------------- |
   | No record                | Process (new job)                              |
   | `COMPLETE`               | Skip                                           |
   | `FAILED`                 | Process (retry)                                |
   | Active + fresh (<30 min) | Skip (already running)                         |
   | Active + stale (>30 min) | Process (reclaim: marks old record as FAILED) |

4. **Queue**: sends one SQS message per job (max 50 per invocation)
5. **Mark QUEUED**: creates DDB record with status `QUEUED` before sending to SQS

### Stale Job Reclaim

If a job has been in an active status (IN_PROGRESS, DETECTING, etc.) for more than 30 minutes without an `updated_at` change, the batch trigger assumes it's stuck. It marks the old record as `FAILED` and re-queues the job for retry.

---

## Workflow Tracker Lambda

Triggered by EventBridge when any Step Functions execution completes (success or failure).

### What It Does

1. **Always decrements** the concurrency counter (-1). This happens regardless of success or failure
2. **On failure**: reads the SF execution input to get `job_id` and `timestamp`, then marks the DDB tracking record as `FAILED` with the error cause

This is the safety net: even if a Lambda crashes mid-execution, the counter is still decremented when the SF execution eventually times out or is aborted.

---

## Failure Modes and Recovery

| Failure                            | What Happens                                                                    | Recovery                                                                          |
| ---------------------------------- | ------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| **Single file fails in Map state** | Map continues (50% tolerance). Failed file recorded in `failed_files` in DDB.   | Re-upload the file to retry.                                                      |
| **Lambda throttled**               | SF retries 5 times with exponential backoff (up to ~10 min).                    | Automatic. Increase Lambda concurrency if persistent.                             |
| **SF execution fails**             | EventBridge triggers Workflow Tracker → counter decremented, job marked FAILED. | In batch mode, next scan retries automatically. In realtime, re-upload file.      |
| **Counter stuck**                  | If Workflow Tracker fails to decrement (rare), counter stays high.              | Manually reset `active_count` on the `workflow_counter` item in IdempotencyTable. |
| **SQS message fails 1000 times**   | Message moves to DLQ.                                                           | Investigate DLQ, fix root cause, redrive messages.                                |
| **Batch trigger finds stale job**  | Marks old record FAILED, re-queues job.                                         | Automatic.                                                                        |

---

## Key Files

| File                                         | Purpose                                                                    |
| -------------------------------------------- | -------------------------------------------------------------------------- |
| `infra/statemachine/pii_processing.asl.json` | Step Functions state machine definition                                    |
| `handlers/router_handler.py`                 | SQS consumer: concurrency control, folder batching, SF start              |
| `handlers/batch_trigger_handler.py`          | Scheduled bucket scanner: groups files, checks DDB, queues jobs           |
| `handlers/workflow_tracker_handler.py`       | SF completion handler: decrements counter, marks failures                 |
| `infra/dynamodb_manager.py`                  | DDB operations: status updates, failed file tracking, PII mapping storage |
| `infra/sqs_handler.py`                       | SQS utilities: S3 event extraction, idempotency locks                     |
