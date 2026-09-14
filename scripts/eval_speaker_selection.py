#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Stage 1 of `docs/SPEAKER-SELECTION-EVALUATION.md`: score selection arms by REPLAY.

## Why replay

Selection can be evaluated without generating a conversation. Replay a recorded transcript;
at each turn, hand the selector the state it would have had and record its pick. Scoring the
sequence of picks answers the distribution questions — starvation, dyad lock, coverage — for
about **40 Haiku calls of ~120 tokens per arm per transcript**, which is cents and two
minutes rather than a full run.

**What it cannot answer**, stated here because a cheap metric invites over-reading: a
different speaker at turn 7 changes every later turn, and replay holds the transcript fixed.
So the picks are scored as a sequence, not as a conversation. An arm that wins here has
earned a live run (Stage 2), not a conclusion.

## The baseline arm is the shipped code, not a copy of it

`baseline` calls `simulator._select_next_speaker` itself, and `--check-baseline` spies on the
prompt to confirm the arms below start from the same text. A harness with its own
reimplementation of the thing under test measures the harness.

Usage:
    export AWS_REGION=us-east-1 TABLE_PREFIX=matrix-studio DATA_BUCKET=... VECTOR_BUCKET=...
    scripts/eval_speaker_selection.py --run 3abd39b3-... --owner SUB
    scripts/eval_speaker_selection.py --run A --run B --owner SUB --arm baseline --arm counts
    scripts/eval_speaker_selection.py --run A --owner SUB --check-baseline
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio.jsonio import extract_json_object  # noqa: E402
from matrix_studio.personas import parse_structured, public_persona  # noqa: E402
from matrix_studio.storage import Database  # noqa: E402

WINDOW = 10  # messages the moderator sees, matching the engine


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def gini(shares: Sequence[int]) -> float:
    """0 = every persona speaks equally, 1 = one persona takes every turn."""
    ordered = sorted(shares)
    n = len(ordered)
    total = sum(ordered) or 1
    return sum((2 * (i + 1) - n - 1) * s for i, s in enumerate(ordered)) / (n * total)


def longest_dyad_chain(order: Sequence[str]) -> int:
    """Longest run of two speakers alternating — the "duel with an audience" shape."""
    longest = current = 0
    for i in range(2, len(order)):
        if order[i] == order[i - 2] and order[i] != order[i - 1]:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest + 2 if longest else 0


def score(order: Sequence[str], cast: Sequence[str]) -> Dict[str, Any]:
    counts = Counter(order)
    shares = [counts.get(name, 0) for name in cast]
    coverage = next(
        (i + 1 for i in range(len(order)) if len(set(order[: i + 1])) == len(cast)), None
    )
    return {
        "turns": len(order),
        "gini": round(gini(shares), 3),
        "min_turns": min(shares) if shares else 0,
        "max_turns": max(shares) if shares else 0,
        "never_spoke": [n for n in cast if counts.get(n, 0) == 0],
        "dyad_chain": longest_dyad_chain(order),
        "coverage_turn": coverage,
        "self_repeats": sum(1 for i in range(1, len(order)) if order[i] == order[i - 1]),
        "picks_from_last_two": sum(
            1 for i in range(2, len(order)) if order[i] in order[i - 2 : i]
        ),
        "shares": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
    }


# --------------------------------------------------------------------------- #
# Arms — each returns the prompt for one selection
# --------------------------------------------------------------------------- #


def _personas_block(cast: List[Dict[str, Any]], personas_on: bool) -> str:
    # `structured` comes off the run row as a dict; the renderer wants the parsed model,
    # exactly as the engine's snapshot gives it. Passing the dict raised
    # `'dict' object has no attribute 'render_public'` — a reminder that the harness has to
    # reproduce the engine's INPUTS as well as its prompt.
    return "\n".join(
        f"- {c['name']}: "
        + public_persona(
            c.get("persona", ""), parse_structured(c.get("structured")), enabled=personas_on
        )
        for c in cast
    )


def _base_prompt(topic, personas_desc, conv_summary, last_speaker) -> str:
    """Verbatim the engine's cognition-on selection prompt."""
    return f"""You are a conversation moderator. Given the following personas and recent conversation about "{topic}", select who should speak next.

Personas:
{personas_desc}

Recent conversation:
{conv_summary}

Last speaker: {last_speaker or 'None (start of conversation)'}

Respond with ONLY a JSON object of the form {{"speaker": "<persona name>", "reason": "<one short sentence on why they should speak next>"}}. Choose naturally based on conversation flow."""


def arm_baseline(**kw) -> str:
    return _base_prompt(kw["topic"], kw["personas_desc"], kw["conv"], kw["last_speaker"])


def arm_counts(**kw) -> str:
    """Intervention A: show turns taken and turns since each persona last spoke.

    The hypothesis under test: starvation is an INFORMATION problem. The moderator sees ten
    messages, which is under two rounds with six speakers, so a persona silent for twelve
    turns is invisible rather than overdue.
    """
    lines = []
    for name in kw["cast_names"]:
        taken = kw["taken"].get(name, 0)
        since = kw["since"].get(name)
        ago = "has not spoken yet" if since is None else f"last spoke {since} turn(s) ago"
        lines.append(f"- {name}: {taken} turn(s) so far, {ago}")
    return _base_prompt(
        kw["topic"], kw["personas_desc"], kw["conv"], kw["last_speaker"]
    ).replace(
        "Choose naturally based on conversation flow.",
        "Participation so far:\n" + "\n".join(lines)
        + "\n\nChoose naturally based on conversation flow.",
    )


