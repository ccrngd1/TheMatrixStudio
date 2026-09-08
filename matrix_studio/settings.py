# SPDX-License-Identifier: Apache-2.0
"""
Configuration settings for TheMatrix Simulation Studio.

Settings precedence: environment variables > .env file > config.json defaults
"""

import os
from pathlib import Path
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def project_root() -> Optional[Path]:
    """
    The checkout root, identified by the ``pyproject.toml`` above the package.

    Returns None when the package is installed rather than run from a checkout
    (site-packages has no ``pyproject.toml`` above it), in which case relative
    paths keep resolving against the working directory.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


class Settings(BaseSettings):
    """Global settings for the simulation engine."""

    model_config = SettingsConfigDict(
        env_file=".env" if not os.getenv("_MSS_TEST_MODE") else None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False
    )

    # LiteLLM model configuration
    litellm_model: str = Field(
        default="bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0",
        description="LiteLLM model string (e.g., openai/gpt-4o, anthropic/..., bedrock/...)"
    )
    litellm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    litellm_max_tokens: int = Field(default=2048, ge=1)
    # The analyst summary needs its OWN budget, not the per-turn utterance one.
    # Measured: a 24-turn run's five-field structured summary overflowed 2048 and was
    # truncated mid-value, so a strict parse returned nothing and the UI showed an
    # empty summary with a JSON blob in the overview. A turn is 2-4 sentences; a
    # summary is a whole analysis of the transcript. Sharing one number was the bug.
    summary_max_tokens: int = Field(default=8000, ge=1)

    # Selectable models offered in the UI (new-run form + in-thread analysis /
    # branch pickers). Comma-separated model strings; env AVAILABLE_MODELS. The
    # current ``litellm_model`` default is always included by ``/api/models``
    # even if omitted here. Keys stay server-side; this is just the allowlist of
    # model strings a user may pick.
    available_models: str = Field(
        default="",
        description="Comma-separated selectable model strings (AVAILABLE_MODELS).",
    )

    @property
    def available_model_list(self) -> list[str]:
        """Parsed, de-duplicated selectable models with the default first."""
        out: list[str] = [self.litellm_model]
        for m in self.available_models.split(","):
            m = m.strip()
            if m and m not in out:
                out.append(m)
        return out

    # AWS credentials (for Bedrock and Nova Canvas)
    aws_access_key_id: Optional[str] = Field(default=None)
    aws_secret_access_key: Optional[str] = Field(default=None)
    aws_region: str = Field(default="us-east-1")

    # OpenAI API key
    openai_api_key: Optional[str] = Field(default=None)

    # Anthropic API key
    anthropic_api_key: Optional[str] = Field(default=None)

    # Simulation defaults
    max_messages: int = Field(default=20, ge=1, description="Default max turns per simulation")
    # Phase 4a: priority-hierarchy validation gate (pre-emit). ON by default;
    # OFF reproduces pre-4a behavior byte-for-byte (no validation events, no
    # extra calls, identical outputs) — regression-locked by test.
    validation_enabled: bool = Field(
        default=True,
        description="Pre-emit priority-hierarchy validation gate (VALIDATION_ENABLED). "
        "OFF = byte-for-byte pre-4a behavior.",
    )
    validation_retry_budget: int = Field(
        default=1,
        ge=0,
        description="How many times a validation-rejected turn is regenerated before "
        "being emitted with a validation.flagged event (never rewritten in place).",
    )
    # Phase 4c (EXPERIMENTAL): adaptive-pressure intervention. OFF by default;
    # while off the adaptive_pressure branch-mutation kind is refused entirely.
    adaptive_pressure_enabled: bool = Field(
        default=False,
        description="Enable the experimental adaptive_pressure branch mutation "
        "(ADAPTIVE_PRESSURE_ENABLED). Pressure modulates the world only; the "
        "agency guard rejects any output that negates participant choice.",
    )
    # Phase 4d: optional structured output view (Narrative/Consequences/State/
    # Possibilities). OFF by default; a derived read-only projection over the
    # canonical events — enabling it changes nothing about events/snapshots.
    structured_output: bool = Field(
        default=False,
        description="Serve the 4-section structured turn view endpoint "
        "(STRUCTURED_OUTPUT). Derived view only; canonical events unchanged.",
    )
    max_run_cost_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="Per-run hard cost cap in USD (0 = OFF, no cap). "
        "Engine checks accumulated real cost after each turn; when cap is reached, "
        "run ends with status 'capped'. Additive, opt-in feature; 0 = pre-Phase-3 behavior.",
    )
    cost_warn_threshold: float = Field(
        default=1.0,
        ge=0.0,
        description="Cost warning threshold in USD (shown in UI cost meter)",
    )

    # Uploaded knowledge-base files. Both caps are enforced SERVER-side, because a
    # browser check is advice and this endpoint accepts arbitrary bytes.
    #
    # The byte cap bounds what a single request can make the server buffer and
    # extract; it is checked while streaming, so an oversized upload is refused
    # before it is held in full. The char cap bounds what one document can add to a
    # run: text is chunked and only the top-k chunks ever reach a prompt, so a big
    # document is legitimate — but it still costs database space and ingest time, and
    # a PDF can expand to far more text than its byte size suggests.
    max_upload_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1024,
        description="Largest single knowledge-base file accepted (MAX_UPLOAD_BYTES).",
    )
    max_document_chars: int = Field(
        default=400_000,
        ge=1000,
        description="Largest extracted text accepted from one file (MAX_DOCUMENT_CHARS).",
    )

    # Storage
    data_dir: str = Field(default="./data", description="Directory for SQLite database")

    @property
    def resolved_data_dir(self) -> Path:
        """
        ``data_dir`` as an absolute path.

        A relative ``data_dir`` resolves against the checkout root, NOT the working
        directory. Measured cost of the old cwd-relative behaviour: starting the
        server from a subdirectory silently created a second, empty database and
        the UI reported no previous conversations — a cwd mistake was
        indistinguishable from data loss. An absolute ``DATA_DIR`` is honoured
        as-is, which is how the container passes ``/app/data``.
        """
        path = Path(self.data_dir).expanduser()
        if path.is_absolute():
            return path
        root = project_root()
        return (root / path).resolve() if root else path.resolve()

    @property
    def db_file(self) -> Path:
        """Absolute path to the SQLite database."""
        return self.resolved_data_dir / "matrix_studio.db"

    # Server settings (for Phase 1)
    matrix_port: int = Field(default=8000, ge=1, le=65535)
    matrix_host: str = Field(default="127.0.0.1")

    # Avatar generation
    enable_avatars: bool = Field(default=True, description="Enable avatar generation via Stability image model on Bedrock")
    avatar_model_id: str = Field(default="stability.sd3-5-large-v1:0")
    # Image models are not always available in the same region as the text model;
    # SD3.5 Large is served from us-west-2.
    avatar_region: str = Field(default="us-west-2")
    avatar_aspect_ratio: str = Field(default="1:1")
    # Avatar art style. Default is a NON-photorealistic illustration so avatars
    # read as clearly-synthetic characters, not photos of real people (avoids
    # misrepresentation / synthetic-media-labeling / accidental-likeness risk).
    # Options: 'illustration' (flat vector), 'anime', '3d' (stylized cartoon
    # render). Unknown values fall back to 'illustration'.
    avatar_style: str = Field(default="anime", description="Avatar art style (env AVATAR_STYLE)")


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create the global settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
