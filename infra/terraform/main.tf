# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Reference to existing S3 buckets (no creation — customer-provided)
data "aws_s3_bucket" "input_bucket" {
  bucket = var.s3_input_bucket_name
}

data "aws_s3_bucket" "output_bucket" {
  bucket = var.s3_output_bucket_name
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.id

  realtime_mode = var.processing_mode == "realtime"
  batch_mode    = var.processing_mode == "batch"

  has_permissions_boundary = var.permissions_boundary_arn != ""
  has_s3_input_kms         = var.s3_input_kms_key_arn != ""
  has_s3_output_kms        = var.s3_output_kms_key_arn != ""
  has_dynamodb_kms         = var.dynamodb_kms_key_arn != ""
  has_cloudwatch_kms       = var.cloudwatch_kms_key_arn != ""
  has_sqs_kms              = var.sqs_kms_key_arn != ""
  has_idempotency_kms      = var.idempotency_table_kms_key_arn != ""
  has_vpc                  = length(var.vpc_subnet_ids) > 0

  create_dynamodb_table    = var.existing_dynamodb_table_arn == ""
  create_idempotency_table = var.existing_idempotency_table_arn == ""

  dynamodb_table_arn  = local.create_dynamodb_table ? aws_dynamodb_table.pii_transformations[0].arn : var.existing_dynamodb_table_arn
  dynamodb_table_name = local.create_dynamodb_table ? aws_dynamodb_table.pii_transformations[0].name : var.existing_dynamodb_table_name

  idempotency_table_arn  = local.create_idempotency_table ? aws_dynamodb_table.idempotency[0].arn : var.existing_idempotency_table_arn
  idempotency_table_name = local.create_idempotency_table ? aws_dynamodb_table.idempotency[0].name : var.existing_idempotency_table_name

  common_tags = merge(var.tags, {
    Environment = var.environment
    Project     = var.project
  })
}

# ===========================================================
# DynamoDB — Mapping Registry (conditional)
# ===========================================================
resource "aws_dynamodb_table" "pii_transformations" {
  count        = local.create_dynamodb_table ? 1 : 0
  name         = "${var.function_name}-${var.dynamodb_table_name}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "filename"
  range_key    = "timestamp"

  attribute {
    name = "filename"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = local.has_dynamodb_kms ? var.dynamodb_kms_key_arn : null
  }

  ttl {
    attribute_name = "expiration_time"
    enabled        = true
  }

  tags = local.common_tags
}

# ===========================================================
# DynamoDB — Idempotency / Concurrency Table (conditional)
# ===========================================================
resource "aws_dynamodb_table" "idempotency" {
  count        = local.create_idempotency_table ? 1 : 0
  name         = "${var.function_name}-IdempotencyTable"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "doc_key"

  attribute {
    name = "doc_key"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = local.has_idempotency_kms ? var.idempotency_table_kms_key_arn : null
  }

  ttl {
    attribute_name = "expiration_time"
    enabled        = true
  }

  tags = local.common_tags
}

# ===========================================================
# Config — upload config.yaml to S3 for dynamic settings
# ===========================================================
resource "aws_s3_object" "config_yaml" {
  bucket = var.s3_artifact_bucket_name
  key    = "config/config.yaml"
  source = "${path.module}/../../src/config.yaml"
  etag   = filemd5("${path.module}/../../src/config.yaml")
  tags   = local.common_tags
}

# ===========================================================
# Module — all Lambda, IAM, SQS, SF, EventBridge resources
# ===========================================================
module "pipeline" {
  source = "./modules"

  function_name = var.function_name
  tags          = local.common_tags

  # S3
  s3_input_bucket_name    = var.s3_input_bucket_name
  s3_input_bucket_arn     = data.aws_s3_bucket.input_bucket.arn
  s3_output_bucket_name   = var.s3_output_bucket_name
  s3_output_bucket_arn    = data.aws_s3_bucket.output_bucket.arn
  s3_artifact_bucket_name = var.s3_artifact_bucket_name
  s3_filter_prefix        = var.s3_filter_prefix

  # DynamoDB (resolved ARNs/names)
  dynamodb_table_arn     = local.dynamodb_table_arn
  dynamodb_table_name    = local.dynamodb_table_name
  idempotency_table_arn  = local.idempotency_table_arn
  idempotency_table_name = local.idempotency_table_name

  # Processing mode
  processing_mode          = var.processing_mode
  sqs_visibility_timeout   = var.sqs_visibility_timeout
  max_concurrent_workflows = var.max_concurrent_workflows
  folder_wait_seconds      = var.folder_wait_seconds
  batch_trigger_schedule   = var.batch_trigger_schedule
  job_folder_depth         = var.job_folder_depth

  # Lambda settings
  memory_size          = var.memory_size
  timeout              = var.timeout
  reserved_concurrency = var.reserved_concurrency
  log_level            = var.log_level
  log_retention_days   = var.log_retention_days

  # KMS
  s3_input_kms_key_arn          = var.s3_input_kms_key_arn
  s3_output_kms_key_arn         = var.s3_output_kms_key_arn
  dynamodb_kms_key_arn          = var.dynamodb_kms_key_arn
  cloudwatch_kms_key_arn        = var.cloudwatch_kms_key_arn
  sqs_kms_key_arn               = var.sqs_kms_key_arn
  idempotency_table_kms_key_arn = var.idempotency_table_kms_key_arn

  # IAM
  permissions_boundary_arn = var.permissions_boundary_arn

  # VPC (optional)
  vpc_subnet_ids         = var.vpc_subnet_ids
  vpc_security_group_ids = var.vpc_security_group_ids
}
