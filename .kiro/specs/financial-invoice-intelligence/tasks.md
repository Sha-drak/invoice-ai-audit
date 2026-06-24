# Implementation Plan: Financial Invoice Intelligence System

## Overview

Implement the entire backend as a single Python 3.12 Lambda function (`lambda_handler.py`) plus three static HTML frontend pages. All AWS interactions use `boto3`. Tests use `pytest`, `hypothesis`, and `unittest.mock` — no real AWS credentials required.

---

## Tasks

- [ ] 1. Create project structure and test framework setup
  - Create `lambda_handler.py` at project root (empty module with placeholder handler)
  - Create `tests/` directory with `__init__.py` and `test_validator.py`, `test_parser.py`, `test_handler.py`
  - Add `requirements-test.txt` containing `pytest`, `hypothesis`, `boto3`, `moto[s3,dynamodb,textract]`
  - Confirm `pytest` runs without errors against empty test files
  - _Requirements: 13.1, 13.5_

- [ ] 2. Implement CORS response helper and Orchestrator routing
  - [ ] 2.1 Implement `make_response(status_code, body_dict)` helper
    - Returns API Gateway-compatible dict with `statusCode`, `headers` (CORS headers), `body` (JSON string)
    - Must include `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Methods: GET, POST, OPTIONS`, `Access-Control-Allow-Headers: Content-Type`
    - _Requirements: 9.1, 9.2, 9.3_
  - [ ] 2.2 Implement `lambda_handler(event, context)` routing skeleton
    - Extract `httpMethod` and `rawPath` (or `routeKey`) from API Gateway event
    - Dispatch OPTIONS to `handle_preflight()` → HTTP 200 with CORS headers and empty body
    - Dispatch POST /upload, GET /records, GET /record/{record_id}, POST /ai/summary, POST /ai/dashboard
    - Return HTTP 404 JSON for unknown routes
    - _Requirements: 9.4, 13.1_
  - [ ]* 2.3 Write unit tests for CORS headers and routing
    - Verify every route handler returns the three CORS headers
    - Verify OPTIONS returns 200 with empty body and CORS headers
    - Verify unknown route returns 404
    - _Requirements: 9.1, 9.2, 9.3, 9.4_

- [ ] 3. Implement the Validator
  - [ ] 3.1 Implement `validate_invoice(data: dict) -> tuple[str, list[str]]`
    - Check for presence of all 5 required fields; return `INVALID_STRUCTURE` with `MISSING_FIELD: <name>` errors on failure
    - Check that `amount`, `vat_amount`, `total` are `int` or `float`; return `INVALID_TYPES` with `NON_NUMERIC: <name>` errors on failure
    - Check that each of those values is `>= 0`; return `INVALID_VALUES` with `NEGATIVE_VALUE: <name>` errors on failure
    - Check `abs(amount + vat_amount - total) <= 0.01`; return `MISMATCH` with `TOTAL_MISMATCH expected <computed> got <total>` on failure
    - Return `VALID` with empty list when all checks pass
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_
  - [ ]* 3.2 Write property test: Validator assigns exactly one status (Property 2)
    - **Property 2: Validator assigns exactly one status**
    - **Validates: Requirements 4.6**
    - Use `hypothesis` `@given` with `fixed_dictionaries` and `one_of` to generate arbitrary dicts; verify return is `(str, list)` with str in `{VALID, MISMATCH, INVALID_STRUCTURE, INVALID_TYPES, INVALID_VALUES}`
  - [ ]* 3.3 Write property test: INVALID_STRUCTURE fires first (Property 3)
    - **Property 3: INVALID_STRUCTURE fires before other checks**
    - **Validates: Requirements 4.1, 4.6**
    - Generate dicts missing at least one required field; verify status == `INVALID_STRUCTURE`
  - [ ]* 3.4 Write property test: INVALID_TYPES fires before value/mismatch checks (Property 4)
    - **Property 4: INVALID_TYPES fires before value and mismatch checks**
    - **Validates: Requirements 4.2, 4.6**
    - Generate dicts with all 5 fields present but with non-numeric value in at least one numeric field; verify status == `INVALID_TYPES`
  - [ ]* 3.5 Write property test: INVALID_VALUES fires before mismatch (Property 5)
    - **Property 5: INVALID_VALUES fires before mismatch check**
    - **Validates: Requirements 4.3, 4.6**
    - Generate dicts with all 5 fields present, numeric, but at least one numeric field negative; verify status == `INVALID_VALUES`
  - [ ]* 3.6 Write property test: VALID iff all invariants hold (Property 6)
    - **Property 6: VALID status iff all invariants hold**
    - **Validates: Requirements 4.4, 4.5, 4.7**
    - Generate non-negative `amount` and `vat_amount`; set `total = round(amount + vat_amount, 2)`; verify status == `VALID` and `validation_errors == []`
  - [ ]* 3.7 Write property test: MISMATCH error message content (Property 7)
    - **Property 7: MISMATCH error message references actual and expected totals**
    - **Validates: Requirements 4.4**
    - Generate inputs triggering MISMATCH; verify `validation_errors` contains a string with both computed total and supplied total
  - [ ]* 3.8 Write unit tests for Validator edge cases
    - Boundary: `abs(amount + vat_amount - total) == 0.01` → VALID
    - Boundary: `abs(amount + vat_amount - total) == 0.011` → MISMATCH
    - `amount = 0`, `vat_amount = 0`, `total = 0` → VALID
    - `total` is string `"100"` → INVALID_TYPES
    - _Requirements: 4.4, 4.7_

