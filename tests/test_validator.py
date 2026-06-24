"""Tests for the Validator component (validate_invoice)."""

import sys
import os

# Ensure the project root is on the path so lambda_handler can be imported
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from lambda_handler import validate_invoice

# ---------------------------------------------------------------------------
# Strategy helpers
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = ["invoice_id", "vendor", "amount", "vat_amount", "total"]
VALID_STATUSES = {"VALID", "MISMATCH", "INVALID_STRUCTURE", "INVALID_TYPES", "INVALID_VALUES"}

# Non-numeric types that amount/vat_amount/total might be given
_non_numeric_types = st.one_of(
    st.text(),
    st.booleans(),
    st.none(),
    st.lists(st.integers()),
)

# A numeric value (int or float, not bool)
_numeric = st.one_of(st.integers(), st.floats(allow_nan=False, allow_infinity=False))

# A non-negative numeric value
_non_negative_numeric = st.one_of(
    st.integers(min_value=0),
    st.floats(min_value=0.0, allow_nan=False, allow_infinity=False),
)


# ---------------------------------------------------------------------------
# Property 2: Validator assigns exactly one status
# Feature: financial-invoice-intelligence, Property 2: Validator assigns exactly one status
# ---------------------------------------------------------------------------

_arbitrary_field_value = st.one_of(
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
    st.booleans(),
    st.none(),
)

_valid_invoice_strategy = st.fixed_dictionaries({
    "invoice_id": st.text(max_size=20),
    "vendor": st.text(max_size=20),
    "amount": _non_negative_numeric,
    "vat_amount": _non_negative_numeric,
    "total": _numeric,
})

_arbitrary_dict_strategy = st.dictionaries(
    keys=st.text(max_size=20),
    values=_arbitrary_field_value,
    max_size=10,
)


@settings(max_examples=100)
@given(st.one_of(_valid_invoice_strategy, _arbitrary_dict_strategy))
def test_property_2_validator_assigns_exactly_one_status(data):
    # Feature: financial-invoice-intelligence, Property 2: Validator assigns exactly one status
    # Validates: Requirements 4.6
    status, errors = validate_invoice(data)
    assert isinstance(status, str), "Status must be a string"
    assert status in VALID_STATUSES, f"Status '{status}' is not one of the valid statuses"
    assert isinstance(errors, list), "validation_errors must be a list"


# ---------------------------------------------------------------------------
# Property 3: INVALID_STRUCTURE fires before other checks
# Feature: financial-invoice-intelligence, Property 3: INVALID_STRUCTURE fires before other checks
# ---------------------------------------------------------------------------

def _dict_with_at_least_one_missing_field():
    """Generate a dict that is missing at least one of the 5 required fields."""
    # Start with all fields present (arbitrary values), then remove 1-5 of them
    all_fields = st.fixed_dictionaries({
        "invoice_id": _arbitrary_field_value,
        "vendor": _arbitrary_field_value,
        "amount": _arbitrary_field_value,
        "vat_amount": _arbitrary_field_value,
        "total": _arbitrary_field_value,
    })

    @st.composite
    def _remove_some(draw):
        base = draw(all_fields)
        # Choose a non-empty subset of fields to remove
        fields_to_remove = draw(
            st.lists(
                st.sampled_from(REQUIRED_FIELDS),
                min_size=1,
                max_size=len(REQUIRED_FIELDS),
                unique=True,
            )
        )
        for f in fields_to_remove:
            del base[f]
        return base

    return _remove_some()


@settings(max_examples=100)
@given(_dict_with_at_least_one_missing_field())
def test_property_3_invalid_structure_fires_first(data):
    # Feature: financial-invoice-intelligence, Property 3: INVALID_STRUCTURE fires before other checks
    # Validates: Requirements 4.1, 4.6
    status, errors = validate_invoice(data)
    assert status == "INVALID_STRUCTURE", (
        f"Expected INVALID_STRUCTURE for dict missing required fields, got {status!r}"
    )
    assert any(e.startswith("MISSING_FIELD:") for e in errors)


# ---------------------------------------------------------------------------
# Property 4: INVALID_TYPES fires before value and mismatch checks
# Feature: financial-invoice-intelligence, Property 4: INVALID_TYPES fires before value and mismatch checks
# ---------------------------------------------------------------------------

