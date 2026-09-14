"""Raw-body decoding and schema-level validation of the sampling sequence.

The body is decoded with ``parse_float=Decimal`` so the lexical form of each
JSON number survives: ``12.3400`` stays ``Decimal("12.3400")`` and its four
decimal places remain visible to the precision rule, instead of being
silently collapsed to ``12.34`` by binary float parsing.

Type/shape validation is interleaved with domain validation in a single
index-ascending pass, so the reported failure is always the unique first
error by sample index regardless of which layer produced it.
"""

import json
from decimal import Decimal
from typing import Any

from .core import (
    CATEGORY_INVALID_TYPE,
    CATEGORY_MISSING_ENDPOINT,
    DomainValidationError,
    ValidationFailure,
    domain_failure_at,
)
from .models import SamplePoint

_POINT_FIELDS = {"timestamp", "ppm"}


class _NonFiniteNumber:
    """Sentinel for JSON ``NaN``/``Infinity`` tokens (not valid JSON)."""

    def __init__(self, token: str) -> None:
        self.token = token


def decode_json_body(data: bytes) -> Any:
    """Decode the raw request body, preserving decimal literals exactly."""
    try:
        return json.loads(
            data, parse_float=Decimal, parse_constant=_NonFiniteNumber
        )
    except (ValueError, RecursionError):
        raise DomainValidationError(
            ValidationFailure(
                index=0,
                category=CATEGORY_INVALID_TYPE,
                message="request body is not valid JSON",
            )
        ) from None


def validate_sequence(raw: Any) -> list[SamplePoint]:
    """Validate decoded JSON into sample points, first failure wins.

    Each point is type-checked and immediately domain-checked before moving
    to the next index, so a failure at a lower index always outranks any
    failure at a higher index.
    """
    if not isinstance(raw, list):
        raise DomainValidationError(
            ValidationFailure(
                index=0,
                category=CATEGORY_INVALID_TYPE,
                message="request body must be a JSON array of sample points",
            )
        )
    count = len(raw)
    if count == 0:
        raise DomainValidationError(
            ValidationFailure(
                index=0,
                category=CATEGORY_MISSING_ENDPOINT,
                message=(
                    "sequence is empty: endpoints at timestamp 0 "
                    "and 28800 are required"
                ),
            )
        )
    points: list[SamplePoint] = []
    for index, item in enumerate(raw):
        points.append(_parse_point(index, item))
        failure = domain_failure_at(points, index, count)
        if failure is not None:
            raise DomainValidationError(failure)
    return points


def _invalid(index: int, message: str) -> DomainValidationError:
    return DomainValidationError(
        ValidationFailure(index=index, category=CATEGORY_INVALID_TYPE, message=message)
    )


def _parse_point(index: int, item: Any) -> SamplePoint:
    if not isinstance(item, dict):
        raise _invalid(
            index, "sample point must be an object with 'timestamp' and 'ppm'"
        )
    missing = sorted(_POINT_FIELDS - item.keys())
    if missing:
        raise _invalid(index, f"missing field(s): {', '.join(missing)}")
    extra = sorted(set(item) - _POINT_FIELDS)
    if extra:
        raise _invalid(index, f"unexpected field(s): {', '.join(extra)}")
    timestamp = item["timestamp"]
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise _invalid(index, "timestamp must be an integer number of seconds")
    ppm = _parse_ppm(index, item["ppm"])
    return SamplePoint(timestamp=timestamp, ppm=ppm)


def _parse_ppm(index: int, value: Any) -> Decimal:
    # ppm must be a JSON number; strings (even numeric ones) are type errors.
    if isinstance(value, Decimal):
        ppm = value
    elif isinstance(value, bool) or value is None:
        raise _invalid(index, "ppm must be a JSON number")
    elif isinstance(value, int):
        ppm = Decimal(value)
    else:
        raise _invalid(index, "ppm must be a JSON number")
    if not ppm.is_finite():
        raise _invalid(index, "ppm must be a finite decimal number")
    return ppm
