"""Upload streaming, ffprobe metadata, and FFmpeg normalization."""

import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from fastapi import UploadFile

from app.config import Settings
from app.core.errors import (
    duration_exceeded,
    empty_upload,
    file_too_large,
    undecodable_audio,
    unsupported_media,
)

logger = logging.getLogger(__name__)

READ_CHUNK_BYTES = 1024 * 1024
ALLOWED_FORMAT_TOKENS = frozenset({"wav", "mp3"})
_AUDIO_MAGIC = (b"RIFF", b"ID3", b"fLaC", b"OggS")


@dataclass(frozen=True)
class SavedAudio:
    path: str
    size: int
    filename: str


@dataclass(frozen=True)
class ProbeResult:
    duration_seconds: float
    format_name: str


def media_tools_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def display_filename(raw: str | None) -> str:
    """Basename only. Slashes and backslashes are separators, never path parts."""
    if raw is None:
        return "audio"
    normalized = raw.replace("\\", "/").strip()
    if not normalized:
        return "audio"
    base = normalized.split("/")[-1].strip()
    if not base or base in {".", ".."}:
        return "audio"
    return base


def cleanup(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("Could not delete a temporary file")


async def save_upload(
    upload: UploadFile,
    settings: Settings,
    content_length: int | None = None,
) -> SavedAudio:
    if content_length is not None and content_length > settings.max_upload_bytes:
        raise file_too_large()

    handle = tempfile.NamedTemporaryFile(
        delete=False,
        dir=settings.temp_directory(),
        suffix=".upload",
    )
    path = handle.name
    succeeded = False
    try:
        total = 0
        while True:
            chunk = await upload.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            if total + len(chunk) > settings.max_upload_bytes:
                raise file_too_large()
            handle.write(chunk)
            total += len(chunk)
        if total == 0:
            raise empty_upload()
        saved = SavedAudio(
            path=path,
            size=total,
            filename=display_filename(getattr(upload, "filename", None)),
        )
        succeeded = True
        return saved
    finally:
        handle.close()
        if not succeeded:
            cleanup(path)


def probe_audio(path: str, settings: Settings) -> ProbeResult:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        path,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=settings.ffprobe_timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        logger.error("ffprobe executable not found")
        raise undecodable_audio()
    except subprocess.TimeoutExpired:
        logger.warning("ffprobe timed out")
        raise undecodable_audio()
    except OSError:
        logger.warning("ffprobe could not be started")
        raise undecodable_audio()

    payload = _parse_probe_stdout(completed.stdout)
    if completed.returncode != 0 or payload is None:
        if _looks_like_text(path):
            raise unsupported_media()
        logger.warning("ffprobe failed status=%s", completed.returncode)
        raise undecodable_audio()
    return interpret_probe(payload, settings)


def interpret_probe(payload: object, settings: Settings) -> ProbeResult:
    if not isinstance(payload, dict):
        raise undecodable_audio()
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise undecodable_audio()
    audio_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if not audio_streams:
        raise unsupported_media()
    fmt = payload.get("format")
    if not isinstance(fmt, dict):
        fmt = {}
    format_name = str(fmt.get("format_name") or "")
    if not format_allowed(format_name):
        raise unsupported_media()
    duration = _positive_duration(fmt, audio_streams)
    if duration > settings.max_duration_seconds:
        raise duration_exceeded()
    return ProbeResult(duration_seconds=duration, format_name=format_name)


def format_allowed(format_name: str) -> bool:
    tokens = {part.strip().lower() for part in format_name.split(",") if part.strip()}
    return bool(tokens & ALLOWED_FORMAT_TOKENS)


def normalize_audio(src: str, settings: Settings) -> str:
    """Convert supported audio to 16 kHz mono WAV. ffprobe does not prove decode."""
    handle = tempfile.NamedTemporaryFile(
        delete=False,
        dir=settings.temp_directory(),
        suffix=".norm.wav",
    )
    handle.close()
    dest = handle.name
    command = [
        "ffmpeg",
        "-y",
        "-i",
        src,
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
    except FileNotFoundError:
        cleanup(dest)
        logger.error("ffmpeg executable not found")
        raise undecodable_audio()
    except subprocess.TimeoutExpired:
        cleanup(dest)
        logger.warning("ffmpeg normalization timed out")
        raise undecodable_audio()
    except OSError:
        cleanup(dest)
        logger.warning("ffmpeg could not be started")
        raise undecodable_audio()
    if completed.returncode != 0 or not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        cleanup(dest)
        logger.warning("ffmpeg normalization failed status=%s", completed.returncode)
        raise undecodable_audio()
    return dest


def _parse_probe_stdout(stdout: bytes) -> dict | None:
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _positive_duration(fmt: dict, audio_streams: list[dict]) -> float:
    candidates: list[object] = [fmt.get("duration")]
    for stream in audio_streams:
        candidates.append(stream.get("duration"))
    for raw in candidates:
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    raise undecodable_audio()


def _looks_like_text(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            sample = handle.read(4096)
    except OSError:
        return False
    if not sample:
        return False
    if sample.startswith(_AUDIO_MAGIC):
        return False
    if len(sample) > 8 and sample[4:8] == b"ftyp":
        return False
    if sample[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return False
    printable = sum(32 <= byte < 127 or byte in (9, 10, 13) for byte in sample)
    return printable / len(sample) > 0.9
