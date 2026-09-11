# SPDX-License-Identifier: Apache-2.0
"""Phase 6 engine tests — structured personas inside the turn loop.

Proven by reading the ACTUAL prompts the engine sends, not by inspecting the
renderer (that is ``tests/test_personas.py``). The properties:

  (1) OFF by default: a cast member's ``structured`` block never reaches any
      prompt, and the speaker prompt is byte-identical to one from a cast with no
      structured block at all.
  (2) ON: the persona's own convictions, formative lessons and the re-tuned
      dismissal rule reach its OWN system prompt.
  (3) The withheld ``underlying_concern`` reaches its own prompt and NO OTHER
      prompt in the run — including the moderator's, which lists the whole cast.
  (4) ``validity`` reaches nothing, ever.
  (5) The ``persona.structured`` event records the seeding, with the private
      fields stripped.
  (6) A malformed ``structured`` block fails at run start rather than being
      silently ignored.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from matrix_studio.engine import run_simulation
from matrix_studio.storage import Database


class _Resp:
    def __init__(self, content):
        self.choices = [MagicMock(message=MagicMock(content=content))]
        self.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        self._hidden_params = {"response_cost": 0.001}


DANA_CONCERN = "I am the one who owns it when a customer never reaches a working run"
DANA_VALIDITY = "overgeneralised"
DANA_LESSON = "Every additional service costs you users before they see it work"

DANA_STRUCTURED = {
    "role": "Head of Distribution & Packaging",
    "background": {
        "tenure_years": 9,
        "prior_roles": ["Release engineering for an on-prem analytics product"],
        "formative_events": [
            {
                "year": 2023,
                "event": "A quickstart that required standing up a separate vector database",
                "lesson": DANA_LESSON,
            }
        ],
    },
    "preferences": {
        "optimises_for": ["time-to-first-run"],
        "dismisses": ["retrieval answer quality"],
        "persuaded_by": ["a working install on a clean machine"],
    },
    "viewpoints": [
        {
            "position": "No feature may add a stateful external service to the default install",
            "underlying_concern": DANA_CONCERN,
            "formed_by": "The 2023 product that stalled at the install step",
            "firmness": "firm",
            "evidence_that_shifts": ["an embedded index that is a file, not a service"],
            "validity": DANA_VALIDITY,
        }
    ],
}


def _request(*, structured=True, personas=None, max_messages=2):
    cast = [
        {"name": "Dana", "persona": "distribution lead", "goals": ["protect the install"]},
        {"name": "Marcus", "persona": "cost analyst", "goals": ["measure spend"]},
    ]
    if structured:
        cast[0]["structured"] = DANA_STRUCTURED
    config = {"max_messages": max_messages, "generate_avatars": False}
    if personas is not None:
        config["personas"] = personas
    return {
        "topic": "Should we add a document retrieval layer",
        "cast": cast,
        "config": config,
    }


def _make_fake(prompts, speakers=("Dana", "Marcus")):
    """Collect every prompt, tagged by which call it came from."""
    state = {"i": 0}

    def fake(*args, **kwargs):
        messages = kwargs["messages"]
        text = " ".join(m["content"] for m in messages)
        if "conversation moderator" in text:
            who = speakers[state["i"] % len(speakers)]
            state["i"] += 1
            prompts.append(("moderator", None, text))
            return _Resp(json.dumps({"speaker": who, "reason": "their turn"}))
        prompts.append(("speaker", speakers[(state["i"] - 1) % len(speakers)], messages[0]["content"]))
        return _Resp("A considered reply about the matter at hand.")

    return fake


def _speaker_prompts(prompts, name):
    return [p for kind, who, p in prompts if kind == "speaker" and who == name]


def _all_text(prompts):
    return "\n".join(p for _, _, p in prompts)


async def _events(db, run_id, event_type):
    rows = await db.get_events(run_id)
    out = []
    for r in rows:
        if r["event_type"] != event_type:
            continue
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload) if payload else {}
        out.append({"turn": r["turn"], "agent": r["agent_name"], "payload": payload})
    return out


async def _run(db, run_id, **kw):
    prompts = []
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake(prompts),
    ):
        await run_simulation(_request(**kw), db=db, run_id=run_id)
    return prompts


# --------------------------------------------------------------------------- #
# (1) OFF by default
# --------------------------------------------------------------------------- #


async def test_structured_off_by_default_reaches_no_prompt(db):
    prompts = await _run(db, "off")
    text = _all_text(prompts)
    assert DANA_CONCERN not in text
    assert DANA_LESSON not in text
    assert "What you do not weigh" not in text
    assert "Head of Distribution" not in text
    assert await _events(db, "off", "persona.structured") == []


async def test_off_prompt_is_identical_to_having_no_structured_block(db):
    """The strong form of "off means untouched": the same run with and without a
    structured block must produce the same speaker prompt, character for
    character."""
    with_block = await _run(db, "with", structured=True)
    without = await _run(db, "without", structured=False)
    assert _speaker_prompts(with_block, "Dana") == _speaker_prompts(without, "Dana")


# --------------------------------------------------------------------------- #
# (2) ON: convictions reach the speaker's own prompt
# --------------------------------------------------------------------------- #


async def test_enabled_puts_convictions_in_the_speakers_own_prompt(db):
    prompts = await _run(db, "on", personas={"enabled": True})
    dana = "\n".join(_speaker_prompts(prompts, "Dana"))
    assert "No feature may add a stateful external service" in dana
    assert "[firm]" in dana
    assert DANA_LESSON in dana
    assert "What would change your mind: an embedded index" in dana


async def test_enabled_renders_the_default_dismissal_rule(db):
    """Default variant is `mandatory` — declining is required, not permitted."""
    prompts = await _run(db, "dismiss", personas={"enabled": True})
    dana = "\n".join(_speaker_prompts(prompts, "Dana"))
    assert "What you do not weigh: retrieval answer quality" in dana
    assert "MUST say plainly that it is not yours to weigh" in dana
    assert "never let declining be your whole turn" in dana


async def test_the_rule_variant_reaches_the_prompt(db):
    """The variant is a measurement lever, so it has to actually change the prompt
    — an arm that silently rendered the default wording would test nothing."""
    prompts = await _run(
        db, "blunt", personas={"enabled": True, "dismissal_rule": "blunt"}
    )
    dana = "\n".join(_speaker_prompts(prompts, "Dana"))
    assert "Ignore the things you consider not your problem" in dana
    assert "MUST say plainly" not in dana


async def test_rule_off_drops_the_dismisses_list_too(db):
    prompts = await _run(
        db, "ruleoff", personas={"enabled": True, "dismissal_rule": "off"}
    )
    dana = "\n".join(_speaker_prompts(prompts, "Dana"))
    assert "What you do not weigh" not in dana
    # ...but the rest of the structured persona is still there
    assert "No feature may add a stateful external service" in dana


async def test_structure_does_not_bleed_into_another_personas_prompt(db):
    """Marcus has no structured block; enabling the feature must not give him one."""
    prompts = await _run(db, "scoped", personas={"enabled": True})
    marcus = "\n".join(_speaker_prompts(prompts, "Marcus"))
    assert "No feature may add a stateful external service" not in marcus
    assert "What you do not weigh" not in marcus


async def test_works_with_cognition_off(db):
    """The premise validation ran with cognition OFF in all three arms, so that
    is the configuration the evidence covers — it must be the one that works."""
    prompts = await _run(db, "nocog", personas={"enabled": True})
    dana = "\n".join(_speaker_prompts(prompts, "Dana"))
    assert "Positions you hold" in dana
    # no JSON schema instruction => cognition really is off
    assert "Return ONLY a JSON object" not in dana


# --------------------------------------------------------------------------- #
# (3) + (4) leakage, measured against every prompt in the run
# --------------------------------------------------------------------------- #


async def test_withheld_concern_reaches_its_own_prompt_and_nothing_else(db):
    prompts = await _run(db, "withheld", personas={"enabled": True})
    dana = _speaker_prompts(prompts, "Dana")
    assert any(DANA_CONCERN in p for p in dana), "the persona must know its own concern"
    others = [p for kind, who, p in prompts if not (kind == "speaker" and who == "Dana")]
    for p in others:
        assert DANA_CONCERN not in p, "the withheld concern leaked into another prompt"


async def test_moderator_prompt_gets_the_public_summary_but_not_the_concern(db):
    prompts = await _run(db, "mod", personas={"enabled": True})
    moderator = "\n".join(p for kind, _, p in prompts if kind == "moderator")
    assert "Head of Distribution & Packaging" in moderator
    assert "cares most about time-to-first-run" in moderator
    assert DANA_CONCERN not in moderator
    assert DANA_VALIDITY not in moderator


async def test_validity_reaches_no_prompt_in_the_run(db):
    prompts = await _run(db, "validity", personas={"enabled": True})
    assert DANA_VALIDITY not in _all_text(prompts)


async def test_withhold_concerns_false_still_keeps_it_out_of_other_prompts(db):
    """Turning withholding off makes the persona free to SAY it. It must not make
    the engine tell everyone else."""
    prompts = await _run(
        db, "nowithhold", personas={"enabled": True, "withhold_concerns": False}
    )
    dana = "\n".join(_speaker_prompts(prompts, "Dana"))
    assert DANA_CONCERN in dana
    assert "Do not volunteer" not in dana
    others = [p for kind, who, p in prompts if not (kind == "speaker" and who == "Dana")]
    for p in others:
        assert DANA_CONCERN not in p


# --------------------------------------------------------------------------- #
# (5) the audit event
# --------------------------------------------------------------------------- #


async def test_persona_structured_event_records_the_seeding_without_private_fields(db):
    await _run(db, "event", personas={"enabled": True})
    events = await _events(db, "event", "persona.structured")
    assert [e["agent"] for e in events] == ["Dana"], "only cast members with structure"
    payload = events[0]["payload"]
    assert payload["withhold_concerns"] is True
    assert payload["dismissal_rule"] == "mandatory"
    flat = json.dumps(payload)
    assert "stateful external service" in flat
    assert DANA_CONCERN not in flat
    assert DANA_VALIDITY not in flat


# --------------------------------------------------------------------------- #
# branch continuity
# --------------------------------------------------------------------------- #


async def test_convictions_survive_reconstruction_at_a_fork(db):
    """A branch asking "what if they had held firm" needs the convictions to
    exist on the other side of the fork, or it answers a different question."""
    from matrix_studio.branching import reconstruct_at_turn

    await _run(db, "fork", personas={"enabled": True})
    run = await db.get_run("fork")
    _topic, agents, _conversation, _threads, _cites = await reconstruct_at_turn(
        db, run, 1
    )
    dana = agents["Dana"]
    assert dana.structured is not None
    assert dana.structured.viewpoints[0].firmness == "firm"
    # including the private field, or a resumed run would stop withholding it
    assert dana.structured.viewpoints[0].underlying_concern == DANA_CONCERN
    assert agents["Marcus"].structured is None


# --------------------------------------------------------------------------- #
# (6) a malformed block fails loudly
# --------------------------------------------------------------------------- #


async def test_bad_firmness_fails_at_run_start_even_with_the_feature_off(db):
    """Parsed always, so a typo surfaces immediately instead of the day someone
    turns the flag on and wonders why nothing changed."""
    request = _request(structured=False)
    request["cast"][0]["structured"] = {
        "viewpoints": [{"position": "x", "firmness": "rock solid"}]
    }
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake([]),
    ):
        with pytest.raises(ValidationError):
            await run_simulation(request, db=db, run_id="bad")
