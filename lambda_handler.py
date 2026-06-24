"""
Financial Invoice Intelligence System
Single Lambda function implementing all backend logic.
"""

import base64
import json
import re
import boto3
from datetime import datetime, timezone
from decimal import Decimal


# ---------------------------------------------------------------------------
# AWS Clients / Resources (lazy singletons)
# ---------------------------------------------------------------------------
# Clients are created on first use so that importing this module in tests
# (without AWS credentials or a region configured) does not fail.

_s3_client = None
_textract_client = None
_bedrock_client = None
_dynamodb_resource = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _get_textract_client():
    global _textract_client
    if _textract_client is None:
        _textract_client = boto3.client("textract")
    return _textract_client


def _get_bedrock_client():
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime")
    return _bedrock_client


def _get_dynamodb_resource():
    global _dynamodb_resource
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb")
    return _dynamodb_resource


# Module-level aliases used by tests via unittest.mock.patch
s3_client = None          # patched in tests as lambda_handler.s3_client
textract_client = None    # patched in tests as lambda_handler.textract_client
bedrock_client = None     # patched in tests as lambda_handler.bedrock_client
dynamodb_resource = None  # patched in tests as lambda_handler.dynamodb_resource

RECORDS_TABLE = "Records"


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

def extract_text(bucket: str, key: str) -> str:
    """
    Call Amazon Textract to extract text from an S3 object.

    Returns all LINE-type blocks joined by newlines.
    Returns empty string if there are no LINE blocks.
    Raises RuntimeError if the Textract call fails.
    """
    client = textract_client or _get_textract_client()
    try:
        response = client.detect_document_text(
            Document={"S3Object": {"Bucket": bucket, "Name": key}}
        )
        lines = [
            block["Text"]
            for block in response.get("Blocks", [])
            if block.get("BlockType") == "LINE"
        ]
        return "\n".join(lines)
    except Exception as e:
        raise RuntimeError(str(e))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_invoice(raw_text: str) -> dict:
    """
    Call Amazon Bedrock (Claude 3 Sonnet) to parse raw invoice text into a
    structured JSON dict.

    Returns a dict with keys: invoice_id, vendor, amount, vat_amount, total.
    Falls back to UNKNOWN/0 values if the model response cannot be parsed.
    """
    prompt = (
        "You are an invoice parser. Extract the following fields from the invoice text "
        "and return ONLY valid JSON with no markdown, no explanation, and no surrounding text.\n\n"
        "Required JSON schema:\n"
        '{"invoice_id": string, "vendor": string, "amount": number, "vat_amount": number, "total": number}\n\n'
        "Rules:\n"
        "- Missing numeric fields MUST be represented as 0\n"
        "- Missing string fields MUST be represented as \"UNKNOWN\"\n"
        "- Output ONLY the JSON object, nothing else\n\n"
        f"Invoice text:\n{raw_text}"
    )

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}]
    })

    _fallback = {
        "invoice_id": "UNKNOWN",
        "vendor": "UNKNOWN",
        "amount": 0,
        "vat_amount": 0,
        "total": 0,
    }

    # Use module-level name when patched by tests; fall back to lazy singleton
    client = bedrock_client or _get_bedrock_client()

    try:
        response = client.invoke_model(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            body=body,
        )
        response_body = json.loads(response["body"].read())
        text = response_body["content"][0]["text"]
    except Exception:
        return _fallback

    # First attempt: direct JSON parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Second attempt: extract JSON with regex
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return _fallback


# ---------------------------------------------------------------------------
# Store (DynamoDB)
# ---------------------------------------------------------------------------

