# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# ===========================================================
# Shared assume role policy for all Lambda roles
# ===========================================================
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# ===========================================================
# Router Role + Policy
# ===========================================================
resource "aws_iam_role" "router" {
  name                 = "${var.function_name}-router-role"
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume.json
  permissions_boundary = local.has_permissions_boundary ? var.permissions_boundary_arn : null
  tags                 = var.tags
}

resource "aws_iam_role_policy" "router" {
  name = "${var.function_name}-router-policy"
  role = aws_iam_role.router.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      # CloudWatch Logs
      [{
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.router.arn}:*"
      }],
      # Start Step Functions
      [{
        Effect   = "Allow"
        Action   = "states:StartExecution"
        Resource = aws_sfn_state_machine.pipeline.arn
      }],
      # S3 ListBucket (input)
      [{
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = "arn:aws:s3:::${var.s3_input_bucket_name}"
      }],
      # S3 GetObject (config from artifacts bucket)
      [{
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "arn:aws:s3:::${var.s3_artifact_bucket_name}/*"
      }],
      # Idempotency table
      [{
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem", "dynamodb:DescribeTable"]
        Resource = var.idempotency_table_arn
      }],
      # Mapping registry table
      [{
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DescribeTable"]
        Resource = var.dynamodb_table_arn
      }],
      # SQS (receive from queue)
      [{
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
        Resource = aws_sqs_queue.main.arn
      }],
      # KMS — SQS
      local.has_sqs_kms ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.sqs_kms_key_arn
      }] : [],
      # KMS — Idempotency table
      local.has_idempotency_kms ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.idempotency_table_kms_key_arn
      }] : [],
      # KMS — DynamoDB
      local.has_dynamodb_kms ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.dynamodb_kms_key_arn
      }] : [],
      # X-Ray tracing
      [{
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
      }],
      # VPC ENI permissions
      local.has_vpc ? [{
        Effect   = "Allow"
        Action   = ["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface", "ec2:DescribeNetworkInterfaces"]
        Resource = "*"
      }] : []
    )
  })
}

# ===========================================================
# Detection Role + Policy
# ===========================================================
resource "aws_iam_role" "detection" {
  name                 = "${var.function_name}-detection-role"
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume.json
  permissions_boundary = local.has_permissions_boundary ? var.permissions_boundary_arn : null
  tags                 = var.tags
}

