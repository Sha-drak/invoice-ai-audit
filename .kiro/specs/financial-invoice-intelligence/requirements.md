# Requirements Document

## Introduction

The Financial Invoice Intelligence System is a serverless, production-grade AWS application that enables organizations to upload invoices, extract and validate their contents, store structured records, and generate AI-powered financial summaries and dashboards. The system is built entirely on AWS Lambda (single function), Amazon S3, Amazon API Gateway (HTTP API), Amazon Textract, Amazon Bedrock (Claude 3 Sonnet), and Amazon DynamoDB. All processing is synchronous; no queues, workflow engines, or additional services are used.

The system exposes a JSON API consumed by a three-page static frontend. The backend performs no UI rendering — it returns structured JSON exclusively. The frontend renders upload, record-view, and AI-companion panels using plain HTML, CSS, and JavaScript.

---

## Glossary

- **System**: The single AWS Lambda function that implements all backend logic.
- **Orchestrator**: The Lambda handler that routes API Gateway requests to the correct internal handler.
- **Extractor**: The component of the System that calls Amazon Textract to convert invoice files to raw text.
- **Parser**: The component of the System that sends raw text to Amazon Bedrock and receives structured JSON.
- **Validator**: The deterministic component of the System that applies rule-based checks to structured invoice data.
- **Store**: The component of the System that writes and reads validated invoice records to/from Amazon DynamoDB.
- **AI_Summarizer**: The component of the System that calls Amazon Bedrock to produce an AI summary for a record.
- **AI_Dashboard**: The component of the System that calls Amazon Bedrock to produce a structured dashboard for a record.
- **Invoice**: A financial document uploaded by a user containing vendor, amount, VAT, and total information.
- **Record**: A logical grouping of one or more invoices identified by a `record_id`.
- **record_id**: A user-supplied string that groups invoices under a single record in DynamoDB.
- **invoice_id**: A unique identifier for a single invoice, extracted from the invoice document.
- **raw_text**: The plain-text output produced by Amazon Textract from an invoice file.
- **Structured_Invoice**: A JSON object with fields `invoice_id`, `vendor`, `amount`, `vat_amount`, `total` produced by the Parser.
- **Validation_Status**: Exactly one of: `VALID`, `MISMATCH`, `INVALID_STRUCTURE`, `INVALID_TYPES`, `INVALID_VALUES`.
- **validation_errors**: A list of human-readable strings describing validation failures for an invoice.
- **Invoice_Record**: A DynamoDB item with attributes: `record_id`, `invoice_id`, `vendor`, `amount`, `vat_amount`, `total`, `status`, `validation_errors`, `processed_at`.
- **Pretty_Printer**: The component of the System that formats Bedrock JSON responses back to strings for re-parsing (used internally for round-trip validation).
- **CORS_Headers**: HTTP response headers that allow cross-origin browser requests: `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Methods`, `Access-Control-Allow-Headers`.

---

## Requirements

---

### Requirement 1: Invoice File Upload and S3 Storage

**User Story:** As a finance user, I want to upload an invoice file through the API so that it is stored durably in S3 for downstream processing.

#### Acceptance Criteria

1. WHEN a `POST /upload` request is received with a file payload and a `record_id`, THE Orchestrator SHALL extract the file bytes (decoding from base64 if the API Gateway integration encodes the body).
2. WHEN the file bytes are extracted, THE System SHALL upload the file to the S3 bucket named `invoice-inbox` under the key `{record_id}/{timestamp}.pdf`, where `{timestamp}` is the UTC ISO 8601 timestamp at time of upload.
3. IF the S3 upload fails, THEN THE System SHALL return an HTTP 500 response with a JSON body `{"error": "S3 upload failed", "detail": "<error message>"}`.
4. THE System SHALL accept files up to the maximum payload size supported by API Gateway HTTP API (6 MB for synchronous Lambda integration).

---

### Requirement 2: Invoice Text Extraction via Textract

**User Story:** As a finance user, I want the system to automatically extract readable text from my uploaded invoice so that structured data can be derived from it.

#### Acceptance Criteria

1. WHEN a file has been stored in S3, THE Extractor SHALL call Amazon Textract `detect_document_text` using the S3 object reference (bucket and key) to produce `raw_text`.
2. THE Extractor SHALL concatenate all `LINE`-type blocks from the Textract response into a single `raw_text` string, separated by newline characters.
3. IF Textract returns no `LINE`-type blocks, THEN THE Extractor SHALL set `raw_text` to an empty string and continue processing.
4. IF the Textract API call fails, THEN THE System SHALL return an HTTP 500 response with a JSON body `{"error": "Textract extraction failed", "detail": "<error message>"}`.

