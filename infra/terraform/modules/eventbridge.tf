# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# ===========================================================
# EventBridge — SF completion → Workflow Tracker
# ===========================================================
resource "aws_cloudwatch_event_rule" "sf_completion" {
  name = "${var.function_name}-sf-completion"

  event_pattern = jsonencode({
    source      = ["aws.states"]
    detail-type = ["Step Functions Execution Status Change"]
    detail = {
      stateMachineArn = [aws_sfn_state_machine.pipeline.arn]
      status          = ["SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"]
    }
  })

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "sf_to_tracker" {
  rule = aws_cloudwatch_event_rule.sf_completion.name
  arn  = aws_lambda_function.workflow_tracker.arn

  retry_policy {
    maximum_retry_attempts       = 3
    maximum_event_age_in_seconds = 86400
  }
}

resource "aws_lambda_permission" "eventbridge_tracker" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.workflow_tracker.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.sf_completion.arn
}

# ===========================================================
# EventBridge — Batch Trigger Schedule (batch mode only)
# ===========================================================
resource "aws_cloudwatch_event_rule" "batch_schedule" {
  count               = local.batch_mode ? 1 : 0
  name                = "${var.function_name}-batch-schedule"
  schedule_expression = "rate(${var.batch_trigger_schedule} minutes)"
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "batch_to_trigger" {
  count = local.batch_mode ? 1 : 0
  rule  = aws_cloudwatch_event_rule.batch_schedule[0].name
  arn   = aws_lambda_function.batch_trigger[0].arn
}

resource "aws_lambda_permission" "eventbridge_batch" {
  count         = local.batch_mode ? 1 : 0
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.batch_trigger[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.batch_schedule[0].arn
}
