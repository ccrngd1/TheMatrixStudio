# SPDX-License-Identifier: Apache-2.0
"""
Configuration settings for TheMatrix Simulation Studio.

Settings precedence: environment variables > .env file > config.json defaults
"""

import logging
import os
from pathlib import Path
from typing import Optional, Sequence
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


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
    # The conversation model. **Sonnet 5**, changed from Haiku 4.5 on 2026-09-13.
    #
    # Haiku was the default because it is cheap, and that was the right call while nothing
    # depended on the output being comparable. It is the wrong one now: every Phase 6
    # measurement in `docs/` — the dismissal-rate work, the validation arms, the
    # `distinct_positions` instability — was run on Sonnet 5, so a run on Haiku produces
    # behaviour that cannot be read against any recorded number.
    #
    # This is the model for the roles where quality is the product. It is deliberately NOT
    # the model for every call: Sonnet 5 accepts only `temperature=1`, so the validation
    # gate (0.0) and speaker selection (0.3) keep a temperature-honouring model of their
    # own. See `matrix_studio/models.py`, which is where that decision lives.
    litellm_model: str = Field(
        default="bedrock/global.anthropic.claude-sonnet-5",
        description="Conversation model — the LiteLLM string for the roles where quality "
        "is the product (persona voice, summary, the persona wizard). Per-role overrides "
        "live in a run's `config.models`; see matrix_studio/models.py. LITELLM_MODEL.",
    )
    litellm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    litellm_max_tokens: int = Field(default=2048, ge=1)
    # The analyst summary needs its OWN budget, not the per-turn utterance one.
    # Measured: a 24-turn run's five-field structured summary overflowed 2048 and was
    # truncated mid-value, so a strict parse returned nothing and the UI showed an
    # empty summary with a JSON blob in the overview. A turn is 2-4 sentences; a
    # summary is a whole analysis of the transcript. Sharing one number was the bug.
    #
    # 16000, raised from 8000 on 2026-10-02: two stored summaries with a long custom focus
    # were cut off at 8000 and lost their later fields. A normal run's summary is about
    # 3,400-6,000 output tokens, so 8000 had little room for a focus that asks for more.
    # - Ceiling: Sonnet 5, the summary's default model, allows 128,000 output tokens on
    #   Bedrock (Haiku 4.5, 64,000). The budget covers the model's reasoning as well as the
    #   reply: Sonnet 5 thinks by default, and those tokens come out of this number.
    # - Cost: output is billed for the tokens generated, not for the ceiling, so a summary
    #   that needs 5,000 costs the same as before. Only a summary that would have been cut
    #   off costs more, and that is the one that needed the room.
    # - Time is the real limit. At the slowest output rate recorded on the deployed stack,
    #   ~30 tokens/s (see `analysis.ASIDE_MAX_TOKENS`), all 16000 take ~9 minutes. Both
    #   workers that generate a deployed summary, finalise and aside, went to 15 minutes for
    #   it (`infra/matrix_infra/stack.py`, pinned by a template test that reads this line).
    #   15 is Lambda's maximum, so past ~18000 a full-length summary no longer fits with
    #   the 1.5x margin that test asks for.
    # - Used by any `analysis._acompletion` call that passes no budget, which is now only
    #   the summary. Asides and the stance classifier have their own budgets
    #   (`stance.CLASSIFIER_MAX_TOKENS`, 3000), and the ensemble's position extraction,
    #   which used to inherit this one, stays at 8000 (`ensemble.EXTRACT_MAX_TOKENS`).
    summary_max_tokens: int = Field(default=16000, ge=1)

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
    # Per-USER monthly cap, distinct from the per-run one above and for a different
    # reason. §7 of the architecture calls this a rollout prerequisite — "a company will
    # require this before rollout" — because a per-run cap bounds one conversation and
    # nothing bounds a user starting fifty of them.
    #
    # 0 = OFF, matching `max_run_cost_usd`, so a single-user install is unaffected.
    max_user_monthly_cost_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="Per-user monthly cost cap in USD (0 = OFF). Checked before a run "
        "starts — refusing to start is cheaper and clearer than stopping one mid-way "
        "(§7) — and again between turns, so a long or resumed run cannot outlive it. "
        "MAX_USER_MONTHLY_COST_USD.",
    )
    user_spend_caps_json: str = Field(
        default="",
        description="Optional per-Cognito-group caps as JSON, e.g. "
        '\'{"trial": 5, "staff": 100}\'. A user in several groups gets the HIGHEST cap '
        "of them. Falls back to MAX_USER_MONTHLY_COST_USD for a user in none. "
        "USER_SPEND_CAPS_JSON.",
    )

    def monthly_cap_for(self, groups: Optional[Sequence[str]] = None) -> float:
        """The monthly cap that applies to a user in ``groups``. 0 means no cap.

        **The highest cap among a user's groups wins, and that direction is deliberate.**
        Being added to a more generous tier must not leave someone held to a lower one
        they also belong to — a user in `trial` and `staff` is staff. The alternative
        (lowest wins) makes group membership subtractive, so granting access would
        sometimes remove it, which nobody expects and which is very hard to debug from
        the outside.

        A malformed `USER_SPEND_CAPS_JSON` falls back to the flat default rather than
        raising. It is read on a request path, and a typo in an environment variable
        should not take the deployment down — but it is logged loudly, because a cap
        silently not applying is the failure that costs money.
        """
        import json as _json

        default = float(self.max_user_monthly_cost_usd or 0.0)
        raw = (self.user_spend_caps_json or "").strip()
        if not raw or not groups:
            return default
        try:
            table = _json.loads(raw)
            if not isinstance(table, dict):
                raise ValueError("not a JSON object")
        except (ValueError, TypeError) as exc:
            logger.warning(
                "USER_SPEND_CAPS_JSON could not be parsed (%s); falling back to the "
                "flat MAX_USER_MONTHLY_COST_USD of %s. Per-group caps are NOT being "
                "applied.", exc, default,
            )
            return default

        caps = [
            float(table[g]) for g in (str(x) for x in groups)
            if g in table and isinstance(table[g], (int, float))
        ]
        if not caps:
            return default
        # A group whose cap is 0 means "no cap" for that group, matching the flag's
        # meaning everywhere else — so it wins outright rather than reading as zero
        # budget, which would lock out the very group somebody exempted.
        if any(c <= 0 for c in caps):
            return 0.0
        return max(caps)

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

    # Multi-tenancy. Every run belongs to one user; this only decides where that
    # user's identity comes from.
    #
    # "single-user" — no authentication; every request is `identity.LOCAL_USER_SUB`.
    #   What this tool has always been, and what the local dev server stays.
    # "jwt" — the identity must arrive as verified claims from an API Gateway
    #   authorizer, and an unauthenticated request is a 401.
    #
    # The default is the permissive value, which is only defensible because the
    # server binds 127.0.0.1 by default. Verified claims take precedence in BOTH
    # modes, so putting an authorizer in front starts attributing runs correctly
    # even if this setting is forgotten — the failure mode that would otherwise
    # merge every user's history into one bucket while appearing to work.
    auth_mode: str = Field(
        default="single-user",
        description="Where the caller's identity comes from: 'single-user' (no "
        "auth, one implicit local user) or 'jwt' (API Gateway verified claims "
        "required). AUTH_MODE.",
    )

    @field_validator("auth_mode")
    @classmethod
    def _known_auth_mode(cls, value: str) -> str:
        """Reject an unrecognised mode at startup rather than at the first request.

        A typo like ``AUTH_MODE=JWT`` must not silently land on the permissive
        branch: that is a whole-system authorisation bypass caused by a
        capitalisation error, discovered by nobody.
        """
        from matrix_studio.tenancy import AUTH_MODES

        if value not in AUTH_MODES:
            raise ValueError(
                f"auth_mode must be one of {AUTH_MODES}, got {value!r}"
            )
        return value

    # Startup stale-run sweep. On a single long-lived server this is correct and
    # necessary: no run can have a live background task in a fresh process, so any
    # row still marked `running` was orphaned by a crash and would otherwise show
    # as live forever.
    #
    # It must be OFF wherever many short-lived processes serve the same database —
    # a Lambda, or any horizontally-scaled deployment. The sweep's premise ("this
    # is the only process") is false there, and two concurrent cold starts would
    # each conclude the other's in-flight run was orphaned and mark it interrupted.
    # That is not a slow path; it is one request killing another user's live run.
    startup_sweep: bool = Field(
        default=True,
        description="Run the orphaned-run sweep at startup (STARTUP_SWEEP). Must "
        "be false on Lambda or any multi-process deployment — see settings.py.",
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
