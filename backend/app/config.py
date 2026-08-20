"""Application configuration.

Every tunable lives here and is read from the environment, so behaviour is
changed by editing .env rather than by editing code. The adapter providers,
the LLM provider and the retrieval mode are all single values, which is the
swappability property the adapter contract is assessed on.

Paths are resolved against the repository root, not the current working
directory, so `make run`, `pytest` and the eval harness all agree on where
the database lives regardless of where they were started from.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> backend/app -> backend -> repository root
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Typed view over the environment. Invalid values fail at startup."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM provider --------------------------------------------------------
    llm_provider: Literal["gemini", "ollama", "fake"] = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"

    # Gemini's free tier allows 15 requests per minute. Staying under a known
    # limit is better than discovering it and backing off.
    gemini_requests_per_minute: int = 15

    llm_temperature: float = 0.0
    llm_max_retries: int = 3
    llm_timeout_seconds: int = 90
    llm_cache_enabled: bool = True

    # --- Storage -------------------------------------------------------------
    db_path: Path = Path("data/meetings.db")
    write_log_path: Path = Path("write_log/tracker_writes.jsonl")
    digest_output_dir: Path = Path("data/digests")
    outcome_record_dir: Path = Path("data/outcome_records")
    llm_cache_dir: Path = Path("data/llm_cache")
    audio_dir: Path = Path("data/audio")

    sample_data_dir: Path = Path("sample_data")
    schema_path: Path = Path("backend/app/db/schema.sql")

    # --- Adapters ------------------------------------------------------------
    tracker_provider: Literal["mock"] = "mock"
    store_provider: Literal["mock"] = "mock"
    notifier_provider: Literal["mock"] = "mock"

    # --- Retrieval -----------------------------------------------------------
    retrieval_mode: Literal["keyword", "dense", "hybrid"] = "hybrid"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    faiss_index_dir: Path = Path("data/faiss")
    retrieval_top_k: int = 8
    rrf_k: int = 60

    # --- Chunking ------------------------------------------------------------
    chunk_max_tokens: int = 1200
    chunk_overlap_tokens: int = 200

    # --- Approval gate -------------------------------------------------------
    pending_expiry_hours: int = 72

    # --- Scheduler -----------------------------------------------------------
    scheduler_enabled: bool = True
    scheduler_timezone: str = "Asia/Colombo"
    digest_hour: int = Field(default=18, ge=0, le=23)
    digest_minute: int = Field(default=0, ge=0, le=59)
    expiry_sweep_hour: int = Field(default=2, ge=0, le=23)

    # --- Audio ---------------------------------------------------------------
    whisper_model: str = "base"
    whisper_compute_type: str = "int8"
    whisper_enabled: bool = True

    # --- Behaviour -----------------------------------------------------------
    resolve_relative_dates: bool = True

    # --- Service -------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    cors_origins: str = "http://localhost:5173"
    log_level: str = "INFO"

    # --- Derived -------------------------------------------------------------
    @field_validator(
        "db_path",
        "write_log_path",
        "digest_output_dir",
        "outcome_record_dir",
        "llm_cache_dir",
        "audio_dir",
        "sample_data_dir",
        "schema_path",
        "faiss_index_dir",
        mode="after",
    )
    @classmethod
    def _absolutise(cls, value: Path) -> Path:
        """Resolve every relative path against the repository root."""
        return value if value.is_absolute() else (REPO_ROOT / value)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def ensure_directories(self) -> None:
        """Create the runtime directories. Called once at startup and by seed."""
        for path in (
            self.db_path.parent,
            self.write_log_path.parent,
            self.digest_output_dir,
            self.outcome_record_dir,
            self.llm_cache_dir,
            self.audio_dir,
            self.faiss_index_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Tests override by clearing the cache."""
    return Settings()


settings = get_settings()
