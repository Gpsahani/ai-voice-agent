"""Stable application errors. Response bodies never include internals."""


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def empty_upload() -> AppError:
    return AppError(
        "EMPTY_UPLOAD",
        "The uploaded file is missing or empty.",
        400,
    )


def invalid_request() -> AppError:
    return AppError(
        "INVALID_REQUEST",
        "The request is malformed.",
        400,
    )


def file_too_large() -> AppError:
    return AppError(
        "FILE_TOO_LARGE",
        "The uploaded file exceeds the configured size limit.",
        413,
    )


def unsupported_media() -> AppError:
    return AppError(
        "UNSUPPORTED_MEDIA",
        "The uploaded file is not a supported audio format.",
        415,
    )


def undecodable_audio() -> AppError:
    return AppError(
        "UNDECODABLE_AUDIO",
        "The uploaded audio could not be decoded.",
        422,
    )


def duration_exceeded() -> AppError:
    return AppError(
        "DURATION_EXCEEDED",
        "The audio exceeds the maximum allowed duration.",
        422,
    )


def engine_unavailable() -> AppError:
    return AppError(
        "ENGINE_UNAVAILABLE",
        "The transcription engine is not ready.",
        503,
    )


def transcription_failed() -> AppError:
    return AppError(
        "TRANSCRIPTION_FAILED",
        "Transcription failed.",
        500,
    )


def internal_error() -> AppError:
    return AppError(
        "INTERNAL_ERROR",
        "An unexpected error occurred.",
        500,
    )
