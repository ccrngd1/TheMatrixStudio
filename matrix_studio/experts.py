# SPDX-License-Identifier: Apache-2.0
"""Consultants: experts outside the room whom the personas can ask, and who answer from their sources.

## Why

Every way of asking a question targeted someone already in the conversation. A panel arguing about
whether a statute applies contains nobody who has read the statute, so the transcripts are full of
"someone needs to find out" — a persona demanding a fact nobody in the room can supply. A consultant
supplies it: a named expert with their own documents and knowledge bases, who is asked a specific
question, answers only from those sources with citations, and otherwise says the answer is not in them.

## What a consultant is NOT

A sixth opinion. A consultant never takes a turn, is never in the speaker pool, has no position to
defend and no goals, and answers only what was asked. The answer enters the transcript attributed to
the consultant, so later speakers can use it — and credit it — like any other thing said in the room.

## How a persona asks

The consultants are listed in each persona's prompt with one instruction: to ask, end the message with
a line ``ASK <name>: <one specific question>``. The engine strips that line from the message, answers it,
and appends the answer. Bounded per run (`consult_limit`), because every consultation is a model call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Default consultations per run. Each is a retrieval plus a model call.
DEFAULT_CONSULT_LIMIT = 6

#: What a consultant says when its sources do not answer the question. Fixed wording, so "no answer"
#: is recognisable in a transcript and countable afterwards.
NOT_IN_SOURCES = "That isn't in my sources."

ASK_RE = re.compile(r"^[ \t>*_-]*ASK[ \t]+([^:\n]{1,80}?)[ \t]*:[ \t]*(\S.{2,600}?)[ \t]*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class Expert:
    name: str
    expertise: str

    @property
    def speaker(self) -> str:
        """How the consultant appears in the transcript, so every reader knows it is not a participant."""
        return f"{self.name} (consultant)"


def from_config(config: Optional[Dict[str, Any]]) -> List[Expert]:
    out: List[Expert] = []
    for raw in (config or {}).get("experts") or []:
        if isinstance(raw, dict) and str(raw.get("name") or "").strip():
            out.append(Expert(str(raw["name"]).strip(), str(raw.get("expertise") or "").strip()))
    return out


def consult_limit(config: Optional[Dict[str, Any]]) -> int:
    value = (config or {}).get("consult_limit")
    return DEFAULT_CONSULT_LIMIT if value is None else max(0, int(value))


def consults_used(conversation: Sequence[Dict[str, Any]]) -> int:
    """Counted from the conversation itself, which the snapshot carries — so the limit holds across
    the one-turn-per-call slices the deployed engine runs in, and across a resume or branch."""
    return sum(1 for m in conversation if m.get("consultant"))


def consultants_block(experts: Sequence[Expert], remaining: int) -> str:
    """The prompt block that tells a persona who can be asked and how. Empty when nobody can be."""
    if not experts or remaining <= 0:
        return ""
    lines = "\n".join(f"- {e.name}: {e.expertise or 'a subject-matter expert'}" for e in experts)
    return (
        "\n\nConsultants you may ask (they are NOT in this conversation, hold no position, and answer "
        f"only from their own sources):\n{lines}\n"
        "If a specific fact from one of them would change this discussion, end your message with one "
        "line exactly like:\nASK <consultant name>: <one specific, answerable question>\n"
        "Ask only when the answer matters; the room can ask only a limited number of questions "
        f"({remaining} left). Do not answer on their behalf."
    )


def parse_ask(content: str, experts: Sequence[Expert]) -> Tuple[str, Optional[Tuple[Expert, str]]]:
    """Split a persona's message into (message without the ASK line, (expert, question) or None).

    Only the LAST ask counts — one question per turn — and only if it names a listed consultant.
    An ASK line naming nobody is left in the message untouched rather than silently deleted: it is
    what the persona said, and a reader should see it.
    """
    by_name = {e.name.lower(): e for e in experts}
    matches = [m for m in ASK_RE.finditer(content or "") if m.group(1).strip().lower() in by_name]
    if not matches:
        return content, None
    m = matches[-1]
    expert = by_name[m.group(1).strip().lower()]
    # Remove every VALID ask line — they were instructions to the engine, not speech — back to front so
    # earlier spans stay put. Lines naming nobody stay, as said above.
    cleaned = content
    for x in reversed(matches):
        cleaned = cleaned[:x.start()] + cleaned[x.end():]
    return cleaned.strip(), (expert, m.group(2).strip())


def answer_messages(expert: Expert, question: str, asked_by: str, topic: str,
                    passages: Sequence[Any]) -> List[Dict[str, str]]:
    """The consultant's prompt: answer from these passages only, cite them, or say they do not say."""
    if passages:
        sources = "\n".join(f"- [{p.citation}] {p.content}" for p in passages)
    else:
        sources = "(no passages were found for this question)"
    system = (
        f"You are {expert.name}, a consultant: {expert.expertise or 'a subject-matter expert'}. You are "
        "not a participant in the discussion and hold no position in it. Answer the question using ONLY "
        "the source passages below. Cite each claim with its label in square brackets, exactly as "
        f"listed. If the passages do not answer the question, reply exactly: \"{NOT_IN_SOURCES}\" and, in "
        "one sentence, what they do cover. Never guess, never give an opinion on what the room should "
        "decide. Two to four sentences."
    )
    user = (
        f"The discussion is about: {topic}\n\n{asked_by} asks: {question}\n\nSource passages:\n{sources}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def answer(expert: Expert, question: str, asked_by: str, topic: str, passages: Sequence[Any],
                 *, model: str, settings: Any) -> Dict[str, Any]:
    """Generate the consultant's answer. Never raises: a failed call becomes a stated failure."""
    from matrix_studio.lazy_litellm import litellm

    try:
        response = await litellm.acompletion(
            model=model,
            messages=answer_messages(expert, question, asked_by, topic, passages),
            temperature=0.2,
            max_tokens=600,
            drop_params=True,
        )
        text = (response.choices[0].message.content or "").strip() or NOT_IN_SOURCES
        usage = getattr(response, "usage", None)
        cost = float((getattr(response, "_hidden_params", None) or {}).get("response_cost") or 0.0)
        return {
            "answer": text,
            "tokens_in": int(getattr(usage, "prompt_tokens", 0) or 0),
            "tokens_out": int(getattr(usage, "completion_tokens", 0) or 0),
            "cost_usd": cost,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — a failed consultation must not end the run
        return {"answer": f"{NOT_IN_SOURCES} (The consultation failed: {type(exc).__name__}.)",
                "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "error": str(exc)[:300]}
