"""
Financial Invoice Intelligence System
Single Lambda function implementing all backend logic.
"""

import base64
import json
import logging
import os
import re
import boto3
from datetime import datetime, timezone
from decimal import Decimal

logger = logging.getLogger("invoice_ai")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)


def _summarize_for_log(value, max_len: int = 400) -> str:
    """Convert values to a compact string suitable for log output."""
    if value is None:
        return "None"
    if isinstance(value, (str, int, float, bool)):
        text = str(value)
    else:
        text = json.dumps(value, default=str, ensure_ascii=False)
    if len(text) > max_len:
        return text[:max_len] + "...[truncated]"
    return text


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

BUCKET_NAME = os.getenv("BUCKET_NAME", "invoice-inbox")
RECORDS_TABLE = os.getenv("TABLE_NAME", "Records")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.amazon.nova-2-lite-v1:0")


def get_runtime_config() -> dict:
    """Return runtime configuration values used by the Lambda and CDK deployment."""
    config = {
        "bucket_name": BUCKET_NAME,
        "table_name": RECORDS_TABLE,
        "region": os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-west-2",
        "stage": os.getenv("STAGE", "dev"),
    }
    logger.info("Runtime config: %s", _summarize_for_log(config))
    return config


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
    logger.info("Textract start: bucket=%s key=%s", bucket, key)
    client = textract_client or _get_textract_client()
    try:
        response = client.detect_document_text(
            Document={"S3Object": {"Bucket": bucket, "Name": key}}
        )
        blocks = response.get("Blocks", [])
        lines = [
            block["Text"]
            for block in blocks
            if block.get("BlockType") == "LINE"
        ]
        extracted_text = "\n".join(lines)
        logger.info(
            "Textract completed: block_count=%s line_count=%s text_length=%s text_preview=%s",
            len(blocks),
            len(lines),
            len(extracted_text),
            _summarize_for_log(extracted_text, 400),
        )
        return extracted_text
    except Exception as e:
        logger.exception("Textract failed for bucket=%s key=%s", bucket, key)
        raise RuntimeError(str(e))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _get_bedrock_model_id() -> str:
    """Return the Bedrock model ID to use, allowing override from the environment."""
    return os.getenv("BEDROCK_MODEL_ID", BEDROCK_MODEL_ID)


def _build_bedrock_request(prompt: str, max_tokens: int, model_id: str) -> dict:
    """Build the Converse API request payload for the selected Bedrock model."""
    return {
        "modelId": model_id,
        "messages": [
            {
                "role": "user",
                "content": [{"text": prompt}],
            }
        ],
        "inferenceConfig": {
            "maxTokens": max_tokens,
            "temperature": 0,
        },
    }


def _extract_bedrock_text(response: dict, model_id: str) -> str:
    """Extract text from a Bedrock Converse API response."""
    if not isinstance(response, dict):
        return ""

    output = response.get("output", {})
    message = output.get("message", {})
    content = message.get("content", [])
    if content:
        first_block = content[0]
        if isinstance(first_block, dict):
            return first_block.get("text", "")
    return ""