@st.composite
def _all_fields_with_at_least_one_non_numeric(draw):
    """All 5 fields present, at least one of amount/vat_amount/total is non-numeric."""
    invoice_id = draw(st.text(max_size=20))
    vendor = draw(st.text(max_size=20))

    # For each numeric field, decide: numeric or non-numeric
    choices = draw(
        st.lists(st.booleans(), min_size=3, max_size=3)
    )
    # Ensure at least one is non-numeric (True = non-numeric)
    assume(any(choices))

    def pick_val(is_non_numeric):
        if is_non_numeric:
            return draw(_non_numeric_types)
        else:
            return draw(_numeric)

    return {
        "invoice_id": invoice_id,
        "vendor": vendor,
        "amount": pick_val(choices[0]),
        "vat_amount": pick_val(choices[1]),
        "total": pick_val(choices[2]),
    }


@settings(max_examples=100)
@given(_all_fields_with_at_least_one_non_numeric())
def test_property_4_invalid_types_fires_before_value_and_mismatch(data):
    # Feature: financial-invoice-intelligence, Property 4: INVALID_TYPES fires before value and mismatch checks
    # Validates: Requirements 4.2, 4.6
    status, errors = validate_invoice(data)
    assert status == "INVALID_TYPES", (
        f"Expected INVALID_TYPES when a numeric field has wrong type, got {status!r}"
    )
    assert any(e.startswith("NON_NUMERIC:") for e in errors)


# ---------------------------------------------------------------------------
# Property 5: INVALID_VALUES fires before mismatch check
# Feature: financial-invoice-intelligence, Property 5: INVALID_VALUES fires before mismatch check
# ---------------------------------------------------------------------------

@st.composite
def _all_fields_numeric_at_least_one_negative(draw):
    """All 5 fields present, all numeric, at least one of numeric fields is negative."""
    invoice_id = draw(st.text(max_size=20))
    vendor = draw(st.text(max_size=20))

    # Pick a sign flag for each of the 3 numeric fields: True = negative
    signs = draw(st.lists(st.booleans(), min_size=3, max_size=3))
    assume(any(signs))

    def pick_numeric(force_negative):
        if force_negative:
            return draw(st.one_of(
                st.integers(max_value=-1),
                st.floats(max_value=-0.001, allow_nan=False, allow_infinity=False),
            ))
        else:
            return draw(st.one_of(
                st.integers(min_value=0),
                st.floats(min_value=0.0, allow_nan=False, allow_infinity=False),
            ))

    return {
        "invoice_id": invoice_id,
        "vendor": vendor,
        "amount": pick_numeric(signs[0]),
        "vat_amount": pick_numeric(signs[1]),
        "total": pick_numeric(signs[2]),
    }


@settings(max_examples=100)
@given(_all_fields_numeric_at_least_one_negative())
def test_property_5_invalid_values_fires_before_mismatch(data):
    # Feature: financial-invoice-intelligence, Property 5: INVALID_VALUES fires before mismatch check
    # Validates: Requirements 4.3, 4.6
    status, errors = validate_invoice(data)
    assert status == "INVALID_VALUES", (
        f"Expected INVALID_VALUES when a numeric field is negative, got {status!r}"
    )
    assert any(e.startswith("NEGATIVE_VALUE:") for e in errors)


# ---------------------------------------------------------------------------
# Property 6: VALID status iff all invariants hold
# Feature: financial-invoice-intelligence, Property 6: VALID status iff all invariants hold
# ---------------------------------------------------------------------------

@st.composite
def _valid_invoice(draw):
    """Generate non-negative amount and vat_amount; total = round(amount + vat_amount, 2)."""
    amount = draw(st.floats(
        min_value=0.0, max_value=1_000_000.0,
        allow_nan=False, allow_infinity=False,
    ))
    vat_amount = draw(st.floats(
        min_value=0.0, max_value=1_000_000.0,
        allow_nan=False, allow_infinity=False,
    ))
    total = round(amount + vat_amount, 2)
    return {
        "invoice_id": draw(st.text(min_size=1, max_size=20)),
        "vendor": draw(st.text(min_size=1, max_size=20)),
        "amount": amount,
        "vat_amount": vat_amount,
        "total": total,
    }


@settings(max_examples=100)
@given(_valid_invoice())
def test_property_6_valid_status_iff_all_invariants_hold(data):
    # Feature: financial-invoice-intelligence, Property 6: VALID status iff all invariants hold
    # Validates: Requirements 4.4, 4.5, 4.7
    status, errors = validate_invoice(data)
    assert status == "VALID", (
        f"Expected VALID for well-formed invoice, got {status!r}. data={data}, errors={errors}"
    )
    assert errors == [], f"Expected empty errors list for VALID invoice, got {errors!r}"


