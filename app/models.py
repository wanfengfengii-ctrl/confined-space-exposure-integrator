"""Pydantic wire models for the adjudication API."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt


class SamplePoint(BaseModel):
    """One gas-concentration sample.

    ``timestamp`` is an integer number of seconds since the start of the
    eight-hour window; ``ppm`` is a decimal concentration. Pydantic rejects
    non-integer timestamps, non-numeric or non-finite ppm values, and any
    unexpected extra keys before domain validation ever runs.
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
