"""Process logging. Callers must not log audio bytes, transcripts, or secrets."""

import logging


def configure_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric)
    logging.getLogger("app").setLevel(numeric)
    if not root.handlers:
        logging.basicConfig(
            level=numeric,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )


def transcription_logger() -> logging.Logger:
    return logging.getLogger("app.transcription")