---

### Requirement 3: Structured Invoice Extraction via Bedrock

**User Story:** As a finance user, I want the system to convert raw invoice text into structured JSON so that fields can be validated and stored.

#### Acceptance Criteria

1. WHEN `raw_text` is available, THE Parser SHALL invoke Amazon Bedrock with model ID `anthropic.claude-3-sonnet-20240229-v1:0` using a strict prompt instructing the model to output ONLY valid JSON with no markdown, no explanation, and no surrounding text.
2. THE Parser SHALL instruct Bedrock to extract exactly the fields: `invoice_id` (string), `vendor` (string), `amount` (number), `vat_amount` (number), `total` (number).
3. THE Parser SHALL instruct Bedrock that any missing numeric field MUST be represented as `0` and any missing string field MUST be represented as `"UNKNOWN"`.
4. WHEN the Bedrock response is received, THE Parser SHALL attempt to parse the response content as JSON.
5. IF the Bedrock response cannot be parsed as valid JSON, THEN THE Parser SHALL substitute a fallback object `{"invoice_id": "UNKNOWN", "vendor": "UNKNOWN", "amount": 0, "vat_amount": 0, "total": 0}` and continue processing without returning an error to the caller.
6. FOR ALL valid invoice documents, parsing the Bedrock response then serializing the Structured_Invoice to a JSON string then parsing that string again SHALL produce an object equal to the original Structured_Invoice (round-trip property).

---

### Requirement 4: Deterministic Validation Engine

**User Story:** As a finance controller, I want every extracted invoice to be validated against strict financial rules so that only accurate records are stored and anomalies are flagged.

#### Acceptance Criteria

1. WHEN a Structured_Invoice is received, THE Validator SHALL check that all five required fields (`invoice_id`, `vendor`, `amount`, `vat_amount`, `total`) are present; IF any field is missing, THEN THE Validator SHALL assign status `INVALID_STRUCTURE` and append `"MISSING_FIELD: <field_name>"` to `validation_errors`.
2. WHEN all required fields are present, THE Validator SHALL verify that `amount`, `vat_amount`, and `total` are numeric (integer or float) types; IF any is not numeric, THEN THE Validator SHALL assign status `INVALID_TYPES` and append `"NON_NUMERIC: <field_name>"` to `validation_errors`.
3. WHEN all numeric fields are confirmed to be numeric types, THE Validator SHALL verify that `amount`, `vat_amount`, and `total` are each greater than or equal to `0`; IF any value is negative, THEN THE Validator SHALL assign status `INVALID_VALUES` and append `"NEGATIVE_VALUE: <field_name>"` to `validation_errors`.
4. WHEN all values are non-negative, THE Validator SHALL verify that `amount + vat_amount` equals `total` within a tolerance of `±0.01`; IF the difference exceeds `0.01`, THEN THE Validator SHALL assign status `MISMATCH` and append `"TOTAL_MISMATCH expected <computed> got <total>"` to `validation_errors`.
5. WHEN all checks pass, THE Validator SHALL assign status `VALID` and set `validation_errors` to an empty list.
6. THE Validator SHALL assign exactly one Validation_Status per invoice — the checks MUST be evaluated in priority order: `INVALID_STRUCTURE` → `INVALID_TYPES` → `INVALID_VALUES` → `MISMATCH` → `VALID`.
7. WHILE performing arithmetic validation, THE Validator SHALL use decimal-safe comparison (rounding to 2 decimal places or using an epsilon of `0.01`) to avoid floating-point false positives.

---

### Requirement 5: Invoice Record Storage in DynamoDB

**User Story:** As a finance user, I want validated invoice data stored in DynamoDB so that records are durable, retrievable, and grouped by record.

#### Acceptance Criteria

1. WHEN validation is complete, THE Store SHALL write a single DynamoDB item to the `Records` table with partition key `record_id` (STRING) and sort key `invoice_id` (STRING).
2. THE Store SHALL write the following attributes: `vendor` (STRING), `amount` (NUMBER), `vat_amount` (NUMBER), `total` (NUMBER), `status` (STRING), `validation_errors` (LIST of strings), `processed_at` (STRING, UTC ISO 8601 format).
3. THE Store SHALL NOT write raw Textract output or raw Bedrock response text to DynamoDB.
4. IF the DynamoDB write fails, THEN THE System SHALL return an HTTP 500 response with a JSON body `{"error": "DynamoDB write failed", "detail": "<error message>"}`.
5. THE `POST /upload` endpoint SHALL return HTTP 200 with a JSON body containing `record_id`, `invoice_id`, `status`, `validation_errors`, and `processed_at` upon successful storage.

---

