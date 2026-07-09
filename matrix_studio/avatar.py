# SPDX-License-Identifier: Apache-2.0
"""
Avatar generation module using a Stability text-to-image model on Amazon Bedrock.

LiteLLM does not route Bedrock image models, so we call bedrock-runtime directly
via boto3. Authentication uses the standard boto3 credential chain, which includes
the Bedrock API key / bearer token (``AWS_BEARER_TOKEN_BEDROCK``) as well as classic
IAM access keys and instance/role credentials. Gracefully falls back to ``None`` if
credentials are unavailable or generation fails, so a simulation never crashes just
because avatars could not be produced.
"""

import hashlib
import json
import logging
from typing import Optional

from matrix_studio.settings import get_settings

logger = logging.getLogger(__name__)

# Stability seed must fit in an unsigned 32-bit range.
_SEED_MODULUS = 2**32 - 1

# Non-photorealistic art styles. Each maps to a positive style clause; all share
# a strong anti-photorealism negative so avatars never read as photos of real
# people (misrepresentation / synthetic-media / accidental-likeness mitigation).
_STYLE_CLAUSES = {
    "illustration": "Flat vector illustration avatar, clean simple digital art, bold shapes",
    "anime": "Anime-style character portrait, cel-shaded digital art",
    "3d": "Stylized 3D cartoon character render, Pixar-like, soft shading",
}
_DEFAULT_STYLE = "illustration"
_ANTI_PHOTO_NEGATIVE = (
    "photorealistic, photograph, photo, realistic skin, real person, lifelike, "
    "deformed, distorted, disfigured, poor quality, low resolution"
)


def _deterministic_seed(persona_name: str) -> int:
    """Stable per-persona seed (independent of Python hash randomization)."""
    digest = hashlib.sha256(persona_name.encode("utf-8")).hexdigest()
    return int(digest, 16) % _SEED_MODULUS


async def generate_avatar(
    persona_name: str,
    persona_description: str,
    seed: Optional[int] = None,
) -> Optional[str]:
    """
    Generate an avatar portrait using a Stability image model on Bedrock.

    Args:
        persona_name: Name of the persona
        persona_description: Description of the persona for image generation
        seed: Optional explicit seed. When None (default) a stable per-persona
            seed is used so a given persona reproduces the same portrait. Pass
            an explicit (e.g. random) seed to intentionally regenerate a
            *different* portrait for the same persona.

    Returns:
        Base64-encoded PNG image string, or None if generation fails/unavailable
    """
    settings = get_settings()

    if not settings.enable_avatars:
        logger.info("Avatar generation disabled in settings")
        return None

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError:
        logger.warning("boto3 not available, cannot generate avatars")
        return None

    try:
        # Rely on the standard boto3 credential chain (bearer token / IAM keys /
        # role). Do NOT force explicit keys — that blocked bearer-token auth.
        client = boto3.client("bedrock-runtime", region_name=settings.avatar_region)

        style_key = (getattr(settings, "avatar_style", _DEFAULT_STYLE) or _DEFAULT_STYLE).lower()
        style_clause = _STYLE_CLAUSES.get(style_key, _STYLE_CLAUSES[_DEFAULT_STYLE])

        prompt = (
            f"{style_clause} of a fictional character named {persona_name}. "
            f"{persona_description}. Clearly non-photorealistic stylized avatar, "
            f"neutral background, centered head-and-shoulders composition."
        )

        # Stability SD3.x request shape (differs from the retired Nova Canvas API).
        body = json.dumps({
            "prompt": prompt,
            "negative_prompt": _ANTI_PHOTO_NEGATIVE,
            "mode": "text-to-image",
            "aspect_ratio": settings.avatar_aspect_ratio,
            "output_format": "png",
            "seed": seed if seed is not None else _deterministic_seed(persona_name),
        })

        response = client.invoke_model(
            modelId=settings.avatar_model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )

        result = json.loads(response["body"].read())

        # Stability returns {"images": [<base64 png>], "seeds": [...], "finish_reasons": [...]}
        images = result.get("images") or []
        finish = (result.get("finish_reasons") or [None])[0]
        if finish and finish != "SUCCESS":
            # Non-null finish reason (e.g. content filter) means no usable image.
            logger.warning(
                "Avatar generation filtered for %s: finish_reason=%s", persona_name, finish
            )
            return None
        if images:
            logger.info("Successfully generated avatar for %s", persona_name)
            return images[0]

        logger.warning(
            "Unexpected image response format for %s: %s", persona_name, list(result.keys())
        )
        return None

    except (NoCredentialsError, ClientError, BotoCoreError) as e:
        logger.warning("AWS error generating avatar for %s: %s", persona_name, e)
        return None
    except Exception as e:  # noqa: BLE001 - never let avatars crash a simulation
        logger.error("Unexpected error generating avatar for %s: %s", persona_name, e, exc_info=True)
        return None
