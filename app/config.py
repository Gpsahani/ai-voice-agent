"""Environment settings. One cached instance per process."""

import os
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    max_upload_bytes: int = Field(default=26_214_400, gt=0)
    max_duration_seconds: float = Field(default=600, gt=0)
    chunk_seconds: float = Field(default=120, gt=0)
    chunk_overlap_seconds: float = Field(default=2, ge=0)
    temp_dir: str = ""
    whisper_model: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_language: str = ""
    ffprobe_timeout_seconds: float = Field(default=30, gt=0)
    ffmpeg_timeout_seconds: float = Field(default=60, gt=0)
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalized not in allowed:
            raise ValueError("LOG_LEVEL must be a standard library log level")
        return normalized

    @field_validator("whisper_model", "whisper_device", "whisper_compute_type")
    @classmethod
    def require_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("value must not be empty")
        return text

    @field_validator("whisper_language", "temp_dir")
    @classmethod
    def strip_optional(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def overlap_fits_chunk(self) -> "Settings":
        if self.chunk_overlap_seconds >= self.chunk_seconds:
            raise ValueError(
                "CHUNK_OVERLAP_SECONDS must be >= 0 and < CHUNK_SECONDS"
            )
        return self

    def temp_directory(self) -> str | None:
        if not self.temp_dir:
            return None
        os.makedirs(self.temp_dir, exist_ok=True)
        return self.temp_dir


@lru_cache
def get_settings() -> Settings:
    return Settings()
