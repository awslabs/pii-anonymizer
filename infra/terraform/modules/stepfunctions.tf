# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# ===========================================================
# Step Functions — CloudWatch Log Group
# ===========================================================
resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/states/${var.function_name}-pipeline"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.has_cloudwatch_kms ? var.cloudwatch_kms_key_arn : null
  tags              = var.tags
}

# ===========================================================
# Step Functions — State Machine
# ===========================================================
resource "aws_sfn_state_machine" "pipeline" {
  name     = "${var.function_name}-pipeline"
  role_arn = aws_iam_role.sfn.arn

  definition = templatefile("${path.root}/../statemachine/pii_processing.asl.json", {
    PIIDetectionFunctionArn = aws_lambda_function.detection.arn
    SyntheticFunctionArn    = aws_lambda_function.synthetic.arn
    RedactFunctionArn       = aws_lambda_function.redact.arn
    DynamoDBTableName       = var.dynamodb_table_name
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = false
    level                  = "ERROR"
  }

  tracing_configuration {
    enabled = true
  }

  tags = var.tags
}
