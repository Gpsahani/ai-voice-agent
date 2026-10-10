"""faster-whisper adapter. The library is imported only inside load()."""

import threading

from app.config import Settings
from app.services.transcriber import Segment, Transcription


class FasterWhisperTranscriber:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = None
        self._loaded = False
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def model_name(self) -> str:
        return self._settings.whisper_model

    @property
    def device(self) -> str:
        return self._settings.whisper_device

    def load(self) -> None:
        from faster_whisper import WhisperModel

        self._model = WhisperModel(
            self._settings.whisper_model,
            device=self._settings.whisper_device,
            compute_type=self._settings.whisper_compute_type,
        )
        self._loaded = True

    def transcribe(self, path: str) -> Transcription:
        if not self._loaded or self._model is None:
            raise RuntimeError("Transcription engine is not loaded")
        language = self._settings.whisper_language or None
        # Called from a worker thread. The lock stays inside that worker.
        with self._lock:
            segment_iter, info = self._model.transcribe(path, language=language)
            segments: list[Segment] = []
            for raw in segment_iter:
                text = (getattr(raw, "text", "") or "").strip()
                if not text:
                    continue
                segments.append(
                    Segment(start=float(raw.start), end=float(raw.end), text=text)
                )
            raw_duration = getattr(info, "duration", None)
            duration = float(raw_duration) if raw_duration is not None else None
            detected = getattr(info, "language", None) or None
            return Transcription(
                language=detected,
                duration_seconds=duration,
                segments=segments,
            )