def parse_invoice(raw_text: str) -> dict:
    """
    Call Amazon Bedrock to parse raw invoice text into a structured JSON dict.

    Returns a dict with keys: invoice_id, vendor, amount, vat_amount, total.
    Falls back to UNKNOWN/0 values if the model response cannot be parsed.
    """
    logger.info("Bedrock parse start: text_length=%s text_preview=%s", len(raw_text), _summarize_for_log(raw_text, 300))
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

    model_id = _get_bedrock_model_id()
    request = _build_bedrock_request(prompt, 1024, model_id)

    _fallback = {
        "invoice_id": "UNKNOWN",
        "vendor": "UNKNOWN",
        "amount": 0,
        "vat_amount": 0,
        "total": 0,
    }

    # Use module-level name when patched by tests; fall back to lazy singleton
    client = bedrock_client or _get_bedrock_client()
    logger.info("Bedrock parse request: model=%s prompt_length=%s", model_id, len(prompt))

    try:
        response = client.converse(
            modelId=request["modelId"],
            messages=request["messages"],
            inferenceConfig=request["inferenceConfig"],
        )
        text = _extract_bedrock_text(response, model_id)
        logger.info("Bedrock parse response received: response_preview=%s", _summarize_for_log(text, 600))
    except Exception as exc:
        logger.exception("Bedrock parse invocation failed")
        logger.warning("Bedrock parse fallback applied: reason=%s", str(exc))
        return _fallback

    # First attempt: direct JSON parse
    try:
        parsed = json.loads(text)
        logger.info("Bedrock parse direct JSON success: %s", _summarize_for_log(parsed))
        return parsed
    except json.JSONDecodeError:
        logger.info("Bedrock parse direct JSON failed; trying regex extraction")
        pass

    # Second attempt: extract JSON with regex
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            logger.info("Bedrock parse regex JSON success: %s", _summarize_for_log(parsed))
            return parsed
        except json.JSONDecodeError:
            logger.warning("Bedrock parse regex JSON failed; using fallback")
            pass

    logger.warning("Bedrock parse fallback applied: raw_text=%s", _summarize_for_log(text, 600))
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
    logger.info("DynamoDB write start: record_id=%s invoice_id=%s status=%s", record_id, invoice_id, status)
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
        logger.info("DynamoDB write succeeded: record_id=%s processed_at=%s", record_id, processed_at)
        return processed_at
    except Exception as e:
        logger.exception("DynamoDB write failed: record_id=%s", record_id)
        raise RuntimeError(str(e))


def get_all_records() -> dict:
    """
    Scan the DynamoDB Records table and return all items grouped by record_id.

    Returns a dict of {record_id: [Invoice_Record, ...]}.
    Raises RuntimeError on DynamoDB failure.
    """
    logger.info("DynamoDB scan start")
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
        logger.info("DynamoDB scan succeeded: item_count=%s", len(items))
        return grouped
    except Exception as e:
        logger.exception("DynamoDB scan failed")
        raise RuntimeError(str(e))


