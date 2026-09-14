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
    area_share_percent,
    check_limit_precision,
    check_limit_syntax,
    find_dominant_interval,
    find_first_failure,
    format_seconds,
    integrate,
)
from app.models import SamplePoint


def points(*pairs: tuple[int, str]) -> list[SamplePoint]:
    return [SamplePoint(timestamp=ts, ppm=ppm) for ts, ppm in pairs]


def close(actual: Decimal, expected: Decimal, tolerance=Decimal("1e-20")) -> bool:
    """Compare rationals whose decimal tails differ only by working precision."""
    return abs(actual - expected) <= tolerance


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


class TestAdjudicateWithCustomThreshold:
    """The verdict follows the supplied threshold; area math never changes."""

    def test_equivalent_exactly_at_custom_threshold_passes(self):
        seq = points((0, "30"), (28800, "30"))
        area, equivalent, verdict = adjudicate(seq, threshold=Decimal("30"))
        assert area == Decimal("864000")
        assert equivalent == Decimal("30.000")
        assert verdict == "PASS"

    def test_equivalent_just_above_custom_threshold_fails(self):
        seq = points((0, "30"), (28800, "30"))
        _, equivalent, verdict = adjudicate(seq, threshold=Decimal("29.999"))
        assert equivalent == Decimal("30.000")
        assert verdict == "FAIL"

    def test_area_and_equivalent_do_not_depend_on_threshold(self):
        seq = points((0, "0"), (100, "0"), (200, "100"), (28800, "0"))
        assert adjudicate(seq, threshold=Decimal("60")) == (
            Decimal("1435000"),
            Decimal("49.826"),
            "PASS",
        )
        assert adjudicate(seq, threshold=Decimal("49")) == (
            Decimal("1435000"),
            Decimal("49.826"),
            "FAIL",
        )

    def test_default_threshold_remains_25(self):
        seq = points((0, "30"), (28800, "30"))
        assert adjudicate(seq)[2] == "FAIL"
        assert adjudicate(seq, threshold=Decimal("30"))[2] == "PASS"


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
        # Ramp 24 -> 27 over 10 s: crossing at (1/3)*10 = 3.3333...; the long
        # descent 27 -> 0 over 28790 s crosses at 10 + 2/27*28790.
        seq = points((0, "24"), (10, "27"), (28800, "0"))
        summary = analyze_exceedance(seq)
        start = Decimal("10") / Decimal("3")
        end = Decimal("10") + Decimal("2") * Decimal("28790") / Decimal("27")
        assert close(summary.longest.start, start)
        assert close(summary.longest.end, end)
        assert close(summary.total_seconds, end - start)
        # Serialization rounds each exact value independently.
        assert format_seconds(summary.longest.start) == "3.333"

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
        # Two equal triangular excursions with fractional crossing points;
        # durations tie exactly at 10/3 s, so the earliest must win without
        # any rounding deciding it.
        seq = points(
            (0, "0"),
            (10, "30"),    # crosses up at 25/3
            (20, "0"),     # crosses down at 35/3
            (100, "0"),
            (110, "30"),   # crosses up at 100 + 25/3
            (120, "0"),    # crosses down at 100 + 35/3
            (28800, "0"),
        )
        summary = analyze_exceedance(seq)
        assert close(summary.longest.start, Decimal(25) / Decimal(3))
        assert close(summary.longest.end, Decimal(35) / Decimal(3))
        assert close(summary.longest.duration, Decimal(10) / Decimal(3))
        assert close(summary.total_seconds, Decimal(20) / Decimal(3))
        # Serialized independently at the wire boundary.
        assert (
            format_seconds(summary.longest.start),
            format_seconds(summary.longest.end),
            format_seconds(summary.longest.duration),
            format_seconds(summary.total_seconds),
        ) == ("8.333", "11.667", "3.333", "6.667")

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
        # First stretch: 25/3 -> 20 (35/3 s); second: 20 -> 32.5 (12.5 s).
        # The second is longer, and the threshold-touching sample at t=20
        # stays a boundary. Internal values stay exact.
        assert summary.longest.start == Decimal("20")
        assert summary.longest.end == Decimal("32.5")
        assert close(summary.total_seconds, Decimal("145") / Decimal("6"))
        assert format_seconds(summary.total_seconds) == "24.167"

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

    def test_three_short_excursions_total_is_not_rounded_up(self):
        # Regression: each triangular excursion truly lasts 4/3 s
        # (0->30 over 4 s crosses at 10/3, 30->0 over 4 s at 14/3).
        # Rounding each crossing before subtracting inflated every span to
        # 1.334 s and reported 4.002 s for three excursions; the exact total
        # is 4 s and must serialize as "4".
        seq = points(
            (0, "0"), (4, "30"), (8, "0"),
            (20, "0"), (24, "30"), (28, "0"),
            (40, "0"), (44, "30"), (48, "0"),
            (28800, "0"),
        )
        summary = analyze_exceedance(seq)
        assert close(summary.total_seconds, Decimal("4"))
        assert format_seconds(summary.total_seconds) == "4"
        assert close(summary.longest.duration, Decimal(4) / Decimal(3))
        assert (
            format_seconds(summary.longest.start),
            format_seconds(summary.longest.end),
            format_seconds(summary.longest.duration),
        ) == ("3.333", "4.667", "1.333")