resource "aws_iam_role_policy" "detection" {
  name = "${var.function_name}-detection-policy"
  role = aws_iam_role.detection.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.detection.arn}:*"
      }],
      [{
        Effect = "Allow"
        Action = "s3:GetObject"
        Resource = [
          "${var.s3_input_bucket_arn}/*",
          "arn:aws:s3:::${var.s3_artifact_bucket_name}/*"
        ]
      }],
      [{
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${var.s3_output_bucket_arn}/*"
      }],
      [{
        Effect = "Allow"
        Action = ["bedrock:Converse", "bedrock:InvokeModel"]
        Resource = [
          "arn:aws:bedrock:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:*::foundation-model/*"
        ]
      }],
      # Textract — no resource-level permissions
      [{
        Effect   = "Allow"
        Action   = "textract:DetectDocumentText"
        Resource = "*"
      }],
      # Audio detection — Amazon Transcribe (scoped to the pii-audio-* job prefix).
      # Transcribe reads the media via the caller's existing s3:GetObject on the
      # input bucket and uses service-managed output, so no extra S3 perms needed.
      [{
        Effect   = "Allow"
        Action   = ["transcribe:StartTranscriptionJob", "transcribe:GetTranscriptionJob"]
        Resource = "arn:aws:transcribe:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:transcription-job/pii-audio-*"
      }],
      # OpenAI GPT-5.x via the bedrock-mantle Responses API (only used when an
      # openai.* model is selected). No resource-level ARNs on this namespace.
      # Verify the action name against your account if OpenAI calls are denied.
      [{
        Effect   = "Allow"
        Action   = "bedrock-mantle:CreateInference"
        Resource = "*"
      }],
      [{
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem", "dynamodb:DescribeTable"]
        Resource = var.dynamodb_table_arn
      }],
      local.has_s3_input_kms ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.s3_input_kms_key_arn
      }] : [],
      local.has_s3_output_kms ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.s3_output_kms_key_arn
      }] : [],
      local.has_dynamodb_kms ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.dynamodb_kms_key_arn
      }] : [],
      # X-Ray tracing
      [{
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
      }],
      # VPC ENI permissions
      local.has_vpc ? [{
        Effect   = "Allow"
        Action   = ["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface", "ec2:DescribeNetworkInterfaces"]
        Resource = "*"
      }] : []
    )
  })
}

# ===========================================================
# Synthetic Role + Policy
# ===========================================================
resource "aws_iam_role" "synthetic" {
  name                 = "${var.function_name}-synthetic-role"
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume.json
  permissions_boundary = local.has_permissions_boundary ? var.permissions_boundary_arn : null
  tags                 = var.tags
}

resource "aws_iam_role_policy" "synthetic" {
  name = "${var.function_name}-synthetic-policy"
  role = aws_iam_role.synthetic.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.synthetic.arn}:*"
      }],
      [{
        Effect = "Allow"
        Action = "s3:GetObject"
        Resource = [
          "${var.s3_output_bucket_arn}/*",
          "arn:aws:s3:::${var.s3_artifact_bucket_name}/*"
        ]
      }],
      [{
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${var.s3_output_bucket_arn}/*"
      }],
      [{
        Effect = "Allow"
        Action = "bedrock:InvokeModel"
        Resource = [
          "arn:aws:bedrock:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:*::foundation-model/*"
        ]
      }],
      # OpenAI GPT-5.x via the bedrock-mantle Responses API (synthetic generation
      # when an openai.* model is selected). Verify the action if calls are denied.
      [{
        Effect   = "Allow"
        Action   = "bedrock-mantle:CreateInference"
        Resource = "*"
      }],
      [{
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DescribeTable"]
        Resource = var.dynamodb_table_arn
      }],
      local.has_s3_output_kms ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.s3_output_kms_key_arn
      }] : [],
      local.has_dynamodb_kms ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.dynamodb_kms_key_arn
      }] : [],
      # X-Ray tracing
      [{
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
      }],
      # VPC ENI permissions
      local.has_vpc ? [{
        Effect   = "Allow"
        Action   = ["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface", "ec2:DescribeNetworkInterfaces"]
        Resource = "*"
      }] : []
    )
  })
}

# ===========================================================
# Redact Role + Policy
# ===========================================================
resource "aws_iam_role" "redact" {
  name                 = "${var.function_name}-redact-role"
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume.json
  permissions_boundary = local.has_permissions_boundary ? var.permissions_boundary_arn : null
  tags                 = var.tags
}

resource "aws_iam_role_policy" "redact" {
  name = "${var.function_name}-redact-policy"
  role = aws_iam_role.redact.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.redact.arn}:*"
      }],
      [{
        Effect = "Allow"
        Action = "s3:GetObject"
        Resource = [
          "${var.s3_input_bucket_arn}/*",
          "${var.s3_output_bucket_arn}/*",
          "arn:aws:s3:::${var.s3_artifact_bucket_name}/*"
        ]
      }],
      [{
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${var.s3_output_bucket_arn}/*"
      }],
      # Textract — no resource-level permissions
      [{
        Effect   = "Allow"
        Action   = "textract:DetectDocumentText"
        Resource = "*"
      }],
      # Audio redaction — Amazon Polly. SynthesizeSpeech does not support
      # resource-level permissions, so Resource must be "*" (AWS limitation).
      [{
        Effect   = "Allow"
        Action   = "polly:SynthesizeSpeech"
        Resource = "*"
      }],
      [{
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem", "dynamodb:DescribeTable"]
        Resource = var.dynamodb_table_arn
      }],
      local.has_s3_input_kms ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.s3_input_kms_key_arn
      }] : [],
      local.has_s3_output_kms ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.s3_output_kms_key_arn
      }] : [],
      local.has_dynamodb_kms ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.dynamodb_kms_key_arn
      }] : [],
      # X-Ray tracing
      [{
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
      }],
      # VPC ENI permissions
      local.has_vpc ? [{
        Effect   = "Allow"
        Action   = ["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface", "ec2:DescribeNetworkInterfaces"]
        Resource = "*"
      }] : []
    )
  })
}

# ===========================================================
# Workflow Tracker Role + Policy
# ===========================================================
resource "aws_iam_role" "workflow_tracker" {
  name                 = "${var.function_name}-workflow-tracker-role"
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume.json
  permissions_boundary = local.has_permissions_boundary ? var.permissions_boundary_arn : null
  tags                 = var.tags
}

resource "aws_iam_role_policy" "workflow_tracker" {
  name = "${var.function_name}-workflow-tracker-policy"
  role = aws_iam_role.workflow_tracker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.workflow_tracker.arn}:*"
      }],
      # Idempotency table (decrement counter)
      [{
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem", "dynamodb:DescribeTable"]
        Resource = var.idempotency_table_arn
      }],
      # Describe SF execution (get input for job_id/timestamp)
      [{
        Effect   = "Allow"
        Action   = "states:DescribeExecution"
        Resource = "arn:aws:states:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:execution:${var.function_name}-pipeline:*"
      }],
      # Mapping registry table (update status on failure)
      [{
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:DescribeTable"]
        Resource = var.dynamodb_table_arn
      }],
      local.has_idempotency_kms ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.idempotency_table_kms_key_arn
      }] : [],
      local.has_dynamodb_kms ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.dynamodb_kms_key_arn
      }] : [],
      # X-Ray tracing
      [{
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
      }],
      # VPC ENI permissions
      local.has_vpc ? [{
        Effect   = "Allow"
        Action   = ["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface", "ec2:DescribeNetworkInterfaces"]
        Resource = "*"
      }] : []
    )
  })
}

# ===========================================================
# Batch Trigger Role + Policy (batch mode only)
# ===========================================================
resource "aws_iam_role" "batch_trigger" {
  count                = local.batch_mode ? 1 : 0
  name                 = "${var.function_name}-batch-trigger-role"
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume.json
  permissions_boundary = local.has_permissions_boundary ? var.permissions_boundary_arn : null
  tags                 = var.tags
}

resource "aws_iam_role_policy" "batch_trigger" {
  count = local.batch_mode ? 1 : 0
  name  = "${var.function_name}-batch-trigger-policy"
  role  = aws_iam_role.batch_trigger[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.batch_trigger[0].arn}:*"
      }],
      [{
        Effect = "Allow"
        Action = "s3:ListBucket"
        Resource = [
          "arn:aws:s3:::${var.s3_input_bucket_name}",
          "arn:aws:s3:::${var.s3_output_bucket_name}"
        ]
      }],
      [{
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${var.s3_output_bucket_arn}/*"
      }],
      [{
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.main.arn
      }],
      [{
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = var.dynamodb_table_arn
      }],
      local.has_sqs_kms ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.sqs_kms_key_arn
      }] : [],
      # X-Ray tracing
      [{
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
      }],
      # VPC ENI permissions
      local.has_vpc ? [{
        Effect   = "Allow"
        Action   = ["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface", "ec2:DescribeNetworkInterfaces"]
        Resource = "*"
      }] : []
    )
  })
}

# ===========================================================
# Step Functions Role + Policy
# ===========================================================
data "aws_iam_policy_document" "sfn_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sfn" {
  name                 = "${var.function_name}-sfn-role"
  assume_role_policy   = data.aws_iam_policy_document.sfn_assume.json
  permissions_boundary = local.has_permissions_boundary ? var.permissions_boundary_arn : null
  tags                 = var.tags
}

resource "aws_iam_role_policy" "sfn" {
  name = "${var.function_name}-sfn-policy"
  role = aws_iam_role.sfn.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Effect = "Allow"
        Action = "lambda:InvokeFunction"
        Resource = [
          aws_lambda_function.detection.arn,
          aws_lambda_function.synthetic.arn,
          aws_lambda_function.redact.arn
        ]
      }],
      [{
        Effect   = "Allow"
        Action   = "dynamodb:UpdateItem"
        Resource = var.dynamodb_table_arn
      }],
      # X-Ray tracing
      [{
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
        Resource = "*"
      }],
      # CloudWatch Logs — execution history logging
      [{
        Effect = "Allow"
        Action = [
          "logs:CreateLogDelivery",
          "logs:GetLogDelivery",
          "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery",
          "logs:ListLogDeliveries",
          "logs:PutResourcePolicy",
          "logs:DescribeResourcePolicies",
          "logs:DescribeLogGroups"
        ]
        Resource = "*"
      }],
      local.has_dynamodb_kms ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
        Resource = var.dynamodb_kms_key_arn
      }] : []
    )
  })
}