- [ ] 4. Checkpoint — Ensure all Validator tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Implement the Extractor
  - [ ] 5.1 Implement `extract_text(bucket: str, key: str) -> str`
    - Call `textract_client.detect_document_text(Document={"S3Object": {"Bucket": bucket, "Name": key}})`
    - Filter response blocks for `BlockType == "LINE"` and join with `"\n"`
    - Return empty string if no LINE blocks
    - Raise `RuntimeError` (caught by the handler) if the Textract call fails
    - _Requirements: 2.1, 2.2, 2.3, 2.4_
  - [ ]* 5.2 Write property test: LINE block concatenation (Property derived from 2.2)
    - **Property: LINE block filtering and concatenation**
    - **Validates: Requirements 2.2, 2.3**
    - Generate arbitrary lists of block dicts with `BlockType` values from `{LINE, WORD, KEY_VALUE_SET, PAGE}`; verify output contains exactly and only LINE block texts in order
  - [ ]* 5.3 Write unit tests for Extractor
    - Empty block list → empty string
    - Mixed block types → only LINE blocks in output
    - Textract failure → RuntimeError raised
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

- [ ] 6. Implement the Parser
  - [ ] 6.1 Implement `parse_invoice(raw_text: str) -> dict`
    - Build a Bedrock prompt that demands JSON-only output with exactly the 5 fields; no markdown, no explanation
    - Instruct model: missing numeric fields → `0`; missing string fields → `"UNKNOWN"`
    - Call `bedrock_client.invoke_model(modelId="anthropic.claude-3-sonnet-20240229-v1:0", body=...)`
    - Parse the response body JSON; on any `json.JSONDecodeError` return the fallback dict
    - Fallback: `{"invoice_id": "UNKNOWN", "vendor": "UNKNOWN", "amount": 0, "vat_amount": 0, "total": 0}`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_
  - [ ]* 6.2 Write property test: Structured Invoice JSON round-trip (Property 1)
    - **Property 1: Structured Invoice JSON round-trip**
    - **Validates: Requirements 3.6**
    - Use `hypothesis` to generate dicts with string `invoice_id`/`vendor` and numeric `amount`/`vat_amount`/`total`; verify `json.loads(json.dumps(invoice)) == invoice`
  - [ ]* 6.3 Write unit tests for Parser
    - Valid Bedrock JSON response → returns parsed dict
    - Invalid JSON in Bedrock response → returns fallback dict
    - Bedrock call mocked; verify `modelId` is `anthropic.claude-3-sonnet-20240229-v1:0`
    - _Requirements: 3.1, 3.4, 3.5_

- [ ] 7. Implement the Store (DynamoDB)
  - [ ] 7.1 Implement `store_record(record_id, invoice_id, vendor, amount, vat_amount, total, status, validation_errors) -> str`
    - Write DynamoDB item to table `Records` with all required attributes
    - Include `processed_at` as UTC ISO 8601 string generated at write time
    - Raise `RuntimeError` on DynamoDB failure (caught by handler → HTTP 500)
    - Return `processed_at` string
    - _Requirements: 5.1, 5.2, 5.3, 5.4_
  - [ ] 7.2 Implement `get_all_records() -> dict`
    - Perform DynamoDB `scan` on `Records` table
    - Group items by `record_id` into a dict of lists
    - Raise `RuntimeError` on DynamoDB failure
    - _Requirements: 6.1, 6.5_
  - [ ] 7.3 Implement `get_records_by_id(record_id: str) -> list`
    - Perform DynamoDB `query` with `KeyConditionExpression` on `record_id`
    - Return list (empty list if none found)
    - Raise `RuntimeError` on DynamoDB failure
    - _Requirements: 6.2, 6.3, 6.5_
  - [ ]* 7.4 Write property test: Record grouping invariant (Property derived from 6.1)
    - **Property: grouping by record_id preserves all records**
    - **Validates: Requirements 6.1**
    - Generate arbitrary lists of Invoice_Record dicts with varying `record_id` values; verify that the grouped dict contains every record under the correct key and total item count is preserved
  - [ ]* 7.5 Write unit tests for Store
    - `store_record` writes item with all 9 required attributes (mock DynamoDB)
    - `get_all_records` groups items correctly
    - `get_records_by_id` returns empty list for unknown record_id
    - DynamoDB failure on write → RuntimeError
    - DynamoDB failure on read → RuntimeError
    - _Requirements: 5.1, 5.2, 5.4, 6.2, 6.3, 6.5_

