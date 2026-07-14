# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

output "state_machine_arn" {
  description = "ARN of the Step Functions pipeline"
  value       = module.pipeline.state_machine_arn
}

output "dynamodb_table_name" {
  description = "Mapping registry table name"
  value       = local.dynamodb_table_name
}

output "idempotency_table_name" {
  description = "Idempotency table name"
  value       = local.idempotency_table_name
}

output "sqs_queue_url" {
  description = "SQS queue URL"
  value       = module.pipeline.sqs_queue_url
}

output "sqs_queue_arn" {
  description = "SQS queue ARN"
  value       = module.pipeline.sqs_queue_arn
}

output "dlq_url" {
  description = "Dead letter queue URL"
  value       = module.pipeline.dlq_url
}

output "s3_input_bucket_name" {
  description = "Name of the S3 input bucket"
  value       = var.s3_input_bucket_name
}

output "s3_output_bucket_name" {
  description = "Name of the S3 output bucket"
  value       = var.s3_output_bucket_name
}

output "s3_artifact_bucket_name" {
  description = "Name of the S3 artifact/config bucket (used by the frontend for pipeline settings)"
  value       = var.s3_artifact_bucket_name
}

output "aws_region" {
  description = "AWS region"
  value       = var.aws_region
}