# ---------------------------------------------------------------------------
# Property 7: MISMATCH error message content
# Feature: financial-invoice-intelligence, Property 7: MISMATCH error message references actual and expected totals
# ---------------------------------------------------------------------------

@st.composite
def _mismatch_invoice(draw):
    """All 5 fields present, numeric, non-negative, but abs(amount+vat_amount-total) > 0.01."""
    amount = draw(st.floats(
        min_value=0.0, max_value=100_000.0,
        allow_nan=False, allow_infinity=False,
    ))
    vat_amount = draw(st.floats(
        min_value=0.0, max_value=100_000.0,
        allow_nan=False, allow_infinity=False,
    ))
    computed = round(amount + vat_amount, 2)
    # Produce a total that is guaranteed to differ by > 0.01
    # Add or subtract a value strictly greater than 0.01
    offset = draw(st.floats(min_value=0.02, max_value=10_000.0,
                            allow_nan=False, allow_infinity=False))
    direction = draw(st.booleans())
    total = computed + offset if direction else max(0.0, computed - offset)
    assume(abs(amount + vat_amount - total) > 0.01)
    assume(total >= 0)
    return {
        "invoice_id": draw(st.text(min_size=1, max_size=20)),
        "vendor": draw(st.text(min_size=1, max_size=20)),
        "amount": amount,
        "vat_amount": vat_amount,
        "total": total,
    }


@settings(max_examples=100)
@given(_mismatch_invoice())
def test_property_7_mismatch_error_message_content(data):
    # Feature: financial-invoice-intelligence, Property 7: MISMATCH error message references actual and expected totals
    # Validates: Requirements 4.4
    status, errors = validate_invoice(data)
    assert status == "MISMATCH", (
        f"Expected MISMATCH for mismatched total, got {status!r}. data={data}"
    )
    assert len(errors) == 1, f"Expected exactly one error for MISMATCH, got {errors!r}"
    error_str = errors[0]
    # Error must contain both the computed total and the supplied total
    computed = str(round(data["amount"] + data["vat_amount"], 2))
    supplied = str(data["total"])
    assert computed in error_str, (
        f"Expected computed total {computed!r} in error string {error_str!r}"
    )
    assert supplied in error_str, (
        f"Expected supplied total {supplied!r} in error string {error_str!r}"
    )


# ---------------------------------------------------------------------------
# Unit tests: Task 3.8
# ---------------------------------------------------------------------------

def test_boundary_exactly_0_01_off_is_valid():
    """abs(amount + vat_amount - total) == 0.01 → VALID (within tolerance)."""
    data = {
        "invoice_id": "INV-001",
        "vendor": "Acme",
        "amount": 100.0,
        "vat_amount": 20.0,
        "total": 120.01,  # diff = 0.01 exactly — within tolerance
    }
    status, errors = validate_invoice(data)
    assert status == "VALID", f"Expected VALID for diff==0.01, got {status!r}"
    assert errors == []


def test_boundary_0_011_off_is_mismatch():
    """abs(amount + vat_amount - total) == 0.011 → MISMATCH (exceeds tolerance)."""
    data = {
        "invoice_id": "INV-002",
        "vendor": "Acme",
        "amount": 100.0,
        "vat_amount": 20.0,
        "total": 120.011,  # diff = 0.011 — exceeds tolerance
    }
    status, errors = validate_invoice(data)
    assert status == "MISMATCH", f"Expected MISMATCH for diff==0.011, got {status!r}"
    assert len(errors) == 1


def test_zero_values_is_valid():
    """amount=0, vat_amount=0, total=0 → VALID."""
    data = {
        "invoice_id": "INV-003",
        "vendor": "Acme",
        "amount": 0,
        "vat_amount": 0,
        "total": 0,
    }
    status, errors = validate_invoice(data)
    assert status == "VALID", f"Expected VALID for all zeros, got {status!r}"
    assert errors == []


def test_string_total_is_invalid_types():
    """total='100' (string) → INVALID_TYPES."""
    data = {
        "invoice_id": "INV-004",
        "vendor": "Acme",
        "amount": 80.0,
        "vat_amount": 20.0,
        "total": "100",
    }
    status, errors = validate_invoice(data)
    assert status == "INVALID_TYPES", f"Expected INVALID_TYPES for string total, got {status!r}"
    assert any("total" in e for e in errors)