- [ ] 8. Implement the upload handler and wire Extractor + Parser + Validator + Store
  - [ ] 8.1 Implement `handle_upload(event) -> dict`
    - Extract file bytes from event body; base64-decode if `isBase64Encoded` flag is set
    - Extract `record_id` from JSON body
    - Generate S3 key as `{record_id}/{utc_iso_timestamp}.pdf`
    - Call S3 `put_object`; on failure return HTTP 500 via `make_response`
    - Call `extract_text(bucket, key)`; on `RuntimeError` return HTTP 500
    - Call `parse_invoice(raw_text)` → `structured_invoice`
    - Call `validate_invoice(structured_invoice)` → `(status, errors)`
    - Call `store_record(...)`; on `RuntimeError` return HTTP 500
    - Return HTTP 200 with `record_id`, `invoice_id`, `status`, `validation_errors`, `processed_at`
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.4, 3.1, 4.1, 5.4, 5.5_
  - [ ]* 8.2 Write property test: S3 key format (Property 8)
    - **Property 8: S3 key format for any valid record_id**
    - **Validates: Requirements 1.2**
    - Generate arbitrary non-empty ASCII strings as `record_id`; verify constructed key starts with `record_id + "/"` and ends with `".pdf"` and contains a valid ISO timestamp segment
  - [ ]* 8.3 Write unit tests for upload handler
    - Happy path: all mocks succeed → 200 response with correct fields
    - S3 failure → 500 with `{"error": "S3 upload failed", ...}`
    - Textract failure → 500 with `{"error": "Textract extraction failed", ...}`
    - DynamoDB write failure → 500 with `{"error": "DynamoDB write failed", ...}`
    - _Requirements: 1.3, 2.4, 5.4_

- [ ] 9. Checkpoint — Ensure all upload pipeline tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 10. Implement the record retrieval handlers
  - [ ] 10.1 Implement `handle_get_all_records() -> dict`
    - Call `get_all_records()`; on `RuntimeError` return HTTP 500
    - Return HTTP 200 with grouped records dict
    - _Requirements: 6.1, 6.4, 6.5_
  - [ ] 10.2 Implement `handle_get_record(record_id: str) -> dict`
    - Call `get_records_by_id(record_id)`; on `RuntimeError` return HTTP 500
    - Return HTTP 200 with list (empty list if no items found — no 404 for empty)
    - _Requirements: 6.2, 6.3, 6.4, 6.5_
  - [ ]* 10.3 Write unit tests for record retrieval handlers
    - `GET /records` → 200 with grouped dict (mock DynamoDB scan)
    - `GET /record/{id}` with existing data → 200 with list
    - `GET /record/{id}` with no data → 200 with empty list `[]`
    - DynamoDB scan failure → 500
    - DynamoDB query failure → 500
    - CORS headers present on all responses
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

- [ ] 11. Implement AI_Summarizer and AI_Dashboard handlers
  - [ ] 11.1 Implement `handle_ai_summary(event) -> dict`
    - Parse `record_id` from request body JSON
    - Call `get_records_by_id(record_id)`; on `RuntimeError` return HTTP 500
    - If records list is empty, return HTTP 404 `{"error": "Record not found"}`
    - Build Bedrock prompt with records JSON, requesting anomaly/duplicate/VAT/risk analysis
    - Instruct model to return ONLY JSON matching schema: `{"summary": str, "flags": [...], "overall_risk_score": float}`
    - Call Bedrock; parse response JSON; on parse failure return fallback `{"summary": "AI analysis unavailable", "flags": [], "overall_risk_score": 0}`
    - Return HTTP 200 with parsed or fallback body
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_
  - [ ] 11.2 Implement `handle_ai_dashboard(event) -> dict`
    - Parse `record_id` from request body JSON
    - Call `get_records_by_id(record_id)`; on `RuntimeError` return HTTP 500
    - If records list is empty, return HTTP 404 `{"error": "Record not found"}`
    - Build Bedrock prompt requesting totals/vendor breakdown/risk indicators/anomalies
    - Instruct model to return ONLY JSON matching dashboard schema
    - Call Bedrock; parse response JSON; on parse failure return zero-value fallback dashboard
    - Return HTTP 200 with parsed or fallback body
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_
  - [ ]* 11.3 Write unit tests for AI handlers
    - Summary happy path: mock DynamoDB returns records, mock Bedrock returns valid JSON → 200 with parsed body
    - Summary Bedrock parse failure → 200 with fallback body
    - Summary empty records → 404
    - Dashboard happy path: valid Bedrock JSON → 200 parsed
    - Dashboard Bedrock parse failure → 200 fallback
    - Dashboard empty records → 404
    - CORS headers on all responses
    - _Requirements: 7.4, 7.5, 7.6, 8.4, 8.5, 8.6_

