"""Unit tests for validation ordering and fixed-point adjudication math."""

from decimal import Decimal

from app.core import (
    CATEGORY_MISSING_ENDPOINT,
    CATEGORY_NON_MONOTONIC_TIMESTAMP,
    CATEGORY_PPM_OUT_OF_RANGE,
    CATEGORY_PPM_PRECISION_EXCEEDED,
    adjudicate,
    find_first_failure,
    integrate,
)
from app.models import SamplePoint


def points(*pairs: tuple[int, str]) -> list[SamplePoint]:
    return [SamplePoint(timestamp=ts, ppm=ppm) for ts, ppm in pairs]


class TestIntegration:
    def test_constant_sequence(self):
        seq = points((0, "10"), (14400, "10"), (28800, "10"))
        area, equivalent, verdict = adjudicate(seq)
        assert area == Decimal("288000")
        assert equivalent == Decimal("10.000")
        assert verdict == "PASS"

    def test_linear_ramp(self):
        seq = points((0, "0"), (28800, "2"))
        area, equivalent, verdict = adjudicate(seq)
        assert area == Decimal("28800")
        assert equivalent == Decimal("1.000")
        assert verdict == "PASS"

    def test_uneven_spacing_weights_long_segments(self):
        # A naive per-sample average of {0, 0, 100, 0} would read 25 ppm;
        # the long 100 ppm tail must dominate the time-weighted result.
        seq = points((0, "0"), (100, "0"), (200, "100"), (28800, "0"))
        area, equivalent, verdict = adjudicate(seq)
        assert area == Decimal("1435000")
        assert equivalent == Decimal("49.826")
        assert verdict == "FAIL"

    def test_round_half_up_rounds_away_from_zero(self):
        # Equivalent is exactly 0.0025; ROUND_HALF_UP must yield 0.003
        # (round-half-even would give 0.002).
        seq = points((0, "0"), (28800, "0.005"))
        area, equivalent, verdict = adjudicate(seq)
        assert area == Decimal("72.000")
        assert equivalent == Decimal("0.003")
        assert verdict == "PASS"

    def test_threshold_exactly_25_passes(self):
        seq = points((0, "25"), (14400, "25"), (28800, "25"))
        area, equivalent, verdict = adjudicate(seq)
        assert area == Decimal("720000")
        assert equivalent == Decimal("25.000")
        assert verdict == "PASS"

    def test_just_above_threshold_fails(self):
        # Equivalent is exactly 25.0005, which rounds up to 25.001 > 25.000.
        seq = points((0, "25"), (28800, "25.001"))
        area, equivalent, verdict = adjudicate(seq)
        assert area == Decimal("720014.400")
        assert equivalent == Decimal("25.001")
        assert verdict == "FAIL"

    def test_maximum_load(self):
        seq = points((0, "1000"), (28800, "1000"))
        area, equivalent, verdict = adjudicate(seq)
        assert area == Decimal("28800000")
        assert equivalent == Decimal("1000.000")
        assert verdict == "FAIL"

    def test_integrate_matches_manual_trapezoids(self):
        seq = points((0, "1.5"), (60, "2.5"), (120, "0.5"), (28800, "1"))
        expected = (
            (Decimal("1.5") + Decimal("2.5")) * 60 / 2
            + (Decimal("2.5") + Decimal("0.5")) * 60 / 2
            + (Decimal("0.5") + Decimal("1")) * 28680 / 2
        )
        assert integrate(seq) == expected == Decimal("21720.0")


