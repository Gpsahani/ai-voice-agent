"""Bounded chunk transcription with global timestamps and overlap dedup."""

import logging
import os
import subprocess
import tempfile
from collections.abc import Callable

from app.config import Settings
from app.core.errors import undecodable_audio
from app.services.audio import cleanup
from app.services.transcriber import Segment, Transcriber, Transcription

logger = logging.getLogger(__name__)

ExtractChunk = Callable[[str, float, float, Settings], str]


def plan_chunk_offsets(duration: float, chunk_seconds: float, overlap: float) -> list[float]:
    """Offsets for sequential chunks. Empty when the file fits in one chunk."""
    if duration <= chunk_seconds:
        return []
    step = chunk_seconds - overlap
    if step <= 0:
        raise ValueError("chunk step must be positive")
    offsets: list[float] = []
    offset = 0.0
    limit = int(duration / step) + 2
    while offset < duration and len(offsets) < limit:
        offsets.append(offset)
        offset += step
    return offsets


def shift_segments(segments: list[Segment], offset: float) -> list[Segment]:
    return [
        Segment(
            start=segment.start + offset,
            end=segment.end + offset,
            text=segment.text,
        )
        for segment in segments
    ]


def drop_overlapped_segments(
    segments: list[Segment],
    chunk_index: int,
    chunk_offset: float,
    overlap: float,
) -> list[Segment]:
    """Drop overlap already covered by the previous chunk.

    For chunk index > 0, remove segments whose global end is at or before
    chunk_offset + overlap. Repeated words later than that window are kept.
    """
    if chunk_index == 0:
        return list(segments)
    cutoff = chunk_offset + overlap
    return [segment for segment in segments if segment.end > cutoff]


def extract_chunk(src: str, offset: float, length: float, settings: Settings) -> str:
    handle = tempfile.NamedTemporaryFile(
        delete=False,
        dir=settings.temp_directory(),
        suffix=".chunk.wav",
    )
    handle.close()
    dest = handle.name
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{offset:.3f}",
        "-i",
        src,
        "-t",
        f"{length:.3f}",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        dest,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=settings.ffmpeg_timeout_seconds,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        cleanup(dest)
        raise undecodable_audio()
    if completed.returncode != 0 or not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        cleanup(dest)
        raise undecodable_audio()
    return dest


def transcribe_audio(
    path: str,
    duration: float,
    transcriber: Transcriber,
    settings: Settings,
    chunk_paths: list[str] | None = None,
    extract: ExtractChunk | None = None,
) -> Transcription:
    """Transcribe a normalized file. Chunk only when duration exceeds the chunk size.

    Any chunk failure raises. Chunk files are deleted before this function returns.
    """
    if duration <= settings.chunk_seconds:
        return transcriber.transcribe(path)

    offsets = plan_chunk_offsets(
        duration,
        settings.chunk_seconds,
        settings.chunk_overlap_seconds,
    )
    extractor = extract or extract_chunk
    created: list[str] = []
    merged: list[Segment] = []
    language: str | None = None
    logger.debug("chunked transcription chunks=%s", len(offsets))
    try:
        for index, offset in enumerate(offsets):
            chunk_path = extractor(path, offset, settings.chunk_seconds, settings)
            created.append(chunk_path)
            if chunk_paths is not None:
                chunk_paths.append(chunk_path)
            local = transcriber.transcribe(chunk_path)
            if language is None and local.language:
                language = local.language
            shifted = shift_segments(local.segments, offset)
            merged.extend(
                drop_overlapped_segments(
                    shifted,
                    index,
                    offset,
                    settings.chunk_overlap_seconds,
                )
            )
        return Transcription(
            language=language,
            duration_seconds=duration,
            segments=merged,
        )
    finally:
        for item in created:
            cleanup(item)
