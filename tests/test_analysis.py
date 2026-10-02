# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the Phase 1.5 analysis module (summary generation + aside
replies). The LLM seam ``analysis._acompletion`` is patched per-test so no live
call is made — we verify JSON parsing, the retry-then-plaintext fallback, and
that persona asides use the REAL stored persona text (never invented).
"""

import json

import pytest

from matrix_studio import analysis

# The real seam, taken at import: the suite-wide `mock_analysis_llm` fixture replaces it in every test,
# and the budget test below is about what the real one sends to the model.
_REAL_ACOMPLETION = analysis._acompletion


CONVERSATION = [
    {"speaker": "Ada", "content": "We should require a provider sign-off.", "turn": 1},
    {"speaker": "Ben", "content": "That adds liability we can't absorb.", "turn": 2},
]


def _mk(content, cost=0.001):
    async def _fake(messages, model=None, temperature=0.4, max_tokens=None):
        return {"content": content, "tokens_in": 50, "tokens_out": 10, "cost_usd": cost}

    return _fake


@pytest.mark.asyncio
async def test_summary_parses_strict_json(monkeypatch):
    payload = {
        "consensus": ["provider sign-off gate"],
        "dissenters": [{"speaker": "Ben", "position": "liability"}],
        "key_ideas": ["tiered windows"],
        "open_questions": ["who audits?"],
        "overview": "A debate about a renewal policy.",
    }
    monkeypatch.setattr(analysis, "_acompletion", _mk(json.dumps(payload)))
    result = await analysis.generate_summary(CONVERSATION, topic="supplies")
    assert result["parsed"] is True
    assert result["payload"]["consensus"] == ["provider sign-off gate"]
    assert result["payload"]["dissenters"][0]["speaker"] == "Ben"
    assert result["cost_usd"] == 0.001


@pytest.mark.asyncio
async def test_summary_parses_fenced_json(monkeypatch):
    payload = {"overview": "fenced", "consensus": [], "dissenters": [],
               "key_ideas": [], "open_questions": []}
    fenced = f"Here you go:\n```json\n{json.dumps(payload)}\n```"
    monkeypatch.setattr(analysis, "_acompletion", _mk(fenced))
    result = await analysis.generate_summary(CONVERSATION, topic="t")
    assert result["parsed"] is True
    assert result["payload"]["overview"] == "fenced"


@pytest.mark.asyncio
async def test_summary_retries_then_plaintext_fallback(monkeypatch):
    """Two non-JSON replies → graceful plain-text overview, never a crash."""
    calls = {"n": 0}

    async def _fake(messages, model=None, temperature=0.4, max_tokens=None):
        calls["n"] += 1
        return {"content": "totally not json", "tokens_in": 5, "tokens_out": 5,
                "cost_usd": 0.0005}

    monkeypatch.setattr(analysis, "_acompletion", _fake)
    result = await analysis.generate_summary(CONVERSATION, topic="t")
    assert result["parsed"] is False
    assert calls["n"] == 2  # one attempt + one retry
    assert result["payload"]["overview"] == "totally not json"
    # Cost is accumulated across both attempts (honest accounting).
    assert result["cost_usd"] == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_summary_llm_exception_never_crashes(monkeypatch):
    async def _boom(messages, model=None, temperature=0.4, max_tokens=None):
        raise RuntimeError("provider down")

    monkeypatch.setattr(analysis, "_acompletion", _boom)
    result = await analysis.generate_summary(CONVERSATION, topic="t")
    assert result["parsed"] is False
    assert "unavailable" in result["payload"]["overview"].lower()


@pytest.mark.asyncio
async def test_summary_honors_field_subset(monkeypatch):
    payload = {"overview": "o", "consensus": ["c"], "dissenters": [],
               "key_ideas": ["k"], "open_questions": ["q"]}
    monkeypatch.setattr(analysis, "_acompletion", _mk(json.dumps(payload)))
    result = await analysis.generate_summary(
        CONVERSATION, topic="t", fields=["overview", "consensus"]
    )
    assert set(result["payload"].keys()) == {"overview", "consensus"}


@pytest.mark.asyncio
async def test_persona_reply_uses_real_persona_text(monkeypatch):
    captured = {}

    async def _fake(messages, model=None, temperature=0.4, max_tokens=None):
        captured["system"] = messages[0]["content"]
        return {"content": "In character reply.", "tokens_in": 1, "tokens_out": 1,
                "cost_usd": 0.0}

    monkeypatch.setattr(analysis, "_acompletion", _fake)
    persona_text = "Dr. Webb is a liability-obsessed corporate lawyer, ex-litigator."
    reply = await analysis.persona_reply(
        user_message="Expand on your liability point.",
        persona_name="Dr. Webb",
        persona_text=persona_text,
        conversation=CONVERSATION,
        topic="supplies",
    )
    assert reply["speaker"] == "Dr. Webb"
    # The REAL stored persona text must be embedded in the system prompt.
    assert persona_text in captured["system"]
    # And the aside must be framed as post-hoc reflection (not a live turn).
    assert "already FINISHED" in captured["system"]


@pytest.mark.asyncio
async def test_room_reply_calls_each_persona(monkeypatch):
    seen = []

    async def _fake(messages, model=None, temperature=0.4, max_tokens=None):
        seen.append(messages[0]["content"])
        return {"content": "reply", "tokens_in": 2, "tokens_out": 2, "cost_usd": 0.001}

    monkeypatch.setattr(analysis, "_acompletion", _fake)
    cast = [
        {"name": "Ada", "persona": "ethicist persona text"},
        {"name": "Ben", "persona": "engineer persona text"},
    ]
    reply = await analysis.room_reply(
        user_message="React to the proposal.",
        cast=cast,
        conversation=CONVERSATION,
        topic="t",
    )
    assert reply["speaker"] == "room"
    assert len(reply["replies"]) == 2
    assert {r["speaker"] for r in reply["replies"]} == {"Ada", "Ben"}
    # Aggregated cost across both persona calls.
    assert reply["cost_usd"] == pytest.approx(0.002)
    assert any("ethicist persona text" in s for s in seen)
    assert any("engineer persona text" in s for s in seen)


# --------------------------------------------------------------------------- #
# Editable summarization prompt (custom instructions replace the analyst-role
# framing while the non-negotiable guardrails always remain).
# --------------------------------------------------------------------------- #
CUSTOM_INSTRUCTIONS = (
    "You are a snarky debate coach. Roast the weakest argument mercilessly."
)


def _guardrails_present(prompt: str, fields) -> None:
    """Assert the three non-removable guardrails are in a built summary prompt."""
    # (a) no-fabrication line
    assert "do not invent" in prompt
    assert "strictly on what was actually said" in prompt
    # (b) JSON-only response instruction
    assert "ONLY a single JSON object" in prompt
    # (c) the schema block for each requested field
    for f in fields:
        assert f'"{f}"' in prompt


def test_summary_prompt_uses_default_instructions_by_default():
    fields = list(analysis.DEFAULT_SUMMARY_FIELDS)
    prompt = analysis._summary_system_prompt(fields, focus=None)
    # The default analyst-role framing is present when no custom text is given.
    assert analysis.DEFAULT_SUMMARY_INSTRUCTIONS in prompt
    _guardrails_present(prompt, fields)


def test_summary_prompt_custom_instructions_replace_role_framing():
    fields = list(analysis.DEFAULT_SUMMARY_FIELDS)
    prompt = analysis._summary_system_prompt(
        fields, focus=None, instructions=CUSTOM_INSTRUCTIONS
    )
    # Custom text IS present, and it REPLACES the default role framing.
    assert CUSTOM_INSTRUCTIONS in prompt
    assert analysis.DEFAULT_SUMMARY_INSTRUCTIONS not in prompt
    # Guardrails are STILL present — they cannot be dropped by a custom prompt.
    _guardrails_present(prompt, fields)


def test_summary_prompt_focus_still_appends_with_custom_instructions():
    fields = list(analysis.DEFAULT_SUMMARY_FIELDS)
    prompt = analysis._summary_system_prompt(
        fields, focus="emphasize legal risk", instructions=CUSTOM_INSTRUCTIONS
    )
    assert CUSTOM_INSTRUCTIONS in prompt
    assert "emphasize legal risk" in prompt
    _guardrails_present(prompt, fields)


def test_summary_prompt_blank_instructions_fall_back_to_default():
    fields = list(analysis.DEFAULT_SUMMARY_FIELDS)
    prompt = analysis._summary_system_prompt(fields, focus=None, instructions="   ")
    assert analysis.DEFAULT_SUMMARY_INSTRUCTIONS in prompt


@pytest.mark.asyncio
async def test_generate_summary_threads_custom_instructions_into_prompt(monkeypatch):
    """The custom instructions text is present in the built system prompt."""
    captured = {}
    payload = {"overview": "o", "consensus": [], "dissenters": [],
               "key_ideas": [], "open_questions": []}

    async def _fake(messages, model=None, temperature=0.4, max_tokens=None):
        captured["system"] = messages[0]["content"]
        return {"content": json.dumps(payload), "tokens_in": 1, "tokens_out": 1,
                "cost_usd": 0.0}

    monkeypatch.setattr(analysis, "_acompletion", _fake)
    result = await analysis.generate_summary(
        CONVERSATION, topic="t", instructions=CUSTOM_INSTRUCTIONS
    )
    assert CUSTOM_INSTRUCTIONS in captured["system"]
    assert analysis.DEFAULT_SUMMARY_INSTRUCTIONS not in captured["system"]
    _guardrails_present(captured["system"], analysis.DEFAULT_SUMMARY_FIELDS)
    # The effective instructions are echoed back for persistence/prefill.
    assert result["instructions"] == CUSTOM_INSTRUCTIONS


@pytest.mark.asyncio
async def test_generate_summary_default_instructions_persist_as_none(monkeypatch):
    """Backward compat: omitting instructions uses the default → persist NULL."""
    captured = {}
    payload = {"overview": "o", "consensus": [], "dissenters": [],
               "key_ideas": [], "open_questions": []}

    async def _fake(messages, model=None, temperature=0.4, max_tokens=None):
        captured["system"] = messages[0]["content"]
        return {"content": json.dumps(payload), "tokens_in": 1, "tokens_out": 1,
                "cost_usd": 0.0}

    monkeypatch.setattr(analysis, "_acompletion", _fake)
    result = await analysis.generate_summary(CONVERSATION, topic="t")
    assert analysis.DEFAULT_SUMMARY_INSTRUCTIONS in captured["system"]
    # None (NULL) signals "the default framing was used" so the UI falls back.
    assert result["instructions"] is None


# --------------------------------------------------------------------------- #
# The evidence plan (BACKLOG "Conversations end in 'it depends'", Stage 1)
# --------------------------------------------------------------------------- #


def test_the_prompt_asks_for_an_evidence_plan_and_forbids_filling_gaps():
    prompt = analysis._summary_system_prompt(list(analysis.DEFAULT_SUMMARY_FIELDS), focus=None)
    assert '"evidence_plan"' in prompt and '"conditional_recommendation"' in prompt
    assert f'exactly "{analysis.NOT_STATED}"' in prompt and "never fill a gap yourself" in prompt


def test_a_missing_evidence_column_reads_not_stated_and_a_row_without_data_is_dropped():
    out = analysis._coerce_summary({
        "evidence_plan": [
            {"data": "pilot churn", "asked_by": "Dana", "moves_them": "under 5% and I'm in", "best_guess": ""},
            {"asked_by": "Marcus"},
            "not a row",
        ],
        "conditional_recommendation": "If churn is under 5%, launch; if not, hold.",
    }, ["evidence_plan", "conditional_recommendation"])
    [row] = out["evidence_plan"]
    assert row["data"] == "pilot churn" and row["moves_them"] == "under 5% and I'm in"
    assert row["best_guess"] == row["decision"] == row["cheapest_way"] == analysis.NOT_STATED
    assert out["conditional_recommendation"].startswith("If churn")


def test_an_empty_summary_has_an_empty_plan():
    empty = analysis._empty_summary(list(analysis.DEFAULT_SUMMARY_FIELDS))
    assert empty["evidence_plan"] == [] and empty["conditional_recommendation"] == ""


# --------------------------------------------------------------------------- #
# The overview a reply leaves out (found 2026-10-01)
#
# With the evidence plan and the conditional recommendation added, the prompt asked for the overview
# LAST, after the recommendation, and the model would end on the recommendation and close the object.
# The reply parsed, was not truncated, and was stored with overview "" and nothing saying so.
# --------------------------------------------------------------------------- #

#: Shaped like the real failing reply: fenced, the fence never closed, every other field complete,
#: an evidence plan with unstated columns, a recommendation that reads as the conclusion — and the
#: object closed straight after it, with no overview key at all.
REPLY_WITHOUT_OVERVIEW = "```json\n" + json.dumps({
    "consensus": ["Open on weekends for a one-term pilot", "Staff the desk from the existing rota"],
    "dissenters": [{"speaker": "Ben", "position": "The rota cannot absorb another shift"}],
    "key_ideas": ["Measure demand before committing to a permanent change"],
    "open_questions": ["Who covers the desk when the rota is short?"],
    "evidence_plan": [
        {"data": "Weekend footfall at the branch next door", "asked_by": "Ben",
         "decision": "pilot or not", "moves_them": "over 200 visits a day and Ben is in",
         "best_guess": "Ada expects about 150", "cheapest_way": "not stated"},
        {"data": "Overtime cost per shift", "asked_by": "Ada", "decision": "how to staff it",
         "moves_them": "not stated", "best_guess": "not stated", "cheapest_way": "ask payroll"},
    ],
    "conditional_recommendation": (
        "If weekend footfall is over 200 a day, run the pilot; if not, hold. The cast's best guess is "
        "about 150, so lean hold."
    ),
}, indent=2) + "\n"


def _reply(content, finish_reason="stop", tokens_out=4870):
    async def _fake(messages, model=None, temperature=0.4, max_tokens=None):
        return {"content": content, "tokens_in": 13000, "tokens_out": tokens_out, "cost_usd": 0.07,
                "finish_reason": finish_reason}

    return _fake


def _shape_lines(prompt):
    """The lines of the JSON shape the prompt shows: between the opening "{" line and the closing "}"."""
    lines = prompt.split("\n")
    return lines[lines.index("{") + 1:lines.index("}")]


def test_the_prompt_asks_for_the_overview_first():
    shape = _shape_lines(analysis._summary_system_prompt(list(analysis.DEFAULT_SUMMARY_FIELDS), focus=None))
    assert shape[0].strip().startswith('"overview":')
    # Whatever order a caller sends the fields in.
    shape = _shape_lines(analysis._summary_system_prompt(
        ["consensus", "conditional_recommendation", "overview"], focus=None))
    assert [line.strip().split('"')[1] for line in shape] == ["overview", "consensus", "conditional_recommendation"]


def test_the_shape_is_only_json_and_the_field_rules_follow_it():
    prompt = analysis._summary_system_prompt(list(analysis.DEFAULT_SUMMARY_FIELDS), focus=None)
    # Every line of the shape is one key and its value, nothing after it. The rules used to trail the
    # evidence plan and the recommendation as prose inside the shape.
    for line in _shape_lines(prompt):
        assert line.rstrip(",").endswith(('"', "]")), line
    after = prompt.split("\n}\n", 1)[1]
    assert "Write every key above, the overview included" in after
    assert "never fill a gap yourself" in after and "empty string if nobody asked for evidence" in after


@pytest.mark.asyncio
async def test_a_reply_without_an_overview_is_named_omitted_and_logged(monkeypatch, caplog):
    monkeypatch.setattr(analysis, "_acompletion", _reply(REPLY_WITHOUT_OVERVIEW))
    with caplog.at_level("WARNING", logger="matrix_studio.analysis"):
        result = await analysis.generate_summary(CONVERSATION, topic="weekend opening")
    payload = result["payload"]
    # It parsed, and every field it did supply is kept.
    assert result["parsed"] is True
    assert payload["conditional_recommendation"].endswith("lean hold.")
    assert len(payload["evidence_plan"]) == 2
    # The overview keeps its empty value, so every reader still finds a string, and is NAMED as left
    # out. Never filled in by the code, from another field or otherwise.
    assert payload["overview"] == ""
    assert payload["omitted"] == ["overview"]
    [record] = [r for r in caplog.records if "did not supply" in r.getMessage()]
    assert "overview" in record.getMessage() and "finish_reason=stop" in record.getMessage()


@pytest.mark.asyncio
async def test_a_complete_reply_has_no_omitted_key(monkeypatch, caplog):
    complete = {"overview": "A debate about weekend opening.",
                **json.loads(REPLY_WITHOUT_OVERVIEW.removeprefix("```json\n"))}
    monkeypatch.setattr(analysis, "_acompletion", _reply(json.dumps(complete)))
    with caplog.at_level("WARNING", logger="matrix_studio.analysis"):
        result = await analysis.generate_summary(CONVERSATION, topic="weekend opening")
    assert "omitted" not in result["payload"]
    assert result["payload"]["overview"] == "A debate about weekend opening."
    assert not [r for r in caplog.records if "did not supply" in r.getMessage()]


@pytest.mark.asyncio
@pytest.mark.parametrize("overview", ["", "   ", None])
async def test_a_blank_or_null_overview_is_omitted_and_never_the_string_null(monkeypatch, overview):
    reply = {"overview": overview, "consensus": ["c"], "dissenters": [], "key_ideas": [], "open_questions": [],
             "evidence_plan": [], "conditional_recommendation": ""}
    monkeypatch.setattr(analysis, "_acompletion", _reply(json.dumps(reply)))
    result = await analysis.generate_summary(CONVERSATION, topic="t")
    assert result["payload"]["omitted"] == ["overview"]
    assert result["payload"]["overview"].strip() == ""
    # An empty list or an empty recommendation is a legitimate answer, not an omission.
    assert result["payload"]["consensus"] == ["c"]


@pytest.mark.asyncio
async def test_a_truncated_reply_keeps_the_overview_and_names_what_it_lost(monkeypatch, caplog):
    """Overview first is also what survives the budget: `jsonio` keeps the leading complete fields."""
    cut = (
        '{\n  "overview": "A debate about weekend opening.",\n  "consensus": ["a pilot"],\n'
        '  "dissenters": [],\n  "key_ideas": [],\n  "open_questions": [],\n'
        '  "evidence_plan": [ {"data": "weekend footfall", "asked_by": "Be'
    )
    monkeypatch.setattr(analysis, "_acompletion", _reply(cut, finish_reason="length", tokens_out=8000))
    with caplog.at_level("WARNING", logger="matrix_studio.analysis"):
        result = await analysis.generate_summary(CONVERSATION, topic="weekend opening")
    assert result["payload"]["overview"] == "A debate about weekend opening."
    assert result["payload"]["omitted"] == ["evidence_plan", "conditional_recommendation"]
    assert any("finish_reason=length" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_summary_asks_the_model_for_the_whole_summary_budget(monkeypatch):
    """What reaches the model, not just what the setting says. `generate_summary` names no budget, so
    the real seam's fallback is the summary's own: 16000 since 2026-10-02, when two summaries with a
    long focus were cut off at 8000. A focus does not shrink it."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from matrix_studio.settings import get_settings

    sent = []
    complete = {"overview": "o", "consensus": [], "dissenters": [], "key_ideas": [], "open_questions": [],
                "evidence_plan": [], "conditional_recommendation": ""}

    async def fake_litellm(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(complete)), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )

    monkeypatch.setattr(analysis, "_acompletion", _REAL_ACOMPLETION)
    with patch("matrix_studio.analysis.litellm.acompletion", side_effect=fake_litellm):
        result = await analysis.generate_summary(
            CONVERSATION, topic="t", focus="Weigh every objection in turn. " * 40,
        )
    assert result["parsed"] is True and "omitted" not in result["payload"]
    assert [k["max_tokens"] for k in sent] == [get_settings().summary_max_tokens]
    assert sent[0]["max_tokens"] == 16000