- [ ] 12. Checkpoint — Ensure all handler tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 13. Implement the static frontend: index.html (Upload Page)
  - Create `frontend/index.html`
  - Add a file input (accepts PDF), a text input for `record_id`, and a submit button
  - On submit: read the file, base64-encode it using `FileReader`, POST to the API Gateway URL with body `{"record_id": ..., "file": "<base64>", "filename": ...}`
  - Display the returned `status`, `invoice_id`, and `validation_errors` in a result section
  - Style with inline CSS only; no external frameworks or CDN links
  - _Requirements: 10.1, 10.2, 10.3, 10.4_

- [ ] 14. Implement the static frontend: records.html (Record View Page)
  - Create `frontend/records.html`
  - Add a text input for `record_id` and a "Fetch Records" button
  - On button click: GET `{API_URL}/record/{record_id}` and render each invoice as a card
  - Each card shows: `invoice_id`, `vendor`, `amount`, `vat_amount`, `total`, `status`, `validation_errors`, `processed_at`
  - Cards with `status == "VALID"` get green border and green badge; all other statuses get red border and red badge
  - Style with inline CSS only; no external frameworks
  - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_

- [ ] 15. Implement the static frontend: ai.html (AI Companion Panel)
  - Create `frontend/ai.html`
  - Add a text input for `record_id`, a "Generate Summary" button, and a "Generate Dashboard" button
  - "Generate Summary": POST `{API_URL}/ai/summary`; render `summary` text, `flags` as a table, `overall_risk_score` as a progress bar or numeric display
  - "Generate Dashboard": POST `{API_URL}/ai/dashboard`; render `totals` section, `vendor_breakdown` as a table, `risk_indicators` counts, `anomalies` list
  - Style with inline CSS only; no external frameworks
  - _Requirements: 12.1, 12.2, 12.3, 12.4_

- [ ] 16. Final checkpoint — Ensure all tests pass and integration is complete
  - Run full `pytest` suite; all tests must pass
  - Verify `lambda_handler.py` is a single file with no imports beyond `boto3`, `json`, `base64`, `datetime`, `decimal`, and other Python standard library modules
  - Verify `frontend/` contains exactly `index.html`, `records.html`, `ai.html`
  - Ensure all tests pass, ask the user if questions arise.

---

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP
- All AWS calls in tests are mocked via `unittest.mock.patch` or `moto` — no real AWS credentials needed
- Property tests use Hypothesis with `max_examples=100` minimum; each test includes a comment referencing its design property number
- The `API_URL` constant in HTML files should be set to the deployed API Gateway endpoint URL before use
- IAM role and DynamoDB table / S3 bucket creation are infrastructure tasks outside this coding plan; they should be set up before deploying the Lambda

## Task Dependency Graph

```json
{
  "waves": [
    {"wave": 1, "tasks": ["1"]},
    {"wave": 2, "tasks": ["2", "3"]},
    {"wave": 3, "tasks": ["4"]},
    {"wave": 4, "tasks": ["5", "6", "7"]},
    {"wave": 5, "tasks": ["8"]},
    {"wave": 6, "tasks": ["9"]},
    {"wave": 7, "tasks": ["10", "11"]},
    {"wave": 8, "tasks": ["12"]},
    {"wave": 9, "tasks": ["13", "14", "15"]},
    {"wave": 10, "tasks": ["16"]}
  ]
}
```

- Task 1 (project setup) must come first
- Tasks 2–3 can proceed in parallel after task 1; checkpoint 4 gates further work
- Tasks 5 (Extractor), 6 (Parser), 7 (Store) depend on task 3 (Validator) being stable but are otherwise independent of each other
- Task 8 (upload handler) wires tasks 5, 6, 7 together and depends on all three
- Tasks 10 and 11 depend on task 7 (Store) being complete
- Tasks 13–15 (frontend) are independent of test results and can be worked in parallel with tasks 10–12
