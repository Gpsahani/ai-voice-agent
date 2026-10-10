"""HTTP response models."""

import math

from pydantic import BaseModel, field_validator, model_validator

DURATION_TOLERANCE_SECONDS = 0.5


class HealthResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
    status: str
    model: str
    device: str


class Segment(BaseModel):
    start: float
    end: float
    text: str

    @field_validator("start", "end")
    @classmethod
    def finite_timestamp(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("timestamp must be finite")
        return float(value)

    @model_validator(mode="after")
    def check_bounds(self) -> "Segment":
        if self.start < 0:
            raise ValueError("start must be >= 0")
        if self.end < self.start:
            raise ValueError("end must be >= start")
        return self


class TranscriptionResponse(BaseModel):
    transcription_id: str
    filename: str
    status: str
    language: str | None = None
    duration_seconds: float | None = None
    text: str
    segments: list[Segment]

    @field_validator("duration_seconds")
    @classmethod
    def finite_duration(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value) or value <= 0:
            raise ValueError("duration must be finite and positive")
        return float(value)

    @model_validator(mode="after")
    def check_timeline(self) -> "TranscriptionResponse":
        previous: float | None = None
        for segment in self.segments:
            if previous is not None and segment.start < previous:
                raise ValueError("segment starts must be chronological")
            previous = segment.start
            if (
                self.duration_seconds is not None
                and segment.end > self.duration_seconds + DURATION_TOLERANCE_SECONDS
            ):
                raise ValueError("segment end exceeds media duration")
        return self


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody
