"""Domain logic: batch validation and fixed-point trapezoidal adjudication.

All concentration math uses :class:`decimal.Decimal` so results are exact
base-10 fixed-point values, never binary floating-point approximations.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Optional, Sequence

from .models import SamplePoint

TOTAL_SECONDS = 28800  # eight-hour window
MIN_PPM = Decimal("0")
MAX_PPM = Decimal("1000")
MAX_PPM_DECIMAL_PLACES = 3
PASS_THRESHOLD = Decimal("25.000")
EQUIVALENT_QUANTUM = Decimal("0.001")
SECOND_QUANTUM = Decimal("0.001")

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
    strictly above ``PASS_THRESHOLD`` on the open interval ``(start, end)``,
    touching the threshold exactly at both endpoints.
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


class DomainValidationError(Exception):
    """Carries the unique first :class:`ValidationFailure` of a batch."""

    def __init__(self, failure: ValidationFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def _decimal_places(value: Decimal) -> int:
    """Number of fractional decimal places of a finite Decimal."""
    exponent = value.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


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


def integrate(points: Sequence[SamplePoint]) -> Decimal:
    """Exact trapezoidal area in ppm*seconds over the sampled sequence.

    Accumulates ``(previous_ppm + current_ppm) * delta_seconds / 2`` for
    every adjacent pair using Decimal arithmetic, so uneven sampling
    intervals weight long high-concentration stretches correctly.
    """
    area = Decimal("0")
    for previous, current in zip(points, points[1:]):
        delta_seconds = current.timestamp - previous.timestamp
        area += (previous.ppm + current.ppm) * delta_seconds / 2
    return area


def equivalent_value(area: Decimal) -> Decimal:
    """Eight-hour equivalent: area / 28800, ROUND_HALF_UP to 3 places."""
    return (area / TOTAL_SECONDS).quantize(EQUIVALENT_QUANTUM, rounding=ROUND_HALF_UP)


def adjudicate(points: Sequence[SamplePoint]) -> tuple[Decimal, Decimal, Verdict]:
    """Compute ``(raw_area, equivalent, verdict)`` for a valid sequence."""
    area = integrate(points)
    equivalent = equivalent_value(area)
    verdict: Verdict = "PASS" if equivalent <= PASS_THRESHOLD else "FAIL"
    return area, equivalent, verdict


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


def analyze_exceedance(points: Sequence[SamplePoint]) -> ExceedanceSummary:
    """Summarize time spent strictly above ``PASS_THRESHOLD`` ppm.

    Adjacent samples are treated as a piecewise-linear concentration curve;
    the crossing second within a segment from ``(t0, p0)`` to ``(t1, p1)``
    is solved exactly as

        t = t0 + (threshold - p0) / (p1 - p0) * (t1 - t0)

    with Decimal arithmetic.  Above-threshold stretches that meet at a
    sample point merge into one maximal segment.  Returns the total
    strictly-above duration and the longest segment; ties resolve to the
    earliest segment.  A curve that only ever equals (never exceeds) the
    threshold yields a zero total and ``longest=None``.
    """
    intervals: list[tuple[Decimal, Decimal, int]] = []
    for index, (previous, current) in enumerate(zip(points, points[1:])):
        t0 = Decimal(previous.timestamp)
        t1 = Decimal(current.timestamp)
        p0 = previous.ppm
        p1 = current.ppm
        above0 = p0 > PASS_THRESHOLD
        above1 = p1 > PASS_THRESHOLD
        if above0 and above1:
            intervals.append((t0, t1, index))
        elif above0 or above1:
            crossing = t0 + (PASS_THRESHOLD - p0) / (p1 - p0) * (t1 - t0)
            # Crossings are reported at millisecond resolution; quantize once
            # here so every derived figure (duration, total) stays consistent
            # with the serialized endpoints.
            crossing = crossing.quantize(SECOND_QUANTUM, rounding=ROUND_HALF_UP)
            intervals.append(
                (t0, crossing, index) if above0 else (crossing, t1, index)
            )

    segments: list[ExceedanceSegment] = []
    for start, end, index in intervals:
        # Quantization can collapse a sub-millisecond excursion to zero
        # length; such an excursion contributes nothing and must not become
        # the longest segment.
        if start >= end:
            continue
        if (
            segments
            and start == segments[-1].end
            # Only stretches sharing a sample that is itself strictly above
            # the threshold are one continuous exceedance; a sample touching
            # 25.000 exactly separates the stretches on either side.
            and points[index].ppm > PASS_THRESHOLD
        ):
            segments[-1] = ExceedanceSegment(segments[-1].start, end)
        else:
            segments.append(ExceedanceSegment(start, end))

    total = sum((segment.duration for segment in segments), Decimal("0"))
    longest: Optional[ExceedanceSegment] = None
    for segment in segments:
        if longest is None or segment.duration > longest.duration:
            longest = segment
    return ExceedanceSummary(total_seconds=total, longest=longest)
