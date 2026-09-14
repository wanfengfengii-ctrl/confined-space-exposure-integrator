"""Pydantic wire models for the adjudication API."""

from decimal import Decimal
from typing import Literal

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


class AdjudicationResult(BaseModel):
    """Successful adjudication payload.

    ``area`` and ``equivalent`` are serialized as strings so the exact
    decimal representation (including trailing zeros) survives the round
    trip without binary floating-point noise.
    """

    area: str
    equivalent: str
    verdict: Literal["PASS", "FAIL"]


class ErrorBody(BaseModel):
    index: int
    category: str
    message: str


class ErrorEnvelope(BaseModel):
    """Validation failure payload; never carries area/equivalent/verdict."""

    error: ErrorBody
