# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""PII Anonymization & Redaction System - Core Module"""

# Model validation
from .validation.model_schemas import (
    InferenceConfig,
    validate_image_input,
    validate_text_input,
    ImageBasedOutput,
    TextBasedOutput,
    safe_get_response_content,
    PIIDetection,
    PIIMapping,
)

# PII Detection
from .core.pii_detector import (
    detect_pii_in_image,
    detect_pii_in_embedded_image,
    invoke_model,
    invoke_model_for_text,
    parse_llm_response,
    parse_text_response,
)

# Text-based processing
from .processors.pdf_text_processor import process_pdf_text_based

# Word processing
from .processors.word_processor import (
    replace_pii_in_word,
    store_word_pii_mapping,
    process_word_file,
)

# Excel processing
from .processors.tabular_processor import (
    extract_text_from_excel,
    replace_pii_in_excel,
    store_excel_pii_mapping,
)

# Shared detection
from .helpers.threaded_detector import detect_pii_in_text, run_threaded_pii_detection

# PDF processing
from .helpers.pdf_processor import (
    extract_all_pages_as_images,
    extract_all_embedded_images,
)
from .redaction.pdf_redactor import redact_pdf
from .validation.pdf_validator import validate_pdf, PDFValidationError

# Synthetic data generation
from .core.synthetic_pii_generator import (
    batch_generate_synthetic_pii,
    generate_synthetic_pii_with_llm,
)

# Redaction (Step 3)
from .core.redactor import redact_file, build_file_mapping, build_blackout_mapping

# Utilities
from .helpers.page_type_checker import get_text_based_pages
from .helpers.model_config_helper import (
    get_inference_config_from_yaml,
    get_precise_config_from_yaml,
    get_creative_config_from_yaml,
)
from .helpers.log_scrubber import scrub_pii
from .infra.dynamodb_manager import DynamoDBManager
from .infra.sqs_handler import (
    extract_s3_event,
    acquire_lock,
    release_lock,
    build_failure_context,
    copy_to_failed,
)

# Prompts
from .core.prompts import (
    SYSTEM_PROMPT,
    PII_DETECTION_PROMPT,
    VISION_SYSTEM_PROMPT,
    VISION_TASK_PROMPT,
)

__all__ = [
    # Validation
    "InferenceConfig",
    "validate_image_input",
    "validate_text_input",
    "ImageBasedOutput",
    "TextBasedOutput",
    "safe_get_response_content",
    "PIIDetection",
    "PIIMapping",
    # Detection
    "detect_pii_in_image",
    "detect_pii_in_embedded_image",
    "invoke_model",
    "invoke_model_for_text",
    "parse_llm_response",
    "parse_text_response",
    # Processing
    "process_pdf_text_based",
    "process_word_file",
    "extract_text_from_word",
    "detect_pii_in_word_text",
    "replace_pii_in_word",
    "store_word_pii_mapping",
    "extract_text_from_excel",
    "detect_pii_in_text",
    "run_threaded_pii_detection",
    "replace_pii_in_excel",
    "store_excel_pii_mapping",
    "extract_all_pages_as_images",
    "extract_all_embedded_images",
    "redact_pdf",
    "validate_pdf",
    "PDFValidationError",
    # Synthetic
    "batch_generate_synthetic_pii",
    "generate_synthetic_pii_with_llm",
    # Redaction
    "redact_file",
    "build_file_mapping",
    "build_blackout_mapping",
    # Utilities
    "get_text_based_pages",
    "get_inference_config_from_yaml",
    "get_precise_config_from_yaml",
    "get_creative_config_from_yaml",
    "get_creative_config_from_yaml",
    "scrub_pii",
    "DynamoDBManager",
    # SQS Handler
    "extract_s3_event",
    "acquire_lock",
    "release_lock",
    "build_failure_context",
    "copy_to_failed",
    # Prompts
    "SYSTEM_PROMPT",
    "PII_DETECTION_PROMPT",
    "VISION_SYSTEM_PROMPT",
    "VISION_TASK_PROMPT",
]
