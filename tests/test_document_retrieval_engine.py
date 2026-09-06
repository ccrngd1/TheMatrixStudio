# SPDX-License-Identifier: Apache-2.0
"""Phase 5 engine tests — document retrieval inside the turn loop.

The properties proven here, by reading the ACTUAL prompts the engine sends:

  (1) retrieval OFF (default): no document events, no ``document_refs``, and no
      document block in any prompt — a pre-Phase-5 run is unchanged.
  (2) retrieval ON: a persona's own document text reaches its prompt, the
      retrieved chunk ids are recorded as ``document_refs``, and a
      ``document.retrieved`` audit event carries the query and the passages.
  (3) scoping is real: persona A's document never appears in persona B's prompt.
  (4) the character budget holds in the live prompt, not just in the unit test.
  (5) retrieval works with cognition OFF — the feature is independent of it.
  (6) ingestion failures degrade: a missing file emits document.failed and the
      run still completes.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.engine import run_simulation
from matrix_studio.storage import Database


@pytest.fixture
async def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()
    Path(db_path).unlink(missing_ok=True)


class _Resp:
    def __init__(self, content):
        self.choices = [MagicMock(message=MagicMock(content=content))]
        self.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        self._hidden_params = {"response_cost": 0.001}


DANA_TEXT = (
    "Egress inspection provides auditable evidence for the compliance auditor.\n\n"
    "A single choke point is what makes exfiltration visible in the logs."
)
MARCUS_TEXT = (
    "Token accounting must show a measured delta before a feature ships.\n\n"
    "Unexplained spend during a demonstration ends the conversation."
)


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def _request(tmp_path, *, retrieval=None, cognition=None, docs=True, max_messages=2):
    cast = [
        {
            "name": "Dana",
            "persona": "distribution lead",
            "goals": ["protect the install"],
        },
        {
            "name": "Marcus",
            "persona": "cost analyst",
            "goals": ["measure spend"],
        },
    ]
    if docs:
        cast[0]["documents"] = [_write(tmp_path, "dana.md", DANA_TEXT)]
        cast[1]["documents"] = [_write(tmp_path, "marcus.md", MARCUS_TEXT)]
    config = {"max_messages": max_messages, "generate_avatars": False}
    if retrieval is not None:
        config["retrieval"] = retrieval
    if cognition is not None:
        config["cognition"] = cognition
    # The topic deliberately shares vocabulary with the documents. FTS5 is
    # lexical: a topic about "retrieval layers" would legitimately match neither
    # document, and the test would then be asserting a model-quality question
    # rather than the wiring it is meant to prove.
    return {
        "topic": "Do we need egress inspection evidence or token accounting first",
        "cast": cast,
        "config": config,
    }


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


def _make_fake(prompts, speakers=("Dana", "Marcus")):
    """Collect every system prompt, alternating the selected speaker."""
    state = {"i": 0}

    def fake(*args, **kwargs):
        messages = kwargs["messages"]
        text = " ".join(m["content"] for m in messages)
        if "conversation moderator" in text:
            who = speakers[state["i"] % len(speakers)]
            state["i"] += 1
            return _Resp(json.dumps({"speaker": who, "reason": "their turn"}))
        prompts.append(messages[0]["content"])
        return _Resp("A considered reply about the matter at hand.")

    return fake


# --------------------------------------------------------------------------- #
# (1) retrieval OFF by default
# --------------------------------------------------------------------------- #


async def test_retrieval_off_by_default_changes_nothing(db, tmp_path):
    prompts = []
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake(prompts),
    ):
        await run_simulation(_request(tmp_path), db=db, run_id="off")

    assert await _events(db, "off", "document.retrieved") == []
    assert await _events(db, "off", "document.ingested") == []
    for ev in await _events(db, "off", "agent.response"):
        assert "document_refs" not in ev["payload"]
    assert prompts
    for p in prompts:
        assert "background material" not in p
        assert "Egress inspection" not in p
    # Nothing was even ingested, so no index rows exist.
    assert await db.count_documents("off") == 0


async def test_retrieval_explicitly_disabled_does_not_ingest(db, tmp_path):
    prompts = []
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake(prompts),
    ):
        await run_simulation(
            _request(tmp_path, retrieval={"enabled": False}), db=db, run_id="dis"
        )
    assert await db.count_documents("dis") == 0


# --------------------------------------------------------------------------- #
# (2) retrieval ON: document text reaches the prompt, refs + audit recorded
# --------------------------------------------------------------------------- #


async def test_document_text_reaches_the_speakers_prompt(db, tmp_path):
    prompts = []
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake(prompts, speakers=("Dana",)),
    ):
        await run_simulation(
            _request(tmp_path, retrieval={"enabled": True, "k": 2, "max_chars": 900}),
            db=db,
            run_id="on",
        )

    assert await db.count_documents("on") == 2, "cast documents were not ingested"
    joined = "\n".join(prompts)
    assert "background material" in joined
    assert "Egress inspection" in joined, "Dana's own document never reached her prompt"

    ingested = await _events(db, "on", "document.ingested")
    assert {e["agent"] for e in ingested} == {"Dana", "Marcus"}
    assert all(e["payload"]["chunk_count"] >= 1 for e in ingested)

    retrieved = await _events(db, "on", "document.retrieved")
    assert retrieved, "no document.retrieved audit event emitted"
    first = retrieved[0]["payload"]
    assert first["query"], "audit event did not record the query"
    assert first["passages"] and "title" in first["passages"][0]
    assert first["total_chars"] <= 900

    responses = await _events(db, "on", "agent.response")
    assert any("document_refs" in r["payload"] for r in responses)
    refs = [r["payload"]["document_refs"] for r in responses if "document_refs" in r["payload"]]
    assert all(isinstance(ids, list) and ids for ids in refs)


async def test_document_refs_match_the_retrieved_passages(db, tmp_path):
    prompts = []
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake(prompts, speakers=("Dana",)),
    ):
        await run_simulation(
            _request(tmp_path, retrieval={"enabled": True, "k": 2, "max_chars": 900}),
            db=db,
            run_id="refs",
        )
    retrieved = {e["turn"]: e["payload"] for e in await _events(db, "refs", "document.retrieved")}
    for resp in await _events(db, "refs", "agent.response"):
        if "document_refs" not in resp["payload"]:
            continue
        expected = [p["chunk_id"] for p in retrieved[resp["turn"]]["passages"]]
        assert resp["payload"]["document_refs"] == expected


# --------------------------------------------------------------------------- #
# (3) scoping is real at the prompt level
# --------------------------------------------------------------------------- #


async def test_persona_never_sees_another_personas_document(db, tmp_path):
    """The strongest scoping claim: proven against the real prompt, not SQL."""
    per_speaker: dict = {}

    def fake(*args, **kwargs):
        messages = kwargs["messages"]
        text = " ".join(m["content"] for m in messages)
        if "conversation moderator" in text:
            who = "Marcus" if len(per_speaker.get("Dana", [])) else "Dana"
            return _Resp(json.dumps({"speaker": who, "reason": "their turn"}))
        system = messages[0]["content"]
        who = "Dana" if system.startswith("distribution lead") else "Marcus"
        per_speaker.setdefault(who, []).append(system)
        return _Resp("A reply.")

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await run_simulation(
            _request(tmp_path, retrieval={"enabled": True, "k": 3, "max_chars": 900}),
            db=db,
            run_id="scope",
        )

    assert per_speaker.get("Dana") and per_speaker.get("Marcus")
    for prompt in per_speaker["Dana"]:
        assert "Token accounting" not in prompt, "Dana saw Marcus's document"
    for prompt in per_speaker["Marcus"]:
        assert "Egress inspection" not in prompt, "Marcus saw Dana's document"


# --------------------------------------------------------------------------- #
# (4) the budget holds in the live prompt
# --------------------------------------------------------------------------- #


async def test_budget_caps_document_text_in_the_prompt(db, tmp_path):
    """A large document must contribute at most max_chars to a call."""
    big = "\n\n".join(
        f"Paragraph {i} about egress inspection evidence and auditable logs."
        for i in range(300)
    )
    cast = [{
        "name": "Dana",
        "persona": "distribution lead",
        "goals": ["protect the install"],
        "documents": [_write(tmp_path, "big.md", big)],
    }]
    request = {
        "topic": "egress inspection evidence",
        "cast": cast,
        "config": {
            "max_messages": 1,
            "generate_avatars": False,
            "retrieval": {"enabled": True, "k": 5, "max_chars": 400},
        },
    }
    prompts = []
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake(prompts, speakers=("Dana",)),
    ):
        await run_simulation(request, db=db, run_id="budget")

    events = await _events(db, "budget", "document.retrieved")
    assert events
    assert events[0]["payload"]["total_chars"] <= 400
    # The document itself is far larger than what reached the prompt: that gap is
    # the feature working.
    doc = (await db.list_documents("budget"))[0]
    assert doc["char_count"] > 4000
    assert events[0]["payload"]["total_chars"] < doc["char_count"] / 5


# --------------------------------------------------------------------------- #
# (5) independent of cognition
# --------------------------------------------------------------------------- #


async def test_retrieval_works_with_cognition_off(db, tmp_path):
    prompts = []
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake(prompts, speakers=("Dana",)),
    ):
        await run_simulation(
            _request(
                tmp_path,
                retrieval={"enabled": True, "k": 2, "max_chars": 900},
                cognition={"enabled": False},
            ),
            db=db,
            run_id="nocog",
        )
    joined = "\n".join(prompts)
    assert "Egress inspection" in joined
    # Cognition off: no JSON schema in the prompt, so this is the plain path.
    assert "Return ONLY a JSON object" not in joined


async def test_retrieval_works_with_cognition_on(db, tmp_path):
    prompts = []

    def fake(*args, **kwargs):
        messages = kwargs["messages"]
        text = " ".join(m["content"] for m in messages)
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Dana", "reason": "her turn"}))
        prompts.append(messages[0]["content"])
        return _Resp(json.dumps({
            "utterance": "A reply.", "rationale": "because", "goal_served": "none",
        }))

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await run_simulation(
            _request(
                tmp_path,
                retrieval={"enabled": True, "k": 2, "max_chars": 900},
                cognition={"enabled": True, "memory": False, "reflection_every": 0},
            ),
            db=db,
            run_id="cog",
        )
    joined = "\n".join(prompts)
    assert "Egress inspection" in joined
    assert "Return ONLY a JSON object" in joined


# --------------------------------------------------------------------------- #
# (6) failure degrades, never fatal
# --------------------------------------------------------------------------- #


async def test_missing_document_emits_failure_but_run_completes(db, tmp_path):
    cast = [{
        "name": "Dana",
        "persona": "distribution lead",
        "goals": ["protect the install"],
        "documents": [str(tmp_path / "does-not-exist.pdf")],
    }]
    request = {
        "topic": "retrieval",
        "cast": cast,
        "config": {
            "max_messages": 1,
            "generate_avatars": False,
            "retrieval": {"enabled": True},
        },
    }
    prompts = []
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake(prompts, speakers=("Dana",)),
    ):
        result = await run_simulation(request, db=db, run_id="missing")

    assert result["status"] == "complete", "a bad document must not fail the run"
    failures = await _events(db, "missing", "document.failed")
    assert len(failures) == 1
    assert "not found" in failures[0]["payload"]["error"]
    assert await db.count_documents("missing") == 0


async def test_run_with_no_documents_and_retrieval_on_is_fine(db, tmp_path):
    prompts = []
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake(prompts, speakers=("Dana",)),
    ):
        result = await run_simulation(
            _request(tmp_path, retrieval={"enabled": True}, docs=False),
            db=db,
            run_id="nodocs",
        )
    assert result["status"] == "complete"
    assert await _events(db, "nodocs", "document.retrieved") == []
    for p in prompts:
        assert "background material" not in p


# --------------------------------------------------------------------------- #
# (7) Phase 5g: unsupported-claim disclosure
#
# The signal is about PROVENANCE ("nothing in front of you"), not evidentiary
# support — the engine only knows nothing was retrieved, and at measured recall
# the supporting passage often exists and was simply missed.
# --------------------------------------------------------------------------- #


def _no_match_request(tmp_path, **retrieval):
    """A run whose topic shares no vocabulary with the attached document, so
    retrieval genuinely returns nothing."""
    doc = tmp_path / "dana.md"
    doc.write_text(DANA_TEXT)
    cfg = {"enabled": True, "k": 3, "max_chars": 900}
    cfg.update(retrieval)
    return {
        "topic": "medieval falconry glove stitching techniques",
        "cast": [{
            "name": "Dana", "persona": "distribution lead",
            "goals": ["protect install"], "documents": [str(doc)],
        }],
        "config": {"max_messages": 1, "generate_avatars": False, "retrieval": cfg},
    }


async def test_disclosure_added_when_retrieval_finds_nothing(db, tmp_path):
    prompts = []
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake(prompts, speakers=("Dana",)),
    ):
        await run_simulation(
            _no_match_request(tmp_path, disclose_unsupported=True),
            db=db, run_id="disc",
        )
    joined = "\n".join(prompts)
    assert "NO source material in front of you" in joined
    assert "in your own words and in character" in joined
    assert "Do not invent a citation" in joined
    # And it is recorded, so transcript and log can be reconciled.
    events = await _events(db, "disc", "document.unsupported")
    assert len(events) == 1
    assert events[0]["agent"] == "Dana"
    assert events[0]["payload"]["query"]


async def test_disclosure_does_not_claim_the_corpus_lacks_support(db, tmp_path):
    """The wording must not assert an absence the engine cannot verify.

    At measured recall a supporting passage often exists and was simply missed,
    so "no documentation supports this" would be false a large fraction of the
    time — an honesty feature that lies is worse than none.
    """
    prompts = []
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake(prompts, speakers=("Dana",)),
    ):
        await run_simulation(
            _no_match_request(tmp_path, disclose_unsupported=True),
            db=db, run_id="wording",
        )
    joined = "\n".join(prompts).lower()
    for forbidden in (
        "no documentation supports",
        "nothing supports this",
        "unsupported by",
        "your documents do not",
    ):
        assert forbidden not in joined, f"prompt overclaims: {forbidden!r}"


async def test_no_disclosure_when_a_passage_was_retrieved(db, tmp_path):
    prompts = []
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake(prompts, speakers=("Dana",)),
    ):
        await run_simulation(
            _request(
                tmp_path,
                retrieval={"enabled": True, "k": 2, "max_chars": 900,
                           "disclose_unsupported": True},
            ),
            db=db, run_id="hit",
        )
    joined = "\n".join(prompts)
    assert "NO source material in front of you" not in joined
    assert "From your own background material" in joined
    assert await _events(db, "hit", "document.unsupported") == []


async def test_disclosure_off_by_default(db, tmp_path):
    prompts = []
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake(prompts, speakers=("Dana",)),
    ):
        await run_simulation(_no_match_request(tmp_path), db=db, run_id="off2")
    joined = "\n".join(prompts)
    assert "NO source material in front of you" not in joined
    assert await _events(db, "off2", "document.unsupported") == []


async def test_no_disclosure_when_retrieval_is_disabled(db, tmp_path):
    """Retrieval off must stay byte-for-byte unchanged, flag or no flag.

    An empty passage list means "retrieval is off" here, and must never be
    confused with "retrieval ran and found nothing".
    """
    prompts = []
    request = _no_match_request(tmp_path, disclose_unsupported=True)
    request["config"]["retrieval"]["enabled"] = False
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake(prompts, speakers=("Dana",)),
    ):
        await run_simulation(request, db=db, run_id="retroff")
    joined = "\n".join(prompts)
    assert "NO source material in front of you" not in joined
    assert await _events(db, "retroff", "document.unsupported") == []


async def test_disclosure_works_with_cognition_on(db, tmp_path):
    prompts = []

    def fake(*args, **kwargs):
        messages = kwargs["messages"]
        text = " ".join(m["content"] for m in messages)
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Dana", "reason": "her turn"}))
        prompts.append(messages[0]["content"])
        return _Resp(json.dumps({
            "utterance": "Speaking from experience here, not from a source.",
            "rationale": "because", "goal_served": "none",
        }))

    request = _no_match_request(tmp_path, disclose_unsupported=True)
    request["config"]["cognition"] = {
        "enabled": True, "memory": False, "reflection_every": 0,
    }
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await run_simulation(request, db=db, run_id="disccog")
    joined = "\n".join(prompts)
    assert "NO source material in front of you" in joined
    assert "Return ONLY a JSON object" in joined


async def test_disclosure_is_a_prompt_request_not_a_gate(db, tmp_path):
    """A model that ignores the request must not fail the turn.

    The in-prompt line is a courtesy; document.unsupported in the event log is
    the authoritative record.
    """
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_make_fake([], speakers=("Dana",)),
    ):
        result = await run_simulation(
            _no_match_request(tmp_path, disclose_unsupported=True),
            db=db, run_id="ignored",
        )
    assert result["status"] == "complete"
    # The mock never hedges, yet the event still records the true state.
    assert len(await _events(db, "ignored", "document.unsupported")) == 1