class TestAnalyzeExceedanceWithCustomThreshold:
    """The crossing analysis follows the supplied site limit."""

    def test_crossings_move_with_the_custom_threshold(self):
        # 20 -> 30 crosses 22 at t = 2; 30 -> 20 crosses back at t = 18.
        seq = points((0, "20"), (10, "30"), (20, "20"), (28800, "20"))
        summary = analyze_exceedance(seq, threshold=Decimal("22"))
        assert (summary.longest.start, summary.longest.end) == (
            Decimal("2"),
            Decimal("18"),
        )
        assert summary.total_seconds == Decimal("16")

    def test_default_threshold_still_crosses_at_25(self):
        seq = points((0, "20"), (10, "30"), (20, "20"), (28800, "20"))
        summary = analyze_exceedance(seq)
        assert (summary.longest.start, summary.longest.end) == (
            Decimal("5"),
            Decimal("15"),
        )
        assert summary.total_seconds == Decimal("10")

    def test_constantly_at_custom_threshold_is_not_an_exceedance(self):
        summary = analyze_exceedance(
            points((0, "22"), (28800, "22")), threshold=Decimal("22")
        )
        assert summary.total_seconds == Decimal("0")
        assert summary.longest is None

    def test_sample_touching_custom_threshold_separates_stretches(self):
        # The sample at t=20 touches 22 exactly: two separate stretches.
        seq = points(
            (0, "20"),
            (10, "24"),    # crosses 22 at t = 5
            (20, "22"),    # touches the custom threshold exactly
            (30, "24"),    # above again immediately
            (50, "20"),    # crosses back at t = 40
            (28800, "20"),
        )
        summary = analyze_exceedance(seq, threshold=Decimal("22"))
        assert (summary.longest.start, summary.longest.end) == (
            Decimal("20"),
            Decimal("40"),
        )
        assert summary.total_seconds == Decimal("35")

    def test_excursion_below_custom_threshold_disappears(self):
        # The 20 -> 30 -> 20 bump exceeds 25 but never reaches 35.
        seq = points((0, "20"), (10, "30"), (20, "20"), (28800, "20"))
        summary = analyze_exceedance(seq, threshold=Decimal("35"))
        assert summary.total_seconds == Decimal("0")
        assert summary.longest is None


