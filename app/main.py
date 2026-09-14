"""FastAPI application exposing the eight-hour gas adjudication endpoint."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .core import DomainValidationError, adjudicate
from .models import AdjudicationResult, ErrorEnvelope
from .parsing import decode_json_body, validate_sequence

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
async def adjudicate_sequence(request: Request) -> AdjudicationResult:
    # The raw body is decoded and validated by the domain layer (not by the
    # framework) so decimal literals keep their exact lexical form and every
    # 422 carries the unique first failure as index + category.
    points = validate_sequence(decode_json_body(await request.body()))
    area, equivalent, verdict = adjudicate(points)
    return AdjudicationResult(
        area=str(area), equivalent=str(equivalent), verdict=verdict
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
