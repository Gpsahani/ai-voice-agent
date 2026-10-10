"""Test double. Wire it only through a FastAPI dependency override."""

import os

from app.services.transcriber import Segment, Transcription


class FakeTranscriber:
    def __init__(
        self,
        segments: list[Segment] | None = None,
        language: str | None = "en",
        duration_seconds: float | None = 1.0,
        error: BaseException | None = None,
    ) -> None:
        self._loaded = False
        self._segments = (
            list(segments)
            if segments is not None
            else [
                Segment(0.0, 0.4, "Hello everyone."),
                Segment(0.4, 0.8, "Let's begin."),
            ]
        )
        self._language = language
        self._duration_seconds = duration_seconds
        self._error = error

    @property
    def loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        self._loaded = True

    def transcribe(self, path: str) -> Transcription:
        if self._error is not None:
            raise self._error
        if not self._loaded:
            raise RuntimeError("FakeTranscriber is not loaded")
        if not os.path.isfile(path):
            raise RuntimeError("audio path is missing")
        return Transcription(
            language=self._language,
            duration_seconds=self._duration_seconds,
            segments=list(self._segments),
        )