class TestCheckLimitSyntax:
    """The limit query parameter accepts only plain decimal literals."""

    @pytest.mark.parametrize(
        "literal",
        ["30", "30.500", "0.001", "1000", "999.999", "1.", ".5", "+30", "-5",
         "1e2", "1E+2", "0.5"],
    )
    def test_plain_decimal_literals_pass_through(self, literal):
        assert check_limit_syntax(literal) == literal

    @pytest.mark.parametrize(
        "literal",
        ["1_00", "1_000", "1_0.0_1", "１２３", " 25", "25 ", "", "abc",
         "1.5.2", "NaN", "Infinity", "0x10", ".", "1e"],
    )
    def test_non_plain_forms_are_rejected(self, literal):
        with pytest.raises(ValueError):
            check_limit_syntax(literal)

    def test_non_string_input_passes_through_for_the_decimal_parser(self):
        # Query parameters always arrive as strings; anything else is left
        # for the decimal parser to judge.
        assert check_limit_syntax(None) is None
        assert check_limit_syntax(30) == 30


class TestCheckLimitPrecision:
    @pytest.mark.parametrize("literal", ["30", "30.500", "0.001", "1000.000"])
    def test_three_or_fewer_lexical_places_pass(self, literal):
        value = Decimal(literal)
        assert check_limit_precision(value) is value

    @pytest.mark.parametrize("literal", ["25.0001", "0.0010", "12.3400"])
    def test_four_lexical_places_are_rejected(self, literal):
        with pytest.raises(ValueError):
            check_limit_precision(Decimal(literal))


class TestFindDominantInterval:
    """Adjacent-point interval contributing the most trapezoidal area."""

    def test_long_high_tail_dominates(self):
        # Same sequence as the uneven-spacing adjudication test: the long
        # 100 ppm tail, not the 100 s ramp, must dominate the contribution.
        seq = points((0, "0"), (100, "0"), (200, "100"), (28800, "0"))
        interval = find_dominant_interval(seq)
        assert (interval.start, interval.end) == (200, 28800)
        assert interval.area == Decimal("1430000")

    def test_area_tie_resolves_to_earliest_start(self):
        # (0, 100) and (300, 400) both contribute exactly 5000 ppm*seconds.
        seq = points(
            (0, "50"), (100, "50"), (200, "0"),
            (300, "50"), (400, "50"), (500, "0"),
            (28800, "0"),
        )
        interval = find_dominant_interval(seq)
        assert (interval.start, interval.end) == (0, 100)
        assert interval.area == Decimal("5000")

    def test_all_zero_sequence_picks_first_interval(self):
        seq = points((0, "0"), (14400, "0"), (28800, "0"))
        interval = find_dominant_interval(seq)
        assert (interval.start, interval.end) == (0, 14400)
        assert interval.area == Decimal("0")

    def test_two_point_sequence_has_a_single_interval(self):
        seq = points((0, "0"), (28800, "2"))
        interval = find_dominant_interval(seq)
        assert (interval.start, interval.end) == (0, 28800)
        assert interval.area == Decimal("28800")

    def test_fractional_segment_areas_stay_exact(self):
        # (0.001+0.002)*3/2 = 0.0045 versus (0.002+0.001)*28797/2 = 43.1955.
        seq = points((0, "0.001"), (3, "0.002"), (28800, "0.001"))
        interval = find_dominant_interval(seq)
        assert (interval.start, interval.end) == (3, 28800)
        assert interval.area == Decimal("43.1955")

    def test_fewer_than_two_points_is_rejected(self):
        with pytest.raises(ValueError):
            find_dominant_interval(points((0, "1")))


class TestAreaSharePercent:
    def test_quarter(self):
        assert area_share_percent(Decimal("1"), Decimal("4")) == Decimal("25.000")

    def test_full_share(self):
        assert area_share_percent(Decimal("28800"), Decimal("28800")) == Decimal(
            "100.000"
        )

    def test_repeating_quotient_rounds_to_three_places(self):
        # 5000/17500 = 2/7 = 28.571428... percent.
        assert area_share_percent(Decimal("5000"), Decimal("17500")) == Decimal(
            "28.571"
        )

    def test_rounds_half_up_away_from_zero(self):
        # 0.002/16*100 = 0.0125 exactly; ROUND_HALF_EVEN would give 0.012.
        assert area_share_percent(Decimal("0.002"), Decimal("16")) == Decimal("0.013")

    def test_zero_total_is_defined_as_zero(self):
        assert area_share_percent(Decimal("0"), Decimal("0")) == Decimal("0.000")


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
