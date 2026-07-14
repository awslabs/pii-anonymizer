# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Centralized application-level AWS X-Ray instrumentation.

The infra (`Tracing: Active` in CloudFormation/Terraform) makes Lambda create one
X-Ray segment per invocation. Calling ``patch_all()`` here additionally wraps
``botocore`` so EVERY AWS SDK call (Bedrock, S3, DynamoDB, Textract, Transcribe,
Polly, Step Functions) becomes a traced SUBSEGMENT under that segment — turning
"the function ran 53s" into a per-call latency breakdown in the X-Ray console.

Each Lambda handler calls ``init_tracing()`` once at import (cold start), BEFORE
it creates any boto3 clients, so all clients are instrumented.

Best-effort and idempotent: if the SDK isn't installed (local dev / unit tests)
or anything fails, it is a silent no-op — observability must never block PII
processing.
"""

import logging

logger = logging.getLogger(__name__)

_patched = False


def init_tracing() -> bool:
    """Enable X-Ray subsegment tracing for AWS SDK calls.

    Returns True if patching was applied, False otherwise (already patched,
    SDK absent, or patch failed). Never raises.
    """
    global _patched
    if _patched:
        return False
    try:
        from aws_xray_sdk.core import patch_all

        patch_all()
        _patched = True
        logger.debug("X-Ray patch_all() applied — AWS SDK calls will be traced")
        return True
    except Exception as exc:  # noqa: BLE001 - never block the handler on observability
        logger.debug("X-Ray instrumentation skipped: %s", exc)
        return False
