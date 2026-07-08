# SPDX-License-Identifier: Apache-2.0
"""
Memorable run naming — topically-resonant two-word codenames.

Every simulation gets a stable, human-friendly identity (a two-word kebab-case
codename plus a one-line description) so users can find and reload it in the UI.

The name is produced by a single cheap ``litellm.acompletion`` call at run
creation, prompted for strict JSON. The output is validated against a regex; on
failure we retry once, then fall back to a random adjective-noun from a built-in
wordlist. Naming is convenience, never a hard dependency — it must NEVER block a
run (same principle as avatars).
"""

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

import litellm

from matrix_studio.settings import get_settings

logger = logging.getLogger(__name__)

# A valid codename is exactly two lowercase words joined by a single hyphen.
NAME_RE = re.compile(r"^[a-z]+-[a-z]+$")

# Generic/placeholder names we reject even if they match the regex.
_BANNED_NAMES = {
    "test-run",
    "new-sim",
    "new-run",
    "sample-run",
    "demo-run",
    "untitled-run",
    "foo-bar",
}

# Small built-in wordlists for the offline fallback. Deliberately neutral and
# evocative so a fallback name still reads like a real codename.
_ADJECTIVES: List[str] = [
    "amber", "azure", "brave", "bright", "calm", "clever", "crimson", "curious",
    "distant", "eager", "electric", "gentle", "golden", "hidden", "lucid",
    "mellow", "nimble", "quiet", "restless", "silent", "silver", "solemn",
    "steady", "swift", "vivid", "wandering", "whispered", "wild", "woven",
]
_NOUNS: List[str] = [
    "anchor", "beacon", "cipher", "compass", "current", "ember", "engine",
    "forest", "harbor", "horizon", "lantern", "meadow", "meridian", "mirror",
    "orbit", "prism", "quill", "river", "signal", "summit", "thread", "tide",
    "vector", "voyage", "willow", "window",
]


