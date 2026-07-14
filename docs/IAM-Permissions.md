# PII Anonymizer: IAM Roles and Permissions

The tool creates 7 IAM roles during deployment (6 for Lambda functions, 1 for Step Functions). Each role has only the permissions it needs. All roles are created automatically by CloudFormation or Terraform. No manual IAM setup is required.

Permissions marked with (optional) are only added when the customer provides the corresponding KMS key ARN or VPC configuration.

## Router Lambda

Receives messages from SQS, checks if there are available execution slots in DynamoDB, lists files in the S3 folder, and starts a Step Functions execution.

| Service         | Actions                                                                    | Resource                      |
| --------------- | -------------------------------------------------------------------------- | ----------------------------- |
| CloudWatch Logs | CreateLogStream, PutLogEvents                                              | Router log group              |
| Step Functions  | StartExecution                                                             | Pipeline state machine        |
| S3              | ListBucket                                                                 | Input bucket                  |
| S3              | GetObject                                                                  | Artifact bucket (config.yaml) |
| DynamoDB        | UpdateItem, DescribeTable                                                  | Idempotency table             |
| DynamoDB        | UpdateItem, GetItem, PutItem, DescribeTable                                | PII Tracking table            |
| SQS             | ReceiveMessage, DeleteMessage, GetQueueAttributes, ChangeMessageVisibility | PII queue                     |
| X-Ray           | PutTraceSegments, PutTelemetryRecords                                      | All                           |
| KMS (optional)  | Decrypt, GenerateDataKey                                                   | SQS KMS key                   |
| KMS (optional)  | Decrypt, GenerateDataKey                                                   | Idempotency table KMS key     |
| KMS (optional)  | Decrypt, GenerateDataKey                                                   | DynamoDB KMS key              |
| EC2 (optional)  | CreateNetworkInterface, DeleteNetworkInterface, DescribeNetworkInterfaces  | All (VPC mode only)           |

## Detection Lambda

Reads documents from the input bucket, sends them to Amazon Bedrock for PII detection (text and vision models), calls Amazon Textract for OCR on scanned pages, and writes detection results to the output bucket.

| Service         | Actions                                                                   | Resource                              |
| --------------- | ------------------------------------------------------------------------- | ------------------------------------- |
| CloudWatch Logs | CreateLogStream, PutLogEvents                                             | Detection log group                   |
| S3              | GetObject                                                                 | Input bucket, Artifact bucket         |
| S3              | PutObject                                                                 | Output bucket                         |
| Bedrock         | Converse, InvokeModel                                                     | Inference profiles, Foundation models |
| Textract        | DetectDocumentText                                                        | All                                   |
| Transcribe      | StartTranscriptionJob, GetTranscriptionJob                                | `pii-audio-*` jobs (audio only)       |
| DynamoDB        | UpdateItem, DescribeTable                                                 | PII Tracking table                    |
| X-Ray           | PutTraceSegments, PutTelemetryRecords                                     | All                                   |
| KMS (optional)  | Decrypt, GenerateDataKey                                                  | Input bucket KMS key                  |
| KMS (optional)  | Decrypt, GenerateDataKey                                                  | Output bucket KMS key                 |
| KMS (optional)  | Decrypt, GenerateDataKey                                                  | DynamoDB KMS key                      |
| EC2 (optional)  | CreateNetworkInterface, DeleteNetworkInterface, DescribeNetworkInterfaces | All (VPC mode only)                   |

## Synthetic Lambda

Reads detection results from the output bucket, calls Bedrock to generate realistic synthetic replacements for detected PII, and writes the synthetic mapping file back to S3.

| Service         | Actions                                                                   | Resource                              |
| --------------- | ------------------------------------------------------------------------- | ------------------------------------- |
| CloudWatch Logs | CreateLogStream, PutLogEvents                                             | Synthetic log group                   |
| S3              | GetObject                                                                 | Output bucket, Artifact bucket        |
| S3              | PutObject                                                                 | Output bucket                         |
| Bedrock         | InvokeModel                                                               | Inference profiles, Foundation models |
| DynamoDB        | PutItem, UpdateItem, DescribeTable                                        | PII Tracking table                    |
| X-Ray           | PutTraceSegments, PutTelemetryRecords                                     | All                                   |
| KMS (optional)  | Decrypt, GenerateDataKey                                                  | Output bucket KMS key                 |
| KMS (optional)  | Decrypt, GenerateDataKey                                                  | DynamoDB KMS key                      |
| EC2 (optional)  | CreateNetworkInterface, DeleteNetworkInterface, DescribeNetworkInterfaces | All (VPC mode only)                   |

## Redact Lambda

Reads original documents from the input bucket and synthetic mappings from the output bucket, applies redaction in the original file format, and writes redacted files and reports to S3. Uses Textract for bounding box refinement on image-based documents.

| Service         | Actions                                                                   | Resource                                     |
| --------------- | ------------------------------------------------------------------------- | -------------------------------------------- |
| CloudWatch Logs | CreateLogStream, PutLogEvents                                             | Redact log group                             |
| S3              | GetObject                                                                 | Input bucket, Output bucket, Artifact bucket |
| S3              | PutObject                                                                 | Output bucket                                |
| Textract        | DetectDocumentText                                                        | All                                          |
| Polly           | SynthesizeSpeech                                                          | All (audio only)                             |
| DynamoDB        | UpdateItem, DescribeTable                                                 | PII Tracking table                           |
| X-Ray           | PutTraceSegments, PutTelemetryRecords                                     | All                                          |
| KMS (optional)  | Decrypt, GenerateDataKey                                                  | Input bucket KMS key                         |
| KMS (optional)  | Decrypt, GenerateDataKey                                                  | Output bucket KMS key                        |
| KMS (optional)  | Decrypt, GenerateDataKey                                                  | DynamoDB KMS key                             |
| EC2 (optional)  | CreateNetworkInterface, DeleteNetworkInterface, DescribeNetworkInterfaces | All (VPC mode only)                          |