def arm_counts_budget(**kw) -> str:
    """Interventions A + B: the counts, plus the run length and the fair share."""
    fair = kw["max_messages"] / max(1, len(kw["cast_names"]))
    return arm_counts(**kw).replace(
        "Choose naturally based on conversation flow.",
        f"This conversation runs for {kw['max_messages']} turns with "
        f"{len(kw['cast_names'])} participants, so a fair share is roughly {fair:.0f} turns "
        "each. Choose naturally based on conversation flow, but do not let a participant "
        "fall far behind their share without reason.",
    )


ARMS = {
    "baseline": arm_baseline,
    "counts": arm_counts,
    "counts+budget": arm_counts_budget,
}

#: Intervention C is not a prompt — it is a deterministic guard applied to any arm's pick.
FLOOR_ROUNDS = 2.0


def apply_floor(pick: str, kw: Dict[str, Any]) -> Tuple[str, bool]:
    """Intervention C: force a persona who is more than FLOOR_ROUNDS overdue.

    The `merge_with_source_floor` argument transplanted: a participant who can never win a
    slot is not in the conversation. Returns `(name, floor_fired)` so the scoreboard can say
    how often the guarantee, rather than the model, chose.
    """
    cast = kw["cast_names"]
    rounds = len(cast)
    overdue = [
        (kw["since"][n] if kw["since"].get(n) is not None else 10**6, n)
        for n in cast
        if n != kw["last_speaker"]
        and (kw["since"].get(n) is None or kw["since"][n] > FLOOR_ROUNDS * rounds)
    ]
    if not overdue:
        return pick, False
    if pick in {n for _, n in overdue}:
        return pick, False
    return max(overdue)[1], True


# --------------------------------------------------------------------------- #


async def select(prompt: str, cast_names: List[str], model: str) -> Tuple[Optional[str], str]:
    """One selection call. Returns `(name or None, raw)`; None means unresolvable."""
    from matrix_studio.lazy_litellm import litellm

    response = await litellm.acompletion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=120,
        response_format={"type": "json_object"},
    )
    raw = (response.choices[0].message.content or "").strip()
    parsed = extract_json_object(raw)
    text = str((parsed or {}).get("speaker", "")).strip() or raw
    # Longest name first, so `Dr. Jordan` cannot be resolved as `Jordan` — the resolver bug
    # recorded as intervention F in the design doc.
    for name in sorted(cast_names, key=len, reverse=True):
        if name.lower() in text.lower():
            return name, raw
    return None, raw


async def replay(
    db, run_id: str, arm: str, model: str, floor: bool, closed_loop: bool = False
) -> Dict[str, Any]:
    """Score one arm over one transcript.

    ``closed_loop`` decides where the participation counts come from, and it is the
    methodological crux rather than a flag:

    - **open loop (default)** — counts come from the RECORDED transcript. Every arm is
      scored on identical state, which is what makes the comparison clean. But an arm
      instructed to correct an imbalance never learns that it already corrected it: on the
      first run of `counts+budget` this produced 15 consecutive identical picks out of 24,
      because the recorded counts never registered the arm's own nominations. That number
      measured the harness, not the arm.
    - **closed loop** — counts come from the arm's OWN picks so far, while the conversation
      text stays the recorded one. Pacing is then measured honestly, at the cost of the
      counts and the transcript disagreeing about who has been speaking.

    Neither is "correct": open loop is right for arms that only read the state, closed loop
    for arms that act on it. Both are reported.
    """
    run = await db.get_run(run_id)
    cast = json.loads(run["cast_json"])
    cast_names = [c["name"] for c in cast]
    config = json.loads(run["config_json"] or "{}")
    personas_on = bool((config.get("personas") or {}).get("enabled"))
    personas_desc = _personas_block(cast, personas_on)

    events = await db.get_events(run_id)
    messages = [
        {"speaker": e.get("agent_name"), "content": json.loads(e["payload"]).get("message", "")}
        for e in events
        if e["event_type"] == "agent.response"
    ]

    picks: List[str] = []
    unresolved = floor_fired = 0
    for i in range(len(messages)):
        history = messages[:i]
        # Whose participation the arm is shown: the transcript's, or its own picks.
        seen = picks if closed_loop else [m["speaker"] for m in history]
        taken = Counter(seen)
        since = {
            n: next(
                (len(seen) - j - 1 for j in range(len(seen) - 1, -1, -1) if seen[j] == n),
                None,
            )
            for n in cast_names
        }
        kw = {
            "topic": run["topic"],
            "personas_desc": personas_desc,
            "conv": "\n".join(
                f"{m['speaker']}: {m['content']}" for m in history[-WINDOW:]
            ),
            "last_speaker": history[-1]["speaker"] if history else None,
            "cast_names": cast_names,
            "taken": taken,
            "since": since,
            "max_messages": int(config.get("max_messages") or len(messages)),
        }
        name, _raw = await select(ARMS[arm](**kw), cast_names, model)
        if name is None:
            unresolved += 1
            # Same degradation as the engine, so the arm is not flattered by a better
            # fallback than the one that ships.
            name = next((n for n in cast_names if n != kw["last_speaker"]), cast_names[0])
        if floor:
            name, fired = apply_floor(name, kw)
            floor_fired += int(fired)
        picks.append(name)

    out = score(picks, cast_names)
    out.update({"unresolved": unresolved, "floor_fired": floor_fired})
    return out


