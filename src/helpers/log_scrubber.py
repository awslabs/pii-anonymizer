# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Log scrubbing utility to prevent PII exposure in CloudWatch logs"""

import re


def scrub_pii(message):
    """Remove PII from log messages"""
    if not isinstance(message, str):
        message = str(message)

    # SSN patterns
    message = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[SSN]", message)
    message = re.sub(r"\b\d{9}\b", "[SSN]", message)

    # Credit card patterns
    message = re.sub(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b", "[CC]", message)

    # Email patterns
    message = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "[EMAIL]", message
    )

    # Phone patterns
    message = re.sub(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "[PHONE]", message)

    # Date of birth patterns
    message = re.sub(r"\b\d{2}/\d{2}/\d{4}\b", "[DOB]", message)

    return message
