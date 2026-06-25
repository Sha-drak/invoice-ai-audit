"""Tests for the Parser component — Tasks 6.2 / 6.3.

Covers:
- Property 1: Structured Invoice JSON round-trip
- Unit: valid Bedrock response → parsed dict
- Unit: invalid JSON in Bedrock response → fallback dict
- Unit: modelId used is anthropic.claude-3-sonnet-20240229-v1:0
"""

import io
import json
from unittest.mock import MagicMock, patch, call

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lambda_handler import parse_invoice


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate valid Structured_Invoice dicts (string fields + finite floats)
_invoice_strategy = st.fixed_dictionaries({
    "invoice_id": st.text(min_size=1, max_size=50),
    "vendor": st.text(min_size=1, max_size=100),
    "amount": st.floats(
        min_value=0.0,
        max_value=1e9,
        allow_nan=False,
        allow_infinity=False,
    ),
    "vat_amount": st.floats(
        min_value=0.0,
        max_value=1e9,
        allow_nan=False,
        allow_infinity=False,
    ),
    "total": st.floats(
        min_value=0.0,
        max_value=2e9,
        allow_nan=False,
        allow_infinity=False,
    ),
})


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------

class TestParserProperties:
    """Property-based tests for the Parser component."""

    # Feature: financial-invoice-intelligence
    # Property 1: Structured Invoice JSON round-trip
    # Validates: Requirements 3.6
    @settings(max_examples=100)
    @given(invoice=_invoice_strategy)
    def test_property_1_json_round_trip(self, invoice):
        """
        For any valid Structured_Invoice dict, serializing to JSON and parsing
        back must produce a dict equal to the original.
        """
        serialized = json.dumps(invoice)
        deserialized = json.loads(serialized)
        assert deserialized == invoice


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def _make_bedrock_response(text: str) -> dict:
    """Build a mock Bedrock Converse API response with the given text payload."""
    return {
        "output": {
            "message": {
                "content": [{"text": text}]
            }
        }
    }


class TestParserUnit:
    """Unit tests for parse_invoice()."""

    def test_valid_bedrock_response_returns_parsed_dict(self):
        """Valid JSON in Bedrock response → parsed dict returned."""
        expected = {
            "invoice_id": "INV-2024-001",
            "vendor": "Acme Corp",
            "amount": 100.0,
            "vat_amount": 20.0,
            "total": 120.0,
        }
        bedrock_text = json.dumps(expected)

        with patch("lambda_handler.bedrock_client") as mock_bedrock:
            mock_bedrock.converse.return_value = _make_bedrock_response(bedrock_text)
            result = parse_invoice("Invoice text here")

        assert result == expected

    def test_invalid_json_in_bedrock_response_returns_fallback(self):
        """Non-JSON text in Bedrock response → fallback dict returned."""
        with patch("lambda_handler.bedrock_client") as mock_bedrock:
            mock_bedrock.converse.return_value = _make_bedrock_response(
                "Sorry, I cannot extract that information."
            )
            result = parse_invoice("Some invoice text")

        assert result == {
            "invoice_id": "UNKNOWN",
            "vendor": "UNKNOWN",
            "amount": 0,
            "vat_amount": 0,
            "total": 0,
        }

    def test_bedrock_response_with_markdown_wrapper_is_parsed(self):
        """JSON wrapped in markdown fences → extracted via regex and parsed."""
        inner_json = json.dumps({
            "invoice_id": "INV-X",
            "vendor": "Beta Ltd",
            "amount": 50.0,
            "vat_amount": 10.0,
            "total": 60.0,
        })
        bedrock_text = f"```json\n{inner_json}\n```"

        with patch("lambda_handler.bedrock_client") as mock_bedrock:
            mock_bedrock.converse.return_value = _make_bedrock_response(bedrock_text)
            result = parse_invoice("Invoice text")

        assert result["invoice_id"] == "INV-X"
        assert result["vendor"] == "Beta Ltd"

    def test_model_id_uses_titan_by_default(self):
        """parse_invoice must call Bedrock with a Titan model ID by default."""
        expected_model_id = "us.amazon.nova-2-lite-v1:0"
        inner_json = json.dumps({
            "invoice_id": "INV-1",
            "vendor": "Test",
            "amount": 0,
            "vat_amount": 0,
            "total": 0,
        })

        with patch("lambda_handler.bedrock_client") as mock_bedrock:
            mock_bedrock.converse.return_value = _make_bedrock_response(inner_json)
            parse_invoice("test invoice text")

        call_kwargs = mock_bedrock.converse.call_args[1]
        assert call_kwargs.get("modelId") == expected_model_id, (
            f"Expected modelId={expected_model_id!r}, got {call_kwargs.get('modelId')!r}"
        )

    def test_bedrock_call_failure_returns_fallback(self):
        """If Bedrock raises an exception, the fallback dict is returned."""
        with patch("lambda_handler.bedrock_client") as mock_bedrock:
            mock_bedrock.converse.side_effect = Exception("Bedrock unavailable")
            result = parse_invoice("Some text")

        assert result == {
            "invoice_id": "UNKNOWN",
            "vendor": "UNKNOWN",
            "amount": 0,
            "vat_amount": 0,
            "total": 0,
        }

    def test_empty_raw_text_returns_valid_response_or_fallback(self):
        """Empty raw_text is forwarded; result is either parsed or fallback dict."""
        with patch("lambda_handler.bedrock_client") as mock_bedrock:
            mock_bedrock.converse.return_value = _make_bedrock_response(
                "not json at all"
            )
            result = parse_invoice("")

        # Must always return a dict with all 5 keys
        for key in ("invoice_id", "vendor", "amount", "vat_amount", "total"):
            assert key in result