### Requirement 6: Invoice Record Retrieval API

**User Story:** As a finance user, I want to retrieve all invoices for a given record so that I can review their status and details.

#### Acceptance Criteria

1. WHEN a `GET /records` request is received, THE Store SHALL scan the DynamoDB `Records` table and return all items grouped by `record_id` as a JSON object where each key is a `record_id` and its value is a list of Invoice_Record objects.
2. WHEN a `GET /record/{record_id}` request is received, THE Store SHALL query the DynamoDB `Records` table for all items with the matching `record_id` partition key and return them as a JSON array.
3. IF no items exist for the requested `record_id`, THEN THE System SHALL return HTTP 200 with an empty JSON array `[]`.
4. THE System SHALL include CORS_Headers in all API responses.
5. IF the DynamoDB read fails, THEN THE System SHALL return an HTTP 500 response with a JSON body `{"error": "DynamoDB read failed", "detail": "<error message>"}`.

---

### Requirement 7: AI Summary Generation

**User Story:** As a finance controller, I want an AI-generated summary of all invoices in a record so that I can quickly identify anomalies, duplicates, and financial risk.

#### Acceptance Criteria

1. WHEN a `POST /ai/summary` request is received with body `{"record_id": "<id>"}`, THE AI_Summarizer SHALL retrieve all Invoice_Records for that `record_id` from DynamoDB.
2. WHEN the Invoice_Records are retrieved, THE AI_Summarizer SHALL call Amazon Bedrock (Claude 3 Sonnet) with a prompt instructing the model to analyze the invoices for anomalies, duplicate invoice IDs, VAT mismatches, and general financial health.
3. THE AI_Summarizer SHALL instruct Bedrock to return ONLY valid JSON with no markdown, no explanation, conforming to the schema: `{"summary": string, "flags": [{"type": string, "invoice_id": string, "severity": "low"|"medium"|"high"}], "overall_risk_score": number between 0 and 1}`.
4. WHEN the Bedrock response is received, THE AI_Summarizer SHALL parse the response as JSON and return it as the HTTP response body with status 200.
5. IF the Bedrock response cannot be parsed as valid JSON, THEN THE AI_Summarizer SHALL return HTTP 200 with a fallback body `{"summary": "AI analysis unavailable", "flags": [], "overall_risk_score": 0}`.
6. IF no Invoice_Records exist for the `record_id`, THEN THE System SHALL return HTTP 404 with body `{"error": "Record not found"}`.

---

### Requirement 8: AI Dashboard Generation

**User Story:** As a finance manager, I want an AI-generated financial dashboard for a record so that I can view totals, vendor breakdowns, and risk indicators at a glance.

#### Acceptance Criteria

1. WHEN a `POST /ai/dashboard` request is received with body `{"record_id": "<id>"}`, THE AI_Dashboard SHALL retrieve all Invoice_Records for that `record_id` from DynamoDB.
2. WHEN the Invoice_Records are retrieved, THE AI_Dashboard SHALL call Amazon Bedrock (Claude 3 Sonnet) with a prompt instructing the model to compute and return financial metrics.
3. THE AI_Dashboard SHALL instruct Bedrock to return ONLY valid JSON conforming to the schema: `{"totals": {"total_invoices": number, "total_amount": number, "average_invoice": number}, "vendor_breakdown": [{"vendor": string, "total": number}], "risk_indicators": {"high": number, "medium": number, "low": number}, "anomalies": [string]}`.
4. WHEN the Bedrock response is received, THE AI_Dashboard SHALL parse the response as JSON and return it as the HTTP response body with status 200.
5. IF the Bedrock response cannot be parsed as valid JSON, THEN THE AI_Dashboard SHALL return HTTP 200 with a fallback body `{"totals": {"total_invoices": 0, "total_amount": 0, "average_invoice": 0}, "vendor_breakdown": [], "risk_indicators": {"high": 0, "medium": 0, "low": 0}, "anomalies": ["AI analysis unavailable"]}`.
6. IF no Invoice_Records exist for the `record_id`, THEN THE System SHALL return HTTP 404 with body `{"error": "Record not found"}`.

---

### Requirement 9: CORS and API Gateway Integration

**User Story:** As a frontend developer, I want all API endpoints to return correct CORS headers so that the static frontend can call them from any origin.

#### Acceptance Criteria

1. THE System SHALL include the header `Access-Control-Allow-Origin: *` in all HTTP responses.
2. THE System SHALL include the header `Access-Control-Allow-Methods: GET, POST, OPTIONS` in all HTTP responses.
3. THE System SHALL include the header `Access-Control-Allow-Headers: Content-Type` in all HTTP responses.
4. WHEN an HTTP `OPTIONS` preflight request is received on any route, THE Orchestrator SHALL return HTTP 200 with CORS_Headers and an empty body.

