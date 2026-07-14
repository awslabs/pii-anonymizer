#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Generate frontend/.env from infrastructure outputs
# Usage: ./generate_frontend_env.sh [terraform|cfn]

set -e

DEPLOYMENT_TYPE="${1:-terraform}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
ENV_FILE="$FRONTEND_DIR/.env"

echo "Generating frontend/.env from $DEPLOYMENT_TYPE outputs..."

if [ "$DEPLOYMENT_TYPE" = "terraform" ]; then
    cd "$SCRIPT_DIR/infra/terraform"

    # Get Terraform outputs
    INPUT_BUCKET=$(terraform output -raw s3_input_bucket_name 2>/dev/null || echo "")
    OUTPUT_BUCKET=$(terraform output -raw s3_output_bucket_name 2>/dev/null || echo "")
    DYNAMODB_TABLE=$(terraform output -raw dynamodb_table_name 2>/dev/null || echo "")
    CONFIG_BUCKET=$(terraform output -raw s3_artifact_bucket_name 2>/dev/null || echo "")
    AWS_REGION=$(terraform output -raw aws_region 2>/dev/null || echo "us-east-2")

elif [ "$DEPLOYMENT_TYPE" = "cfn" ]; then
    # Get CloudFormation stack name from parameters (uses FunctionName as stack name)
    STACK_NAME=$(jq -r '.[] | select(.ParameterKey=="FunctionName") | .ParameterValue' "$SCRIPT_DIR/infra/cfn/parameters.json" 2>/dev/null || echo "PII-Anonymizer")
    AWS_REGION=$(jq -r '.[] | select(.ParameterKey=="Region") | .ParameterValue' "$SCRIPT_DIR/infra/cfn/parameters.json" 2>/dev/null || echo "us-east-2")
    CONFIG_BUCKET=$(jq -r '.[] | select(.ParameterKey=="ArtifactBucket") | .ParameterValue' "$SCRIPT_DIR/infra/cfn/parameters.json" 2>/dev/null || echo "")

    # Get CloudFormation outputs
    OUTPUTS=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" --query 'Stacks[0].Outputs' 2>/dev/null || echo "[]")

    INPUT_BUCKET=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="S3InputBucketName") | .OutputValue' 2>/dev/null || echo "")
    OUTPUT_BUCKET=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="S3OutputBucketName") | .OutputValue' 2>/dev/null || echo "")
    DYNAMODB_TABLE=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="DynamoDBTableName") | .OutputValue' 2>/dev/null || echo "")
else
    echo "Error: Invalid deployment type. Use 'terraform' or 'cfn'"
    exit 1
fi

# Validate required values
if [ -z "$INPUT_BUCKET" ] || [ -z "$DYNAMODB_TABLE" ]; then
    echo "Error: Could not retrieve required outputs from $DEPLOYMENT_TYPE"
    echo "  INPUT_BUCKET: $INPUT_BUCKET"
    echo "  DYNAMODB_TABLE: $DYNAMODB_TABLE"
    exit 1
fi

# Generate .env file
cat > "$ENV_FILE" << EOF
# Auto-generated from $DEPLOYMENT_TYPE outputs
# Generated: $(date -u +"%Y-%m-%d %H:%M:%S UTC")

# AWS Configuration
AWS_REGION=$AWS_REGION

# S3 Buckets
INPUT_BUCKET=$INPUT_BUCKET
OUTPUT_BUCKET=${OUTPUT_BUCKET:-$INPUT_BUCKET}

# DynamoDB
DYNAMODB_TABLE_NAME=$DYNAMODB_TABLE

# Config (S3-backed pipeline settings)
CONFIG_BUCKET=$CONFIG_BUCKET
CONFIG_KEY=config/config.yaml

# S3 Prefixes (optional)
INPUT_PREFIX=pii_data/
OUTPUT_PREFIX=redacted/
EOF

echo "✅ Generated $ENV_FILE"
echo ""
echo "Configuration:"
echo "  AWS_REGION: $AWS_REGION"
echo "  INPUT_BUCKET: $INPUT_BUCKET"
echo "  OUTPUT_BUCKET: ${OUTPUT_BUCKET:-$INPUT_BUCKET}"
echo "  DYNAMODB_TABLE_NAME: $DYNAMODB_TABLE"
echo ""
echo "Next steps:"
echo "  cd frontend"
echo "  streamlit run app.py"