## Workflow Tracker Lambda

Triggered by EventBridge when a Step Functions execution completes (success, failure, timeout, or abort). Decrements the concurrency counter in the idempotency table and updates job status to FAILED in the tracking table when needed.

| Service         | Actions                                                                   | Resource                   |
| --------------- | ------------------------------------------------------------------------- | -------------------------- |
| CloudWatch Logs | CreateLogStream, PutLogEvents                                             | Workflow tracker log group |
| DynamoDB        | UpdateItem, DescribeTable                                                 | Idempotency table          |
| DynamoDB        | UpdateItem, GetItem, DescribeTable                                        | PII Tracking table         |
| Step Functions  | DescribeExecution                                                         | Pipeline executions        |
| X-Ray           | PutTraceSegments, PutTelemetryRecords                                     | All                        |
| KMS (optional)  | Decrypt, GenerateDataKey                                                  | Idempotency table KMS key  |
| KMS (optional)  | Decrypt, GenerateDataKey                                                  | DynamoDB KMS key           |
| EC2 (optional)  | CreateNetworkInterface, DeleteNetworkInterface, DescribeNetworkInterfaces | All (VPC mode only)        |

## Batch Trigger Lambda (batch mode only)

Runs on a schedule via EventBridge. Scans the input bucket, groups files by folder into jobs, checks DynamoDB for already-processed jobs, and sends new jobs to SQS.

| Service         | Actions                                                                   | Resource                    |
| --------------- | ------------------------------------------------------------------------- | --------------------------- |
| CloudWatch Logs | CreateLogStream, PutLogEvents                                             | Batch trigger log group     |
| S3              | ListBucket                                                                | Input bucket, Output bucket |
| S3              | GetObject                                                                 | Output bucket               |
| SQS             | SendMessage                                                               | PII queue                   |
| DynamoDB        | GetItem, Query, PutItem, UpdateItem                                       | PII Tracking table          |
| X-Ray           | PutTraceSegments, PutTelemetryRecords                                     | All                         |
| KMS (optional)  | Decrypt, GenerateDataKey                                                  | SQS KMS key                 |
| EC2 (optional)  | CreateNetworkInterface, DeleteNetworkInterface, DescribeNetworkInterfaces | All (VPC mode only)         |

## Step Functions Role

Orchestrates the three-stage pipeline by invoking the Detection, Synthetic, and Redact Lambda functions. Also writes execution history to CloudWatch Logs.

| Service         | Actions                                                                                                                                                    | Resource                               |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| Lambda          | InvokeFunction                                                                                                                                             | Detection, Synthetic, Redact functions |
| DynamoDB        | UpdateItem                                                                                                                                                 | PII Tracking table                     |
| X-Ray           | PutTraceSegments, PutTelemetryRecords, GetSamplingRules, GetSamplingTargets                                                                                | All                                    |
| CloudWatch Logs | CreateLogDelivery, GetLogDelivery, UpdateLogDelivery, DeleteLogDelivery, ListLogDeliveries, PutResourcePolicy, DescribeResourcePolicies, DescribeLogGroups | All                                    |
| KMS (optional)  | Decrypt, Encrypt, GenerateDataKey                                                                                                                          | DynamoDB KMS key                       |

## Deployer Permissions

The IAM identity running the deployment (your user or CI/CD role) needs permissions to create all the resources above:

| Service         | Why                                                                         |
| --------------- | --------------------------------------------------------------------------- |
| Lambda          | Create functions, layers, event source mappings                             |
| IAM             | Create roles and policies for the Lambda functions and Step Functions       |
| SQS             | Create the processing queue and dead-letter queue                           |
| DynamoDB        | Create the tracking and idempotency tables                                  |
| Step Functions  | Create the pipeline state machine                                           |
| EventBridge     | Create completion rules and batch schedule                                  |
| CloudWatch Logs | Create log groups                                                           |
| S3              | Upload deployment artifacts, configure bucket notifications (realtime mode) |
| CloudFormation  | Create and update the stack (CFN deployments only)                          |
| KMS             | Describe keys and create grants (only if using customer-managed keys)       |
| EC2             | DescribeSecurityGroups, DescribeSubnets, DescribeVpcs (only if using VPC)   |

## S3 Bucket Access Summary

| Lambda           | Input Bucket | Output Bucket         | Artifact Bucket |
| ---------------- | ------------ | --------------------- | --------------- |
| Router           | ListBucket   |                       | GetObject       |
| Detection        | GetObject    | PutObject             | GetObject       |
| Synthetic        |              | GetObject, PutObject  | GetObject       |
| Redact           | GetObject    | GetObject, PutObject  | GetObject       |
| Batch Trigger    | ListBucket   | ListBucket, GetObject |                 |
| Workflow Tracker |              |                       |                 |

## Notes

All roles use lambda.amazonaws.com as the trusted service principal, except Step Functions which uses states.amazonaws.com.

KMS permissions are only added when you provide customer-managed KMS key ARNs in the deployment parameters. If you leave them empty, AWS-managed encryption is used and no KMS permissions are needed.

VPC permissions (EC2 network interface actions) are only added when you provide VPC subnet and security group IDs. If you leave them empty, Lambda functions run outside a VPC.

An optional permissions boundary can be attached to all roles via the PermissionsBoundaryArn deployment parameter. This is useful in enterprise environments that require boundary policies on all IAM roles.
