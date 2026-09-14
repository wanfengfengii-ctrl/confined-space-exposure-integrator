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


def _decimal_places(value: Decimal) -> int:
    """Number of fractional decimal places of a finite Decimal."""
    exponent = value.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def find_first_failure(points: Sequence[SamplePoint]) -> Optional[ValidationFailure]:
    """Return the unique first validation failure, or ``None`` if valid.

    Failures are located by sample index ascending.  Within a single index
    the category priority is fixed: missing endpoint, non-monotonic
    timestamp, ppm out of range, ppm precision exceeded.
    """
    count = len(points)
    if count == 0:
        return ValidationFailure(
            index=0,
            category=CATEGORY_MISSING_ENDPOINT,
            message="sequence is empty: endpoints at timestamp 0 and 28800 are required",
        )
    for index, point in enumerate(points):
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
                message=f"ppm {point.ppm} has more than {MAX_PPM_DECIMAL_PLACES} decimal places",
            )
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
