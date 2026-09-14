"""FastAPI application exposing the eight-hour gas adjudication endpoint."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .core import ValidationFailure, adjudicate, find_first_failure
from .models import AdjudicationResult, ErrorEnvelope, SamplePoint

app = FastAPI(
    title="Confined-Space Gas Adjudication API",
    version="1.0.0",
    summary="Eight-hour time-weighted gas exposure adjudication",
    description=(
        "Accepts one JSON sampling sequence covering an eight-hour window, "
        "integrates it trapezoidally with decimal fixed-point arithmetic, "
        "and returns the raw area, the three-decimal equivalent value, and "
        "a PASS/FAIL verdict against the 25.000 ppm threshold."
    ),
)


class DomainValidationError(Exception):
    """Carries the unique first :class:`ValidationFailure` of a batch."""

    def __init__(self, failure: ValidationFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


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
    responses={422: {"model": ErrorEnvelope, "description": "Validation failure"}},
)
def adjudicate_sequence(points: list[SamplePoint]) -> AdjudicationResult:
    failure = find_first_failure(points)
    if failure is not None:
        raise DomainValidationError(failure)
    area, equivalent, verdict = adjudicate(points)
    return AdjudicationResult(
        area=str(area), equivalent=str(equivalent), verdict=verdict
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