async def check_baseline(db, run_id: str) -> bool:
    """Confirm `arm_baseline` is byte-identical to what the engine sends."""
    from matrix_studio.engine import simulator
    from matrix_studio.settings import get_settings
    from matrix_studio.state import CognitionConfig, PersonaConfig
    import litellm as L

    run = await db.get_run(run_id)
    cast = json.loads(run["cast_json"])
    config = json.loads(run["config_json"] or "{}")
    snapshot = await db.get_snapshot(run_id, turn=None)
    conversation = [
        {"speaker": m.get("speaker"), "content": m.get("content")}
        for m in (snapshot.conversation or [])
    ]
    captured: Dict[str, str] = {}
    original = L.acompletion

    async def spy(**kwargs):
        captured["prompt"] = kwargs["messages"][-1]["content"]
        raise RuntimeError("captured")

    L.acompletion = spy
    try:
        # (topic, agents, ...) — in that order. Passing them the other way round made
        # `list(agents.keys())` fail on a string, which the function's broad
        # `except Exception` swallowed into a silent fallback pick: no call, no error, a
        # speaker returned anyway. Recorded in the design doc as an observability gap.
        await simulator._select_next_speaker(
            run["topic"], snapshot.agents, conversation,
            conversation[-1]["speaker"], get_settings(),
            cognition=CognitionConfig.from_config(config),
            personas=PersonaConfig.from_config(config),
        )
    except Exception:
        pass
    finally:
        L.acompletion = original

    mine = arm_baseline(
        topic=run["topic"],
        personas_desc=_personas_block(
            cast, bool((config.get("personas") or {}).get("enabled"))
        ),
        conv="\n".join(
            f"{m['speaker']}: {m['content']}" for m in conversation[-WINDOW:]
        ),
        last_speaker=conversation[-1]["speaker"],
    )
    same = captured.get("prompt") == mine
    print(f"baseline prompt identical to the engine's: {same}")
    if not same and captured.get("prompt"):
        engine, harness = captured["prompt"], mine
        for i, (a, b) in enumerate(zip(engine, harness)):
            if a != b:
                print(f"  first difference at {i}:\n    engine : {engine[i:i+90]!r}\n"
                      f"    harness: {harness[i:i+90]!r}")
                break
        else:
            print(f"  lengths differ: engine {len(engine)}, harness {len(harness)}")
    return same


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, help="recorded run id")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--arm", action="append", choices=sorted(ARMS), default=None)
    parser.add_argument("--floor", action="store_true", help="also apply intervention C")
    parser.add_argument(
        "--closed-loop", action="store_true",
        help="participation counts come from the arm's own picks, not the transcript",
    )
    parser.add_argument(
        "--model", default="bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    parser.add_argument("--check-baseline", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("DATA_BUCKET"):
        raise SystemExit("DATA_BUCKET must be set (see the stack outputs).")
    db = Database(
        table_prefix=os.environ.get("TABLE_PREFIX", "matrix-studio"),
        bucket=os.environ["DATA_BUCKET"],
        region=os.environ.get("AWS_REGION", "us-east-1"),
    )
    await db.connect()
    bound = db.for_owner(args.owner)
    try:
        if args.check_baseline:
            return 0 if await check_baseline(bound, args.run[0]) else 1

        arms = args.arm or ["baseline"]
        print(f"model {args.model} · floor {'on' if args.floor else 'off'} · "
              f"{'closed' if args.closed_loop else 'open'} loop\n")
        header = f"{'run':<12} {'arm':<14} {'gini':>5} {'min':>4} {'max':>4} {'dyad':>5} {'cover':>6} {'self':>5} {'last2':>6} {'unres':>6} {'floor':>6}"
        print(header); print("-" * len(header))
        for run_id in args.run:
            for arm in arms:
                s = await replay(
                    bound, run_id, arm, args.model, args.floor, args.closed_loop,
                )
                print(f"{run_id[:11]:<12} {arm:<14} {s['gini']:>5.2f} {s['min_turns']:>4} "
                      f"{s['max_turns']:>4} {s['dyad_chain']:>5} "
                      f"{str(s['coverage_turn']):>6} {s['self_repeats']:>5} "
                      f"{s['picks_from_last_two']:>6} {s['unresolved']:>6} {s['floor_fired']:>6}")
                if s["never_spoke"]:
                    print(f"{'':<27}never spoke: {', '.join(s['never_spoke'])}")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