def _decimal_to_float(obj):
    """Recursively convert Decimal values to float for JSON serialization."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_float(i) for i in obj]
    return obj


def store_record(
    record_id: str,
    invoice_id: str,
    vendor: str,
    amount,
    vat_amount,
    total,
    status: str,
    validation_errors: list,
) -> str:
    """
    Write an Invoice_Record to the DynamoDB Records table.

    Returns the processed_at ISO 8601 UTC timestamp string.
    Raises RuntimeError on DynamoDB failure.
    """
    processed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    ddb = dynamodb_resource or _get_dynamodb_resource()
    try:
        table = ddb.Table(RECORDS_TABLE)
        table.put_item(Item={
            "record_id": record_id,
            "invoice_id": invoice_id,
            "vendor": vendor,
            "amount": Decimal(str(amount)),
            "vat_amount": Decimal(str(vat_amount)),
            "total": Decimal(str(total)),
            "status": status,
            "validation_errors": validation_errors,
            "processed_at": processed_at,
        })
        return processed_at
    except Exception as e:
        raise RuntimeError(str(e))


def get_all_records() -> dict:
    """
    Scan the DynamoDB Records table and return all items grouped by record_id.

    Returns a dict of {record_id: [Invoice_Record, ...]}.
    Raises RuntimeError on DynamoDB failure.
    """
    ddb = dynamodb_resource or _get_dynamodb_resource()
    try:
        table = ddb.Table(RECORDS_TABLE)
        response = table.scan()
        items = response.get("Items", [])
        grouped = {}
        for item in items:
            item = _decimal_to_float(item)
            rid = item.get("record_id")
            grouped.setdefault(rid, []).append(item)
        return grouped
    except Exception as e:
        raise RuntimeError(str(e))


def get_records_by_id(record_id: str) -> list:
    """
    Query the DynamoDB Records table for all items with the given record_id.

    Returns a list of Invoice_Record dicts (empty list if none found).
    Raises RuntimeError on DynamoDB failure.
    """
    from boto3.dynamodb.conditions import Key
    ddb = dynamodb_resource or _get_dynamodb_resource()
    try:
        table = ddb.Table(RECORDS_TABLE)
        response = table.query(
            KeyConditionExpression=Key("record_id").eq(record_id)
        )
        items = response.get("Items", [])
        return [_decimal_to_float(item) for item in items]
    except Exception as e:
        raise RuntimeError(str(e))


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ["invoice_id", "vendor", "amount", "vat_amount", "total"]
_NUMERIC_FIELDS = ["amount", "vat_amount", "total"]


def validate_invoice(data: dict) -> tuple:
    """
    Validate a structured invoice dict.

    Returns a tuple of (status: str, validation_errors: list[str]).
    Checks are applied in strict priority order:
      1. INVALID_STRUCTURE  — any required field is absent
      2. INVALID_TYPES      — amount / vat_amount / total not int or float (bools excluded)
      3. INVALID_VALUES     — any numeric field is negative
      4. MISMATCH           — abs(amount + vat_amount - total) > 0.01
      5. VALID              — all checks pass
    """
    # Step 1: Check all 5 required fields are present
    missing = [f for f in _REQUIRED_FIELDS if f not in data]
    if missing:
        errors = [f"MISSING_FIELD: {f}" for f in missing]
        return ("INVALID_STRUCTURE", errors)

    # Step 2: Check amount, vat_amount, total are numeric (int or float, not bool)
    # Note: bool is a subclass of int in Python — treat booleans as non-numeric
    non_numeric = [
        f for f in _NUMERIC_FIELDS
        if isinstance(data[f], bool) or not isinstance(data[f], (int, float))
    ]
    if non_numeric:
        errors = [f"NON_NUMERIC: {f}" for f in non_numeric]
        return ("INVALID_TYPES", errors)

    amount = data["amount"]
    vat_amount = data["vat_amount"]
    total = data["total"]

    # Step 3: Check all numeric fields are >= 0
    negative = [f for f in _NUMERIC_FIELDS if data[f] < 0]
    if negative:
        errors = [f"NEGATIVE_VALUE: {f}" for f in negative]
        return ("INVALID_VALUES", errors)

    # Step 4: Check amount + vat_amount ≈ total (tolerance ±0.01)
    # Use round() for decimal-safe comparison to avoid floating-point false positives
    # (Requirement 4.7: epsilon of 0.01; round to 10 places to strip FP noise)
    computed = round(amount + vat_amount, 2)
    if round(abs(amount + vat_amount - total), 10) > 0.01:
        return ("MISMATCH", [f"TOTAL_MISMATCH expected {computed} got {total}"])

    # Step 5: All checks pass
    return ("VALID", [])


# ---------------------------------------------------------------------------
# CORS / response helper
# ---------------------------------------------------------------------------

def make_response(status_code: int, body_dict: dict) -> dict:
    """Return an API Gateway-compatible response dict with CORS headers."""
    return {
        "statusCode": status_code,
        "headers": {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Content-Type": "application/json",
        },
        "body": json.dumps(body_dict),
    }


# ---------------------------------------------------------------------------
# Route handlers (stubs)
# ---------------------------------------------------------------------------

def handle_preflight() -> dict:
    """Handle CORS preflight OPTIONS request."""
    return make_response(200, {})


def handle_upload(event: dict) -> dict:
    """POST /upload — upload an invoice file and run the full processing pipeline."""
    # 1. Parse body
    raw_body = event.get("body", "{}")
    if isinstance(raw_body, dict):
        body = raw_body
    else:
        body = json.loads(raw_body)

    # 2. Extract record_id and file_b64
    record_id = body.get("record_id", "")
    file_b64 = body.get("file", "")

    # 3. If isBase64Encoded is True the raw body itself is base64; decode that first
    if event.get("isBase64Encoded", False):
        raw_bytes = base64.b64decode(event.get("body", ""))
        decoded_body = json.loads(raw_bytes)
        record_id = decoded_body.get("record_id", record_id)
        file_b64 = decoded_body.get("file", file_b64)

    # 4. Decode the base64 file bytes
    file_bytes = base64.b64decode(file_b64)

    # 5. Generate S3 key (no colons — use compact timestamp format)
    key = f"{record_id}/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.pdf"

    # 6. Upload to S3
    try:
        (s3_client or _get_s3_client()).put_object(
            Bucket="invoice-inbox", Key=key, Body=file_bytes
        )
    except Exception as e:
        return make_response(500, {"error": "S3 upload failed", "detail": str(e)})

    # 7. Extract text via Textract
    try:
        raw_text = extract_text("invoice-inbox", key)
    except RuntimeError as e:
        return make_response(500, {"error": "Textract extraction failed", "detail": str(e)})

    # 8. Parse invoice via Bedrock (always returns a dict — no exception)
    structured_invoice = parse_invoice(raw_text)

    # 9. Validate
    status, validation_errors = validate_invoice(structured_invoice)

    # 10. Store in DynamoDB
    try:
        processed_at = store_record(
            record_id,
            structured_invoice["invoice_id"],
            structured_invoice["vendor"],
            structured_invoice["amount"],
            structured_invoice["vat_amount"],
            structured_invoice["total"],
            status,
            validation_errors,
        )
    except RuntimeError as e:
        return make_response(500, {"error": "DynamoDB write failed", "detail": str(e)})

    # 11. Return success
    return make_response(200, {
        "record_id": record_id,
        "invoice_id": structured_invoice["invoice_id"],
        "status": status,
        "validation_errors": validation_errors,
        "processed_at": processed_at,
    })


def handle_get_all_records() -> dict:
    """GET /records — return all records grouped by record_id."""
    try:
        grouped = get_all_records()
        return make_response(200, grouped)
    except RuntimeError as e:
        return make_response(500, {"error": "DynamoDB read failed", "detail": str(e)})


def handle_get_record(record_id: str) -> dict:
    """GET /record/{record_id} — return invoices for a given record_id."""
    try:
        items = get_records_by_id(record_id)
        return make_response(200, items)
    except RuntimeError as e:
        return make_response(500, {"error": "DynamoDB read failed", "detail": str(e)})


def _call_bedrock(prompt: str) -> str | None:
    """Call Bedrock Claude 3 Sonnet. Returns response text or None on failure."""
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": prompt}]
    })
    try:
        client = bedrock_client or _get_bedrock_client()
        response = client.invoke_model(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            body=body,
        )
        resp_body = json.loads(response["body"].read())
        return resp_body["content"][0]["text"]
    except Exception:
        return None


def _parse_bedrock_json(text: str | None, fallback: dict) -> dict:
    """Parse Bedrock text response as JSON; use regex fallback; return fallback dict on failure."""
    if text is None:
        return fallback
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return fallback


def handle_ai_summary(event: dict) -> dict:
    """POST /ai/summary — generate AI summary for a record."""
    body = json.loads(event.get("body", "{}")) if isinstance(event.get("body"), str) else event.get("body", {})
    record_id = body.get("record_id", "")

    try:
        records = get_records_by_id(record_id)
    except RuntimeError as e:
        return make_response(500, {"error": "DynamoDB read failed", "detail": str(e)})

    if not records:
        return make_response(404, {"error": "Record not found"})

    _summary_fallback = {"summary": "AI analysis unavailable", "flags": [], "overall_risk_score": 0}

    prompt = (
        "You are a financial analyst reviewing invoice records.\n"
        "Analyze the following invoices and detect anomalies, duplicate invoice IDs, "
        "VAT mismatches, and assess general financial health.\n\n"
        f"Invoices:\n{json.dumps(records, indent=2)}\n\n"
        "OUTPUT MUST BE STRICT JSON ONLY — no markdown, no explanation:\n"
        '{"summary": string, "flags": [{"type": string, "invoice_id": string, "severity": "low"|"medium"|"high"}], "overall_risk_score": number between 0 and 1}'
    )

    text = _call_bedrock(prompt)
    result = _parse_bedrock_json(text, _summary_fallback)
    return make_response(200, result)


def handle_ai_dashboard(event: dict) -> dict:
    """POST /ai/dashboard — generate AI dashboard for a record."""
    body = json.loads(event.get("body", "{}")) if isinstance(event.get("body"), str) else event.get("body", {})
    record_id = body.get("record_id", "")

    try:
        records = get_records_by_id(record_id)
    except RuntimeError as e:
        return make_response(500, {"error": "DynamoDB read failed", "detail": str(e)})

    if not records:
        return make_response(404, {"error": "Record not found"})

    _dashboard_fallback = {
        "totals": {"total_invoices": 0, "total_amount": 0, "average_invoice": 0},
        "vendor_breakdown": [],
        "risk_indicators": {"high": 0, "medium": 0, "low": 0},
        "anomalies": ["AI analysis unavailable"],
    }

    prompt = (
        "You are a financial reporting engine.\n"
        "Analyze the following invoices and produce a UI-ready financial dashboard.\n\n"
        f"Invoices:\n{json.dumps(records, indent=2)}\n\n"
        "Return UI-ready JSON only — no markdown, no explanation:\n"
        '{"totals": {"total_invoices": number, "total_amount": number, "average_invoice": number}, '
        '"vendor_breakdown": [{"vendor": string, "total": number}], '
        '"risk_indicators": {"high": number, "medium": number, "low": number}, '
        '"anomalies": [string]}'
    )

    text = _call_bedrock(prompt)
    result = _parse_bedrock_json(text, _dashboard_fallback)
    return make_response(200, result)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context) -> dict:
    """AWS Lambda entry point. Routes API Gateway HTTP API events to the correct handler."""
    # Extract HTTP method — HTTP API v2 format first, fall back to v1
    http_method = (
        event.get("requestContext", {}).get("http", {}).get("method", "")
        or event.get("httpMethod", "")
    ).upper()

    # Extract path — HTTP API v2 uses rawPath, v1 uses path
    path = event.get("rawPath", event.get("path", "/"))

    # OPTIONS preflight — any path
    if http_method == "OPTIONS":
        return handle_preflight()

    # POST /upload
    if http_method == "POST" and path == "/upload":
        return handle_upload(event)

    # GET /records
    if http_method == "GET" and path == "/records":
        return handle_get_all_records()

    # GET /record/{record_id}
    if http_method == "GET" and path.startswith("/record/"):
        record_id = path[len("/record/"):]
        return handle_get_record(record_id)

    # POST /ai/summary
    if http_method == "POST" and path == "/ai/summary":
        return handle_ai_summary(event)

    # POST /ai/dashboard
    if http_method == "POST" and path == "/ai/dashboard":
        return handle_ai_dashboard(event)

    # Catch-all 404
    return make_response(404, {"error": "Not found"})
