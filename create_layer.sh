#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0


# Create Lambda Layer for PII Anonymizer
# Creates ONE optimized Lambda layer with all dependencies
# Run this script from main_branch/pii_anonymizer/ directory
#
# Usage:
#   ./create_layer.sh           # auto-detect: Docker if available, else pip cross-compile
#   ./create_layer.sh --docker  # force Docker build (recommended on Windows)
#   ./create_layer.sh --pip     # force pip cross-compile (macOS/Linux only)

set -e  # Exit on error

# Parse args
BUILD_MODE="auto"
for arg in "$@"; do
    case $arg in
        --docker) BUILD_MODE="docker" ;;
        --pip)    BUILD_MODE="pip" ;;
    esac
done

# Auto-detect: use Docker on Windows or when available, fall back to pip
if [ "$BUILD_MODE" = "auto" ]; then
    if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
        BUILD_MODE="docker"
    elif command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
        BUILD_MODE="docker"
    else
        BUILD_MODE="pip"
    fi
fi

echo "============================================"
echo "Creating Lambda Layer (mode: $BUILD_MODE)"
echo "============================================"

# Ensure we're in the correct directory
if [ ! -f "requirements_lambda.txt" ]; then
    echo "❌ Error: requirements_lambda.txt not found. Run this script from main_branch/pii_anonymizer/"
    exit 1
fi

# Clean and recreate lambda_layer directory
echo ""
echo "Step 1: Preparing layers/lambda_layer directory..."
rm -rf layers/lambda_layer/python 2>/dev/null || true
mkdir -p layers/lambda_layer/python
mkdir -p layers/lambda_layer/fonts

# Download DejaVu font if not exists
if [ ! -f "layers/lambda_layer/fonts/DejaVuSans.ttf" ]; then
    echo "  - Downloading DejaVuSans.ttf font..."
    curl -L -s -o layers/lambda_layer/fonts/DejaVuSans.ttf "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans.ttf"
fi

echo "Step 2: Installing packages from requirements_lambda.txt..."
echo "  (This may take a few minutes...)"

if [ "$BUILD_MODE" = "docker" ]; then
    if ! command -v docker &>/dev/null; then
        echo "❌ Docker not found. Install Docker Desktop (required on Windows, recommended on all platforms)."
        echo "   On macOS/Linux you can also use: ./create_layer.sh --pip"
        exit 1
    fi
    echo "  Using Docker (Dockerfile.layer)..."
    docker build -f Dockerfile.layer -t pii-layer-builder .
    docker run --rm -v "$(pwd)/layers/lambda_layer:/output" pii-layer-builder
else
    echo "  Using pip cross-compile (manylinux2014_x86_64)..."
    pip install -r requirements_lambda.txt \
        -t layers/lambda_layer/python/ \
        --platform manylinux2014_x86_64 \
        --implementation cp \
        --python-version 3.13 \
        --only-binary=:all: \
        --quiet
fi

# Clean the layer
echo ""
echo "Step 3: Optimizing layer..."
echo "  - Removing __pycache__ directories..."
find layers/lambda_layer/python -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

echo "  - Removing .pyc and .pyo files..."
find layers/lambda_layer/python -name "*.pyc" -delete 2>/dev/null || true
find layers/lambda_layer/python -name "*.pyo" -delete 2>/dev/null || true

echo "  - Removing test directories..."
find layers/lambda_layer/python -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true
find layers/lambda_layer/python -type d -name "test" -exec rm -rf {} + 2>/dev/null || true

echo "  - Removing documentation files..."
find layers/lambda_layer/python -type f \( -name "*.md" -o -name "*.rst" -o -name "*.txt" \) ! -name "METADATA" ! -name "RECORD" ! -name "WHEEL" ! -name "INSTALLER" ! -name "REQUESTED" -delete 2>/dev/null || true

echo "  - Removing runtime-provided boto stack (aws-xray-sdk pulls botocore; Lambda already provides it)..."
for pkg in boto3 botocore s3transfer; do
  rm -rf layers/lambda_layer/python/"$pkg" layers/lambda_layer/python/"$pkg"-*.dist-info 2>/dev/null || true
done

echo "  ✓ Layer optimized"

# Report sizes
UNZIPPED_SIZE=$(du -sh layers/lambda_layer/python | awk '{print $1}')

# ===========================================================
# Step 4: Build ffmpeg layer (audio redaction — Redact Lambda only)
# ===========================================================
# Static ffmpeg binary for Amazon Linux 2023 x86_64, mounted at /opt/bin/ffmpeg.
# Kept in a SEPARATE layer so only the Redact Lambda carries the ~75MB weight.
echo ""
echo "Step 4: Building ffmpeg layer..."
if [ ! -f "layers/ffmpeg_layer/bin/ffmpeg" ]; then
    echo "  - Downloading static ffmpeg (amd64)..."
    mkdir -p layers/ffmpeg_layer/bin
    TMP_FF=$(mktemp -d)
    curl -L -s -o "$TMP_FF/ffmpeg.tar.xz" \
        "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
    tar -xf "$TMP_FF/ffmpeg.tar.xz" -C "$TMP_FF"
    FF_BIN=$(find "$TMP_FF" -name ffmpeg -type f | head -1)
    cp "$FF_BIN" layers/ffmpeg_layer/bin/ffmpeg
    chmod +x layers/ffmpeg_layer/bin/ffmpeg
    rm -rf "$TMP_FF"
    echo "  ✓ ffmpeg placed at layers/ffmpeg_layer/bin/ffmpeg (→ /opt/bin/ffmpeg)"
else
    echo "  ✓ ffmpeg binary already present, skipping download"
fi
FFMPEG_SIZE=$(du -sh layers/ffmpeg_layer/bin/ffmpeg 2>/dev/null | awk '{print $1}')

echo ""
echo "============================================"
echo "✅ Lambda Layers Built Successfully!"
echo "============================================"
echo ""
echo "📊 Python layer unzipped: $UNZIPPED_SIZE (limit: 250MB)"
echo "📊 ffmpeg binary: $FFMPEG_SIZE (separate layer, Redact Lambda only)"
echo ""
echo "📦 Layer contains:"
echo "   • faker, pypdf, PyYAML, termcolor"
echo "   • fuzzywuzzy, python-Levenshtein, rapidfuzz"
echo "   • pypdfium2 (PDFium renderer)"
echo "   • Pillow (PIL)"
echo "   • DejaVuSans.ttf font"
echo ""
echo "🚀 Run 'make cfn-deploy' to package and deploy via SAM"