def get_records_by_id(record_id: str) -> list:
    """
    Query the DynamoDB Records table for all items with the given record_id.

    Returns a list of Invoice_Record dicts (empty list if none found).
    Raises RuntimeError on DynamoDB failure.
    """
    from boto3.dynamodb.conditions import Key
    logger.info("DynamoDB query start: record_id=%s", record_id)
    ddb = dynamodb_resource or _get_dynamodb_resource()
    try:
        table = ddb.Table(RECORDS_TABLE)
        response = table.query(
            KeyConditionExpression=Key("record_id").eq(record_id)
        )
        items = response.get("Items", [])
        logger.info("DynamoDB query succeeded: record_id=%s item_count=%s", record_id, len(items))
        return [_decimal_to_float(item) for item in items]
    except Exception as e:
        logger.exception("DynamoDB query failed: record_id=%s", record_id)
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
    logger.info("Validation start: data=%s", _summarize_for_log(data))
    # Step 1: Check all 5 required fields are present
    missing = [f for f in _REQUIRED_FIELDS if f not in data]
    if missing:
        errors = [f"MISSING_FIELD: {f}" for f in missing]
        logger.warning("Validation failed with INVALID_STRUCTURE: errors=%s", errors)
        return ("INVALID_STRUCTURE", errors)

    # Step 2: Check amount, vat_amount, total are numeric (int or float, not bool)
    # Note: bool is a subclass of int in Python — treat booleans as non-numeric
    non_numeric = [
        f for f in _NUMERIC_FIELDS
        if isinstance(data[f], bool) or not isinstance(data[f], (int, float))
    ]
    if non_numeric:
        errors = [f"NON_NUMERIC: {f}" for f in non_numeric]
        logger.warning("Validation failed with INVALID_TYPES: errors=%s", errors)
        return ("INVALID_TYPES", errors)

    amount = data["amount"]
    vat_amount = data["vat_amount"]
    total = data["total"]

    # Step 3: Check all numeric fields are >= 0
    negative = [f for f in _NUMERIC_FIELDS if data[f] < 0]
    if negative:
        errors = [f"NEGATIVE_VALUE: {f}" for f in negative]
        logger.warning("Validation failed with INVALID_VALUES: errors=%s", errors)
        return ("INVALID_VALUES", errors)

    # Step 4: Check amount + vat_amount ≈ total (tolerance ±0.01)
    # Use round() for decimal-safe comparison to avoid floating-point false positives
    # (Requirement 4.7: epsilon of 0.01; round to 10 places to strip FP noise)
    computed = round(amount + vat_amount, 2)
    if round(abs(amount + vat_amount - total), 10) > 0.01:
        logger.warning("Validation failed with MISMATCH: expected=%s got=%s", computed, total)
        return ("MISMATCH", [f"TOTAL_MISMATCH expected {computed} got {total}"])

    # Step 5: All checks pass
    logger.info("Validation succeeded: data=%s", _summarize_for_log(data))
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
    logger.info("Upload request received: event=%s", _summarize_for_log(event, 1200))
    # 1. Parse body
    raw_body = event.get("body", "{}")
    if isinstance(raw_body, dict):
        body = raw_body
    else:
        body = json.loads(raw_body)

    # 2. Extract record_id and file_b64
    record_id = body.get("record_id", "")
    file_b64 = body.get("file", "")
    logger.info("Upload payload parsed: record_id=%s file_length=%s", record_id, len(file_b64))

    # 3. If isBase64Encoded is True the raw body itself is base64; decode that first
    if event.get("isBase64Encoded", False):
        raw_bytes = base64.b64decode(event.get("body", ""))
        decoded_body = json.loads(raw_bytes)
        record_id = decoded_body.get("record_id", record_id)
        file_b64 = decoded_body.get("file", file_b64)

    # 4. Decode the base64 file bytes
    file_bytes = base64.b64decode(file_b64)
    logger.info("Upload file decoded: bytes=%s", len(file_bytes))

    # 5. Generate S3 key (no colons — use compact timestamp format)
    key = f"{record_id}/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.pdf"
    logger.info("Upload S3 key prepared: key=%s", key)

    # 6. Upload to S3
    try:
        (s3_client or _get_s3_client()).put_object(
            Bucket=BUCKET_NAME, Key=key, Body=file_bytes
        )
        logger.info("S3 upload succeeded: bucket=%s key=%s", BUCKET_NAME, key)
    except Exception as e:
        logger.exception("S3 upload failed: bucket=%s key=%s", BUCKET_NAME, key)
        return make_response(500, {"error": "S3 upload failed", "detail": str(e)})

    # 7. Extract text via Textract
    try:
        raw_text = extract_text(BUCKET_NAME, key)
    except RuntimeError as e:
        logger.exception("Textract pipeline step failed: bucket=%s key=%s", BUCKET_NAME, key)
        return make_response(500, {"error": "Textract extraction failed", "detail": str(e)})

    # 8. Parse invoice via Bedrock (always returns a dict — no exception)
    structured_invoice = parse_invoice(raw_text)
    logger.info("Parsed invoice payload: %s", _summarize_for_log(structured_invoice))

    # 9. Validate
    status, validation_errors = validate_invoice(structured_invoice)
    logger.info("Upload validation result: status=%s errors=%s", status, validation_errors)

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
        logger.info("DynamoDB write succeeded: record_id=%s processed_at=%s", record_id, processed_at)
    except RuntimeError as e:
        logger.exception("DynamoDB write failed: record_id=%s", record_id)
        return make_response(500, {"error": "DynamoDB write failed", "detail": str(e)})

    # 11. Return success
    logger.info("Upload pipeline complete: record_id=%s status=%s", record_id, status)
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
    """Call the configured Bedrock model and return the response text or None on failure."""
    logger.info("Bedrock AI request start: prompt_length=%s", len(prompt))
    model_id = _get_bedrock_model_id()
    request = _build_bedrock_request(prompt, 2048, model_id)
    try:
        client = bedrock_client or _get_bedrock_client()
        response = client.converse(
            modelId=request["modelId"],
            messages=request["messages"],
            inferenceConfig=request["inferenceConfig"],
        )
        text = _extract_bedrock_text(response, model_id)
        logger.info("Bedrock AI response received: %s", _summarize_for_log(text, 800))
        return text
    except Exception as exc:
        logger.exception("Bedrock AI request failed")
        logger.warning("Bedrock AI request fallback triggered: %s", str(exc))
        return None


