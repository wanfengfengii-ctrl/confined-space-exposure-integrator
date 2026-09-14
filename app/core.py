"""Domain logic: batch validation and fixed-point trapezoidal adjudication.

All concentration math uses :class:`decimal.Decimal` so results are exact
base-10 fixed-point values, never binary floating-point approximations.
"""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any, Literal, Optional, Sequence

from .models import SamplePoint

TOTAL_SECONDS = 28800  # eight-hour window
MIN_PPM = Decimal("0")
MAX_PPM = Decimal("1000")
MAX_PPM_DECIMAL_PLACES = 3
PASS_THRESHOLD = Decimal("25.000")
# Site-specific adjudication limits (the optional limit_ppm query parameter)
# follow the per-sample ppm rules: decimal fixed-point with at most three
# lexical decimal places; the accepted range is 0.001-1000 inclusive.
MIN_LIMIT = Decimal("0.001")
MAX_LIMIT = Decimal("1000")
MAX_LIMIT_DECIMAL_PLACES = 3
EQUIVALENT_QUANTUM = Decimal("0.001")
SECOND_QUANTUM = Decimal("0.001")
PERCENT_QUANTUM = Decimal("0.001")
# Working precision for the crossing analysis and the tolerance used to tell
# genuinely different segment durations apart.  Inputs carry 3-decimal ppm
# and integer seconds; at 40-digit precision the accumulated division tail
# over a maximal 28801-point window stays below 1e-30 s, while two distinct
# single-crossing durations differ by at least 1e-12 s.  Durations closer
# than 1e-18 s are therefore exact ties as far as any millisecond-resolution
# consumer can observe, and the earliest one wins; strict comparison would
# instead let a 1e-30 division tail decide between identical excursions.
ANALYSIS_PRECISION = 40
DURATION_EPSILON = Decimal("0.000000000000000001")

CATEGORY_INVALID_TYPE = "invalid_type"
CATEGORY_MISSING_ENDPOINT = "missing_endpoint"
CATEGORY_NON_MONOTONIC_TIMESTAMP = "non_monotonic_timestamp"
CATEGORY_PPM_OUT_OF_RANGE = "ppm_out_of_range"
CATEGORY_PPM_PRECISION_EXCEEDED = "ppm_precision_exceeded"

Verdict = Literal["PASS", "FAIL"]


@dataclass(frozen=True)
class ValidationFailure:
    """The unique first failure of a rejected sequence."""

    index: int
    category: str
    message: str


@dataclass(frozen=True)
class ExceedanceSegment:
    """One maximal contiguous stretch strictly above the threshold.

    ``start``/``end`` are seconds since window start; the concentration is
    strictly above the applicable threshold on the open interval
    ``(start, end)``, touching the threshold exactly at both endpoints.
    """

    start: Decimal
    end: Decimal

    @property
    def duration(self) -> Decimal:
        return self.end - self.start


@dataclass(frozen=True)
class ExceedanceSummary:
    """Aggregate result of the threshold-crossing analysis."""

    total_seconds: Decimal
    longest: Optional[ExceedanceSegment]


@dataclass(frozen=True)
class DominantInterval:
    """The adjacent-point interval contributing the most to the total area.

    ``start``/``end`` are the bounding sample timestamps (seconds since
    window start); ``area`` is the interval's exact trapezoidal area in
    ppm*seconds.
    """

    start: int
    end: int
    area: Decimal


