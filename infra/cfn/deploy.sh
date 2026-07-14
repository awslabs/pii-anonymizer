#!/bin/bash
# PII Anonymizer — Deploy Script (IDP-ACC pattern)
# Uses sam build + sam package + cfn deploy. No sam deploy, no managed bucket.
# Usage: ./deploy.sh [deploy|delete]
set -euo pipefail

# Auto-detect Python command (python3 on Mac/Linux, python on Windows)
if command -v python3 &>/dev/null; then
  PYTHON=python3
elif command -v python &>/dev/null; then
  PYTHON=python
else
  echo "❌ Python not found. Install Python 3.12+ and ensure it's on your PATH."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PARAMS_FILE="${SCRIPT_DIR}/parameters.json"
TEMPLATE_FILE="${SCRIPT_DIR}/template.yaml"

# Convert MSYS/Git Bash paths for Windows Python (/c/... → C:/...)
if command -v cygpath &>/dev/null; then
  PARAMS_FILE_PY="$(cygpath -m "$PARAMS_FILE")"
else
  PARAMS_FILE_PY="$PARAMS_FILE"
fi

ACTION="${1:-deploy}"

# Build Lambda layers if python/ or ffmpeg binary missing, OR if
# requirements_lambda.txt changed since the layer was last built (staleness check).
LAYER_DIR="${PROJECT_ROOT}/layers/lambda_layer/python"
FFMPEG_BIN="${PROJECT_ROOT}/layers/ffmpeg_layer/bin/ffmpeg"
REQS="${PROJECT_ROOT}/requirements_lambda.txt"
if [ "$ACTION" = "deploy" ] && { [ ! -d "$LAYER_DIR" ] || [ ! -f "$FFMPEG_BIN" ] || [ "$REQS" -nt "$LAYER_DIR" ]; }; then
  echo "📦 Building Lambda layers (missing or requirements_lambda.txt changed)..."
  (cd "$PROJECT_ROOT" && bash create_layer.sh)
fi

# Read params
read STACK_NAME REGION ARTIFACT_BUCKET ARTIFACT_KMS <<< $($PYTHON -c "
import json
params = {p['ParameterKey']: p['ParameterValue'] for p in json.load(open('${PARAMS_FILE_PY}')) if 'ParameterKey' in p}
print(params.get('FunctionName', 'PII-Anonymizer'), params.get('Region', 'us-east-2'), params.get('ArtifactBucket', ''), params.get('ArtifactBucketKmsKeyArn', ''))
")

# Warn if using customer KMS key with realtime SQS
if [ "$ACTION" = "deploy" ]; then
  $PYTHON -c "
import json
params = {p['ParameterKey']: p['ParameterValue'] for p in json.load(open('${PARAMS_FILE_PY}')) if 'ParameterKey' in p}
mode = params.get('ProcessingMode', 'realtime')
sqs_kms = params.get('SQSKmsKeyArn', '')
if mode == 'realtime' and sqs_kms:
    print()
    print('⚠️  WARNING: You are using a customer-managed KMS key for SQS in realtime mode.')
    print('   Your KMS key policy MUST allow s3.amazonaws.com to call kms:GenerateDataKey and kms:Decrypt.')
    print('   Without this, S3 event notifications will fail. See README.md for the required policy statement.')
    print()
"
fi

if [ -z "$ARTIFACT_BUCKET" ] && [ "$ACTION" = "deploy" ]; then
  echo "❌ ArtifactBucket parameter required in parameters.json"
  exit 1
fi

# Convert parameters.json to --parameter-overrides format (skip empty values)
OVERRIDES=$($PYTHON -c "
import json
params = json.load(open('${PARAMS_FILE_PY}'))
parts = []
for p in params:
    if 'ParameterKey' not in p: continue
    k, v = p['ParameterKey'], p['ParameterValue']
    if k in ('Region', 'ArtifactBucketKmsKeyArn'): continue
    if not v: continue
    parts.append(f'{k}={v}')
print(' '.join(parts))
")

echo "Stack: ${STACK_NAME} | Region: ${REGION} | Bucket: ${ARTIFACT_BUCKET} | KMS: ${ARTIFACT_KMS:-AWS-managed} | Action: ${ACTION}"

case "$ACTION" in
  deploy)
    cd "${PROJECT_ROOT}"

    echo "📦 Building..."
    sam build --template-file "$TEMPLATE_FILE"

    echo "📤 Packaging..."
    KMS_ARG=""
    if [ -n "$ARTIFACT_KMS" ]; then
      KMS_ARG="--kms-key-id $ARTIFACT_KMS"
    fi
    sam package \
      --template-file .aws-sam/build/template.yaml \
      --output-template-file .aws-sam/packaged.yaml \
      --s3-bucket "$ARTIFACT_BUCKET" \
      --s3-prefix "${STACK_NAME}/artifacts" \
      --region "$REGION" \
      --force-upload \
      $KMS_ARG

    echo "🚀 Deploying..."
    aws cloudformation deploy \
      --template-file .aws-sam/packaged.yaml \
      --stack-name "$STACK_NAME" \
      --capabilities CAPABILITY_NAMED_IAM \
      --parameter-overrides $OVERRIDES \
      --region "$REGION"

    echo "📄 Uploading config.yaml to S3..."
    aws s3 cp "${PROJECT_ROOT}/src/config.yaml" "s3://${ARTIFACT_BUCKET}/config/config.yaml" --region "$REGION"

    echo "✅ Deployed. Outputs:"
    aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
      --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' --output table
    ;;

  delete)
    echo "🗑️  Deleting stack ${STACK_NAME}..."
    aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
    aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
    echo "✅ Stack deleted."
    ;;

  *)
    echo "Usage: $0 [deploy|delete]"
    exit 1
    ;;
esac