---

### Requirement 10: Frontend Upload Page

**User Story:** As a finance user, I want a simple upload page so that I can submit invoice files and associate them with a record ID.

#### Acceptance Criteria

1. THE `index.html` page SHALL provide a file input accepting PDF files, a text input for `record_id`, and a submit button.
2. WHEN the submit button is clicked, THE page SHALL send a `POST /upload` request to the API Gateway endpoint with the file encoded as base64 in the request body alongside the `record_id`.
3. WHEN the API response is received, THE page SHALL display the returned `status`, `invoice_id`, and `validation_errors` to the user without any client-side computation.
4. THE page SHALL use only HTML, CSS, and vanilla JavaScript with no external frameworks.

---

### Requirement 11: Frontend Record View Page

**User Story:** As a finance user, I want to view all invoices in a record with their validation status so that I can audit invoice quality.

#### Acceptance Criteria

1. THE `records.html` page SHALL provide a text input for `record_id` and a button to fetch records via `GET /record/{record_id}`.
2. WHEN the API response is received, THE page SHALL render each invoice as a card showing `invoice_id`, `vendor`, `amount`, `vat_amount`, `total`, `status`, `validation_errors`, and `processed_at`.
3. WHEN an invoice has `status` equal to `VALID`, THE page SHALL render the card with a green border and green badge.
4. WHEN an invoice has `status` equal to `MISMATCH` or any `INVALID_*` status, THE page SHALL render the card with a red border and red badge.
5. THE page SHALL use only HTML, CSS, and vanilla JavaScript with no client-side computation of invoice totals or validation logic.

---

### Requirement 12: Frontend AI Companion Panel

**User Story:** As a finance manager, I want an AI companion panel that lets me generate summaries and dashboards for any record so that I can get instant financial insights.

#### Acceptance Criteria

1. THE `ai.html` page SHALL provide a text input for `record_id`, a "Generate Summary" button, and a "Generate Dashboard" button.
2. WHEN the "Generate Summary" button is clicked, THE page SHALL send a `POST /ai/summary` request and render the returned `summary`, `flags`, and `overall_risk_score` in a readable format.
3. WHEN the "Generate Dashboard" button is clicked, THE page SHALL send a `POST /ai/dashboard` request and render `totals`, `vendor_breakdown`, `risk_indicators`, and `anomalies`.
4. THE page SHALL use only HTML, CSS, and vanilla JavaScript with no client-side AI computation or data aggregation.

---

### Requirement 13: Single Lambda Architecture Constraint

**User Story:** As a platform engineer, I want the entire backend implemented in a single Lambda function so that the system is easy to deploy, debug, and maintain.

#### Acceptance Criteria

1. THE System SHALL implement all routing, extraction, parsing, validation, storage, and AI generation logic within a single Python 3.12 Lambda function in a single `.py` file.
2. THE System SHALL NOT use AWS Step Functions, EventBridge, SQS, SNS, or any additional AWS services beyond: S3, Lambda, DynamoDB, Textract, Bedrock, and API Gateway.
3. THE System SHALL NOT split logic into multiple Lambda functions or multiple code files.
4. THE System SHALL use the `boto3` SDK (available in the Lambda Python 3.12 runtime) for all AWS service interactions.
5. THE System SHALL use only Python standard library modules and `boto3` — no third-party packages requiring Lambda layers are needed for core logic.

---

### Requirement 14: IAM Permissions (Least Privilege)

**User Story:** As a security engineer, I want the Lambda execution role to have only the minimum permissions required so that the system follows the principle of least privilege.

#### Acceptance Criteria

1. THE Lambda execution role SHALL include permission to call `s3:PutObject` on the `invoice-inbox` bucket.
2. THE Lambda execution role SHALL include permission to call `textract:DetectDocumentText` on all resources.
3. THE Lambda execution role SHALL include permission to call `bedrock:InvokeModel` for model ARN `arn:aws:bedrock:*::foundation-model/anthropic.claude-3-sonnet-20240229-v1:0`.
4. THE Lambda execution role SHALL include permissions to call `dynamodb:PutItem`, `dynamodb:GetItem`, `dynamodb:Query`, and `dynamodb:Scan` on the `Records` table ARN.
5. THE Lambda execution role SHALL include `logs:CreateLogGroup`, `logs:CreateLogStream`, and `logs:PutLogEvents` permissions for CloudWatch Logs.
6. THE Lambda execution role SHALL NOT include wildcard (`*`) resource permissions for Bedrock, DynamoDB, or S3 put operations.
