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


class _JsonObject(dict):
    """JSON object that remembers keys supplied more than once.

    Standard decoding silently keeps the last value of a duplicated key,
    but the sampling contract treats a repeated field as an ambiguous
    structure.  The duplicates are recorded here so point validation can
    reject them at the correct sample index, keeping the single
    index-ascending first-error pass intact.
    """

    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        super().__init__()
        seen: set[str] = set()
        duplicates: set[str] = set()
        for key, value in pairs:
            if key in seen:
                duplicates.add(key)
            seen.add(key)
            self[key] = value
        self.duplicates = duplicates


def decode_json_body(data: bytes) -> Any:
    """Decode the raw request body, preserving decimal literals exactly."""
    try:
        return json.loads(
            data,
            parse_float=Decimal,
            parse_constant=_NonFiniteNumber,
            object_pairs_hook=_JsonObject,
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


def _field_name_for_message(name: str) -> str:
    """Render a client-supplied field name so the 422 envelope stays
    UTF-8 encodable.

    JSON keys may contain lone surrogates (e.g. ``"\\ud800"``); embedded
    verbatim in the error message, one would make the error response
    itself fail to serialize and surface as a server exception instead of
    the unique first-error envelope.
    """
    return name.encode("utf-8", "backslashreplace").decode("utf-8")


def _parse_point(index: int, item: Any) -> SamplePoint:
    if not isinstance(item, dict):
        raise _invalid(
            index, "sample point must be an object with 'timestamp' and 'ppm'"
        )
    if isinstance(item, _JsonObject) and item.duplicates:
        names = ", ".join(
            _field_name_for_message(name) for name in sorted(item.duplicates)
        )
        raise _invalid(index, f"duplicate field(s): {names}")
    missing = sorted(_POINT_FIELDS - item.keys())
    if missing:
        raise _invalid(index, f"missing field(s): {', '.join(missing)}")
    extra = sorted(set(item) - _POINT_FIELDS)
    if extra:
        names = ", ".join(_field_name_for_message(name) for name in extra)
        raise _invalid(index, f"unexpected field(s): {names}")
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