def _parse_bedrock_json(text: str | None, fallback: dict) -> dict:
    """Parse Bedrock text response as JSON; use regex fallback; return fallback dict on failure."""
    if text is None:
        logger.warning("Bedrock JSON parse skipped because response text was None; using fallback")
        return fallback
    try:
        parsed = json.loads(text)
        logger.info("Bedrock JSON parse succeeded: %s", _summarize_for_log(parsed))
        return parsed
    except json.JSONDecodeError:
        logger.info("Bedrock JSON parse direct decode failed; trying regex")
        pass
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            logger.info("Bedrock JSON parse regex succeeded: %s", _summarize_for_log(parsed))
            return parsed
        except json.JSONDecodeError:
            logger.warning("Bedrock JSON parse regex failed; using fallback")
            pass
    logger.warning("Bedrock JSON parse fallback used: response=%s fallback=%s", _summarize_for_log(text, 800), _summarize_for_log(fallback))
    return fallback


def handle_ai_summary(event: dict) -> dict:
    """POST /ai/summary — generate AI summary for a record."""
    logger.info("AI summary request received: event=%s", _summarize_for_log(event, 1200))
    body = json.loads(event.get("body", "{}")) if isinstance(event.get("body"), str) else event.get("body", {})
    record_id = body.get("record_id", "")
    logger.info("AI summary request for record_id=%s", record_id)

    try:
        records = get_records_by_id(record_id)
        logger.info("AI summary loaded %s record(s) from DynamoDB", len(records))
    except RuntimeError as e:
        logger.exception("AI summary DynamoDB read failed for record_id=%s", record_id)
        return make_response(500, {"error": "DynamoDB read failed", "detail": str(e)})

    if not records:
        logger.warning("AI summary record not found: record_id=%s", record_id)
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
    logger.info("AI summary response: %s", _summarize_for_log(result))
    return make_response(200, result)


def handle_ai_dashboard(event: dict) -> dict:
    """POST /ai/dashboard — generate AI dashboard for a record."""
    logger.info("AI dashboard request received: event=%s", _summarize_for_log(event, 1200))
    body = json.loads(event.get("body", "{}")) if isinstance(event.get("body"), str) else event.get("body", {})
    record_id = body.get("record_id", "")
    logger.info("AI dashboard request for record_id=%s", record_id)

    try:
        records = get_records_by_id(record_id)
        logger.info("AI dashboard loaded %s record(s) from DynamoDB", len(records))
    except RuntimeError as e:
        logger.exception("AI dashboard DynamoDB read failed for record_id=%s", record_id)
        return make_response(500, {"error": "DynamoDB read failed", "detail": str(e)})

    if not records:
        logger.warning("AI dashboard record not found: record_id=%s", record_id)
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
    logger.info("AI dashboard response: %s", _summarize_for_log(result))
    return make_response(200, result)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context) -> dict:
    """AWS Lambda entry point. Routes API Gateway HTTP API events to the correct handler."""
    logger.info("Lambda invoked: method=%s path=%s event=%s", (
        event.get("requestContext", {}).get("http", {}).get("method", "")
        or event.get("httpMethod", "")
    ), event.get("rawPath", event.get("path", "/")), _summarize_for_log(event, 1600))
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
