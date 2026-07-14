# CloudFormation (SAM) Deployment

Deploys the full PII Anonymizer pipeline using AWS SAM and CloudFormation: 6 Lambda functions, Step Functions, SQS, DynamoDB, EventBridge, CloudWatch log groups, and IAM roles.

## Prerequisites

### make

Build automation tool that runs the deploy/destroy commands.

- **macOS**: `xcode-select --install` (installs Command Line Tools which includes make)
- **Linux**: `sudo apt-get install build-essential` (Ubuntu/Debian) or `sudo yum install make` (Amazon Linux/RHEL)
- **Windows**: comes with Git Bash. Alternatively use WSL.

### AWS SAM CLI

Builds and packages Lambda code for deployment.

- **macOS**: `brew install aws-sam-cli`
- **All platforms**: [install guide](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)

### AWS CLI v2

Deploys the CloudFormation stack and uploads config to S3.

- **macOS**: `brew install awscli`
- **All platforms**: [install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
- Must be configured with credentials: `aws configure`

### Docker Desktop (for Lambda layer build)

The Lambda layer contains Python dependencies (Pillow, pypdfium2, pypdf, Faker, etc.) compiled for Amazon Linux 2023 x86_64. The build script (`create_layer.sh`) auto-detects the best method:

| Platform    | Docker Available | What Happens                                                                |
| ----------- | ---------------- | --------------------------------------------------------------------------- |
| macOS/Linux | Yes              | Uses Docker with the official SAM build image (recommended)                 |
| macOS/Linux | No               | Falls back to `pip install --platform manylinux2014_x86_64` (cross-compile) |
| Windows     | Yes              | Uses Docker (only supported method)                                         |
| Windows     | No               | ❌ Fails: Docker is required on Windows                                    |

- **macOS/Linux**: Docker is recommended but not required. Without Docker, pip cross-compile works for most dependencies.
- **Windows**: Docker Desktop is required. [Download here](https://www.docker.com/products/docker-desktop/).

The layer is built automatically on first `make cfn-deploy`. To rebuild manually: `make layer`.

### S3 Buckets

Three buckets must already exist (the stack does not create them):

- **Input bucket**: where documents are uploaded
- **Output bucket**: where redacted files are written
- **Artifact bucket**: where SAM uploads Lambda code and the layer zip

### Bedrock Model Access

Amazon Bedrock model access must be enabled in your deployment region. Go to the Bedrock console → Model access → enable the model specified in `src/config.yaml`.

---

## Deploy

```bash
# 1. Edit parameters with your values
vi infra/cfn/parameters.json

# 2. Deploy
make cfn-deploy
```

## Update

```bash
make cfn-deploy    # rebuilds, repackages, updates the stack
```

## Delete

```bash
make cfn-delete
```

## What `make cfn-deploy` Does

1. Builds the Lambda layer if `layers/lambda_layer/python/` doesn't exist (first deploy after clone)
2. `sam build`: packages Lambda code
3. `sam package`: uploads artifacts to your S3 bucket
4. `aws cloudformation deploy`: creates/updates the stack
5. Uploads `src/config.yaml` to the artifact bucket at `config/config.yaml`

---

## Parameters

### Required

| Parameter            | What It Is                                                                                                           |
| -------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `Region`             | AWS region. Must have Bedrock and Textract access.                                                                   |
| `FunctionName`       | Prefix for all resource names: stack name, Lambda names, table names, queue names.                                  |
| `S3InputBucketName`  | Existing bucket where documents are uploaded.                                                                        |
| `S3OutputBucketName` | Existing bucket where redacted files are written.                                                                    |
| `ArtifactBucket`     | Existing bucket where SAM uploads Lambda code and the layer zip.                                                     |
| `ProcessingMode`     | `"realtime"`: S3 events trigger processing immediately. `"batch"`: EventBridge schedule scans bucket periodically. |

### Lambda

| Parameter             | Default | What It Controls                                                     |
| --------------------- | ------- | -------------------------------------------------------------------- |
| `MemorySize`          | 2048    | Lambda memory in MB (512 to 10240). Higher = faster but more expensive. |
| `Timeout`             | 600     | Lambda timeout in seconds (max 900).                                 |
| `ReservedConcurrency` | 10      | Max concurrent Lambda invocations across all functions.              |
| `LogLevel`            | INFO    | Lambda log level: DEBUG, INFO, WARNING, ERROR.                       |

### Pipeline Behavior

| Parameter                | Default | What It Controls                                                                                       |
| ------------------------ | ------- | ------------------------------------------------------------------------------------------------------ |
| `MaxConcurrentWorkflows` | 10      | Max parallel Step Functions executions. When reached, new jobs wait in SQS.                            |
| `FolderWaitSeconds`      | 30      | Seconds the Router waits for more files before listing a folder (realtime mode only).                  |
| `BatchTriggerSchedule`   | 5       | Minutes between bucket scans (batch mode only).                                                        |
| `JobFolderDepth`         | 1       | Folder depth for job grouping. 1 = top-level folder is one job. 2 = second-level subfolder is one job. |
| `SQSVisibilityTimeout`   | 90      | Seconds before a failed SQS message becomes visible again for retry.                                   |
| `LogRetentionDays`       | 365     | CloudWatch log retention in days.                                                                      |
| `S3FilterPrefix`         | (empty) | Only process files under this S3 prefix. Empty = entire bucket.                                        |

### KMS Encryption

Leave empty for AWS-managed encryption. Only set these if your environment requires customer-managed KMS keys.

| Parameter                   | What It Encrypts                                                       |
| --------------------------- | ---------------------------------------------------------------------- |
| `S3InputKmsKeyArn`          | Input bucket (Lambda needs kms:Decrypt)                                |
| `S3OutputKmsKeyArn`         | Output bucket (Lambda needs kms:Encrypt + kms:Decrypt)                 |
| `DynamoDBKmsKeyArn`         | PII Tracking Table                                                     |
| `IdempotencyTableKmsKeyArn` | Idempotency/Concurrency Table                                          |
| `CloudWatchKmsKeyArn`       | CloudWatch log groups                                                  |
| `SQSKmsKeyArn`              | SQS queue. In realtime mode, key policy must allow `s3.amazonaws.com`. |
| `ArtifactBucketKmsKeyArn`   | Artifact bucket (SAM packaging)                                        |

### Existing Resources

Use these when your environment has pre-provisioned DynamoDB tables (e.g., SCP-restricted accounts). Leave empty to create new tables.

| Parameter                      | What It Does                                         |
| ------------------------------ | ---------------------------------------------------- |
| `ExistingDynamoDBTableArn`     | ARN of existing PII Tracking Table.                  |
| `ExistingDynamoDBTableName`    | Name of the existing table (required if ARN is set). |
| `ExistingIdempotencyTableArn`  | ARN of existing Idempotency Table.                   |
| `ExistingIdempotencyTableName` | Name of the existing table (required if ARN is set). |

### VPC (Optional)

| Parameter             | What It Does                                                |
| --------------------- | ----------------------------------------------------------- |
| `VpcSubnetIds`        | Comma-separated subnet IDs. Leave empty for no VPC.         |
| `VpcSecurityGroupIds` | Comma-separated security group IDs. Leave empty for no VPC. |

When set, all Lambdas run inside the VPC. You must configure VPC endpoints for S3, DynamoDB, Bedrock, Textract, SQS, Step Functions, and CloudWatch. See [SECURITY.md](../../docs/SECURITY.md).

### Tags

| Parameter                | Default        | What It Does                                                                     |
| ------------------------ | -------------- | -------------------------------------------------------------------------------- |
| `Environment`            | QA             | Tag on all resources.                                                            |
| `Project`                | PII-Anonymizer | Tag on all resources.                                                            |
| `PermissionsBoundaryArn` | (empty)        | IAM permissions boundary on all roles. Required in some enterprise environments. |

---

## KMS Key Policy Requirements

Only needed if you use customer-managed KMS keys.

### SQS KMS Key (realtime mode only)

S3 needs to encrypt event notification messages:

```json
{
  "Sid": "AllowS3ToEncryptSQSMessages",
  "Effect": "Allow",
  "Principal": { "Service": "s3.amazonaws.com" },
  "Action": ["kms:GenerateDataKey", "kms:Decrypt"],
  "Resource": "*",
  "Condition": {
    "ArnLike": { "aws:SourceArn": "arn:aws:s3:::YOUR-INPUT-BUCKET" }
  }
}
```

### CloudWatch KMS Key

```json
{
  "Sid": "AllowCloudWatchLogs",
  "Effect": "Allow",
  "Principal": { "Service": "logs.YOUR-REGION.amazonaws.com" },
  "Action": [
    "kms:Encrypt",
    "kms:Decrypt",
    "kms:GenerateDataKey*",
    "kms:DescribeKey"
  ],
  "Resource": "*",
  "Condition": {
    "ArnLike": {
      "kms:EncryptionContext:aws:logs:arn": "arn:aws:logs:YOUR-REGION:YOUR-ACCOUNT:log-group:*"
    }
  }
}
```

---

## Troubleshooting

**`FileNotFoundError: parameters.json`**: The file exists but may have wrong values. Edit it with your bucket names and region.

**`Failed to create changeset`**: Stack is likely in `ROLLBACK_COMPLETE`. Delete it first:

```bash
aws cloudformation delete-stack --stack-name YOUR-STACK --region YOUR-REGION
aws cloudformation wait stack-delete-complete --stack-name YOUR-STACK --region YOUR-REGION
```

**`The specified KMS key does not exist`**: KMS key policy missing the required service principal. See KMS section above.

**IAM roles not deleted on stack delete**: If Lambda functions were invoked with session policies, CloudFormation can't delete the roles. Delete them manually in IAM console.

**`No module named 'xxx'` on Lambda (e.g. `defusedxml`)**: The built Lambda layer is stale (a dependency was added to `requirements_lambda.txt` after the layer was last built). `make cfn-deploy` auto-rebuilds when `requirements_lambda.txt` is newer than the built layer, but if you hit this, force a clean rebuild:

```bash
rm -rf layers/lambda_layer/python && make layer
```

Then redeploy.
