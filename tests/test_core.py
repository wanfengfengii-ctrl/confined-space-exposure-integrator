"""Unit tests for validation ordering and fixed-point adjudication math."""

from decimal import Decimal

import pytest

from app.core import (
    CATEGORY_MISSING_ENDPOINT,
    CATEGORY_NON_MONOTONIC_TIMESTAMP,
    CATEGORY_PPM_OUT_OF_RANGE,
    CATEGORY_PPM_PRECISION_EXCEEDED,
    adjudicate,
    analyze_exceedance,
    find_first_failure,
    format_seconds,
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


class TestAnalyzeExceedance:
    """Linear-interpolation crossing analysis, all in exact Decimal."""

    def test_constantly_above_is_one_full_window_segment(self):
        summary = analyze_exceedance(points((0, "30"), (28800, "30")))
        assert summary.total_seconds == Decimal("28800")
        assert (summary.longest.start, summary.longest.end) == (
            Decimal("0"),
            Decimal("28800"),
        )
        assert summary.longest.duration == Decimal("28800")

    def test_two_crossings_between_samples_interpolate_both_points(self):
        # From 0 to 10s: 20 -> 30, crosses 25 at t = 5 exactly.
        # From 10 to 20s: 30 -> 20, crosses 25 at t = 15 exactly.
        seq = points((0, "20"), (10, "30"), (20, "20"), (28800, "20"))
        summary = analyze_exceedance(seq)
        assert summary.total_seconds == Decimal("10")
        assert (summary.longest.start, summary.longest.end) == (
            Decimal("5"),
            Decimal("15"),
        )

    def test_crossing_second_is_solved_with_decimal_arithmetic(self):
        # 24 -> 26 over 3 s crosses at 1.5; 26 -> 24 over the next 3 s
        # crosses back at 4.5: exactly 3 s of strict exceedance.
        summary = analyze_exceedance(
            points((0, "24"), (3, "26"), (6, "24"), (28800, "24"))
        )
        assert (summary.longest.start, summary.longest.end) == (
            Decimal("1.5"),
            Decimal("4.5"),
        )
        assert summary.total_seconds == Decimal("3.0")

    def test_fractional_crossing_rounds_half_up_to_milliseconds(self):
        # Ramp 24 -> 27 over 10 s: crossing at (1/3)*10 = 3.3333... -> 3.333.
        # The long descent 27 -> 0 over 28790 s crosses at 10 + 2/27*28790.
        seq = points((0, "24"), (10, "27"), (28800, "0"))
        summary = analyze_exceedance(seq)
        end = Decimal("10") + Decimal("2") * Decimal("28790") / Decimal("27")
        end = end.quantize(Decimal("0.001"))
        assert summary.longest.start == Decimal("3.333")
        assert summary.longest.end == end
        assert summary.total_seconds == end - Decimal("3.333")

    def test_contiguous_above_segments_merge_across_sample_points(self):
        # Three above-threshold samples joined by above-threshold sample
        # points must collapse into one maximal segment.
        seq = points(
            (0, "0"),
            (5, "50"),
            (10, "50"),
            (15, "50"),
            (20, "0"),
            (28800, "0"),
        )
        summary = analyze_exceedance(seq)
        # Ramp 0->50 over 5 s crosses at 2.5; descent reaches 25 at 17.5.
        assert (summary.longest.start, summary.longest.end) == (
            Decimal("2.5"),
            Decimal("17.5"),
        )
        assert summary.longest.duration == Decimal("15.000")
        assert summary.total_seconds == Decimal("15.000")

    def test_multiple_separate_segments_with_tie_returns_earliest(self):
        # Two equal triangular excursions: each rounds to 3.334 s, so the
        # earliest one must be reported.
        seq = points(
            (0, "0"),
            (10, "30"),    # crosses up at 8.333
            (20, "0"),     # crosses down at 11.667
            (100, "0"),
            (110, "30"),   # crosses up at 108.333
            (120, "0"),    # crosses down at 111.667
            (28800, "0"),
        )
        summary = analyze_exceedance(seq)
        assert summary.longest.start == Decimal("8.333")
        assert summary.longest.end == Decimal("11.667")
        assert summary.total_seconds == Decimal("6.668")

    def test_exact_tie_keeps_earliest_segment(self):
        # Exactly equal 4 s excursions at integer crossings: tie broken by
        # start time.
        seq = points(
            (0, "0"), (4, "50"), (8, "0"),
            (100, "0"), (104, "50"), (108, "0"),
            (28800, "0"),
        )
        summary = analyze_exceedance(seq)
        assert (summary.longest.start, summary.longest.end) == (
            Decimal("2"),
            Decimal("6"),
        )
        assert summary.total_seconds == Decimal("8")

    def test_sample_point_exactly_on_threshold_separates_stretches(self):
        # A sample touching 25.000 exactly between two above-threshold
        # stretches keeps them as separate segments rather than one.
        seq = points(
            (0, "0"),
            (10, "30"),    # cross up at 8.333
            (20, "25"),    # touches the threshold exactly
            (30, "30"),    # immediately above again
            (45, "0"),     # cross down at 32.5
            (28800, "0"),
        )
        summary = analyze_exceedance(seq)
        # First stretch 8.333 -> 20 (11.667 s); second 20 -> 32.5 (12.5 s),
        # so the second wins, and the threshold point stayed a boundary.
        assert summary.longest.start == Decimal("20")
        assert summary.longest.end == Decimal("32.5")
        assert summary.total_seconds == Decimal("24.167")

    def test_always_equal_to_threshold_is_not_an_exceedance(self):
        seq = points((0, "25"), (14400, "25.000"), (28800, "25"))
        summary = analyze_exceedance(seq)
        assert summary.total_seconds == Decimal("0")
        assert summary.longest is None

    def test_kiss_threshold_from_below_is_not_an_exceedance(self):
        # Rises to exactly 25 at the middle sample and falls back: strictly
        # greater nowhere.
        seq = points((0, "0"), (100, "25"), (200, "0"), (28800, "0"))
        summary = analyze_exceedance(seq)
        assert summary.total_seconds == Decimal("0")
        assert summary.longest is None

    def test_always_below_threshold_has_no_exceedance(self):
        summary = analyze_exceedance(points((0, "0"), (28800, "24.999")))
        assert summary.total_seconds == Decimal("0")
        assert summary.longest is None

    def test_endpoints_above_threshold_anchor_to_window_edges(self):
        summary = analyze_exceedance(
            points((0, "30"), (10, "20"), (28800, "20"))
        )
        # Cross at 25/10*10 = 5 s; strictly above on (0, 5).
        assert summary.longest.start == Decimal("0")
        assert summary.longest.end == Decimal("5")
        assert summary.total_seconds == Decimal("5")


class TestFormatSeconds:
    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (Decimal("0"), "0"),
            (Decimal("28800"), "28800"),
            (Decimal("1.5"), "1.5"),
            (Decimal("1.500"), "1.5"),
            (Decimal("3.333"), "3.333"),
            (Decimal("8.333333"), "8.333"),
            (Decimal("11.666666"), "11.667"),
            (Decimal("0.0004"), "0"),
        ],
    )
    def test_at_most_three_decimal_places(self, value, text):
        assert format_seconds(value) == text
