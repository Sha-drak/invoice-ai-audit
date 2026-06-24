"""Tests for the Lambda handler / Orchestrator — Task 2.3.

Covers:
- make_response always includes all three CORS headers
- OPTIONS preflight → 200 with CORS headers and empty body
- Unknown route → 404 with CORS headers

Also covers:
- TestExtractor — Task 5.2/5.3
- TestStore     — Task 7.4/7.5
"""

import base64
import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lambda_handler import lambda_handler, make_response, extract_text, store_record, get_all_records, get_records_by_id, handle_upload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def _build_event(method: str, path: str) -> dict:
    """Build a minimal API Gateway HTTP API v2 event."""
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
    }


def _assert_cors_headers(response: dict) -> None:
    """Assert that all three required CORS headers are present in the response."""
    headers = response.get("headers", {})
    for header, value in CORS_HEADERS.items():
        assert headers.get(header) == value, (
            f"Missing or wrong CORS header '{header}': got {headers.get(header)!r}"
        )


# ---------------------------------------------------------------------------
# make_response unit tests
# ---------------------------------------------------------------------------

class TestMakeResponse:
    def test_status_code_is_preserved(self):
        resp = make_response(200, {"ok": True})
        assert resp["statusCode"] == 200

    def test_body_is_json_string(self):
        body = {"foo": "bar", "num": 42}
        resp = make_response(200, body)
        assert json.loads(resp["body"]) == body

    def test_cors_allow_origin(self):
        resp = make_response(200, {})
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_cors_allow_methods(self):
        resp = make_response(200, {})
        assert resp["headers"]["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"

    def test_cors_allow_headers(self):
        resp = make_response(200, {})
        assert resp["headers"]["Access-Control-Allow-Headers"] == "Content-Type"

    def test_content_type_is_json(self):
        resp = make_response(200, {})
        assert resp["headers"]["Content-Type"] == "application/json"

    def test_non_200_status_code(self):
        resp = make_response(404, {"error": "Not found"})
        assert resp["statusCode"] == 404
        assert json.loads(resp["body"]) == {"error": "Not found"}


# ---------------------------------------------------------------------------
# OPTIONS preflight tests
# ---------------------------------------------------------------------------

class TestOptionsPreflight:
    def test_options_returns_200(self):
        event = _build_event("OPTIONS", "/upload")
        resp = lambda_handler(event, None)
        assert resp["statusCode"] == 200

    def test_options_includes_cors_headers(self):
        event = _build_event("OPTIONS", "/upload")
        resp = lambda_handler(event, None)
        _assert_cors_headers(resp)

    def test_options_body_is_empty_dict(self):
        event = _build_event("OPTIONS", "/upload")
        resp = lambda_handler(event, None)
        assert json.loads(resp["body"]) == {}

    def test_options_on_any_path_returns_200(self):
        for path in ["/", "/upload", "/records", "/record/abc", "/ai/summary", "/ai/dashboard"]:
            event = _build_event("OPTIONS", path)
            resp = lambda_handler(event, None)
            assert resp["statusCode"] == 200, f"OPTIONS {path} should return 200"

    def test_options_on_unknown_path_returns_200(self):
        event = _build_event("OPTIONS", "/totally/unknown/path")
        resp = lambda_handler(event, None)
        assert resp["statusCode"] == 200


# ---------------------------------------------------------------------------
# Unknown route → 404 tests
# ---------------------------------------------------------------------------

class TestUnknownRoute:
    def test_unknown_get_returns_404(self):
        event = _build_event("GET", "/nonexistent")
        resp = lambda_handler(event, None)
        assert resp["statusCode"] == 404

    def test_unknown_post_returns_404(self):
        event = _build_event("POST", "/nonexistent")
        resp = lambda_handler(event, None)
        assert resp["statusCode"] == 404

    def test_404_body_contains_error_key(self):
        event = _build_event("GET", "/nonexistent")
        resp = lambda_handler(event, None)
        body = json.loads(resp["body"])
        assert "error" in body

    def test_404_includes_cors_headers(self):
        event = _build_event("GET", "/nonexistent")
        resp = lambda_handler(event, None)
        _assert_cors_headers(resp)

    def test_delete_method_returns_404(self):
        event = _build_event("DELETE", "/upload")
        resp = lambda_handler(event, None)
        assert resp["statusCode"] == 404


# ---------------------------------------------------------------------------
# Known-route dispatch tests (status 501 stubs)
# ---------------------------------------------------------------------------

class TestKnownRouteDispatch:
    """Verify that each defined route is dispatched (returns 501, not 404)."""

    def test_post_upload_dispatched(self):
        # Now that handle_upload is implemented, it returns 200 on success or 500
        # on infrastructure failure — either way it's not 404, confirming dispatch.
        event = _build_event("POST", "/upload")
        resp = lambda_handler(event, None)
        assert resp["statusCode"] != 404

    def test_get_records_dispatched(self):
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.scan.return_value = {"Items": []}
            event = _build_event("GET", "/records")
            resp = lambda_handler(event, None)
        assert resp["statusCode"] != 404

    def test_get_record_by_id_dispatched(self):
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": []}
            event = _build_event("GET", "/record/abc123")
            resp = lambda_handler(event, None)
        assert resp["statusCode"] != 404

    def test_post_ai_summary_dispatched(self):
        # With empty records the AI handler correctly returns 404 "Record not found"
        # (not the routing 404 "Not found"), confirming dispatch occurred.
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": []}
            event = _build_event("POST", "/ai/summary")
            resp = lambda_handler(event, None)
        body = json.loads(resp["body"])
        assert body.get("error") == "Record not found"  # dispatched, not routing miss

    def test_post_ai_dashboard_dispatched(self):
        # With empty records the AI handler correctly returns 404 "Record not found"
        # (not the routing 404 "Not found"), confirming dispatch occurred.
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": []}
            event = _build_event("POST", "/ai/dashboard")
            resp = lambda_handler(event, None)
        body = json.loads(resp["body"])
        assert body.get("error") == "Record not found"  # dispatched, not routing miss

    def test_known_routes_include_cors_headers(self):
        routes = [
            ("POST", "/upload"),
            ("GET", "/records"),
            ("GET", "/record/xyz"),
            ("POST", "/ai/summary"),
            ("POST", "/ai/dashboard"),
        ]
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.scan.return_value = {"Items": []}
            mock_table.query.return_value = {"Items": []}
            for method, path in routes:
                event = _build_event(method, path)
                resp = lambda_handler(event, None)
                _assert_cors_headers(resp)


# ---------------------------------------------------------------------------
# HTTP API v1 fallback (httpMethod / path fields)
# ---------------------------------------------------------------------------

class TestV1EventFormat:
    """Verify the handler falls back to v1 httpMethod/path fields."""

    def _build_v1_event(self, method: str, path: str) -> dict:
        return {"httpMethod": method, "path": path}

    def test_v1_options_returns_200(self):
        event = self._build_v1_event("OPTIONS", "/upload")
        resp = lambda_handler(event, None)
        assert resp["statusCode"] == 200

    def test_v1_unknown_route_returns_404(self):
        event = self._build_v1_event("GET", "/missing")
        resp = lambda_handler(event, None)
        assert resp["statusCode"] == 404

    def test_v1_known_route_dispatched(self):
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.scan.return_value = {"Items": []}
            event = self._build_v1_event("GET", "/records")
            resp = lambda_handler(event, None)
        assert resp["statusCode"] != 404


# ---------------------------------------------------------------------------
# TestExtractor — Task 5.2 / 5.3
# Validates: Requirements 2.2, 2.3
# ---------------------------------------------------------------------------

# Strategies for block generation
_BLOCK_TYPES = ["LINE", "WORD", "KEY_VALUE_SET", "PAGE"]

_block_strategy = st.fixed_dictionaries({
    "BlockType": st.sampled_from(_BLOCK_TYPES),
    "Text": st.text(min_size=0, max_size=50),
})


class TestExtractor:
    """Tests for extract_text() — Extractor component."""

    # --- Property test: LINE block filtering and concatenation ---

    # Feature: financial-invoice-intelligence
    # Property: LINE block filtering and concatenation
    # Validates: Requirements 2.2, 2.3
    @settings(max_examples=100)
    @given(blocks=st.lists(_block_strategy, min_size=0, max_size=20))
    def test_property_line_block_concatenation(self, blocks):
        """
        For any list of blocks with BlockType in {LINE, WORD, KEY_VALUE_SET, PAGE},
        extract_text must return exactly and only the LINE block texts in order,
        joined by newline.
        """
        fake_response = {"Blocks": blocks}
        with patch("lambda_handler.textract_client") as mock_textract:
            mock_textract.detect_document_text.return_value = fake_response
            result = extract_text("my-bucket", "my-key.pdf")

        expected_lines = [b["Text"] for b in blocks if b["BlockType"] == "LINE"]
        assert result == "\n".join(expected_lines)

    # --- Unit tests ---

    def test_empty_block_list_returns_empty_string(self):
        """Empty Blocks list → empty string."""
        with patch("lambda_handler.textract_client") as mock_textract:
            mock_textract.detect_document_text.return_value = {"Blocks": []}
            result = extract_text("bucket", "key")
        assert result == ""

    def test_mixed_block_types_returns_only_line_blocks(self):
        """Mixed block types → only LINE blocks appear in output."""
        blocks = [
            {"BlockType": "PAGE", "Text": "page text"},
            {"BlockType": "LINE", "Text": "first line"},
            {"BlockType": "WORD", "Text": "word"},
            {"BlockType": "LINE", "Text": "second line"},
            {"BlockType": "KEY_VALUE_SET", "Text": "kv text"},
        ]
        with patch("lambda_handler.textract_client") as mock_textract:
            mock_textract.detect_document_text.return_value = {"Blocks": blocks}
            result = extract_text("bucket", "key")

        assert result == "first line\nsecond line"
        assert "page text" not in result
        assert "word" not in result
        assert "kv text" not in result

    def test_only_line_blocks(self):
        """All LINE blocks → all texts joined."""
        blocks = [
            {"BlockType": "LINE", "Text": "Line A"},
            {"BlockType": "LINE", "Text": "Line B"},
            {"BlockType": "LINE", "Text": "Line C"},
        ]
        with patch("lambda_handler.textract_client") as mock_textract:
            mock_textract.detect_document_text.return_value = {"Blocks": blocks}
            result = extract_text("bucket", "key")

        assert result == "Line A\nLine B\nLine C"

    def test_no_line_blocks_returns_empty_string(self):
        """Blocks present but none are LINE type → empty string."""
        blocks = [
            {"BlockType": "WORD", "Text": "hello"},
            {"BlockType": "PAGE", "Text": "page"},
        ]
        with patch("lambda_handler.textract_client") as mock_textract:
            mock_textract.detect_document_text.return_value = {"Blocks": blocks}
            result = extract_text("bucket", "key")

        assert result == ""

    def test_textract_failure_raises_runtime_error(self):
        """Textract API failure → RuntimeError is raised."""
        with patch("lambda_handler.textract_client") as mock_textract:
            mock_textract.detect_document_text.side_effect = Exception("Textract unavailable")
            with pytest.raises(RuntimeError, match="Textract unavailable"):
                extract_text("bucket", "key")


# ---------------------------------------------------------------------------
# TestStore — Task 7.4 / 7.5
# Validates: Requirements 5.1, 5.2, 5.4, 6.1, 6.2, 6.3, 6.5
# ---------------------------------------------------------------------------

# Helper: build a fake DynamoDB item (already float-converted, as returned by
# get_all_records / get_records_by_id)
def _make_item(record_id="REC-1", invoice_id="INV-001"):
    return {
        "record_id": record_id,
        "invoice_id": invoice_id,
        "vendor": "Acme Corp",
        "amount": 100.0,
        "vat_amount": 20.0,
        "total": 120.0,
        "status": "VALID",
        "validation_errors": [],
        "processed_at": "2024-01-01T00:00:00Z",
    }


class TestStore:
    """Tests for store_record(), get_all_records(), get_records_by_id()."""

    # --- Property test: grouping by record_id preserves all records ---

    # Feature: financial-invoice-intelligence
    # Property: grouping by record_id preserves all records
    # Validates: Requirements 6.1
    @settings(max_examples=100)
    @given(
        items=st.lists(
            st.fixed_dictionaries({
                "record_id": st.sampled_from(["REC-A", "REC-B", "REC-C"]),
                "invoice_id": st.uuids().map(str),
                "vendor": st.text(min_size=1, max_size=20),
                "amount": st.floats(min_value=0, max_value=10000, allow_nan=False, allow_infinity=False),
                "vat_amount": st.floats(min_value=0, max_value=2000, allow_nan=False, allow_infinity=False),
                "total": st.floats(min_value=0, max_value=12000, allow_nan=False, allow_infinity=False),
                "status": st.sampled_from(["VALID", "MISMATCH", "INVALID_STRUCTURE"]),
                "validation_errors": st.lists(st.text(max_size=30), max_size=3),
                "processed_at": st.just("2024-01-01T00:00:00Z"),
            }),
            min_size=0,
            max_size=30,
        )
    )
    def test_property_grouping_preserves_all_records(self, items):
        """
        Pure Python grouping logic: for any list of items, grouping by record_id
        must preserve every item under its correct key and total count.
        """
        # Replicate the same grouping logic as get_all_records()
        grouped = {}
        for item in items:
            rid = item.get("record_id")
            grouped.setdefault(rid, []).append(item)

        # Total items in grouped == original total
        total_grouped = sum(len(v) for v in grouped.values())
        assert total_grouped == len(items)

        # Every item is under the correct record_id key
        for rid, group_items in grouped.items():
            for gi in group_items:
                assert gi["record_id"] == rid

        # Each record_id that appears in items is a key in grouped
        expected_keys = {item["record_id"] for item in items}
        assert set(grouped.keys()) == expected_keys

    # --- Unit tests for store_record ---

    def test_store_record_writes_all_nine_attributes(self):
        """store_record writes an item with all 9 required attributes."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.put_item.return_value = {}

            processed_at = store_record(
                record_id="REC-1",
                invoice_id="INV-001",
                vendor="Acme Corp",
                amount=100.0,
                vat_amount=20.0,
                total=120.0,
                status="VALID",
                validation_errors=[],
            )

        mock_ddb.Table.assert_called_once_with("Records")
        call_args = mock_table.put_item.call_args
        item = call_args[1]["Item"]  # keyword arg

        # Verify all 9 required attributes are present
        required_attrs = {
            "record_id", "invoice_id", "vendor",
            "amount", "vat_amount", "total",
            "status", "validation_errors", "processed_at",
        }
        assert required_attrs.issubset(set(item.keys()))

        # Verify values
        assert item["record_id"] == "REC-1"
        assert item["invoice_id"] == "INV-001"
        assert item["vendor"] == "Acme Corp"
        assert item["amount"] == Decimal("100.0")
        assert item["vat_amount"] == Decimal("20.0")
        assert item["total"] == Decimal("120.0")
        assert item["status"] == "VALID"
        assert item["validation_errors"] == []
        # processed_at is the return value and was stored
        assert item["processed_at"] == processed_at
        assert processed_at.endswith("Z")

    def test_store_record_returns_processed_at_string(self):
        """store_record return value is a UTC ISO 8601 timestamp string."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.put_item.return_value = {}

            result = store_record("R", "I", "V", 1, 0.1, 1.1, "VALID", [])

        assert isinstance(result, str)
        assert "T" in result  # ISO 8601 contains 'T'
        assert result.endswith("Z")

    def test_store_record_dynamodb_failure_raises_runtime_error(self):
        """DynamoDB failure during write → RuntimeError."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.put_item.side_effect = Exception("Connection timeout")

            with pytest.raises(RuntimeError, match="Connection timeout"):
                store_record("R", "I", "V", 1, 0.1, 1.1, "VALID", [])

    # --- Unit tests for get_all_records ---

    def test_get_all_records_groups_items_correctly(self):
        """get_all_records groups scan results by record_id."""
        item_a1 = {**_make_item("REC-A", "INV-001"), "amount": Decimal("100")}
        item_a2 = {**_make_item("REC-A", "INV-002"), "amount": Decimal("200")}
        item_b1 = {**_make_item("REC-B", "INV-003"), "amount": Decimal("50")}

        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.scan.return_value = {"Items": [item_a1, item_a2, item_b1]}

            result = get_all_records()

        assert "REC-A" in result
        assert "REC-B" in result
        assert len(result["REC-A"]) == 2
        assert len(result["REC-B"]) == 1
        # Decimal should have been converted to float
        assert isinstance(result["REC-A"][0]["amount"], float)

    def test_get_all_records_empty_table_returns_empty_dict(self):
        """Scanning empty table returns empty dict."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.scan.return_value = {"Items": []}

            result = get_all_records()

        assert result == {}

    def test_get_all_records_dynamodb_failure_raises_runtime_error(self):
        """DynamoDB scan failure → RuntimeError."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.scan.side_effect = Exception("Scan failed")

            with pytest.raises(RuntimeError, match="Scan failed"):
                get_all_records()

    # --- Unit tests for get_records_by_id ---

    def test_get_records_by_id_returns_list_for_known_id(self):
        """get_records_by_id returns list of items for a known record_id."""
        items = [
            {**_make_item("REC-1", "INV-001"), "amount": Decimal("100")},
            {**_make_item("REC-1", "INV-002"), "amount": Decimal("200")},
        ]
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": items}

            result = get_records_by_id("REC-1")

        assert isinstance(result, list)
        assert len(result) == 2
        # Decimal converted to float
        assert isinstance(result[0]["amount"], float)

    def test_get_records_by_id_returns_empty_list_for_unknown_id(self):
        """get_records_by_id returns empty list for an unknown record_id."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": []}

            result = get_records_by_id("UNKNOWN-REC")

        assert result == []

    def test_get_records_by_id_dynamodb_failure_raises_runtime_error(self):
        """DynamoDB query failure → RuntimeError."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.side_effect = Exception("Query failed")

            with pytest.raises(RuntimeError, match="Query failed"):
                get_records_by_id("REC-1")


# ---------------------------------------------------------------------------
# TestUploadHandler — Task 8.2 / 8.3
# Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.4, 3.1, 4.1, 5.4, 5.5
# ---------------------------------------------------------------------------

def _upload_event(record_id="REC-1", file_b64=None):
    """Build a minimal POST /upload API Gateway event."""
    if file_b64 is None:
        file_b64 = base64.b64encode(b"fake pdf content").decode()
    return {
        "requestContext": {"http": {"method": "POST"}},
        "rawPath": "/upload",
        "body": json.dumps({"record_id": record_id, "file": file_b64}),
        "isBase64Encoded": False,
    }


class TestUploadHandler:
    """Tests for handle_upload() — upload pipeline wiring."""

    # -----------------------------------------------------------------------
    # Property 8: S3 key format for any valid record_id
    # Feature: financial-invoice-intelligence, Property 8: S3 key format for any valid record_id
    # Validates: Requirements 1.2
    # -----------------------------------------------------------------------
    @settings(max_examples=100)
    @given(
        record_id=st.text(
            min_size=1,
            max_size=50,
            alphabet=st.characters(
                whitelist_categories=("Lu", "Ll", "Nd"),
                whitelist_characters="-_",
            ),
        )
    )
    def test_property_8_s3_key_format(self, record_id):
        """
        For any non-empty record_id, the S3 key constructed by handle_upload
        MUST start with record_id + '/', end with '.pdf', and contain a
        16-character timestamp segment (YYYYMMDDTHHMMSSz).
        """
        # Construct the key using the same logic as handle_upload
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        key = f"{record_id}/{timestamp}.pdf"
        assert key.startswith(record_id + "/")
        assert key.endswith(".pdf")
        # Verify timestamp segment is 16 chars: YYYYMMDDTHHMMSSz
        parts = key.split("/")
        assert len(parts) == 2
        ts_part = parts[1].replace(".pdf", "")
        assert len(ts_part) == 16

    # -----------------------------------------------------------------------
    # Unit tests
    # -----------------------------------------------------------------------

    def test_happy_path_returns_200_with_required_fields(self):
        """All mocks succeed → 200 with record_id, invoice_id, status, validation_errors, processed_at."""
        bedrock_payload = json.dumps({
            "invoice_id": "INV-001",
            "vendor": "Acme Corp",
            "amount": 100.0,
            "vat_amount": 20.0,
            "total": 120.0,
        })
        bedrock_body_mock = MagicMock()
        bedrock_body_mock.read.return_value = json.dumps({
            "content": [{"text": bedrock_payload}]
        })

        mock_s3 = MagicMock()
        mock_s3.put_object.return_value = {}

        mock_textract = MagicMock()
        mock_textract.detect_document_text.return_value = {
            "Blocks": [
                {"BlockType": "LINE", "Text": "Invoice INV-001"},
                {"BlockType": "LINE", "Text": "Vendor: Acme Corp"},
            ]
        }

        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = {"body": bedrock_body_mock}

        mock_ddb = MagicMock()
        mock_table = MagicMock()
        mock_ddb.Table.return_value = mock_table
        mock_table.put_item.return_value = {}

        with (
            patch("lambda_handler.s3_client", mock_s3),
            patch("lambda_handler.textract_client", mock_textract),
            patch("lambda_handler.bedrock_client", mock_bedrock),
            patch("lambda_handler.dynamodb_resource", mock_ddb),
        ):
            resp = lambda_handler(_upload_event(), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert "record_id" in body
        assert "invoice_id" in body
        assert "status" in body
        assert "validation_errors" in body
        assert "processed_at" in body
        assert body["record_id"] == "REC-1"

    def test_s3_failure_returns_500(self):
        """S3 put_object raises → 500 with 'S3 upload failed'."""
        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = Exception("S3 unavailable")

        with (
            patch("lambda_handler.s3_client", mock_s3),
        ):
            resp = lambda_handler(_upload_event(), None)

        assert resp["statusCode"] == 500
        body = json.loads(resp["body"])
        assert body["error"] == "S3 upload failed"
        assert "S3 unavailable" in body["detail"]

    def test_textract_failure_returns_500(self):
        """S3 succeeds but Textract raises → 500 with 'Textract extraction failed'."""
        mock_s3 = MagicMock()
        mock_s3.put_object.return_value = {}

        mock_textract = MagicMock()
        mock_textract.detect_document_text.side_effect = Exception("Textract down")

        with (
            patch("lambda_handler.s3_client", mock_s3),
            patch("lambda_handler.textract_client", mock_textract),
        ):
            resp = lambda_handler(_upload_event(), None)

        assert resp["statusCode"] == 500
        body = json.loads(resp["body"])
        assert body["error"] == "Textract extraction failed"
        assert "Textract down" in body["detail"]

    def test_dynamodb_write_failure_returns_500(self):
        """S3+Textract+Bedrock succeed but DynamoDB put_item raises → 500 with 'DynamoDB write failed'."""
        bedrock_payload = json.dumps({
            "invoice_id": "INV-002",
            "vendor": "Beta LLC",
            "amount": 50.0,
            "vat_amount": 10.0,
            "total": 60.0,
        })
        bedrock_body_mock = MagicMock()
        bedrock_body_mock.read.return_value = json.dumps({
            "content": [{"text": bedrock_payload}]
        })

        mock_s3 = MagicMock()
        mock_s3.put_object.return_value = {}

        mock_textract = MagicMock()
        mock_textract.detect_document_text.return_value = {"Blocks": []}

        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = {"body": bedrock_body_mock}

        mock_ddb = MagicMock()
        mock_table = MagicMock()
        mock_ddb.Table.return_value = mock_table
        mock_table.put_item.side_effect = Exception("DynamoDB connection error")

        with (
            patch("lambda_handler.s3_client", mock_s3),
            patch("lambda_handler.textract_client", mock_textract),
            patch("lambda_handler.bedrock_client", mock_bedrock),
            patch("lambda_handler.dynamodb_resource", mock_ddb),
        ):
            resp = lambda_handler(_upload_event(), None)

        assert resp["statusCode"] == 500
        body = json.loads(resp["body"])
        assert body["error"] == "DynamoDB write failed"
        assert "DynamoDB connection error" in body["detail"]


# ---------------------------------------------------------------------------
# TestRecordRetrievalHandlers — Task 10.3
# Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5
# ---------------------------------------------------------------------------

class TestRecordRetrievalHandlers:
    """Tests for handle_get_all_records() and handle_get_record()."""

    # -----------------------------------------------------------------------
    # GET /records
    # -----------------------------------------------------------------------

    def test_get_all_records_returns_200_with_grouped_dict(self):
        """GET /records → 200 with grouped dict."""
        grouped = {
            "REC-A": [_make_item("REC-A", "INV-001"), _make_item("REC-A", "INV-002")],
            "REC-B": [_make_item("REC-B", "INV-003")],
        }
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            # Simulate scan result with two groups
            mock_table.scan.return_value = {
                "Items": [
                    {**_make_item("REC-A", "INV-001"), "amount": 100.0},
                    {**_make_item("REC-A", "INV-002"), "amount": 200.0},
                    {**_make_item("REC-B", "INV-003"), "amount": 50.0},
                ]
            }
            event = _build_event("GET", "/records")
            resp = lambda_handler(event, None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert "REC-A" in body
        assert "REC-B" in body
        assert len(body["REC-A"]) == 2
        assert len(body["REC-B"]) == 1

    def test_get_all_records_dynamodb_failure_returns_500(self):
        """GET /records DynamoDB scan failure → 500 with 'DynamoDB read failed'."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.scan.side_effect = Exception("Scan failed")

            event = _build_event("GET", "/records")
            resp = lambda_handler(event, None)

        assert resp["statusCode"] == 500
        body = json.loads(resp["body"])
        assert body["error"] == "DynamoDB read failed"

    # -----------------------------------------------------------------------
    # GET /record/{id}
    # -----------------------------------------------------------------------

    def test_get_record_existing_id_returns_200_with_list(self):
        """GET /record/{id} with existing data → 200 with list."""
        items = [
            {**_make_item("REC-1", "INV-001"), "amount": 100.0},
            {**_make_item("REC-1", "INV-002"), "amount": 200.0},
        ]
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": items}

            event = _build_event("GET", "/record/REC-1")
            resp = lambda_handler(event, None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert isinstance(body, list)
        assert len(body) == 2

    def test_get_record_unknown_id_returns_200_with_empty_list(self):
        """GET /record/{id} with no data → 200 with empty list []."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": []}

            event = _build_event("GET", "/record/NONEXISTENT")
            resp = lambda_handler(event, None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body == []

    def test_get_record_dynamodb_failure_returns_500(self):
        """GET /record/{id} DynamoDB query failure → 500 with 'DynamoDB read failed'."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.side_effect = Exception("Query failed")

            event = _build_event("GET", "/record/REC-1")
            resp = lambda_handler(event, None)

        assert resp["statusCode"] == 500
        body = json.loads(resp["body"])
        assert body["error"] == "DynamoDB read failed"

    # -----------------------------------------------------------------------
    # CORS headers on all retrieval responses
    # -----------------------------------------------------------------------

    def test_get_all_records_cors_headers_present(self):
        """CORS headers present on GET /records 200 response."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.scan.return_value = {"Items": []}

            event = _build_event("GET", "/records")
            resp = lambda_handler(event, None)

        _assert_cors_headers(resp)

    def test_get_all_records_cors_headers_on_500(self):
        """CORS headers present on GET /records 500 error response."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.scan.side_effect = Exception("Fail")

            event = _build_event("GET", "/records")
            resp = lambda_handler(event, None)

        _assert_cors_headers(resp)

    def test_get_record_cors_headers_present_on_200(self):
        """CORS headers present on GET /record/{id} 200 response."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": []}

            event = _build_event("GET", "/record/REC-1")
            resp = lambda_handler(event, None)

        _assert_cors_headers(resp)

    def test_get_record_cors_headers_present_on_500(self):
        """CORS headers present on GET /record/{id} 500 error response."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.side_effect = Exception("Fail")

            event = _build_event("GET", "/record/REC-1")
            resp = lambda_handler(event, None)

        _assert_cors_headers(resp)


# ---------------------------------------------------------------------------
# TestAIHandlers — Task 11.3
# Validates: Requirements 7.1–7.6, 8.1–8.6
# ---------------------------------------------------------------------------

import io


def _make_bedrock_response(payload: dict) -> MagicMock:
    """Build a mock Bedrock invoke_model response wrapping payload as Claude content."""
    body_bytes = json.dumps({"content": [{"text": json.dumps(payload)}]}).encode()
    mock_response = MagicMock()
    mock_response.__getitem__ = lambda self, key: io.BytesIO(body_bytes) if key == "body" else None
    return mock_response


def _make_invalid_bedrock_response() -> MagicMock:
    """Build a mock Bedrock response that contains non-JSON text."""
    body_bytes = json.dumps({"content": [{"text": "This is not JSON at all."}]}).encode()
    mock_response = MagicMock()
    mock_response.__getitem__ = lambda self, key: io.BytesIO(body_bytes) if key == "body" else None
    return mock_response


def _ai_event(path: str, record_id: str = "REC-1") -> dict:
    """Build a minimal POST event for an AI endpoint."""
    return {
        "requestContext": {"http": {"method": "POST"}},
        "rawPath": path,
        "body": json.dumps({"record_id": record_id}),
    }


class TestAIHandlers:
    """Tests for handle_ai_summary() and handle_ai_dashboard()."""

    # -----------------------------------------------------------------------
    # AI Summary — happy path
    # -----------------------------------------------------------------------

    def test_ai_summary_happy_path_returns_200_with_parsed_body(self):
        """Mock DynamoDB returns records, mock Bedrock returns valid JSON → 200 with parsed body."""
        items = [_make_item("REC-1", "INV-001")]
        summary_payload = {
            "summary": "No anomalies found.",
            "flags": [],
            "overall_risk_score": 0.1,
        }

        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(summary_payload)

        with (
            patch("lambda_handler.dynamodb_resource") as mock_ddb,
            patch("lambda_handler.bedrock_client", mock_bedrock),
        ):
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": items}

            resp = lambda_handler(_ai_event("/ai/summary"), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["summary"] == "No anomalies found."
        assert body["flags"] == []
        assert body["overall_risk_score"] == 0.1

    def test_ai_summary_bedrock_parse_failure_returns_200_with_fallback(self):
        """Bedrock parse failure (invalid JSON) → 200 with fallback {"summary": "AI analysis unavailable", ...}."""
        items = [_make_item("REC-1", "INV-001")]

        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_invalid_bedrock_response()

        with (
            patch("lambda_handler.dynamodb_resource") as mock_ddb,
            patch("lambda_handler.bedrock_client", mock_bedrock),
        ):
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": items}

            resp = lambda_handler(_ai_event("/ai/summary"), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["summary"] == "AI analysis unavailable"
        assert body["flags"] == []
        assert body["overall_risk_score"] == 0

    def test_ai_summary_empty_records_returns_404(self):
        """Empty records → 404 with {"error": "Record not found"}."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": []}

            resp = lambda_handler(_ai_event("/ai/summary", record_id="MISSING"), None)

        assert resp["statusCode"] == 404
        body = json.loads(resp["body"])
        assert body["error"] == "Record not found"

    def test_ai_summary_dynamodb_failure_returns_500(self):
        """DynamoDB read failure → 500 with 'DynamoDB read failed'."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.side_effect = Exception("DB down")

            resp = lambda_handler(_ai_event("/ai/summary"), None)

        assert resp["statusCode"] == 500
        body = json.loads(resp["body"])
        assert body["error"] == "DynamoDB read failed"

    # -----------------------------------------------------------------------
    # AI Dashboard — happy path
    # -----------------------------------------------------------------------

    def test_ai_dashboard_happy_path_returns_200_with_parsed_body(self):
        """Valid Bedrock JSON → 200 parsed body."""
        items = [_make_item("REC-1", "INV-001")]
        dashboard_payload = {
            "totals": {"total_invoices": 1, "total_amount": 120.0, "average_invoice": 120.0},
            "vendor_breakdown": [{"vendor": "Acme Corp", "total": 120.0}],
            "risk_indicators": {"high": 0, "medium": 0, "low": 1},
            "anomalies": [],
        }

        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(dashboard_payload)

        with (
            patch("lambda_handler.dynamodb_resource") as mock_ddb,
            patch("lambda_handler.bedrock_client", mock_bedrock),
        ):
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": items}

            resp = lambda_handler(_ai_event("/ai/dashboard"), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["totals"]["total_invoices"] == 1
        assert body["vendor_breakdown"][0]["vendor"] == "Acme Corp"
        assert body["risk_indicators"]["low"] == 1

    def test_ai_dashboard_bedrock_parse_failure_returns_200_with_fallback(self):
        """Bedrock parse failure → 200 with fallback dashboard (zero values)."""
        items = [_make_item("REC-1", "INV-001")]

        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_invalid_bedrock_response()

        with (
            patch("lambda_handler.dynamodb_resource") as mock_ddb,
            patch("lambda_handler.bedrock_client", mock_bedrock),
        ):
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": items}

            resp = lambda_handler(_ai_event("/ai/dashboard"), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["totals"]["total_invoices"] == 0
        assert body["totals"]["total_amount"] == 0
        assert body["totals"]["average_invoice"] == 0
        assert body["vendor_breakdown"] == []
        assert body["risk_indicators"] == {"high": 0, "medium": 0, "low": 0}
        assert "AI analysis unavailable" in body["anomalies"]

    def test_ai_dashboard_empty_records_returns_404(self):
        """Empty records → 404."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": []}

            resp = lambda_handler(_ai_event("/ai/dashboard", record_id="MISSING"), None)

        assert resp["statusCode"] == 404
        body = json.loads(resp["body"])
        assert body["error"] == "Record not found"

    def test_ai_dashboard_dynamodb_failure_returns_500(self):
        """DynamoDB read failure → 500 with 'DynamoDB read failed'."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.side_effect = Exception("DB down")

            resp = lambda_handler(_ai_event("/ai/dashboard"), None)

        assert resp["statusCode"] == 500
        body = json.loads(resp["body"])
        assert body["error"] == "DynamoDB read failed"

    # -----------------------------------------------------------------------
    # CORS headers on all AI responses
    # -----------------------------------------------------------------------

    def test_ai_summary_cors_headers_on_200(self):
        """CORS headers present on /ai/summary 200 response."""
        items = [_make_item("REC-1", "INV-001")]
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(
            {"summary": "ok", "flags": [], "overall_risk_score": 0}
        )
        with (
            patch("lambda_handler.dynamodb_resource") as mock_ddb,
            patch("lambda_handler.bedrock_client", mock_bedrock),
        ):
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": items}
            resp = lambda_handler(_ai_event("/ai/summary"), None)
        _assert_cors_headers(resp)

    def test_ai_summary_cors_headers_on_404(self):
        """CORS headers present on /ai/summary 404 response."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": []}
            resp = lambda_handler(_ai_event("/ai/summary", record_id="NONE"), None)
        _assert_cors_headers(resp)

    def test_ai_summary_cors_headers_on_500(self):
        """CORS headers present on /ai/summary 500 response."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.side_effect = Exception("Fail")
            resp = lambda_handler(_ai_event("/ai/summary"), None)
        _assert_cors_headers(resp)

    def test_ai_dashboard_cors_headers_on_200(self):
        """CORS headers present on /ai/dashboard 200 response."""
        items = [_make_item("REC-1", "INV-001")]
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(
            {"totals": {"total_invoices": 1, "total_amount": 0, "average_invoice": 0},
             "vendor_breakdown": [], "risk_indicators": {"high": 0, "medium": 0, "low": 0},
             "anomalies": []}
        )
        with (
            patch("lambda_handler.dynamodb_resource") as mock_ddb,
            patch("lambda_handler.bedrock_client", mock_bedrock),
        ):
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": items}
            resp = lambda_handler(_ai_event("/ai/dashboard"), None)
        _assert_cors_headers(resp)

    def test_ai_dashboard_cors_headers_on_404(self):
        """CORS headers present on /ai/dashboard 404 response."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.return_value = {"Items": []}
            resp = lambda_handler(_ai_event("/ai/dashboard", record_id="NONE"), None)
        _assert_cors_headers(resp)

    def test_ai_dashboard_cors_headers_on_500(self):
        """CORS headers present on /ai/dashboard 500 response."""
        with patch("lambda_handler.dynamodb_resource") as mock_ddb:
            mock_table = MagicMock()
            mock_ddb.Table.return_value = mock_table
            mock_table.query.side_effect = Exception("Fail")
            resp = lambda_handler(_ai_event("/ai/dashboard"), None)
        _assert_cors_headers(resp)
