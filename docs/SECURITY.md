# Security Guide

## Reporting a Vulnerability

If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public GitHub issue.

---

## IMPORTANT SECURITY NOTICE

**THIS APPLICATION IS A PII DETECTION AND REDACTION TOOL. WHILE IT USES ADVANCED AI AND OCR TECHNOLOGIES, IT IS NOT INFALLIBLE.**

### Critical Requirements Before Production Use

1. **ALWAYS VERIFY REDACTIONS**: Manually review all redacted documents to ensure:
   - All PII has been detected and redacted
   - No false positives (non-PII incorrectly redacted)
   - No false negatives (PII missed by detection)

2. **MULTIPLE VERIFICATION PASSES**:
   - Perform at least 2-3 independent reviews of redacted documents
   - Use different reviewers for each pass
   - Compare original and redacted versions side-by-side

3. **SECURITY TEAM APPROVAL REQUIRED**:
   - Consult your organization's security team before deploying
   - Obtain written approval for production use
   - Establish review procedures and audit trails

4. **COMPLIANCE VALIDATION**:
   - Verify compliance with applicable regulations (GDPR, HIPAA, CCPA, etc.)
   - Document your verification process
   - Maintain audit logs of all redactions

### Limitations

- AI models may hallucinate or miss PII
- OCR accuracy depends on document quality
- Complex layouts may cause detection failures
- AI models may have varying accuracy on unusual or context-specific PII types

### Liability

**Users are solely responsible for verifying the accuracy and completeness of all redactions. This tool is provided as-is without warranties of any kind.**

---

## Security Best Practices

**You own your AWS infrastructure. You're responsible for securing your S3 buckets, DynamoDB tables, KMS keys, and IAM policies.**

### 1. S3 Bucket Security

- Enable **S3 Block Public Access** on input and output buckets
- Use **bucket policies with least privilege** (Lambda execution role only)
- Enable **S3 server-side encryption** (SSE-KMS with customer keys recommended)
- Configure **S3 access logging** to track operations
- Set **lifecycle policies** to auto-delete documents after retention period

### 2. DynamoDB Table Security

- Enable **encryption at rest** with customer-managed KMS keys
- Implement **fine-grained access control**:
  - Table-level: IAM policies restricting access
  - Record-level: DynamoDB condition keys for specific items
  - Column-level: Attribute-based access for sensitive fields
- Enable **point-in-time recovery** for PII mappings table
- Configure **CloudWatch alarms** for unusual access

### 3. KMS Key Management

- Use **customer-managed keys** for sensitive data (input bucket, PII mappings)
- Set **least privilege key policies** (specific IAM roles only)
- Enable **automatic key rotation** (yearly)
- Use **separate keys** per service (S3, DynamoDB, SQS, CloudWatch)
- Enable **CloudTrail logging** for KMS API calls
- Set **CloudWatch alarms** for unusual key usage

### 4. IAM Role Security

- Use the **provided least privilege templates** for Lambda execution role
- Enable **IAM Access Analyzer** to catch overly permissive policies
- Add **IAM condition keys** for restrictions (source IP, MFA)
- Use **AWS Organizations SCPs** for multi-account guardrails
- Review and audit IAM policies regularly

### 5. Amazon Bedrock Data Protection

- **PII goes to Amazon Bedrock**: this is required for AI detection to work
- Configure **Bedrock in same region** as S3 buckets (no cross-region transfer)
- Enable **Bedrock opt-out** to prevent AWS using your data for improvements
- Review **AWS Bedrock data policies** for compliance
- Document **customer acknowledgment** that PII will be processed by Bedrock

### 6. CloudWatch Logs Protection

- Enable **log encryption** with customer-managed KMS keys
- Set **retention policies** (90+ days recommended)
- Add **resource policies** to prevent deletion
- Restrict **DeleteLogGroup/DeleteLogStream** permissions
- Export logs to **S3 for long-term storage**
- Enable **CloudTrail** to audit log modifications

### 7. SQS Queue Security

- Enable **SQS encryption** with customer-managed KMS keys
- Configure **dead-letter queue** for failed messages
- Set **message retention** for your use case

### 8. Network Security (VPC)

Deploying Lambda in a VPC is optional but recommended for sensitive workloads. Set `VpcSubnetIds` and `VpcSecurityGroupIds` in your parameters file.

When deployed in a VPC, Lambda functions lose direct internet access. You must provide network connectivity to AWS services via **VPC endpoints** or **NAT gateway**.

**Required VPC endpoints (if not using NAT gateway):**

| Service         | Endpoint Type  | Required By                                                           |
| --------------- | -------------- | --------------------------------------------------------------------- |
| S3              | Gateway (free) | All Lambdas                                                           |
| DynamoDB        | Gateway (free) | Router, Detection, Synthetic, Redact, Batch Trigger, Workflow Tracker |
| SQS             | Interface      | Router, Batch Trigger                                                 |
| Step Functions  | Interface      | Router                                                                |
| Bedrock Runtime | Interface      | Detection, Synthetic, Redact                                          |
| Textract        | Interface      | Detection, Redact                                                     |
| CloudWatch Logs | Interface      | All Lambdas                                                           |
| KMS             | Interface      | All Lambdas (only if using customer-managed KMS keys)                 |
| STS             | Interface      | All Lambdas                                                           |

