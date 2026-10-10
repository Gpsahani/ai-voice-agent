"""FastAPI dependencies."""

from fastapi import Request

from app.config import get_settings
from app.core.errors import engine_unavailable
from app.services.transcriber import Transcriber

__all__ = ["get_settings", "get_transcriber"]


def get_transcriber(request: Request) -> Transcriber:
    transcriber = getattr(request.app.state, "transcriber", None)
    if transcriber is None:
        raise engine_unavailable()
    return transcriber
