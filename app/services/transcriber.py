"""Transcription engine contract. Production code never selects a fake."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcription:
    language: str | None
    duration_seconds: float | None
    segments: list[Segment]

    @property
    def text(self) -> str:
        parts = [
            segment.text.strip()
            for segment in self.segments
            if segment.text and segment.text.strip()
        ]
        return " ".join(parts).strip()


@runtime_checkable
class Transcriber(Protocol):
    @property
    def loaded(self) -> bool: ...

    def load(self) -> None: ...

    def transcribe(self, path: str) -> Transcription: ...
