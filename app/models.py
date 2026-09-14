"""Pydantic wire models for the adjudication API."""

from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, StrictInt


class SamplePoint(BaseModel):
    """One gas-concentration sample.

    ``timestamp`` is an integer number of seconds since the start of the
    eight-hour window; ``ppm`` is a decimal concentration.  On the wire,
    ``timestamp`` must be a JSON integer and ``ppm`` a JSON number — that
    contract is enforced by the parsing layer before this model is built.
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: StrictInt
    ppm: Decimal


class ExceedanceSegment(BaseModel):
    """One maximal contiguous stretch strictly above the applicable threshold.

    The threshold is 25.000 ppm by default, or the ``limit_ppm`` query
    parameter when supplied.  Seconds are strings with at most three decimal
    places so fractional crossing points keep their exact millisecond
    resolution.
    """

    start: str
    end: str
    duration_seconds: str


class Exceedance(BaseModel):
    """Threshold-exceedance analysis, attached only when explicitly requested.

    ``total_seconds`` is ``"0"`` and ``longest_segment`` is ``null`` when the
    concentration never rises strictly above the threshold.
    """

    total_seconds: str
    longest_segment: Optional[ExceedanceSegment]


class DominantInterval(BaseModel):
    """The adjacent-sample interval contributing the most to the total area.

    Attached only for ``include_dominant_interval=true`` requests.
    ``start``/``end`` are the bounding sample timestamps in seconds;
    ``area`` is the exact trapezoidal area serialized as a string (like the
    top-level ``area``); ``percentage`` is the interval's share of the total
    area, ROUND_HALF_UP to three decimals — ``"0.000"`` when the total area
    is zero.
    """

    start: int
    end: int
    area: str
    percentage: str


class AdjudicationResult(BaseModel):
    """Successful adjudication payload.

    ``area`` and ``equivalent`` are serialized as strings so the exact
    decimal representation (including trailing zeros) survives the round
    trip without binary floating-point noise.  ``applied_limit`` echoes the
    site-specific ``limit_ppm`` query parameter and is populated only when
    that parameter is present.  ``exceedance`` is populated only for
    ``include_exceedance=true`` requests and ``dominant_interval`` only for
    ``include_dominant_interval=true`` requests; each field is omitted
    entirely otherwise.
    """

    area: str
    equivalent: str
    verdict: Literal["PASS", "FAIL"]
    applied_limit: Optional[str] = None
    exceedance: Optional[Exceedance] = None
    dominant_interval: Optional[DominantInterval] = None


class ErrorBody(BaseModel):
    index: int
    category: str
    message: str


class ErrorEnvelope(BaseModel):
    """Validation failure payload; never carries area/equivalent/verdict."""

    error: ErrorBody
