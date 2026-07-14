# Terraform Deployment

Deploys the same PII Anonymizer pipeline as the CloudFormation option: 6 Lambda functions, Step Functions, SQS, DynamoDB, EventBridge, CloudWatch log groups, and IAM roles.

## Prerequisites

### make

- **macOS**: `xcode-select --install`
- **Linux**: `sudo apt-get install build-essential` (Ubuntu/Debian) or `sudo yum install make` (Amazon Linux/RHEL)
- **Windows**: use Git Bash or WSL

### Terraform

- **macOS**: `brew install terraform`
- **All platforms**: [install guide](https://developer.hashicorp.com/terraform/install)

### AWS CLI v2

- **macOS**: `brew install awscli`
- **All platforms**: [install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
- Must be configured with credentials: `aws configure`

### Docker Desktop (for Lambda layer build)

The Lambda layer contains Python dependencies compiled for Amazon Linux 2023 x86_64. The build script auto-detects the best method:

| Platform    | Docker Available | What Happens                                                                |
| ----------- | ---------------- | --------------------------------------------------------------------------- |
| macOS/Linux | Yes              | Uses Docker with the official SAM build image (recommended)                 |
| macOS/Linux | No               | Falls back to `pip install --platform manylinux2014_x86_64` (cross-compile) |
| Windows     | Yes              | Uses Docker (only supported method)                                         |
| Windows     | No               | ❌ Fails: Docker is required on Windows                                    |

[Download Docker Desktop](https://www.docker.com/products/docker-desktop/)

### S3 Buckets

Two buckets must already exist (the stack does not create them):

- **Input bucket**: where documents are uploaded
- **Output bucket**: where redacted files are written

An artifact bucket is optional. Terraform uses it to upload `config.yaml`. If provided, the deployment auto-uploads `src/config.yaml` to `config/config.yaml` in that bucket.

### Bedrock Model Access

Amazon Bedrock model access must be enabled in your deployment region.

---

## Deploy

```bash
# 1. Edit variables with your values
vi infra/terraform/terraform.tfvars

# 2. Initialize (first time only)
make tf-init

# 3. Preview changes
make tf-plan

# 4. Deploy
make tf-deploy
```

`make tf-plan` and `make tf-deploy` automatically build the Lambda layer if `layers/lambda_layer/python/` doesn't exist.

## Update

```bash
make tf-deploy    # rebuilds layer if needed, plans, and applies
```

## Destroy

```bash
make tf-destroy
```

---

## Variables

Edit `infra/terraform/terraform.tfvars` with your values.

### Required

| Variable                | What It Is                                         |
| ----------------------- | -------------------------------------------------- |
| `aws_region`            | AWS region. Must have Bedrock and Textract access. |
| `function_name`         | Prefix for all resource names.                     |
| `s3_input_bucket_name`  | Existing bucket where documents are uploaded.      |
| `s3_output_bucket_name` | Existing bucket where redacted files are written.  |
| `processing_mode`       | `"realtime"` or `"batch"`.                         |

### Optional

| Variable                  | Default | What It Is                                                                       |
| ------------------------- | ------- | -------------------------------------------------------------------------------- |
| `s3_artifact_bucket_name` | (empty) | Bucket for config.yaml upload. If set, Terraform auto-uploads `src/config.yaml`. |

### Lambda

| Variable               | Default | What It Controls                     |
| ---------------------- | ------- | ------------------------------------ |
| `memory_size`          | 2048    | Lambda memory in MB (512 to 10240).     |
| `timeout`              | 600     | Lambda timeout in seconds (max 900). |
| `reserved_concurrency` | 10      | Max concurrent Lambda invocations.   |
| `log_level`            | INFO    | DEBUG, INFO, WARNING, ERROR.         |

### Pipeline Behavior

| Variable                   | Default | What It Controls                                                |
| -------------------------- | ------- | --------------------------------------------------------------- |
| `max_concurrent_workflows` | 10      | Max parallel Step Functions executions.                         |
| `folder_wait_seconds`      | 30      | Seconds Router waits before listing a folder (realtime mode).   |
| `batch_trigger_schedule`   | 5       | Minutes between bucket scans (batch mode).                      |
| `job_folder_depth`         | 1       | Folder depth for job grouping. 1 = top-level folder is one job. |
| `sqs_visibility_timeout`   | 90      | Seconds before failed SQS message retries.                      |
| `log_retention_days`       | 365     | CloudWatch log retention (minimum 365).                         |
| `s3_filter_prefix`         | (empty) | Only process files under this prefix.                           |

### KMS Encryption

Leave empty for AWS-managed encryption.

| Variable                        | What It Encrypts      |
| ------------------------------- | --------------------- |
| `s3_input_kms_key_arn`          | Input bucket          |
| `s3_output_kms_key_arn`         | Output bucket         |
| `dynamodb_kms_key_arn`          | PII Tracking Table    |
| `idempotency_table_kms_key_arn` | Idempotency Table     |
| `cloudwatch_kms_key_arn`        | CloudWatch log groups |
| `sqs_kms_key_arn`               | SQS queue             |

### Existing Resources

Leave empty to create new tables.

| Variable                          | What It Does                                  |
| --------------------------------- | --------------------------------------------- |
| `existing_dynamodb_table_arn`     | Use existing PII Tracking Table.              |
| `existing_dynamodb_table_name`    | Name of existing table (required if ARN set). |
| `existing_idempotency_table_arn`  | Use existing Idempotency Table.               |
| `existing_idempotency_table_name` | Name of existing table (required if ARN set). |

### VPC (Optional)

| Variable                 | What It Does                                |
| ------------------------ | ------------------------------------------- |
| `vpc_subnet_ids`         | List of subnet IDs. Empty = no VPC.         |
| `vpc_security_group_ids` | List of security group IDs. Empty = no VPC. |

See [SECURITY.md](../../docs/SECURITY.md) for required VPC endpoints.

### Tags

| Variable                   | Default                                            |
| -------------------------- | -------------------------------------------------- |
| `environment`              | QA                                                 |
| `project`                  | PII-Anonymizer                                     |
| `permissions_boundary_arn` | (empty)                                            |
| `tags`                     | `{Environment = "QA", Project = "PII-Anonymizer"}` |

---

## Input Bucket KMS Key

If your input bucket uses a customer-managed KMS key, you must set `s3_input_kms_key_arn` in `terraform.tfvars`. Without it, Lambda can't read files from the bucket.

To find your bucket's KMS key:

```bash
aws s3api get-bucket-encryption --bucket YOUR-INPUT-BUCKET --region YOUR-REGION
```

---

## Troubleshooting

**`AccessDenied` reading from input bucket**: The input bucket likely uses a KMS CMK. Set `s3_input_kms_key_arn` in tfvars.

**`Error acquiring state lock`**: Another Terraform process is running, or a previous run crashed. Force unlock:

```bash
cd infra/terraform && terraform force-unlock LOCK-ID
```

**Batch trigger not processing files**: Check that `dynamodb:PutItem` and `dynamodb:UpdateItem` are in the batch trigger IAM policy. These were added in v3.5.0.

**`No module named 'xxx'` on Lambda (e.g. `defusedxml`)**: The built Lambda layer is stale (a dependency was added to `requirements_lambda.txt` after the layer was last built). `make tf-deploy` auto-rebuilds when `requirements_lambda.txt` is newer than the built layer, but if you hit this, force a clean rebuild:

```bash
rm -rf layers/lambda_layer/python && make layer
```

Then redeploy.

**OpenAI (`openai.gpt-5.*`) calls denied**: The detection/synthetic Lambda roles need `bedrock-mantle:CreateInference`. Both deployments add this automatically. If still denied, verify the action name in the IAM policy simulator (the service namespace may differ in your account). OpenAI models are US-region only; see [docs/config.md](../../docs/config.md).