**Security group requirements:**

- Lambda security group: allow **outbound HTTPS (443)** to all destinations
- VPC endpoint security group (interface endpoints): allow **inbound HTTPS (443)** from the Lambda security group

**If using NAT gateway:** No VPC endpoints are required: Lambda traffic routes through NAT to reach all AWS services.

### 9. Monitoring and Alerting

- Configure **CloudWatch alarms** for:
  - Lambda errors and timeouts
  - Unusual S3 access patterns
  - DynamoDB throttling or anomalies
  - KMS key usage spikes
- Enable **AWS CloudTrail** for API auditing
- Set up **SNS notifications** for critical events
- Add **AWS Config rules** for compliance monitoring

### 10. Manual Verification (MANDATORY)

- **Always verify** redacted documents manually before sharing
- Use **2-3 independent reviewers**
- Enable **bounding box visualization** to check PII coverage
- Test with **sample documents** before production
- Document your **verification process** for audits

---

## Hardening Items Not Enforced by Default

The IaC templates ship with a secure baseline, but the following items are not enforced by default and are not covered elsewhere in this guide. Evaluate and apply them based on your compliance posture:

1. **SQS transport encryption policy.** Beyond the at-rest SQS encryption in [SQS Queue Security](#7-sqs-queue-security), the queues do not include a queue policy requiring SSL/TLS (the `aws:SecureTransport` condition). Add a deny policy for non-SSL access in production environments.
2. **Lambda function-level Dead Letter Queue.** The SQS redrive DLQ is provisioned, but the Lambda functions themselves do not set a function-level Dead Letter Queue, so asynchronous invocation failures (as distinct from SQS redrive) are not captured unless you add one.
3. **Container image pinning.** `Dockerfile.layer` references `public.ecr.aws/sam/build-python3.13:latest`. Pin this to a specific image digest for reproducible, tamper-resistant builds.
4. **Lambda environment variable encryption.** Lambda environment variables are encrypted at rest with AWS-managed keys by default. Pass a `kms_key_arn` to the `aws_lambda_function` resource if your compliance posture requires customer-managed envelope encryption. (Customer-managed KMS for S3, DynamoDB, SQS, and CloudWatch is covered in [KMS Key Management](#3-kms-key-management).)

---

## S3 Bucket Access by Lambda Role

Each Lambda function gets its own IAM execution role (auto-created by CloudFormation). These roles need to be allowed in your bucket policies.

| Lambda           | Input Bucket                                           | Output Bucket                  |
| ---------------- | ------------------------------------------------------ | ------------------------------ |
| Router           | `s3:ListBucket`                                        | None                           |
| Detection        | `s3:GetObject`                                         | `s3:PutObject`                 |
| Synthetic        | None                                                   | `s3:GetObject`, `s3:PutObject` |
| Redact           | `s3:GetObject`                                         | `s3:GetObject`, `s3:PutObject` |
| Batch Trigger    | `s3:ListBucket`                                        | None                           |
| S3 Notification  | `s3:GetBucketNotification`, `s3:PutBucketNotification` | None                           |
| Workflow Tracker | None                                                   | None                           |

---

## Pre-Deployment Checklist

- [ ] S3 buckets created with Block Public Access
- [ ] Customer-managed KMS keys created (if required)
- [ ] IAM policies follow least privilege
- [ ] VPC endpoints or NAT gateway configured (if using VPC)
- [ ] Security group allows outbound HTTPS (443)
- [ ] CloudWatch alarms set up
- [ ] CloudTrail enabled
- [ ] Security team approved deployment
- [ ] Compliance requirements documented (GDPR, HIPAA, etc.)
- [ ] Manual verification procedures established
- [ ] Incident response plan ready

---

## Security Architecture

**Data Flow:**

1. Documents with PII → Encrypted S3 (customer KMS)
2. Lambda → Bedrock (PII sent for AI) → Textract (OCR)
3. PII mappings → Encrypted DynamoDB (customer KMS)
4. Redacted documents → Encrypted S3 (customer KMS)
5. All operations → Encrypted CloudWatch Logs (customer KMS)

**Trust Boundaries:**

- Customer AWS Account ↔ AWS Bedrock (PII crosses here)
- Lambda Role ↔ Customer Resources (S3, DynamoDB, KMS)
- Input Bucket ↔ Output Bucket (PII transformation)

---

## Additional Resources

- [AWS Security Best Practices](https://aws.amazon.com/architecture/security-identity-compliance/)
- [S3 Security](https://docs.aws.amazon.com/AmazonS3/latest/userguide/security-best-practices.html)
- [DynamoDB Security](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/best-practices-security.html)
- [KMS Best Practices](https://docs.aws.amazon.com/kms/latest/developerguide/best-practices.html)
- [Bedrock Data Protection](https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html)
