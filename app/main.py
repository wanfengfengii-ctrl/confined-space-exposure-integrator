"""FastAPI application exposing the eight-hour gas adjudication endpoint."""

from decimal import Decimal
from typing import Annotated, Optional, Sequence

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AfterValidator

from .core import (
    CATEGORY_INVALID_TYPE,
    MAX_LIMIT,
    MIN_LIMIT,
    PASS_THRESHOLD,
    DomainValidationError,
    ValidationFailure,
    adjudicate,
    analyze_exceedance,
    area_share_percent,
    check_limit_precision,
    find_dominant_interval,
    format_seconds,
)
from .models import (
    AdjudicationResult,
    DominantInterval,
    ErrorEnvelope,
    Exceedance,
    ExceedanceSegment,
    SamplePoint,
)
from .parsing import decode_json_body, validate_sequence

# A site-specific adjudication limit: decimal, 0.001-1000, at most three
# decimal places judged on the lexical form.  Range and non-finite failures
# come from the Query constraints below; the lexical precision rule needs
# the parsed Decimal's exponent, so it runs as a type-level validator.
LimitPpm = Annotated[Decimal, AfterValidator(check_limit_precision)]

app = FastAPI(
    title="Confined-Space Gas Adjudication API",
    version="1.0.0",
    summary="Eight-hour time-weighted gas exposure adjudication",
    description=(
        "Accepts one JSON sampling sequence covering an eight-hour window, "
        "integrates it trapezoidally with decimal fixed-point arithmetic, "
        "and returns the raw area, the three-decimal equivalent value, and "
        "a PASS/FAIL verdict against the 25.000 ppm threshold (or an "
        "optional site-specific limit_ppm)."
    ),
)


@app.exception_handler(DomainValidationError)
async def domain_validation_handler(
    _request: Request, exc: DomainValidationError
) -> JSONResponse:
    failure = exc.failure
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "index": failure.index,
                "category": failure.category,
                "message": failure.message,
            }
        },
    )


@app.post(
    "/adjudicate",
    response_model=AdjudicationResult,
    response_model_exclude_unset=True,
    responses={422: {"model": ErrorEnvelope, "description": "Validation failure"}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["timestamp", "ppm"],
                            "additionalProperties": False,
                            "properties": {
                                "timestamp": {
                                    "type": "integer",
                                    "description": (
                                        "Seconds since window start; first 0, "
                                        "last 28800, strictly increasing"
                                    ),
                                },
                                "ppm": {
                                    "type": "number",
                                    "description": (
                                        "Decimal concentration, 0-1000, "
                                        "at most three decimal places"
                                    ),
                                },
                            },
                        },
                    },
                    "example": [
                        {"timestamp": 0, "ppm": 12.345},
                        {"timestamp": 14400, "ppm": 20},
                        {"timestamp": 28800, "ppm": 12.345},
                    ],
                }
            },
        }
    },
)
async def adjudicate_sequence(
    request: Request,
    include_exceedance: bool = Query(
        default=False,
        description=(
            "Attach the strictly-above-threshold exceedance analysis "
            "(25.000 ppm, or limit_ppm when supplied) to the response."
        ),
    ),
    include_dominant_interval: bool = Query(
        default=False,
        description=(
            "Attach the adjacent-sample interval contributing the most "
            "to the total area."
        ),
    ),
    limit_ppm: Optional[LimitPpm] = Query(
        default=None,
        ge=MIN_LIMIT,
        le=MAX_LIMIT,
        description=(
            "Site-specific adjudication limit in ppm (0.001-1000, at most "
            "three decimal places). When present, the PASS/FAIL verdict and "
            "the exceedance analysis use this limit instead of 25.000, and "
            "the response echoes it as applied_limit. Unparseable, "
            "out-of-range, or over-precise values are rejected with a 422 "
            "located at this query parameter before the body is processed."
        ),
    ),
) -> AdjudicationResult:
    # The contract is a single JSON sampling sequence; anything submitted
    # without a JSON media type is rejected before any parsing happens.
    # (An invalid limit_ppm never reaches this point: the framework
    # validates query parameters before the body is read.)
    if not _is_json_content_type(request.headers.get("content-type")):
        raise DomainValidationError(
            ValidationFailure(
                index=0,
                category=CATEGORY_INVALID_TYPE,
                message="request Content-Type must be application/json",
            )
        )
    # The raw body is decoded and validated by the domain layer (not by the
    # framework) so decimal literals keep their exact lexical form and every
    # 422 carries the unique first failure as index + category.
    points = validate_sequence(decode_json_body(await request.body()))
    # The whole batch is validated, then adjudicated, exactly as in the main
    # flow; the exceedance and dominant-interval analyses run only after both
    # have succeeded, so a failure here can never leak area, equivalent, or
    # verdict — a bad batch still returns its single first-failure envelope.
    threshold = limit_ppm if limit_ppm is not None else PASS_THRESHOLD
    area, equivalent, verdict = adjudicate(points, threshold)
    result = AdjudicationResult(
        area=str(area), equivalent=str(equivalent), verdict=verdict
    )
    if limit_ppm is not None:
        # Echo the applied limit for the record; "f" keeps the lexical
        # decimal places without scientific notation.
        result.applied_limit = format(limit_ppm, "f")
    if include_exceedance:
        result.exceedance = _build_exceedance(points, threshold)
    if include_dominant_interval:
        result.dominant_interval = _build_dominant_interval(points, area)
    return result


def _build_exceedance(
    points: Sequence[SamplePoint], threshold: Decimal
) -> Exceedance:
    """Run the crossing analysis and map it to the wire model."""
    summary = analyze_exceedance(points, threshold)
    longest = None
    if summary.longest is not None:
        longest = ExceedanceSegment(
            start=format_seconds(summary.longest.start),
            end=format_seconds(summary.longest.end),
            duration_seconds=format_seconds(summary.longest.duration),
        )
    return Exceedance(
        total_seconds=format_seconds(summary.total_seconds),
        longest_segment=longest,
    )


def _build_dominant_interval(
    points: Sequence[SamplePoint], total_area: Decimal
) -> DominantInterval:
    """Run the interval-contribution analysis and map it to the wire model."""
    interval = find_dominant_interval(points)
    return DominantInterval(
        start=interval.start,
        end=interval.end,
        area=str(interval.area),
        percentage=str(area_share_percent(interval.area, total_area)),
    )


def _is_json_content_type(content_type: Optional[str]) -> bool:
    """Whether the request declared a JSON media type.

    Only ``application/json`` (or an ``application/*+json`` suffix type)
    is adjudicated; parameters such as ``; charset=utf-8`` are ignored.
    """
    if not content_type:
        return False
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
