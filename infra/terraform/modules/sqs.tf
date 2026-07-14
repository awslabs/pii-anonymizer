# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

locals {
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
}

# ===========================================================
# SQS — Always created (both modes use SQS)
# ===========================================================

resource "aws_sqs_queue" "dlq" {
  name                      = "${var.function_name}-dlq"
  message_retention_seconds = 1209600 # 14 days
  kms_master_key_id         = local.has_sqs_kms ? var.sqs_kms_key_arn : null
  sqs_managed_sse_enabled   = local.has_sqs_kms ? false : true
  tags                      = var.tags
}

resource "aws_sqs_queue" "main" {
  name                       = "${var.function_name}-queue"
  visibility_timeout_seconds = var.sqs_visibility_timeout
  message_retention_seconds  = 86400 # 1 day
  kms_master_key_id          = local.has_sqs_kms ? var.sqs_kms_key_arn : null
  sqs_managed_sse_enabled    = local.has_sqs_kms ? false : true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 1000
  })

  tags = var.tags
}

# SQS Queue Policy — allow S3 to send messages (realtime mode only)
resource "aws_sqs_queue_policy" "s3_to_sqs" {
  count     = local.realtime_mode ? 1 : 0
  queue_url = aws_sqs_queue.main.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.main.arn
      Condition = {
        ArnLike = { "aws:SourceArn" = "arn:aws:s3:::${var.s3_input_bucket_name}" }
      }
    }]
  })
}

# CloudWatch Alarm on DLQ
resource "aws_cloudwatch_metric_alarm" "dlq_alarm" {
  alarm_name          = "${var.function_name}-dlq-alarm"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"

  dimensions = {
    QueueName = aws_sqs_queue.dlq.name
  }

  tags = var.tags
}
