# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

variable "aws_region" {
  description = "AWS region to deploy resources"
  type        = string
  default     = "us-east-2" # Update to your region
}

variable "function_name" {
  description = "Base name prefix for all resources (Lambda functions, SQS, DDB, Step Functions)"
  type        = string
  default     = "PII-Anonymizer"
  validation {
    condition     = can(regex("^[a-zA-Z0-9-]+$", var.function_name))
    error_message = "Only alphanumeric and hyphens allowed."
  }
}

variable "s3_input_bucket_name" {
  description = "Name of the S3 bucket for input (original) documents"
  type        = string
  default     = "your-input-bucket-name" # Update with your input bucket name
}

variable "s3_output_bucket_name" {
  description = "Name of the S3 bucket for output (anonymized) documents"
  type        = string
  default     = "your-output-bucket-name"
}

variable "s3_artifact_bucket_name" {
  description = "Name of the S3 bucket for deployment artifacts and config"
  type        = string
  default     = "your-artifact-bucket-name"
}

variable "s3_filter_prefix" {
  description = "Optional S3 prefix path that triggers processing (e.g., 'pii_data/'). Leave empty to process all uploads."
  type        = string
  default     = "" # Empty means trigger on all files in bucket
}

# --- DynamoDB ---

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table (used as suffix when creating new table)"
  type        = string
  default     = "MappingRegistry"
}

variable "existing_dynamodb_table_arn" {
  description = "ARN of an existing DynamoDB table to use. If provided, no new table is created. Leave empty to create a new table."
  type        = string
  default     = ""
}

variable "existing_dynamodb_table_name" {
  description = "Name of an existing DynamoDB table. Required when existing_dynamodb_table_arn is provided."
  type        = string
  default     = ""
}

variable "existing_idempotency_table_arn" {
  description = "ARN of an existing DynamoDB idempotency table. Leave empty to create a new table."
  type        = string
  default     = ""
}

variable "existing_idempotency_table_name" {
  description = "Name of an existing idempotency table. Required when existing_idempotency_table_arn is provided."
  type        = string
  default     = ""
}

# --- KMS (all optional — leave empty for AWS-managed encryption) ---

variable "s3_input_kms_key_arn" {
  description = "ARN of the KMS key for input S3 bucket encryption (leave empty if using AWS-managed key)"
  type        = string
  default     = ""
}

variable "s3_output_kms_key_arn" {
  description = "ARN of the KMS key for output S3 bucket encryption (leave empty if using AWS-managed key)"
  type        = string
  default     = ""
}

variable "dynamodb_kms_key_arn" {
  description = "ARN of a customer-managed KMS key for DynamoDB encryption (leave empty for AWS-managed encryption)"
  type        = string
  default     = ""
}

variable "cloudwatch_kms_key_arn" {
  description = "ARN of a customer-managed KMS key for CloudWatch Logs encryption (leave empty for no KMS encryption)"
  type        = string
  default     = ""
}

variable "sqs_kms_key_arn" {
  description = "ARN of a customer-managed KMS key for SQS encryption (leave empty for SQS-managed SSE)"
  type        = string
  default     = ""
}

variable "idempotency_table_kms_key_arn" {
  description = "ARN of a customer-managed KMS key for idempotency table encryption (leave empty for AWS-managed)"
  type        = string
  default     = ""
}

# --- Processing mode ---

variable "processing_mode" {
  description = "Processing mode: 'realtime' (S3→SQS) or 'batch' (EventBridge schedule→SQS)"
  type        = string
  default     = "realtime"
  validation {
    condition     = contains(["realtime", "batch"], var.processing_mode)
    error_message = "Must be realtime or batch."
  }
}

variable "sqs_visibility_timeout" {
  description = "SQS visibility timeout in seconds (should exceed Lambda timeout)"
  type        = number
  default     = 90
}

variable "max_concurrent_workflows" {
  description = "Maximum concurrent Step Functions workflows (concurrency counter in idempotency table)"
  type        = number
  default     = 10
}

variable "folder_wait_seconds" {
  description = "Seconds to wait for additional files in a folder before starting workflow"
  type        = number
  default     = 30
}

variable "batch_trigger_schedule" {
  description = "Batch trigger schedule interval in minutes (only used in batch mode)"
  type        = number
  default     = 5
}

variable "job_folder_depth" {
  description = "Folder depth for job grouping: 1=top folder, 2=subfolder, 3=sub-subfolder (batch mode only)"
  type        = number
  default     = 1
}

variable "permissions_boundary_arn" {
  description = "ARN of IAM permissions boundary to apply to all roles (leave empty for none)"
  type        = string
  default     = ""
}

# --- Lambda settings ---

variable "memory_size" {
  description = "Memory size for Lambda function in MB"
  type        = number
  default     = 2048
}

variable "timeout" {
  description = "Timeout for Lambda function in seconds"
  type        = number
  default     = 600
}

variable "reserved_concurrency" {
  description = "Max concurrent Lambda executions"
  type        = number
  default     = 10
}

variable "log_level" {
  description = "Lambda log level"
  type        = string
  default     = "INFO"
  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR"], var.log_level)
    error_message = "Must be DEBUG, INFO, WARNING, or ERROR."
  }
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 365
  validation {
    condition     = var.log_retention_days >= 365
    error_message = "Log retention must be at least 365 days (CKV_AWS_338)."
  }
}

# --- Tags ---

variable "environment" {
  description = "Environment tag value"
  type        = string
  default     = "QA"
}

variable "project" {
  description = "Project tag value"
  type        = string
  default     = "PII-Anonymizer"
}

variable "tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default = {
    Environment = "QA"
    Project     = "PII-Anonymizer"
  }
}

# --- VPC (optional) ---

variable "vpc_subnet_ids" {
  description = "List of subnet IDs for Lambda VPC config (leave empty for no VPC)"
  type        = list(string)
  default     = []
}

variable "vpc_security_group_ids" {
  description = "List of security group IDs for Lambda VPC config (leave empty for no VPC)"
  type        = list(string)
  default     = []
}
