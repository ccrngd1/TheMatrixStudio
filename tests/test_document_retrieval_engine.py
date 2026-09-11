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


# --------------------------------------------------------------------------- #
# (8) Phase 5i: citation provenance in the live turn loop
# --------------------------------------------------------------------------- #


async def test_gate_rejects_citing_a_document_the_speaker_never_held(db, tmp_path):
    """The observed real failure, now caught pre-emit and regenerated.

    Dana holds dana.md only. Her first attempt asserts what marcus.md specifies —
    a document she has never retrieved. The 4a gate must reject it, regenerate,
    and the committed turn must be the clean second attempt.
    """
    attempts = {"n": 0}

    def fake(*args, **kwargs):
        messages = kwargs["messages"]
        text = " ".join(m["content"] for m in messages)
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Dana", "reason": "her turn"}))
        if "consistency validator" in text:
            return _Resp(json.dumps({"violation": True}))
        attempts["n"] += 1
        if attempts["n"] == 1:
            return _Resp(
                "Per marcus.md #0, token accounting is the gate here, so egress "
                "inspection can wait until the numbers land."
            )
        return _Resp(
            "Egress inspection gives us the auditable evidence we need, and that "
            "is the constraint I care about most this quarter."
        )

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await run_simulation(
            _request(
                tmp_path,
                retrieval={"enabled": True, "k": 2, "max_chars": 900},
                max_messages=1,
            ),
            db=db, run_id="cite-gate",
        )

    checked = await _events(db, "cite-gate", "validation.checked")
    violations = [c for c in checked if not c["payload"]["passed"]]
    assert violations, "the gate did not reject the illegitimate citation"
    assert violations[0]["payload"]["principle"] == "citation_integrity"
    assert "marcus.md" in violations[0]["payload"]["reason"]

    responses = await _events(db, "cite-gate", "agent.response")
    assert "marcus.md" not in responses[0]["payload"]["message"], (
        "the bad citation was committed instead of regenerated"
    )


async def test_firsthand_citation_is_recorded_as_provenance(db, tmp_path):
    def fake(*args, **kwargs):
        messages = kwargs["messages"]
        text = " ".join(m["content"] for m in messages)
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Dana", "reason": "her turn"}))
        return _Resp(
            "Per dana.md #0, egress inspection is what gives the auditor "
            "evidence, so that is the line I am holding."
        )

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await run_simulation(
            _request(
                tmp_path,
                retrieval={"enabled": True, "k": 2, "max_chars": 900},
                max_messages=1,
            ),
            db=db, run_id="cite-prov",
        )
    responses = await _events(db, "cite-prov", "agent.response")
    prov = responses[0]["payload"].get("citation_provenance")
    assert prov, "provenance was not recorded"
    assert prov[0]["kind"] == "firsthand"
    assert prov[0]["label"].startswith("dana.md")


async def test_honest_disclaimer_about_another_document_is_not_rejected(db, tmp_path):
    """Flagging this would punish the exact behaviour 5g is trying to produce."""
    def fake(*args, **kwargs):
        messages = kwargs["messages"]
        text = " ".join(m["content"] for m in messages)
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Dana", "reason": "her turn"}))
        if "consistency validator" in text:
            return _Resp(json.dumps({"violation": True}))
        return _Resp(
            "I haven't seen that marcus.md file you're referencing, so I will "
            "speak to what egress inspection actually buys us instead."
        )

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await run_simulation(
            _request(
                tmp_path,
                retrieval={"enabled": True, "k": 2, "max_chars": 900},
                max_messages=1,
            ),
            db=db, run_id="cite-honest",
        )
    checked = await _events(db, "cite-honest", "validation.checked")
    bad = [
        c for c in checked
        if not c["payload"]["passed"]
        and c["payload"].get("principle") == "citation_integrity"
    ]
    assert not bad, "an honest disclaimer was flagged as a citation violation"


async def test_no_citation_checking_when_retrieval_is_off(db, tmp_path):
    """Retrieval off means no citation context, so the check is skipped entirely."""
    def fake(*args, **kwargs):
        messages = kwargs["messages"]
        text = " ".join(m["content"] for m in messages)
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Dana", "reason": "her turn"}))
        return _Resp("Per some-invented-file.md, the answer is obviously yes here.")

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await run_simulation(_request(tmp_path, max_messages=1), db=db, run_id="cite-off")
    responses = await _events(db, "cite-off", "agent.response")
    assert "citation_provenance" not in responses[0]["payload"]
    checked = await _events(db, "cite-off", "validation.checked")
    assert all(
        c["payload"].get("principle") != "citation_integrity" for c in checked
    )


# --------------------------------------------------------------------------- #
# Phase 5i: the first-hand ledger is cross-turn state and must survive a
# resume, a branch and a per-turn Lambda boundary.
#
# It did not. `_run_turns` initialised it to `[]` at both call sites, so a
# resumed or branched run treated every legitimate second-hand credit as
# unverifiable. Latent while resume was rare; permanent under Phase 5, where
# every turn is a resume.
# --------------------------------------------------------------------------- #