class DomainValidationError(Exception):
    """Carries the unique first :class:`ValidationFailure` of a batch."""

    def __init__(self, failure: ValidationFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def _decimal_places(value: Decimal) -> int:
    """Number of fractional decimal places of a finite Decimal."""
    exponent = value.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def check_limit_precision(limit: Decimal) -> Decimal:
    """Return ``limit`` unchanged, rejecting over-precise lexical forms.

    Plugged into the ``limit_ppm`` query-parameter validator, so an
    over-precise limit is refused before the request body is even read.
    Precision is judged on the lexical form exactly like the per-sample
    ppm rule: ``12.3400`` is rejected even though it equals ``12.34``.
    """
    if _decimal_places(limit) > MAX_LIMIT_DECIMAL_PLACES:
        raise ValueError(
            f"limit_ppm {limit} has more than "
            f"{MAX_LIMIT_DECIMAL_PLACES} decimal places"
        )
    return limit


# A plain decimal literal: ASCII digits with an optional sign, fraction, and
# exponent.  Python's Decimal is more lenient than the limit contract — it
# accepts underscores ("1_00"), Unicode digits ("１２３"), and surrounding
# whitespace — so the lexical form is gated on this grammar before parsing.
_LIMIT_SYNTAX = re.compile(
    r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?"
)


def check_limit_syntax(value: Any) -> Any:
    """Pass through plain decimal literals, rejecting anything else.

    Plugged into the ``limit_ppm`` query-parameter validator ahead of
    decimal parsing, so forms that :class:`~decimal.Decimal` would silently
    normalize — underscores (``1_00``), Unicode digits, padding — are
    refused with a 422 located at the query parameter instead of being
    adjudicated under a limit the operator never wrote.
    """
    if isinstance(value, str) and not _LIMIT_SYNTAX.fullmatch(value):
        raise ValueError(f"limit_ppm {value!r} is not a plain decimal number")
    return value


def domain_failure_at(
    points: Sequence[SamplePoint], index: int, count: int
) -> Optional[ValidationFailure]:
    """Domain-check the point at ``index`` within a sequence of ``count``.

    Within one index the category priority is fixed: missing endpoint,
    non-monotonic timestamp, ppm out of range, ppm precision exceeded.
    """
    point = points[index]
    if index == 0 and point.timestamp != 0:
        return ValidationFailure(
            index=index,
            category=CATEGORY_MISSING_ENDPOINT,
            message=f"first timestamp must be 0, got {point.timestamp}",
        )
    if index == count - 1 and point.timestamp != TOTAL_SECONDS:
        return ValidationFailure(
            index=index,
            category=CATEGORY_MISSING_ENDPOINT,
            message=f"last timestamp must be {TOTAL_SECONDS}, got {point.timestamp}",
        )
    if index > 0 and point.timestamp <= points[index - 1].timestamp:
        return ValidationFailure(
            index=index,
            category=CATEGORY_NON_MONOTONIC_TIMESTAMP,
            message=(
                f"timestamp {point.timestamp} is not strictly greater than "
                f"previous timestamp {points[index - 1].timestamp}"
            ),
        )
    if point.ppm < MIN_PPM or point.ppm > MAX_PPM:
        return ValidationFailure(
            index=index,
            category=CATEGORY_PPM_OUT_OF_RANGE,
            message=f"ppm {point.ppm} is outside the allowed range [0, 1000]",
        )
    if _decimal_places(point.ppm) > MAX_PPM_DECIMAL_PLACES:
        return ValidationFailure(
            index=index,
            category=CATEGORY_PPM_PRECISION_EXCEEDED,
            message=(
                f"ppm {point.ppm} has more than "
                f"{MAX_PPM_DECIMAL_PLACES} decimal places"
            ),
        )
    return None


def find_first_failure(points: Sequence[SamplePoint]) -> Optional[ValidationFailure]:
    """Return the unique first domain failure, or ``None`` if valid.

    Failures are located by sample index ascending.
    """
    count = len(points)
    if count == 0:
        return ValidationFailure(
            index=0,
            category=CATEGORY_MISSING_ENDPOINT,
            message="sequence is empty: endpoints at timestamp 0 and 28800 are required",
        )
    for index in range(count):
        failure = domain_failure_at(points, index, count)
        if failure is not None:
            return failure
    return None


def _segment_areas(points: Sequence[SamplePoint]) -> list[Decimal]:
    """Exact trapezoidal area of every adjacent-point segment, in order."""
    return [
        (previous.ppm + current.ppm) * (current.timestamp - previous.timestamp) / 2
        for previous, current in zip(points, points[1:])
    ]


def integrate(points: Sequence[SamplePoint]) -> Decimal:
    """Exact trapezoidal area in ppm*seconds over the sampled sequence.

    Accumulates ``(previous_ppm + current_ppm) * delta_seconds / 2`` for
    every adjacent pair using Decimal arithmetic, so uneven sampling
    intervals weight long high-concentration stretches correctly.
    """
    area = Decimal("0")
    for segment_area in _segment_areas(points):
        area += segment_area
    return area


def equivalent_value(area: Decimal) -> Decimal:
    """Eight-hour equivalent: area / 28800, ROUND_HALF_UP to 3 places."""
    return (area / TOTAL_SECONDS).quantize(EQUIVALENT_QUANTUM, rounding=ROUND_HALF_UP)


def adjudicate(
    points: Sequence[SamplePoint], threshold: Decimal = PASS_THRESHOLD
) -> tuple[Decimal, Decimal, Verdict]:
    """Compute ``(raw_area, equivalent, verdict)`` for a valid sequence.

    The verdict compares the three-decimal equivalent against ``threshold``
    — the shared 25.000 ppm default, or a site-specific limit supplied as
    the ``limit_ppm`` query parameter.
    """
    area = integrate(points)
    equivalent = equivalent_value(area)
    verdict: Verdict = "PASS" if equivalent <= threshold else "FAIL"
    return area, equivalent, verdict


def find_dominant_interval(points: Sequence[SamplePoint]) -> DominantInterval:
    """Locate the adjacent-point interval contributing the most area.

    Candidate areas are exactly the per-segment trapezoids summed by
    :func:`integrate`.  Scanning in sampling order and replacing the best
    only on a strictly larger area keeps the earliest interval when several
    contribute identical areas — including an all-zero sequence, where the
    first interval wins at area zero.
    """
    best: Optional[DominantInterval] = None
    for previous, current, area in zip(points, points[1:], _segment_areas(points)):
        if best is None or area > best.area:
            best = DominantInterval(
                start=previous.timestamp, end=current.timestamp, area=area
            )
    if best is None:
        raise ValueError("dominant interval requires at least two sample points")
    return best


def area_share_percent(part: Decimal, whole: Decimal) -> Decimal:
    """``part`` as a percentage of ``whole``, ROUND_HALF_UP to three places.

    A zero total means every segment contributed nothing, so the share is
    defined as exactly ``0.000`` instead of being left undefined.
    """
    if whole == 0:
        return Decimal("0.000")
    return (part / whole * 100).quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_UP)


