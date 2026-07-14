# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# ===========================================================
# S3 → SQS Notification (realtime mode only)
# ===========================================================
resource "aws_s3_bucket_notification" "input_to_sqs" {
  count  = local.realtime_mode ? 1 : 0
  bucket = var.s3_input_bucket_name

  dynamic "queue" {
    for_each = [".pdf", ".docx", ".xlsx", ".csv", ".json", ".txt", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp", ".mp3", ".wav"]
    content {
      queue_arn     = aws_sqs_queue.main.arn
      events        = ["s3:ObjectCreated:*"]
      filter_prefix = var.s3_filter_prefix != "" ? var.s3_filter_prefix : null
      filter_suffix = queue.value
    }
  }

  depends_on = [aws_sqs_queue_policy.s3_to_sqs]
}