async def test_firsthand_ledger_rides_the_snapshot(db, tmp_path):
    """The ledger has to be ON the snapshot, because that is what a turn Lambda loads.

    Reconstruction from the log is the fallback; the snapshot is the O(1) path Phase 2a
    chose precisely so a turn need not replay history.
    """
    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Dana", "reason": "her turn"}))
        return _Resp(
            "Per dana.md #0, egress inspection is what gives the auditor evidence."
        )

    # max_messages=2 on purpose: turn 1's snapshot is then the per-turn RUNNING one
    # (the write a turn Lambda's successor actually loads), not the terminal
    # completion snapshot. With max_messages=1 the two coincide and deleting the
    # field from the running write left the whole suite green.
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await run_simulation(
            _request(tmp_path, retrieval={"enabled": True, "k": 2, "max_chars": 900},
                     max_messages=2),
            db=db, run_id="cite-snap",
        )
    snap = await db.get_snapshot("cite-snap", 1)
    assert snap.status == "running", "meant to assert the per-turn write, not the final one"
    assert snap is not None
    titles = [title for _speaker, title in snap.firsthand_citations]
    assert "dana.md" in titles, (
        f"the snapshot lost the first-hand ledger: {snap.firsthand_citations}"
    )
    assert all(s == "Dana" for s, _t in snap.firsthand_citations)


async def test_firsthand_ledger_is_replayed_from_the_event_log(db, tmp_path):
    """reconstruct_at_turn must rebuild it, which is what fixes resume and branch."""
    from matrix_studio.branching import reconstruct_at_turn

    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Dana", "reason": "her turn"}))
        return _Resp(
            "Per dana.md #0, egress inspection is what gives the auditor evidence."
        )

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await run_simulation(
            _request(tmp_path, retrieval={"enabled": True, "k": 2, "max_chars": 900},
                     max_messages=1),
            db=db, run_id="cite-replay",
        )
    run = await db.get_run("cite-replay")
    *_rest, replayed = await reconstruct_at_turn(db, run, 1)
    snap = await db.get_snapshot("cite-replay", 1)
    # The two sources must AGREE. Either alone could be self-consistently wrong;
    # a snapshot written by the loop and a ledger replayed from the log are
    # independent derivations of the same fact.
    assert [list(p) for p in replayed] == [list(p) for p in snap.firsthand_citations]
    assert replayed, "replay produced an empty ledger"


async def test_provenance_carries_the_bare_title_not_only_the_label(db, tmp_path):
    """The replay keys on title, and `label` is "title #ordinal".

    Recovering the title by splitting the label is a guess about titles, and wrong
    for any document whose own name contains " #". So the event has to state it.
    """
    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Dana", "reason": "her turn"}))
        return _Resp("Per dana.md #0, egress inspection gives the auditor evidence.")

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await run_simulation(
            _request(tmp_path, retrieval={"enabled": True, "k": 2, "max_chars": 900},
                     max_messages=1),
            db=db, run_id="cite-title",
        )
    responses = await _events(db, "cite-title", "agent.response")
    prov = responses[0]["payload"]["citation_provenance"]
    assert prov[0]["title"] == "dana.md"
    # The label keeps its ordinal, so the two are genuinely different strings and
    # the test is not tautological.
    assert prov[0]["label"] == "dana.md #0"


async def test_a_second_turn_still_sees_the_first_turn_s_firsthand_citation(db, tmp_path):
    """The behaviour the ledger exists for, across a turn boundary.

    Dana cites dana.md first-hand on turn 1. On turn 2 Marcus credits Dana for it,
    which is legitimate ONLY because the ledger remembers turn 1. With an empty
    ledger the same utterance is an unverified citation and the gate rejects it —
    so this test fails if the ledger is dropped between turns, which is exactly
    what a per-turn Lambda does when the state is not persisted.
    """
    turns = {"n": 0}

    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            turns["n"] += 1
            speaker = "Dana" if turns["n"] == 1 else "Marcus"
            return _Resp(json.dumps({"speaker": speaker, "reason": "their turn"}))
        if "consistency validator" in text:
            return _Resp(json.dumps({"violation": False}))
        if "Dana" in text and "dana.md" in text and turns["n"] >= 2:
            # Marcus, crediting Dana second-hand.
            return _Resp(
                "Dana cited dana.md as saying egress inspection is the evidence, "
                "and I will take that at face value."
            )
        return _Resp("Per dana.md #0, egress inspection is the auditor's evidence.")

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await run_simulation(
            _request(tmp_path, retrieval={"enabled": True, "k": 2, "max_chars": 900},
                     max_messages=2),
            db=db, run_id="cite-carry",
        )

    snap = await db.get_snapshot("cite-carry", 2)
    assert snap is not None
    # Turn 1's entry must still be present at turn 2 — the ledger accumulates
    # rather than being rebuilt per turn.
    assert any(t == "dana.md" for _s, t in snap.firsthand_citations), (
        f"turn 1's first-hand citation was lost by turn 2: {snap.firsthand_citations}"
    )

    responses = await _events(db, "cite-carry", "agent.response")
    second = next((r for r in responses if r["turn"] == 2), None)
    if second is not None and "Dana cited dana.md" in second["payload"]["message"]:
        prov = second["payload"].get("citation_provenance") or []
        kinds = {p["kind"] for p in prov if p.get("title") == "dana.md"}
        assert "unverified" not in kinds, (
            "a legitimate second-hand credit was judged unverifiable, which means "
            f"the ledger did not survive the turn boundary: {prov}"
        )


