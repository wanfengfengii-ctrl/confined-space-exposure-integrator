"""Unit tests for raw-body decoding and interleaved sequence validation."""

from decimal import Decimal

import pytest

from app.core import (
    CATEGORY_INVALID_TYPE,
    CATEGORY_MISSING_ENDPOINT,
    CATEGORY_PPM_OUT_OF_RANGE,
    CATEGORY_PPM_PRECISION_EXCEEDED,
    DomainValidationError,
)
from app.parsing import decode_json_body, validate_sequence


def failure_of(callable_, *args):
    with pytest.raises(DomainValidationError) as excinfo:
        callable_(*args)
    return excinfo.value.failure


class TestDecodeJsonBody:
    def test_decimal_literals_keep_trailing_zeros(self):
        raw = decode_json_body(b'[{"timestamp": 0, "ppm": 12.3400}]')
        assert raw[0]["ppm"] == Decimal("12.3400")
        assert raw[0]["ppm"].as_tuple().exponent == -4

    def test_integer_literals_stay_integers(self):
        raw = decode_json_body(b'[{"timestamp": 28800, "ppm": 25}]')
        assert raw[0]["timestamp"] == 28800
        assert isinstance(raw[0]["timestamp"], int)
        assert raw[0]["ppm"] == 25

    def test_invalid_json_raises_invalid_type(self):
        failure = failure_of(decode_json_body, b'[{"timestamp": 0,')
        assert (failure.index, failure.category) == (0, CATEGORY_INVALID_TYPE)

    def test_empty_body_raises_invalid_type(self):
        failure = failure_of(decode_json_body, b"")
        assert (failure.index, failure.category) == (0, CATEGORY_INVALID_TYPE)


class TestValidateSequence:
    def test_valid_sequence_returns_points(self):
        points = validate_sequence(
            decode_json_body(
                b'[{"timestamp":0,"ppm":1.5},{"timestamp":28800,"ppm":2}]'
            )
        )
        assert len(points) == 2
        assert points[0].timestamp == 0
        assert points[0].ppm == Decimal("1.5")
        assert points[1].ppm == Decimal("2")

    def test_non_array_body(self):
        failure = failure_of(validate_sequence, {"samples": []})
        assert (failure.index, failure.category) == (0, CATEGORY_INVALID_TYPE)

    def test_empty_array_reports_missing_endpoint(self):
        failure = failure_of(validate_sequence, [])
        assert (failure.index, failure.category) == (0, CATEGORY_MISSING_ENDPOINT)

    def test_trailing_zero_precision_is_visible(self):
        # 12.3400 as a JSON number must be rejected: four decimal places.
        failure = failure_of(
            validate_sequence,
            decode_json_body(
                b'[{"timestamp":0,"ppm":12.3400},{"timestamp":28800,"ppm":1}]'
            ),
        )
        assert (failure.index, failure.category) == (0, CATEGORY_PPM_PRECISION_EXCEEDED)

    def test_domain_error_at_lower_index_beats_type_error(self):
        failure = failure_of(
            validate_sequence,
            [
                {"timestamp": 5, "ppm": 1},
                {"timestamp": "oops", "ppm": 1},
                {"timestamp": 28800, "ppm": 1},
            ],
        )
        assert (failure.index, failure.category) == (0, CATEGORY_MISSING_ENDPOINT)

    def test_type_error_at_lower_index_beats_domain_error(self):
        failure = failure_of(
            validate_sequence,
            [
                {"timestamp": 0, "ppm": "oops"},
                {"timestamp": 100, "ppm": 2000},
                {"timestamp": 28800, "ppm": 1},
            ],
        )
        assert (failure.index, failure.category) == (0, CATEGORY_INVALID_TYPE)

    def test_type_error_does_not_mask_later_domain_error(self):
        failure = failure_of(
            validate_sequence,
            [
                {"timestamp": 0, "ppm": 1},
                {"timestamp": 100, "ppm": 2000},
                {"timestamp": "oops", "ppm": 1},
            ],
        )
        assert (failure.index, failure.category) == (1, CATEGORY_PPM_OUT_OF_RANGE)

    def test_non_finite_ppm_sentinel(self):
        failure = failure_of(
            validate_sequence,
            decode_json_body(
                b'[{"timestamp":0,"ppm":NaN},{"timestamp":28800,"ppm":1}]'
            ),
        )
        assert (failure.index, failure.category) == (0, CATEGORY_INVALID_TYPE)

    def test_string_ppm_is_a_type_error(self):
        # ppm must be a JSON number; even a well-formed decimal string is
        # rejected as a type error, never adjudicated.
        for literal in ("12.340", "12.3400", "abc"):
            failure = failure_of(
                validate_sequence,
                [{"timestamp": 0, "ppm": literal}, {"timestamp": 28800, "ppm": 1}],
            )
            assert (failure.index, failure.category) == (0, CATEGORY_INVALID_TYPE)


class TestDuplicateFields:
    def test_duplicate_field_is_invalid_type(self):
        failure = failure_of(
            validate_sequence,
            decode_json_body(
                b'[{"timestamp":0,"timestamp":5,"ppm":1},'
                b'{"timestamp":28800,"ppm":1}]'
            ),
        )
        assert (failure.index, failure.category) == (0, CATEGORY_INVALID_TYPE)

    def test_duplicate_at_higher_index_loses_to_domain_error(self):
        failure = failure_of(
            validate_sequence,
            decode_json_body(
                b'[{"timestamp":5,"ppm":1},'
                b'{"timestamp":100,"timestamp":200,"ppm":1},'
                b'{"timestamp":28800,"ppm":1}]'
            ),
        )
        assert (failure.index, failure.category) == (0, CATEGORY_MISSING_ENDPOINT)

    def test_plain_dicts_without_duplicates_are_unaffected(self):
        points = validate_sequence(
            [{"timestamp": 0, "ppm": 1}, {"timestamp": 28800, "ppm": 1}]
        )
        assert len(points) == 2


class TestUnencodableFieldNames:
    def test_surrogate_field_name_keeps_message_encodable(self):
        failure = failure_of(
            validate_sequence,
            decode_json_body(
                b'[{"timestamp":0,"ppm":1,"\\ud800":2},'
                b'{"timestamp":28800,"ppm":1}]'
            ),
        )
        assert (failure.index, failure.category) == (0, CATEGORY_INVALID_TYPE)
        # The message must survive UTF-8 encoding, or the 422 envelope
        # itself would fail to serialize and surface as a server error.
        failure.message.encode("utf-8")

    def test_surrogate_duplicate_field_name_keeps_message_encodable(self):
        failure = failure_of(
            validate_sequence,
            decode_json_body(
                b'[{"timestamp":0,"ppm":1,"\\ud800":2,"\\ud800":3},'
                b'{"timestamp":28800,"ppm":1}]'
            ),
        )
        assert (failure.index, failure.category) == (0, CATEGORY_INVALID_TYPE)
        failure.message.encode("utf-8")
