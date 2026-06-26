# Financial Invoice Intelligence System

A serverless AWS application for uploading invoices, extracting structured data with Textract and Bedrock, validating financial fields, storing records in DynamoDB, and generating AI-powered summaries and dashboards.

The backend is a single Python 3.12 Lambda function (`lambda_handler.py`). The frontend is three static HTML pages that talk to the API over JSON.

## Architecture

```
Browser (index.html / records.html / ai.html)
        │
        ▼
API Gateway HTTP API
        │
        ▼
Lambda (lambda_handler.py)
   ├── S3          invoice file storage
   ├── Textract    OCR / text extraction
   ├── Bedrock     invoice parsing + AI analysis
   └── DynamoDB    structured invoice records
```

**Upload flow:** `POST /upload` → store PDF in S3 → Textract extracts text → Bedrock parses fields → validator checks totals → record saved to DynamoDB.

## Prerequisites

| Tool | Version |
|------|---------|
| Python | 3.12+ |
| AWS account | With access to S3, Lambda, API Gateway, Textract, Bedrock, DynamoDB |
| AWS CLI | Configured with credentials (`aws configure`) |

For local development you only need Python. The test suite mocks all AWS calls — no credentials required to run tests.

## Project structure

```
invoice-ai-audit/
├── lambda_handler.py      # Single Lambda backend (all routes and logic)
├── frontend/
│   ├── index.html         # Upload invoices
│   ├── records.html       # View records by record_id
│   └── ai.html            # AI summary and dashboard
├── tests/                 # pytest + hypothesis (mocked AWS)
└── requirements-test.txt  # Test dependencies only
```

## Run tests locally

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install test dependencies
pip install -r requirements-test.txt

# Run the full suite
pytest
```

All 87 tests use `unittest.mock` and `moto` — no live AWS services are called.

## Deploy to AWS

There is no infrastructure-as-code in this repository. Deploy the AWS resources below, then attach `lambda_handler.py` as the function code.

### 1. Create AWS resources

**S3 bucket** — stores uploaded invoice PDFs.

```bash
aws s3 mb s3://invoice-inbox --region us-west-2
```

**DynamoDB table** — partition key `record_id` (String), sort key `invoice_id` (String).

```bash
aws dynamodb create-table \
  --table-name Records \
  --attribute-definitions \
      AttributeName=record_id,AttributeType=S \
      AttributeName=invoice_id,AttributeType=S \
  --key-schema \
      AttributeName=record_id,KeyType=HASH \
      AttributeName=invoice_id,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --region us-west-2
```

**Bedrock model access** — in the AWS console, open Amazon Bedrock → Model access and enable the model you plan to use. The default in code is `us.amazon.nova-2-lite-v1:0`. Override with the `BEDROCK_MODEL_ID` environment variable if needed.

### 2. Create the Lambda function

1. Runtime: **Python 3.12**
2. Handler: `lambda_handler.lambda_handler`
3. Upload `lambda_handler.py` as the function code (no extra dependencies — `boto3` is included in the runtime)
4. Set environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `BUCKET_NAME` | `invoice-inbox` | S3 bucket for uploaded PDFs |
| `TABLE_NAME` | `Records` | DynamoDB table name |
| `BEDROCK_MODEL_ID` | `us.amazon.nova-2-lite-v1:0` | Bedrock model for parsing and AI |
| `AWS_REGION` | `us-west-2` | AWS region (set automatically in Lambda) |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

5. Attach an execution role with least-privilege permissions:

- `s3:PutObject` on `arn:aws:s3:::invoice-inbox/*`
- `textract:DetectDocumentText`
- `bedrock:InvokeModel` on your chosen model ARN
- `dynamodb:PutItem`, `dynamodb:Query`, `dynamodb:Scan` on the `Records` table
- CloudWatch Logs (`logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`)

6. Increase the timeout (recommended: **60 seconds**) — Textract and Bedrock calls can be slow.

### 3. Create API Gateway HTTP API

Create an HTTP API and integrate each route with the Lambda function:

| Method | Path | Handler |
|--------|------|---------|
| `OPTIONS` | `/*` | Lambda (CORS preflight) |
| `POST` | `/upload` | Lambda |
| `GET` | `/records` | Lambda |
| `GET` | `/record/{record_id}` | Lambda |
| `POST` | `/ai/summary` | Lambda |
| `POST` | `/ai/dashboard` | Lambda |

Enable CORS on the API (the Lambda also returns CORS headers). Note the invoke URL, e.g. `https://abc123.execute-api.us-west-2.amazonaws.com`.

## Run the frontend

The frontend pages are static HTML. They do not run through a build step.

### 1. Set the API URL

In each file under `frontend/`, replace the placeholder:

```javascript
const API_URL = "https://YOUR-API-GATEWAY-URL";
```

with your deployed API Gateway URL (no trailing slash):

```javascript
const API_URL = "https://abc123.execute-api.us-west-2.amazonaws.com";
```

Files to update:

- `frontend/index.html`
- `frontend/records.html`
- `frontend/ai.html`

### 2. Open the pages

**Option A — local file:** open the HTML files directly in a browser. This works if your API Gateway CORS policy allows requests from `file://` origins (the Lambda returns `Access-Control-Allow-Origin: *`, which covers most cases).

**Option B — static hosting:** upload the `frontend/` folder to S3 static website hosting, CloudFront, or any web server.

### 3. Use the app

1. **Upload** (`index.html`) — enter a `record_id`, choose a PDF invoice, and submit. The API runs extraction, parsing, validation, and storage.
2. **Records** (`records.html`) — enter a `record_id` to fetch all invoices in that group.
3. **AI** (`ai.html`) — enter a `record_id`, then generate a risk summary or financial dashboard.

## API reference

### `POST /upload`

Upload and process an invoice.

**Request body:**

```json
{
  "record_id": "batch-001",
  "file": "<base64-encoded PDF>"
}
```

**Response (200):**

```json
{
  "record_id": "batch-001",
  "invoice_id": "INV-123",
  "status": "VALID",
  "validation_errors": [],
  "processed_at": "2026-06-26T08:00:00Z"
}
```

Possible `status` values: `VALID`, `MISMATCH`, `INVALID_STRUCTURE`, `INVALID_TYPES`, `INVALID_VALUES`.

### `GET /records`

Returns all invoice records grouped by `record_id`.

### `GET /record/{record_id}`

Returns all invoices for a single `record_id`.

### `POST /ai/summary`

**Request body:** `{ "record_id": "batch-001" }`

**Response:** `{ "summary", "flags", "overall_risk_score" }`

### `POST /ai/dashboard`

**Request body:** `{ "record_id": "batch-001" }`

**Response:** `{ "totals", "vendor_breakdown", "risk_indicators", "anomalies" }`

All endpoints return JSON with CORS headers. Errors use `{ "error": "...", "detail": "..." }`.

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `Network error` in the browser | `API_URL` not set, or API Gateway URL is wrong |
| `S3 upload failed` | Lambda role missing `s3:PutObject`, or bucket name mismatch |
| `Textract extraction failed` | File is not a readable PDF, or Textract permissions missing |
| `DynamoDB write failed` | Table name mismatch, or missing DynamoDB permissions |
| AI returns fallback text | Bedrock model not enabled, wrong `BEDROCK_MODEL_ID`, or invoke permission missing |
| Tests fail on import | Run from the repo root with the virtual environment activated |

## License

See repository license file if present.
