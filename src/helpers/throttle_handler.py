# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Throttling detection and re-raise for Step Functions retry matching.
All Lambda handlers wrap their main logic with this pattern.
"""

import logging

logger = logging.getLogger(__name__)

THROTTLING_KEYWORDS = [
    "throttl",
    "too many requests",
    "rate exceeded",
    "limit exceeded",
    "service quota",
    "provisioned throughput",
    "request limit",
    "toomanyrequestsexception",
    "servicequotaexceededexception",
]


class ThrottlingException(Exception):
    """Re-raised so Step Functions can match in Retry config."""

    pass


def check_and_raise_throttling(error):
    """If error is throttling-related, raise ThrottlingException for SF retry."""
    error_str = str(error).lower()
    error_type = type(error).__name__.lower()

    for keyword in THROTTLING_KEYWORDS:
        if keyword in error_str or keyword in error_type:
            logger.error(
                f"Throttling detected, raising for SF retry: {type(error).__name__}"
            )
            raise ThrottlingException(f"Throttling: {type(error).__name__}") from error
