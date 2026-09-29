# SPDX-License-Identifier: Apache-2.0
"""Scheduled injections: operator messages that enter a run at a turn fixed in its config.

## Why, when branching already injects

A branch injects into ONE conversation that already happened (`inject_message`, the scrubber). That
answers "what if this had been said here?" for one draw — and one draw is uninterpretable
(`docs/ENSEMBLE-CONVERSATIONS.md` §3.3). To attribute an effect to an injection it has to be a declared
variable: the same message at the same point in every replicate of a cell, against replicates without
it. A config key does that, and an ensemble cell can vary it like any other; replaying a branch into
every replicate would re-execute each one from the branch point, which is the cost the backlog warned of.

## Shape

`config.injections: [{after_turn, speaker, content}]`. The message enters the conversation after turn
`after_turn` (0 = before the first turn) as an `agent.response` flagged `injected` with source
`operator`, exactly as a branch injection is recorded. It shares the turn number of the turn it follows
rather than taking one, so it never eats a generated turn and the budget every slice recomputes from the
config is unchanged. A persona name as `speaker` puts the words in that persona's mouth, and threads them
into its history, as a branch injection does; any other name (a customer, a regulator's letter) is simply
a voice in the feed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

MAX_INJECTIONS = 5
MAX_CONTENT_CHARS = 2000
MAX_SPEAKER_CHARS = 60
OPERATOR = "operator"


@dataclass(frozen=True)
class Injection:
    key: str
    after_turn: int
    speaker: str
    content: str


def from_config(config: Optional[Dict[str, Any]]) -> List[Injection]:
    out: List[Injection] = []
    for raw in (config or {}).get("injections") or []:
        if not isinstance(raw, dict):
            continue
        speaker = " ".join(str(raw.get("speaker") or "").split())[:MAX_SPEAKER_CHARS]
        content = str(raw.get("content") or "").strip()[:MAX_CONTENT_CHARS]
        try:
            after = int(raw.get("after_turn"))
        except (TypeError, ValueError):
            continue
        if speaker and content and after >= 0:
            out.append(Injection(f"I{len(out) + 1}", after, speaker, content))
        if len(out) >= MAX_INJECTIONS:
            break
    return out


def due(injections: Sequence[Injection], completed_turns: int,
        conversation: Sequence[Dict[str, Any]]) -> List[Injection]:
    """The injections to deliver now: scheduled after this turn and not already in the conversation. The
    second test is what makes a retried slice safe — the conversation rides the snapshot."""
    delivered = {m.get("injection") for m in conversation if m.get("injection")}
    return [i for i in injections if i.after_turn == completed_turns and i.key not in delivered]
