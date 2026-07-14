# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ===========================================================
# Lambda Layer
# ===========================================================
data "archive_file" "layer_zip" {
  type        = "zip"
  source_dir  = "${path.root}/../../layers/lambda_layer"
  output_path = "${path.module}/pii_anonymizer_layer.zip"
  excludes    = ["*.zip"]
}

resource "aws_lambda_layer_version" "pii_layer" {
  layer_name          = "${var.function_name}-layer"
  filename            = data.archive_file.layer_zip.output_path
  source_code_hash    = data.archive_file.layer_zip.output_base64sha256
  compatible_runtimes = ["python3.13"]
}

# ===========================================================
# ffmpeg Layer (audio redaction — attached to Redact Lambda only)
# Provides the static ffmpeg binary at /opt/bin/ffmpeg.
# ===========================================================
data "archive_file" "ffmpeg_layer_zip" {
  type        = "zip"
  source_dir  = "${path.root}/../../layers/ffmpeg_layer"
  output_path = "${path.module}/ffmpeg_layer.zip"
  excludes    = ["*.zip"]
}

resource "aws_lambda_layer_version" "ffmpeg_layer" {
  layer_name          = "${var.function_name}-ffmpeg"
  filename            = data.archive_file.ffmpeg_layer_zip.output_path
  source_code_hash    = data.archive_file.ffmpeg_layer_zip.output_base64sha256
  compatible_runtimes = ["python3.13"]
}

# ===========================================================
# Lambda code zip (shared by all functions)
# ===========================================================
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.root}/../../src"
  output_path = "${path.module}/lambda_function.zip"
}

