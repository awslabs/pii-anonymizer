# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

variable "function_name" {
  description = "Base name prefix for all resources"
  type        = string
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
}

# --- S3 ---

variable "s3_input_bucket_name" {
  description = "Name of the S3 input bucket"
  type        = string
}

variable "s3_input_bucket_arn" {
  description = "ARN of the S3 input bucket"
  type        = string
}

variable "s3_output_bucket_name" {
  description = "Name of the S3 output bucket"
  type        = string
}

variable "s3_output_bucket_arn" {
  description = "ARN of the S3 output bucket"
  type        = string
}

variable "s3_artifact_bucket_name" {
  description = "Name of the S3 artifacts bucket (config storage)"
  type        = string
}

variable "s3_filter_prefix" {
  description = "S3 prefix filter for notifications"
  type        = string
}

# --- DynamoDB (resolved by root) ---

variable "dynamodb_table_arn" {
  description = "ARN of the DynamoDB mapping registry table"
  type        = string
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB mapping registry table"
  type        = string
}

variable "idempotency_table_arn" {
  description = "ARN of the DynamoDB idempotency table"
  type        = string
}

variable "idempotency_table_name" {
  description = "Name of the DynamoDB idempotency table"
  type        = string
}

# --- Processing mode ---

variable "processing_mode" {
  description = "Processing mode: realtime or batch"
  type        = string
}

variable "sqs_visibility_timeout" {
  description = "SQS visibility timeout in seconds"
  type        = number
}

variable "max_concurrent_workflows" {
  description = "Maximum concurrent Step Functions workflows"
  type        = number
}

variable "folder_wait_seconds" {
  description = "Seconds to wait for folder batching"
  type        = number
}

variable "batch_trigger_schedule" {
  description = "Batch trigger interval in minutes"
  type        = number
}

variable "job_folder_depth" {
  description = "Folder depth for job grouping (batch mode only)"
  type        = number
}

# --- Lambda settings ---

variable "memory_size" {
  description = "Lambda memory in MB"
  type        = number
}

variable "timeout" {
  description = "Lambda timeout in seconds"
  type        = number
}

variable "reserved_concurrency" {
  description = "Max concurrent Lambda executions"
  type        = number
}

variable "log_level" {
  description = "Lambda log level"
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days"
  type        = number
}

# --- KMS (all optional) ---

variable "s3_input_kms_key_arn" {
  description = "KMS key ARN for input S3 bucket"
  type        = string
}

variable "s3_output_kms_key_arn" {
  description = "KMS key ARN for output S3 bucket"
  type        = string
}

variable "dynamodb_kms_key_arn" {
  description = "KMS key ARN for DynamoDB"
  type        = string
}

variable "cloudwatch_kms_key_arn" {
  description = "KMS key ARN for CloudWatch Logs"
  type        = string
}

variable "sqs_kms_key_arn" {
  description = "KMS key ARN for SQS"
  type        = string
}

variable "idempotency_table_kms_key_arn" {
  description = "KMS key ARN for idempotency table"
  type        = string
}

# --- IAM ---

variable "permissions_boundary_arn" {
  description = "IAM permissions boundary ARN (empty for none)"
  type        = string
}

# --- VPC (optional) ---

variable "vpc_subnet_ids" {
  description = "Subnet IDs for Lambda VPC config"
  type        = list(string)
  default     = []
}

variable "vpc_security_group_ids" {
  description = "Security group IDs for Lambda VPC config"
  type        = list(string)
  default     = []
}
