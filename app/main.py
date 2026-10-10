"""FastAPI application: health, readiness, and synchronous transcription."""

import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import Settings, get_settings
from app.core.errors import (
    AppError,
    empty_upload,
    engine_unavailable,
    file_too_large,
    internal_error,
    invalid_request,
    transcription_failed,
)
from app.core.logging import configure_logging, transcription_logger
from app.dependencies import get_transcriber
from app.schemas import (
    HealthResponse,
    ReadyResponse,
    Segment as SegmentSchema,
    TranscriptionResponse,
)
from app.services import audio as audio_service
from app.services.chunking import transcribe_audio
from app.services.faster_whisper import FasterWhisperTranscriber
from app.services.transcriber import Transcriber, Transcription

logger = logging.getLogger(__name__)


@dataclass
class EngineState:
    loaded: bool = False
    model: str = ""
    device: str = ""


@dataclass
class Artifacts:
    normalized: str | None = None
    chunks: list[str] = field(default_factory=list)


def request_id_value(raw: str | None) -> str:
    if not raw:
        return str(uuid.uuid4())
    cleaned = raw.strip()
    if not cleaned or len(cleaned) > 200 or any(char in cleaned for char in "\r\n"):
        return str(uuid.uuid4())
    return cleaned


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request_id_value(request.headers.get("x-request-id"))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _error_body(error: AppError) -> dict:
    return {"error": {"code": error.code, "message": error.message}}


def run_pipeline(
    upload_path: str,
    settings: Settings,
    transcriber: Transcriber,
    artifacts: Artifacts,
) -> Transcription:
    probe = audio_service.probe_audio(upload_path, settings)
    artifacts.normalized = audio_service.normalize_audio(upload_path, settings)
    try:
        transcription = transcribe_audio(
            artifacts.normalized,
            probe.duration_seconds,
            transcriber,
            settings,
            chunk_paths=artifacts.chunks,
        )
    except AppError:
        raise
    except Exception:
        logger.exception("Transcription engine failed")
        raise transcription_failed()
    transcription.duration_seconds = probe.duration_seconds
    return transcription


def build_response(transcription: Transcription, filename: str) -> TranscriptionResponse:
    try:
        return TranscriptionResponse(
            transcription_id=str(uuid.uuid4()),
            filename=filename,
            status="completed",
            language=transcription.language,
            duration_seconds=transcription.duration_seconds,
            text=transcription.text,
            segments=[
                SegmentSchema(start=segment.start, end=segment.end, text=segment.text)
                for segment in transcription.segments
                if segment.text and segment.text.strip()
            ],
        )
    except ValidationError:
        logger.warning("Rejected invalid transcription timestamps")
        raise transcription_failed()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = EngineState(model=settings.whisper_model, device=settings.whisper_device)
    transcriber = FasterWhisperTranscriber(settings)
    app.state.engine = engine
    app.state.transcriber = transcriber
    try:
        transcriber.load()
        engine.loaded = bool(transcriber.loaded)
    except Exception:
        logger.exception("Failed to load the transcription model")
        engine.loaded = False
    yield


app = FastAPI(title="Audio Transcription", lifespan=lifespan)
app.add_middleware(RequestIdMiddleware)


@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=_error_body(exc))


@app.exception_handler(RequestValidationError)
async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    error = invalid_request()
    return JSONResponse(status_code=error.status_code, content=_error_body(error))


@app.exception_handler(StarletteHTTPException)
async def handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 413:
        error = file_too_large()
    elif exc.status_code >= 500:
        error = internal_error()
    else:
        error = invalid_request()
    return JSONResponse(status_code=exc.status_code, content=_error_body(error))


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error")
    error = internal_error()
    return JSONResponse(status_code=error.status_code, content=_error_body(error))


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/ready", response_model=ReadyResponse)
def ready(request: Request) -> ReadyResponse:
    engine: EngineState = request.app.state.engine
    if not engine.loaded:
        raise engine_unavailable()
    return ReadyResponse(status="ready", model=engine.model, device=engine.device)


@app.post("/v1/transcriptions", response_model=TranscriptionResponse)
async def create_transcription(
    request: Request,
    file: UploadFile | None = File(default=None),
    settings: Settings = Depends(get_settings),
    transcriber: Transcriber = Depends(get_transcriber),
) -> TranscriptionResponse:
    started = time.perf_counter()
    status_code = 200
    error_code: str | None = None
    byte_size: int | None = None
    duration: float | None = None
    saved: audio_service.SavedAudio | None = None
    artifacts = Artifacts()
    try:
        if file is None:
            raise empty_upload()
        saved = await audio_service.save_upload(
            file,
            settings,
            _content_length(request),
        )
        byte_size = saved.size
        if not transcriber.loaded:
            raise engine_unavailable()
        transcription = await run_in_threadpool(
            run_pipeline,
            saved.path,
            settings,
            transcriber,
            artifacts,
        )
        duration = transcription.duration_seconds
        return build_response(transcription, saved.filename)
    except AppError as exc:
        status_code = exc.status_code
        error_code = exc.code
        raise
    except Exception:
        logger.exception("Unexpected transcription failure")
        status_code = 500
        error_code = "INTERNAL_ERROR"
        raise internal_error()
    finally:
        audio_service.cleanup(saved.path if saved is not None else None)
        audio_service.cleanup(artifacts.normalized)
        for chunk_path in artifacts.chunks:
            audio_service.cleanup(chunk_path)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        request_id = getattr(request.state, "request_id", "-")
        transcription_logger().info(
            "request_id=%s status=%s bytes=%s duration=%s elapsed_ms=%s error_code=%s",
            request_id,
            status_code,
            byte_size if byte_size is not None else "-",
            f"{duration:.3f}" if duration is not None else "-",
            elapsed_ms,
            error_code or "-",
        )