class TestFindFirstFailure:
    def test_valid_sequence_passes(self):
        seq = points((0, "0"), (14400, "999.999"), (28800, "1000"))
        assert find_first_failure(seq) is None

    def test_empty_sequence_reports_missing_endpoint_at_zero(self):
        failure = find_first_failure([])
        assert failure is not None
        assert failure.index == 0
        assert failure.category == CATEGORY_MISSING_ENDPOINT

    def test_missing_start_endpoint(self):
        failure = find_first_failure(points((5, "1"), (28800, "1")))
        assert failure is not None
        assert (failure.index, failure.category) == (0, CATEGORY_MISSING_ENDPOINT)

    def test_missing_end_endpoint(self):
        failure = find_first_failure(points((0, "1"), (100, "1")))
        assert failure is not None
        assert (failure.index, failure.category) == (1, CATEGORY_MISSING_ENDPOINT)

    def test_single_point_cannot_cover_window(self):
        failure = find_first_failure(points((0, "1")))
        assert failure is not None
        assert (failure.index, failure.category) == (0, CATEGORY_MISSING_ENDPOINT)

    def test_duplicate_timestamp(self):
        failure = find_first_failure(points((0, "1"), (100, "1"), (100, "2"), (28800, "1")))
        assert failure is not None
        assert (failure.index, failure.category) == (2, CATEGORY_NON_MONOTONIC_TIMESTAMP)

    def test_decreasing_timestamp(self):
        failure = find_first_failure(points((0, "1"), (500, "1"), (400, "1"), (28800, "1")))
        assert failure is not None
        assert (failure.index, failure.category) == (2, CATEGORY_NON_MONOTONIC_TIMESTAMP)

    def test_ppm_below_range(self):
        failure = find_first_failure(points((0, "1"), (100, "-0.001"), (28800, "1")))
        assert failure is not None
        assert (failure.index, failure.category) == (1, CATEGORY_PPM_OUT_OF_RANGE)

    def test_ppm_above_range(self):
        failure = find_first_failure(points((0, "1"), (100, "1000.001"), (28800, "1")))
        assert failure is not None
        assert (failure.index, failure.category) == (1, CATEGORY_PPM_OUT_OF_RANGE)

    def test_ppm_precision_exceeded(self):
        failure = find_first_failure(points((0, "1"), (100, "0.0001"), (28800, "1")))
        assert failure is not None
        assert (failure.index, failure.category) == (1, CATEGORY_PPM_PRECISION_EXCEEDED)

    def test_range_beats_precision_at_same_index(self):
        # 1000.0001 is both out of range and too precise; range wins.
        failure = find_first_failure(points((0, "1"), (100, "1000.0001"), (28800, "1")))
        assert failure is not None
        assert (failure.index, failure.category) == (1, CATEGORY_PPM_OUT_OF_RANGE)

    def test_endpoint_beats_monotonicity_at_same_index(self):
        # Last point is neither 28800 nor increasing; endpoint wins.
        failure = find_first_failure(points((0, "1"), (20000, "1"), (15000, "1")))
        assert failure is not None
        assert (failure.index, failure.category) == (2, CATEGORY_MISSING_ENDPOINT)

    def test_monotonicity_beats_ppm_at_same_index(self):
        failure = find_first_failure(points((0, "1"), (500, "1"), (400, "2000"), (28800, "1")))
        assert failure is not None
        assert (failure.index, failure.category) == (2, CATEGORY_NON_MONOTONIC_TIMESTAMP)

    def test_lowest_index_wins_across_categories(self):
        # Endpoint error at index 0 beats the ppm error at index 1.
        failure = find_first_failure(points((5, "1"), (100, "2000"), (28800, "1")))
        assert failure is not None
        assert (failure.index, failure.category) == (0, CATEGORY_MISSING_ENDPOINT)

    def test_lowest_index_wins_within_same_category(self):
        failure = find_first_failure(points((0, "1"), (100, "2000"), (200, "3000"), (28800, "1")))
        assert failure is not None
        assert (failure.index, failure.category) == (1, CATEGORY_PPM_OUT_OF_RANGE)

    def test_boundary_ppm_values_are_valid(self):
        seq = points((0, "0"), (14400, "1000.000"), (28800, "0.001"))
        assert find_first_failure(seq) is None
