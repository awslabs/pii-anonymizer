# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

output "state_machine_arn" {
  value = aws_sfn_state_machine.pipeline.arn
}

output "sqs_queue_url" {
  value = aws_sqs_queue.main.url
}

output "sqs_queue_arn" {
  value = aws_sqs_queue.main.arn
}

output "dlq_url" {
  value = aws_sqs_queue.dlq.url
}

output "router_function_arn" {
  value = aws_lambda_function.router.arn
}

output "detection_function_arn" {
  value = aws_lambda_function.detection.arn
}

output "synthetic_function_arn" {
  value = aws_lambda_function.synthetic.arn
}

output "redact_function_arn" {
  value = aws_lambda_function.redact.arn
}

output "workflow_tracker_function_arn" {
  value = aws_lambda_function.workflow_tracker.arn
}