def format_seconds(value: Decimal) -> str:
    """Serialize a seconds value with at most three decimal places.

    Trailing zeros are stripped (``Decimal('100.000')`` -> ``"100"``), so
    integral crossing seconds stay compact while fractional crossings retain
    their exact digits up to the millisecond quantum.
    """
    text = format(value.quantize(SECOND_QUANTUM, rounding=ROUND_HALF_UP), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def analyze_exceedance(
    points: Sequence[SamplePoint], threshold: Decimal = PASS_THRESHOLD
) -> ExceedanceSummary:
    """Summarize time spent strictly above ``threshold`` ppm.

    Adjacent samples are treated as a piecewise-linear concentration curve;
    the crossing second within a segment from ``(t0, p0)`` to ``(t1, p1)``
    is solved exactly as

        t = t0 + (threshold - p0) / (p1 - p0) * (t1 - t0)

    with Decimal arithmetic.  Above-threshold stretches that meet at a
    sample point merge into one maximal segment.  Returns the total
    strictly-above duration and the longest segment; ties resolve to the
    earliest segment.  A curve that only ever equals (never exceeds) the
    threshold yields a zero total and ``longest=None``.

    ``threshold`` is the shared 25.000 ppm default unless a site-specific
    ``limit_ppm`` was supplied.  Crossing seconds and durations are kept as
    high-precision Decimals internally; rounding to millisecond resolution
    happens only in :func:`format_seconds` at serialization time, so
    rounding individual endpoints never inflates the reported durations
    (which three short excursions would otherwise turn into 4.002 s
    instead of 4 s).
    """
    with localcontext() as context:
        context.prec = ANALYSIS_PRECISION
        intervals: list[tuple[Decimal, Decimal, int]] = []
        for index, (previous, current) in enumerate(zip(points, points[1:])):
            t0 = Decimal(previous.timestamp)
            t1 = Decimal(current.timestamp)
            p0 = previous.ppm
            p1 = current.ppm
            above0 = p0 > threshold
            above1 = p1 > threshold
            if above0 and above1:
                intervals.append((t0, t1, index))
            elif above0 or above1:
                crossing = t0 + (threshold - p0) / (p1 - p0) * (
                    t1 - t0
                )
                intervals.append(
                    (t0, crossing, index) if above0 else (crossing, t1, index)
                )

        segments: list[ExceedanceSegment] = []
        for start, end, index in intervals:
            if (
                segments
                and start == segments[-1].end
                # Only stretches sharing a sample that is itself strictly
                # above the threshold are one continuous exceedance; a
                # sample touching the threshold exactly separates them.
                and points[index].ppm > threshold
            ):
                segments[-1] = ExceedanceSegment(segments[-1].start, end)
            else:
                segments.append(ExceedanceSegment(start, end))

        total = sum((segment.duration for segment in segments), Decimal("0"))
        longest: Optional[ExceedanceSegment] = None
        for segment in segments:
            # Durations that differ only by a division tail (well under a
            # nanosecond) are ties; a strict-greater comparison here would
            # otherwise pick the "longer" of identical excursions.
            if longest is None or segment.duration > longest.duration + (
                DURATION_EPSILON
            ):
                longest = segment
        return ExceedanceSummary(total_seconds=total, longest=longest)