async def test_a_branch_carries_the_parent_s_firsthand_ledger_into_new_turns(db, tmp_path):
    """THE test for the original bug, and the one the others missed.

    `_run_turns` used to do `firsthand_citations = []` unconditionally. On a fresh
    run that is indistinguishable from correct — the ledger legitimately starts
    empty and fills as the run proceeds — so every fresh-run test passes with the
    bug in place. The defect only shows where the ledger is *supplied*: a branch or
    a resume. Under Phase 5 that is every turn.

    So: parent cites dana.md first-hand at turn 1, branch at turn 1, let the branch
    generate turn 2, and require the parent's entry to still be in the branch's own
    turn-2 snapshot.
    """
    from matrix_studio import branching

    # The branch's generated turn must NOT cite dana.md again. If it did, its own
    # snapshot would hold the entry whether or not the parent's ledger was
    # inherited, and the test would pass with the bug in place — which is exactly
    # what the first version of it did.
    utterances = {"n": 0}

    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Dana", "reason": "her turn"}))
        if "consistency validator" in text:
            return _Resp(json.dumps({"violation": False}))
        utterances["n"] += 1
        if utterances["n"] == 1:
            return _Resp("Per dana.md #0, egress inspection is the auditor's evidence.")
        return _Resp("I have said my piece and will let the point stand for now.")

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await run_simulation(
            _request(tmp_path, retrieval={"enabled": True, "k": 2, "max_chars": 900},
                     max_messages=1),
            db=db, run_id="cite-parent",
        )
        parent = await db.get_run("cite-parent")
        meta = await branching.create_branch_run(db, parent, from_turn=1)
        branch_id = meta["run_id"]
        await branching.execute_branch(
            db, parent, branch_run_id=branch_id, from_turn=1,
            max_messages=meta["max_messages"],
        )

    fork = await db.get_snapshot(branch_id, 1)
    assert any(t == "dana.md" for _s, t in fork.firsthand_citations), (
        "the fork snapshot lost the parent's ledger"
    )

    # The generated turn is the part `_run_turns` owns, and the part the bug broke.
    generated = await db.get_snapshot(branch_id, 2)
    assert generated is not None, "the branch generated no turn, so nothing is proved"
    # Non-vacuity: turn 2 cited nothing, so the ONLY way dana.md can be here is
    # inheritance from the parent's ledger.
    turn2 = [r for r in await _events(db, branch_id, "agent.response") if r["turn"] == 2]
    assert turn2, "no generated response at turn 2"
    assert "dana.md" not in turn2[0]["payload"]["message"], (
        "the branch's own turn cited dana.md, so this test cannot distinguish "
        "inheritance from a fresh citation"
    )
    assert any(t == "dana.md" for _s, t in generated.firsthand_citations), (
        "the branch's generated turn dropped the inherited ledger: "
        f"{generated.firsthand_citations}"
    )


async def test_replay_admits_only_firsthand_entries(db):
    """A second-hand credit must not vouch for the next one.

    The ledger answers "did this participant actually read that document". If
    replay folded a `secondhand` entry in, an unverified chain would bootstrap
    itself: Marcus credits Dana, that credit enters the ledger, and now Priya can
    credit Marcus for a document nobody ever retrieved.

    Written against the log directly rather than through a run, because the point
    is the filter, and a generated conversation is a slow and indirect way to
    control what kinds of citation appear.
    """
    from matrix_studio.branching import reconstruct_at_turn

    await db.create_run(
        run_id="cite-kinds", topic="t",
        cast=[{"name": "Dana", "persona": "p"}, {"name": "Marcus", "persona": "p"}],
    )
    await db.append_event(
        run_id="cite-kinds", turn=1, seq=0, event_type="agent.response",
        agent_name="Dana",
        payload={
            "speaker": "Dana", "message": "Per real.md #0, yes.",
            "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0,
            "citation_provenance": [{"label": "real.md #0", "title": "real.md",
                                     "kind": "firsthand", "attributive": False}],
        },
    )
    await db.append_event(
        run_id="cite-kinds", turn=2, seq=1, event_type="agent.response",
        agent_name="Marcus",
        payload={
            "speaker": "Marcus", "message": "Dana cited real.md, and also ghost.md.",
            "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0,
            "citation_provenance": [
                {"label": "real.md", "title": "real.md", "kind": "secondhand",
                 "attributive": True, "via": "Dana"},
                {"label": "ghost.md", "title": "ghost.md", "kind": "unverified",
                 "attributive": False, "reason": "never retrieved"},
            ],
        },
    )
    run = await db.get_run("cite-kinds")
    *_rest, ledger = await reconstruct_at_turn(db, run, 2)
    assert ledger == [["Dana", "real.md"]], (
        f"only Dana's first-hand citation belongs in the ledger, got {ledger}"
    )