# ===========================================================
# CloudWatch Log Groups (per function)
# ===========================================================
resource "aws_cloudwatch_log_group" "router" {
  name              = "/aws/lambda/${var.function_name}-router"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.has_cloudwatch_kms ? var.cloudwatch_kms_key_arn : null
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "detection" {
  name              = "/aws/lambda/${var.function_name}-detection"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.has_cloudwatch_kms ? var.cloudwatch_kms_key_arn : null
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "synthetic" {
  name              = "/aws/lambda/${var.function_name}-synthetic"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.has_cloudwatch_kms ? var.cloudwatch_kms_key_arn : null
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "redact" {
  name              = "/aws/lambda/${var.function_name}-redact"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.has_cloudwatch_kms ? var.cloudwatch_kms_key_arn : null
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "workflow_tracker" {
  name              = "/aws/lambda/${var.function_name}-workflow-tracker"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.has_cloudwatch_kms ? var.cloudwatch_kms_key_arn : null
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "batch_trigger" {
  count             = local.batch_mode ? 1 : 0
  name              = "/aws/lambda/${var.function_name}-batch-trigger"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.has_cloudwatch_kms ? var.cloudwatch_kms_key_arn : null
  tags              = var.tags
}

# ===========================================================
# Router Lambda (SQS → Step Functions)
# ===========================================================
resource "aws_lambda_function" "router" {
  function_name    = "${var.function_name}-router"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  handler          = "handlers.router_handler.lambda_handler"
  role             = aws_iam_role.router.arn
  runtime          = "python3.13"
  timeout          = 60
  memory_size      = 256
  layers           = [aws_lambda_layer_version.pii_layer.arn]

  tracing_config {
    mode = "Active"
  }

  dynamic "vpc_config" {
    for_each = local.has_vpc ? [1] : []
    content {
      subnet_ids         = var.vpc_subnet_ids
      security_group_ids = var.vpc_security_group_ids
    }
  }

  environment {
    variables = {
      LOG_LEVEL             = var.log_level
      STATE_MACHINE_ARN     = aws_sfn_state_machine.pipeline.arn
      CONCURRENCY_TABLE     = var.idempotency_table_name
      S3_OUTPUT_BUCKET_NAME = var.s3_output_bucket_name
      DYNAMODB_TABLE_NAME   = var.dynamodb_table_name
      MAX_CONCURRENT        = var.max_concurrent_workflows
      FOLDER_WAIT_SECONDS   = var.folder_wait_seconds
      CONFIG_BUCKET         = var.s3_artifact_bucket_name
      CONFIG_KEY            = "config/config.yaml"
    }
  }

  depends_on = [aws_cloudwatch_log_group.router]
  tags       = var.tags
}

# SQS → Router event source mapping
resource "aws_lambda_event_source_mapping" "sqs_to_router" {
  event_source_arn = aws_sqs_queue.main.arn
  function_name    = aws_lambda_function.router.arn
  batch_size       = 1

  scaling_config {
    maximum_concurrency = var.reserved_concurrency
  }

  function_response_types = ["ReportBatchItemFailures"]
  depends_on              = [aws_iam_role_policy.router]
}

# ===========================================================
# Detection Lambda (Step 1 — SF Map per file)
# ===========================================================
resource "aws_lambda_function" "detection" {
  function_name                  = "${var.function_name}-detection"
  filename                       = data.archive_file.lambda_zip.output_path
  source_code_hash               = data.archive_file.lambda_zip.output_base64sha256
  handler                        = "handlers.pii_detection_handler.lambda_handler"
  role                           = aws_iam_role.detection.arn
  runtime                        = "python3.13"
  timeout                        = var.timeout
  memory_size                    = var.memory_size
  reserved_concurrent_executions = var.reserved_concurrency
  layers                         = [aws_lambda_layer_version.pii_layer.arn]

  tracing_config {
    mode = "Active"
  }

  dynamic "vpc_config" {
    for_each = local.has_vpc ? [1] : []
    content {
      subnet_ids         = var.vpc_subnet_ids
      security_group_ids = var.vpc_security_group_ids
    }
  }

  environment {
    variables = {
      LOG_LEVEL             = var.log_level
      S3_INPUT_BUCKET_NAME  = var.s3_input_bucket_name
      S3_OUTPUT_BUCKET_NAME = var.s3_output_bucket_name
      CONFIG_BUCKET         = var.s3_artifact_bucket_name
      CONFIG_KEY            = "config/config.yaml"
      DYNAMODB_TABLE_NAME   = var.dynamodb_table_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.detection]
  tags       = var.tags
}

# ===========================================================
# Synthetic Lambda (Step 2 — single invocation, 15 min max)
# ===========================================================
resource "aws_lambda_function" "synthetic" {
  function_name    = "${var.function_name}-synthetic"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  handler          = "handlers.synthetic_handler.lambda_handler"
  role             = aws_iam_role.synthetic.arn
  runtime          = "python3.13"
  timeout          = 900
  memory_size      = 2048
  layers           = [aws_lambda_layer_version.pii_layer.arn]

  tracing_config {
    mode = "Active"
  }

  dynamic "vpc_config" {
    for_each = local.has_vpc ? [1] : []
    content {
      subnet_ids         = var.vpc_subnet_ids
      security_group_ids = var.vpc_security_group_ids
    }
  }

  environment {
    variables = {
      LOG_LEVEL             = var.log_level
      S3_OUTPUT_BUCKET_NAME = var.s3_output_bucket_name
      CONFIG_BUCKET         = var.s3_artifact_bucket_name
      CONFIG_KEY            = "config/config.yaml"
      DYNAMODB_TABLE_NAME   = var.dynamodb_table_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.synthetic]
  tags       = var.tags
}

# ===========================================================
# Redact Lambda (Step 3 — SF Map per file)
# ===========================================================
resource "aws_lambda_function" "redact" {
  function_name                  = "${var.function_name}-redact"
  filename                       = data.archive_file.lambda_zip.output_path
  source_code_hash               = data.archive_file.lambda_zip.output_base64sha256
  handler                        = "handlers.redact_handler.lambda_handler"
  role                           = aws_iam_role.redact.arn
  runtime                        = "python3.13"
  timeout                        = var.timeout
  memory_size                    = var.memory_size
  reserved_concurrent_executions = var.reserved_concurrency
  layers                         = [aws_lambda_layer_version.pii_layer.arn, aws_lambda_layer_version.ffmpeg_layer.arn]

  tracing_config {
    mode = "Active"
  }

  dynamic "vpc_config" {
    for_each = local.has_vpc ? [1] : []
    content {
      subnet_ids         = var.vpc_subnet_ids
      security_group_ids = var.vpc_security_group_ids
    }
  }

  environment {
    variables = {
      LOG_LEVEL             = var.log_level
      S3_INPUT_BUCKET_NAME  = var.s3_input_bucket_name
      S3_OUTPUT_BUCKET_NAME = var.s3_output_bucket_name
      CONFIG_BUCKET         = var.s3_artifact_bucket_name
      CONFIG_KEY            = "config/config.yaml"
      DYNAMODB_TABLE_NAME   = var.dynamodb_table_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.redact]
  tags       = var.tags
}

# ===========================================================
# Workflow Tracker Lambda (EventBridge SF completion → decrement)
# ===========================================================
resource "aws_lambda_function" "workflow_tracker" {
  function_name    = "${var.function_name}-workflow-tracker"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  handler          = "handlers.workflow_tracker_handler.lambda_handler"
  role             = aws_iam_role.workflow_tracker.arn
  runtime          = "python3.13"
  timeout          = 60
  memory_size      = 256
  layers           = [aws_lambda_layer_version.pii_layer.arn]

  tracing_config {
    mode = "Active"
  }

  dynamic "vpc_config" {
    for_each = local.has_vpc ? [1] : []
    content {
      subnet_ids         = var.vpc_subnet_ids
      security_group_ids = var.vpc_security_group_ids
    }
  }

  environment {
    variables = {
      LOG_LEVEL           = var.log_level
      CONCURRENCY_TABLE   = var.idempotency_table_name
      DYNAMODB_TABLE_NAME = var.dynamodb_table_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.workflow_tracker]
  tags       = var.tags
}

# ===========================================================
# Batch Trigger Lambda (batch mode only)
# ===========================================================
resource "aws_lambda_function" "batch_trigger" {
  count            = local.batch_mode ? 1 : 0
  function_name    = "${var.function_name}-batch-trigger"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  handler          = "handlers.batch_trigger_handler.handler"
  role             = aws_iam_role.batch_trigger[0].arn
  runtime          = "python3.13"
  timeout          = 900
  memory_size      = 256
  layers           = [aws_lambda_layer_version.pii_layer.arn]

  tracing_config {
    mode = "Active"
  }

  dynamic "vpc_config" {
    for_each = local.has_vpc ? [1] : []
    content {
      subnet_ids         = var.vpc_subnet_ids
      security_group_ids = var.vpc_security_group_ids
    }
  }

  environment {
    variables = {
      LOG_LEVEL             = var.log_level
      S3_INPUT_BUCKET_NAME  = var.s3_input_bucket_name
      S3_OUTPUT_BUCKET_NAME = var.s3_output_bucket_name
      SQS_QUEUE_URL         = aws_sqs_queue.main.url
      SCAN_PREFIX           = var.s3_filter_prefix
      JOB_FOLDER_DEPTH      = tostring(var.job_folder_depth)
      DYNAMODB_TABLE_NAME   = var.dynamodb_table_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.batch_trigger[0]]
  tags       = var.tags
}
