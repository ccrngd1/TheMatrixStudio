# SPDX-License-Identifier: Apache-2.0
"""
Avatar generation module using Amazon Nova Canvas via boto3.

LiteLLM does not route Bedrock image models, so we use direct boto3 calls.
Gracefully falls back to None if AWS credentials are not available.
"""

import json
import logging
from typing import Optional

from matrix_studio.settings import get_settings

logger = logging.getLogger(__name__)


async def generate_avatar(persona_name: str, persona_description: str) -> Optional[str]:
    """
    Generate an avatar portrait using Amazon Nova Canvas.

    Args:
        persona_name: Name of the persona
        persona_description: Description of the persona for image generation

    Returns:
        Base64-encoded PNG image string, or None if generation fails/unavailable
    """
    settings = get_settings()

    if not settings.enable_avatars:
        logger.info("Avatar generation disabled in settings")
        return None

    # Check if AWS credentials are available
    if not settings.aws_access_key_id or not settings.aws_secret_access_key:
        logger.warning("AWS credentials not available, skipping avatar generation")
        return None

    try:
        import boto3
        from botocore.exceptions import ClientError, NoCredentialsError

        # Create Bedrock Runtime client
        client = boto3.client(
            "bedrock-runtime",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )

        # Construct the prompt for a portrait
        prompt = f"Professional portrait photograph of {persona_name}. {persona_description}. High quality, photorealistic, neutral background, centered composition."

        # Nova Canvas request body
        body = json.dumps({
            "taskType": "TEXT_IMAGE",
            "textToImageParams": {
                "text": prompt,
                "negativeText": "deformed, distorted, disfigured, poor quality, cartoon, anime, illustration, low resolution"
            },
            "imageGenerationConfig": {
                "numberOfImages": 1,
                "height": settings.avatar_height,
                "width": settings.avatar_width,
                "cfgScale": 8.0,
                "seed": hash(persona_name) % (2**31)  # Deterministic seed per persona name
            }
        })

        # Invoke the model
        response = client.invoke_model(
            modelId=settings.avatar_model_id,
            body=body,
            contentType="application/json",
            accept="application/json"
        )

        # Parse response - handle Nova Canvas response shape defensively
        result = json.loads(response["body"].read())

        # Nova Canvas returns images as a list of base64 strings
        if "images" in result and len(result["images"]) > 0:
            image_b64 = result["images"][0]
            logger.info(f"Successfully generated avatar for {persona_name}")
            return image_b64
        else:
            logger.warning(f"Unexpected Nova Canvas response format for {persona_name}: {list(result.keys())}")
            return None

    except (NoCredentialsError, ClientError) as e:
        logger.warning(f"AWS error generating avatar for {persona_name}: {e}")
        return None
    except ImportError:
        logger.warning("boto3 not available, cannot generate avatars")
        return None
    except Exception as e:
        logger.error(f"Unexpected error generating avatar for {persona_name}: {e}", exc_info=True)
        return None