def _slugify(name: str) -> str:
    """Normalize a name into a lookup-friendly slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
    return slug.strip("-")


def _is_valid_name(name: str) -> bool:
    """True if ``name`` is a well-formed, non-generic codename."""
    return bool(NAME_RE.match(name)) and name not in _BANNED_NAMES


def _fallback_name(topic: str, index_hint: int = 0) -> str:
    """
    Deterministic-ish random adjective-noun from the built-in wordlist.

    We avoid ``random`` (kept reproducible / dependency-free) by hashing the
    topic plus an index hint, so retries with a different hint yield a different
    name without a stateful RNG.
    """
    h = abs(hash((topic, index_hint)))
    adj = _ADJECTIVES[h % len(_ADJECTIVES)]
    noun = _NOUNS[(h // len(_ADJECTIVES)) % len(_NOUNS)]
    return f"{adj}-{noun}"


def _fallback_description(topic: str) -> str:
    """One-line description derived from the topic string (<=12 words)."""
    words = topic.strip().split()
    if len(words) > 12:
        words = words[:12]
    text = " ".join(words)
    return text[:1].upper() + text[1:] if text else "A multi-agent simulation"


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    """Best-effort parse of a JSON object from a model response."""
    raw = raw.strip()
    # Strip common ```json fences.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Fall back to the first {...} block.
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


async def _ask_llm_for_name(
    topic: str,
    cast_names: List[str],
    model: str,
    avoid: Optional[List[str]] = None,
) -> Optional[Dict[str, str]]:
    """
    One short litellm call asking for a codename + description as strict JSON.

    Returns a validated ``{"name", "description"}`` dict, or None on any failure
    (bad JSON, invalid name, API error). Callers handle the fallback.
    """
    avoid = avoid or []
    avoid_clause = (
        f" Do not use any of these already-taken names: {', '.join(avoid)}."
        if avoid
        else ""
    )
    cast_clause = f" The participants are: {', '.join(cast_names)}." if cast_names else ""

    prompt = (
        "You name multi-agent simulation runs with memorable codenames.\n"
        f'The simulation topic is: "{topic}".{cast_clause}\n'
        "Invent a memorable two-word codename that evokes the THEME of this "
        "topic. It must be lowercase, hyphenated, exactly two words matching "
        "^[a-z]+-[a-z]+$ (for example an AI-ethics topic could be "
        '"trusted-robot", a hiking trip "summit-compass"). Avoid generic names '
        "like test-run or new-sim." + avoid_clause + "\n"
        "Also write a one-line description of at most 12 words.\n"
        'Respond with ONLY strict JSON, no prose, exactly: '
        '{"name": "<two-word-kebab>", "description": "<max 12 words>"}'
    )

    try:
        response = await litellm.acompletion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=60,
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001 - naming must never break a run
        logger.warning("Codename LLM call failed: %s", e)
        return None

    data = _extract_json(raw)
    if not isinstance(data, dict):
        logger.warning("Codename response was not JSON: %r", raw[:120])
        return None

    name = str(data.get("name", "")).strip().lower()
    description = str(data.get("description", "")).strip()

    if not _is_valid_name(name):
        logger.warning("Codename %r failed validation", name)
        return None

    if not description:
        description = _fallback_description(topic)

    return {"name": name, "description": description}


async def generate_run_name(
    topic: str,
    cast_names: Optional[List[str]] = None,
    model: Optional[str] = None,
    name_exists: Optional[Callable[[str], Any]] = None,
) -> Dict[str, str]:
    """
    Produce a unique, topically-resonant codename + description for a run.

    Strategy:
      1. Ask the LLM (strict JSON, regex-validated). Retry once on invalid.
      2. Ensure uniqueness via ``name_exists`` (async predicate); on collision,
         ask the model again telling it to avoid taken names, then disambiguate
         with a numeric suffix.
      3. On any LLM failure, fall back to a random wordlist name — NEVER block.

    Args:
        topic: The simulation topic (drives the theme).
        cast_names: Optional participant names for flavor.
        model: LiteLLM model string; defaults to the configured model.
        name_exists: Optional async callable ``(name) -> bool`` for uniqueness.

    Returns:
        Dict with ``name``, ``description``, ``slug``, and ``source``
        ("llm" or "fallback").
    """
    cast_names = cast_names or []
    model = model or get_settings().litellm_model

    async def _taken(candidate: str) -> bool:
        if name_exists is None:
            return False
        result = name_exists(candidate)
        if hasattr(result, "__await__"):
            result = await result
        return bool(result)

    taken: List[str] = []

    # Up to two LLM attempts (initial + one retry / collision-avoidance round).
    for attempt in range(2):
        result = await _ask_llm_for_name(topic, cast_names, model, avoid=taken)
        if result is None:
            continue
        name = result["name"]
        if not await _taken(name):
            return {
                "name": name,
                "description": result["description"],
                "slug": _slugify(name),
                "source": "llm",
            }
        # Collision: remember it and, if we can, disambiguate with a suffix.
        taken.append(name)
        for suffix in range(2, 6):
            candidate = f"{name}-{suffix}"
            if not await _taken(candidate):
                return {
                    "name": candidate,
                    "description": result["description"],
                    "slug": _slugify(candidate),
                    "source": "llm",
                }

    # Fallback: random wordlist name, guaranteed to resolve a unique handle.
    description = _fallback_description(topic)
    for hint in range(64):
        name = _fallback_name(topic, hint)
        if not await _taken(name):
            return {
                "name": name,
                "description": description,
                "slug": _slugify(name),
                "source": "fallback",
            }
        # Also try numeric suffixes on the last resort.
        for suffix in range(2, 6):
            candidate = f"{name}-{suffix}"
            if not await _taken(candidate):
                return {
                    "name": candidate,
                    "description": description,
                    "slug": _slugify(candidate),
                    "source": "fallback",
                }

    # Extremely unlikely: everything collided. Return a name anyway (unique
    # handle not guaranteed, but naming must never raise).
    name = _fallback_name(topic, 0)
    return {
        "name": name,
        "description": description,
        "slug": _slugify(name),
        "source": "fallback",
    }
