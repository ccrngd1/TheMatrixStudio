# SPDX-License-Identifier: Apache-2.0
"""
Aggregate a set of runs of the same brief: what held, what dissented, what cannot coexist.

## Why this is a module and not a report script

One run answers "what did this room decide". A set of runs answers a different and more
useful question — **which parts of that decision are properties of the brief, and which are
properties of this particular conversation.** Nine renewal runs made the difference concrete:
`net-new is out of scope` appeared in 9 of 9, while the launch posture ranged from "records
pull, licensure to GC as an open theory" to "video check-in mandatory everywhere". The first is
a finding; the second is a coin whose weighting nobody had measured.

## The split, which is the whole design

**Computed, never asked.** Turn shares, coverage, dyad chains, cost, pass rates, and every
COUNT in the output. A model that is asked "how many runs agreed" will answer plausibly and
be unfalsifiable; the same number derived from the extractions is checkable, and if it is
wrong the extraction is wrong in a way somebody can see.

**Extracted per run, with a schema.** One call per run turns a transcript into each persona's
final position, their demands, their concessions and what moved them, and their refusals.
Per-run rather than all-at-once because a single call over nine transcripts is 300k tokens of
context in which the middle runs get skimmed.

**Synthesised once.** The only genuinely generative step: given every extraction, name the
agreements, the standing dissents, and the pairs that cannot both hold. It must cite run ids,
and it is forbidden from producing a single "the answer" — see `_COMPATIBILITY_RULES`.

## The trap this design exists to avoid

Asked to summarise a set of conversations, a model produces consensus. The closing-round work
measured the cost of that framing directly: the same instruction moved a persona's visible
behaviour from 0.000 to 0.333 depending only on whether it asked for agreement. An ensemble
report that reads "the group agreed on a phased rollout" when three of nine runs refused a
rollout is worse than no report, because it looks like evidence.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from matrix_studio.jsonio import extract_json_object

logger = logging.getLogger(__name__)

#: What one run contributes to the ensemble.
@dataclass
class RunView:
    run_id: str
    name: str
    #: The settings that make this arm different: method, turns, flags.
    arm: Dict[str, Any]
    #: `[(turn, speaker, text)]` in order.
    turns: List[Tuple[int, str, str]] = field(default_factory=list)
    #: Deterministic metrics, computed here rather than asked for.
    metrics: Dict[str, Any] = field(default_factory=dict)
    #: The schema'd extraction, once it has been made.
    positions: Dict[str, Any] = field(default_factory=dict)

    @property
    def cast(self) -> List[str]:
        seen: List[str] = []
        for _, who, _ in self.turns:
            if who and who not in seen:
                seen.append(who)
        return seen


def gini(shares: Sequence[int]) -> float:
    """0 = every persona spoke equally, 1 = one took every turn.

    Duplicated from `scripts/eval_speaker_selection.py` deliberately: that file is a research
    harness and this one ships, and a shared import would tie the deployed package to a
    script directory. The two are checked against each other by test.
    """
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


def measure(view: RunView) -> Dict[str, Any]:
    """Every number in the report, derived from the transcript.

    Deliberately does NOT include a "quality" score. There isn't one, and inventing a
    composite would let a worse conversation outrank a better one because it scored well on
    the parts that are easy to count.
    """
    order = [who for _, who, _ in view.turns]
    shares = Counter(order)
    cast = view.cast
    vec = [shares.get(n, 0) for n in cast]
    lengths = [len(t) for _, _, t in view.turns]
    half = len(lengths) // 2
    slots = len({t for t, _, _ in view.turns})
    return {
        "turns": len(order),
        "slots": slots,
        "multi_speaker_slots": sum(
            1 for t in {t for t, _, _ in view.turns}
            if sum(1 for tt, _, _ in view.turns if tt == t) > 1
        ),
        "shares": dict(sorted(shares.items(), key=lambda kv: -kv[1])),
        "gini": round(gini(vec), 3) if vec else 0.0,
        "min_turns": min(vec) if vec else 0,
        "max_turns": max(vec) if vec else 0,
        "never_spoke": [n for n in cast if not shares.get(n)],
        "dyad_chain": longest_dyad_chain(order),
        "coverage_turn": next(
            (i + 1 for i in range(len(order)) if len(set(order[: i + 1])) == len(cast)),
            None,
        ),
        # First-half vs second-half utterance length. The one within-run signal for whether
        # a conversation was still gaining substance when it stopped.
        "chars_first_half": round(sum(lengths[:half]) / half) if half else 0,
        "chars_second_half": (
            round(sum(lengths[half:]) / (len(lengths) - half)) if len(lengths) > half else 0
        ),
    }


# --------------------------------------------------------------------------- #
# Per-run extraction
# --------------------------------------------------------------------------- #

#: The shape the prompt asks for, kept as data so a test can assert the prompt still names
#: every field. Writing this and forgetting to put a JSON contract in the prompt is what
#: made the first run of this module return prose for nine transcripts in a row.
_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "personas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "final_position": {"type": "string"},
                    "demands": {"type": "array", "items": {"type": "string"}},
                    "concessions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "gave_up": {"type": "string"},
                                "because": {"type": "string"},
                            },
                        },
                    },
                    "refusals": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "final_position"],
            },
        },
        "outcome": {"type": "string"},
        "unresolved": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["personas", "outcome"],
}

_EXTRACT_PROMPT = """You are reading one transcript of a multi-participant discussion and \
recording what each participant ended up holding. You are not summarising the discussion and \
you are not judging it.

For every participant, record:
  final_position  where they stood at the end, in one or two sentences, in their terms
  demands         what they required before they would agree — concrete conditions only
  concessions     anything they gave up, and what moved them (quote or paraphrase the cause)
  refusals        what they explicitly would not accept

Then record:
  outcome         what the group actually decided or committed to, in two sentences
  unresolved      questions left open, deferred, or escalated rather than settled

Rules that matter more than completeness:
- A participant who never conceded has an EMPTY concessions list. Do not manufacture movement.
- Do not smooth disagreement into agreement. If two participants agreed to different things, \
record two different things.
- If the discussion did not reach a decision, say so in `outcome`. "Escalated without \
deciding" is a real outcome and the most common one in short conversations.

Transcript:
{transcript}

Return ONLY a JSON object of exactly this shape, with no prose around it and no code fence:

{{"personas": [{{"name": "<participant>",
                "final_position": "<one or two sentences>",
                "demands": ["<concrete condition>"],
                "concessions": [{{"gave_up": "<what>", "because": "<what moved them>"}}],
                "refusals": ["<what they would not accept>"]}}],
 "outcome": "<what the group decided or committed to, in two sentences>",
 "unresolved": ["<question left open, deferred or escalated>"]}}
"""


async def extract_positions(
    view: RunView, *, model: Optional[Any] = None, call: Optional[Any] = None
) -> Dict[str, Any]:
    """One call per run. Returns the parsed extraction, or `{}` if it could not be read.

    `call` is the injection seam — `matrix_studio.analysis._acompletion` in production, a fake in
    tests — for the same reason the analysis layer has one: it keeps the whole module testable
    without a network, and every test in this project that mocks a model mocks exactly one
    function.
    """
    if call is None:
        from matrix_studio.analysis import _acompletion as call  # type: ignore

    transcript = "\n".join(f"[{t}] {who}: {text}" for t, who, text in view.turns)
    result = await call(
        [{"role": "user", "content": _EXTRACT_PROMPT.format(transcript=transcript)}],
        model=model,
        temperature=0.0,
    )
    parsed = extract_json_object(result.get("content", "") or "")
    if parsed is None:
        logger.warning(
            "Could not read the position extraction for run %s; it will be missing from the "
            "ensemble rather than guessed at", view.run_id,
        )
        return {}
    parsed["_cost_usd"] = result.get("cost_usd", 0.0)
    return parsed


# --------------------------------------------------------------------------- #
# Cross-run tallies — computed, not asked
# --------------------------------------------------------------------------- #


def _normalise(claim: str) -> str:
    """A crude key for "is this the same demand". Deliberately crude.

    Clustering claims properly needs a model, and a model that clusters is a model that can
    merge two positions which differ in the way that matters. So this only collapses the
    obvious — case, punctuation, filler — and anything subtler is left for a human or the
    synthesis step to notice. Over-merging here would silently delete a dissent.
    """
    text = re.sub(r"[^a-z0-9 ]+", " ", claim.lower())
    stop = {"the", "a", "an", "of", "to", "for", "and", "or", "in", "on", "is", "be", "that",
            "this", "with", "as", "it", "at", "by", "must", "should", "we", "i"}
    words = [w for w in text.split() if w and w not in stop]
    return " ".join(sorted(set(words)))


def per_persona(views: Sequence[RunView]) -> Dict[str, Dict[str, Any]]:
    """Each persona across every run: what they always demanded, and what varied.

    This is the artefact a persona AUTHOR needs. A conviction that appears in 9 of 9 runs is
    load-bearing; one that appears in 3 of 9 is either a persona that is genuinely persuadable
    or a persona whose position depends on who else got a turn — and the run list attached to
    each claim is what tells those two apart.
    """
    out: Dict[str, Dict[str, Any]] = {}
    runs_with = defaultdict(lambda: defaultdict(list))   # persona -> key -> [run names]
    text_of: Dict[Tuple[str, str], str] = {}
    positions: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    concessions: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
    refusals = defaultdict(lambda: defaultdict(list))
    spoke = Counter()

    for view in views:
        for p in (view.positions.get("personas") or []):
            name = str(p.get("name") or "").strip()
            if not name:
                continue
            spoke[name] += 1
            positions[name].append((view.name, str(p.get("final_position") or "")))
            for d in p.get("demands") or []:
                key = _normalise(str(d))
                if key:
                    runs_with[name][key].append(view.name)
                    text_of.setdefault((name, key), str(d))
            for c in p.get("concessions") or []:
                if isinstance(c, dict) and c.get("gave_up"):
                    concessions[name].append(
                        (view.name, str(c.get("gave_up")), str(c.get("because") or ""))
                    )
            for r in p.get("refusals") or []:
                key = _normalise(str(r))
                if key:
                    refusals[name][key].append(view.name)
                    text_of.setdefault((name, key), str(r))

    total = len(views)
    for name in sorted(spoke, key=lambda n: -spoke[n]):
        demands = runs_with[name]
        out[name] = {
            "appears_in_runs": spoke[name],
            "of_runs": total,
            # Split at "every run in which this persona was extracted at all", not at the
            # ensemble size: a persona who never spoke in a run cannot be said to have
            # dropped a demand there.
            "invariant_demands": [
                {"claim": text_of[(name, k)], "runs": v}
                for k, v in sorted(demands.items(), key=lambda kv: -len(kv[1]))
                if len(v) == spoke[name]
            ],
            "situational_demands": [
                {"claim": text_of[(name, k)], "runs": v}
                for k, v in sorted(demands.items(), key=lambda kv: -len(kv[1]))
                if len(v) < spoke[name]
            ],
            "refusals": [
                {"claim": text_of[(name, k)], "runs": v}
                for k, v in sorted(refusals[name].items(), key=lambda kv: -len(kv[1]))
            ],
            "concessions": [
                {"run": r, "gave_up": g, "because": b} for r, g, b in concessions[name]
            ],
            "final_positions": [{"run": r, "position": p} for r, p in positions[name]],
        }
    return out


def agreements_and_dissents(views: Sequence[RunView]) -> Dict[str, Any]:
    """What every run's outcome shared, and what was left standing.

    A dissent here is defined mechanically: a demand or refusal that its holder carried into
    their final position while the run's `outcome` did not adopt it. That is deliberately
    narrower than "somebody disagreed" — a disagreement that got resolved is not a dissent,
    and the interesting artefact is the objection that survived being answered.
    """
    outcomes = [(v.name, str(v.positions.get("outcome") or "")) for v in views]
    unresolved = defaultdict(list)
    for v in views:
        for u in v.positions.get("unresolved") or []:
            unresolved[_normalise(str(u))].append((v.name, str(u)))

    standing = []
    for v in views:
        outcome = str(v.positions.get("outcome") or "").lower()
        for p in v.positions.get("personas") or []:
            for r in p.get("refusals") or []:
                # A refusal is "standing" when nothing in the recorded outcome echoes it.
                key_words = set(_normalise(str(r)).split())
                if not key_words:
                    continue
                overlap = len(key_words & set(_normalise(outcome).split())) / len(key_words)
                if overlap < 0.34:
                    standing.append(
                        {"run": v.name, "persona": p.get("name"), "refusal": str(r)}
                    )
    return {
        "outcomes": outcomes,
        "unresolved_by_frequency": [
            {"claim": items[0][1], "runs": [r for r, _ in items], "count": len(items)}
            for _, items in sorted(unresolved.items(), key=lambda kv: -len(kv[1]))
        ],
        "standing_refusals": standing,
    }


# --------------------------------------------------------------------------- #
# The synthesis — the only generative step
# --------------------------------------------------------------------------- #

_COMPATIBILITY_RULES = """\
For every pair of positions you compare, choose exactly one verdict and justify it:

  same            the same requirement in different words
  compatible      both can hold at once; say what a design satisfying both looks like
  same-action-different-reason  they require the same thing for reasons that are not the
                  same, which matters because the reasons decide what happens when
                  circumstances change
  incompatible    satisfying one violates the other. ONLY use this if you can name a
                  concrete case that satisfies one and breaks the other. If you cannot name
                  that case, it is not incompatible.

You are forbidden from producing a single reconciled recommendation. This is a report on a
SET of conversations, and collapsing them into one answer destroys the only thing the set is
good for. Where the runs disagree, say that they disagree and attribute each side.

Every claim you make must name the runs it came from, like (fair, hybrid). A claim you cannot
attribute is a claim you should not make.
"""

_SYNTH_PROMPT = """You are collating {n} separate conversations of the SAME brief by the SAME \
cast, run under different settings. Below is a structured extraction from each.

Produce:

1. HELD EVERYWHERE — requirements or conclusions present in every run. For each, name the runs \
and note whether the reason given was the same in each.
2. HELD SOMETIMES — present in some runs and not others. For each, say what distinguishes the \
runs where it appears. The settings are given; if the difference tracks a setting, say so, and \
if it does not, say that instead of inventing one.
3. STANDING DISSENT — objections a participant carried to the end that the outcome did not \
adopt. Who, what, in which runs, and whether anyone ever answered it.
4. CAN THEY COEXIST — take the requirements from (1) and (2) and test them pairwise. Do NOT
enumerate every pair: choose the **{pairs} pairs that matter most** — the ones where a reader
would otherwise assume compatibility — and test those.
{rules}

Be terse. Bullet points, no preamble, no restating the question. The budget is finite and a
report that runs out of room mid-sentence is worth nothing: an over-long reply comes back
EMPTY, not truncated.

The arms and their settings:
{arms}

The extractions:
{extractions}
"""


async def synthesise(
    views: Sequence[RunView],
    *,
    model: Optional[Any] = None,
    call: Optional[Any] = None,
    max_tokens: int = 20000,
    max_pairs: int = 8,
) -> Dict[str, Any]:
    """One call over every extraction. Returns `{content, cost_usd}`.

    Uses the `summary` role by default, which is the right precedent: like a run summary this
    happens once, a human reads it, and `models.py` argues that frequency — not output size —
    is what decides whether a role can afford the strong model.
    """
    if call is None:
        from matrix_studio.analysis import _acompletion as call  # type: ignore

    arms = "\n".join(
        f"  {v.name}: {json.dumps(v.arm)}  "
        f"[{v.metrics.get('turns')} turns, gini {v.metrics.get('gini')}, "
        f"min {v.metrics.get('min_turns')}]"
        for v in views
    )
    extractions = "\n\n".join(
        f"### {v.name}\n{json.dumps({k: x for k, x in v.positions.items() if k != '_cost_usd'}, indent=1)}"
        for v in views if v.positions
    )
    result = await call(
        [{"role": "user", "content": _SYNTH_PROMPT.format(
            n=len(views), rules=_COMPATIBILITY_RULES, arms=arms, extractions=extractions,
            pairs=max_pairs,
        )}],
        model=model,
        temperature=0.2,
        # Explicit, and larger than the `summary` default. The first attempt at this used
        # that default (8000) and spent every token of it without producing a readable
        # word — four sections over nine runs is simply a longer document than a run
        # summary, and the failure mode is silent.
        max_tokens=max_tokens,
    )
    content = (result.get("content") or "").strip()
    if not content:
        # Paid for and empty. Worth its own message because the first run of this hid the
        # cause: the report printed an empty section under a "SYNTHESIS" heading and the
        # only clue was the cost. Today's other empty-content case was a truncated reply
        # (`finish_reason=length` returns nothing, not a fragment), so the token counts are
        # the first thing to look at.
        logger.error(
            "The ensemble synthesis returned no content: %d tokens in, %d out, $%.4f. "
            "A truncated reply comes back empty rather than partial — check the output "
            "budget before assuming the model refused.",
            result.get("tokens_in", 0), result.get("tokens_out", 0),
            result.get("cost_usd", 0.0),
        )
        if result.get("finish_reason") == "length":
            logger.error(
                "It ran out of output budget (max_tokens=%d). Raise it or narrow the "
                "report — this is not a refusal.", max_tokens,
            )
    return {"content": content, "cost_usd": result.get("cost_usd", 0.0),
            "tokens_in": result.get("tokens_in", 0),
            "tokens_out": result.get("tokens_out", 0)}


async def collect(db, run_ids: Sequence[str]) -> List[RunView]:
    """Load each run's transcript, arm settings and metrics. No model calls."""
    views: List[RunView] = []
    for rid in run_ids:
        run = await db.get_run(rid)
        if not run:
            logger.warning("Run %s not found; skipping", rid)
            continue
        cfg = json.loads(run.get("config_json") or "{}")
        sel = cfg.get("selection") or {}
        turns: List[Tuple[int, str, str]] = []
        for e in await db.get_events(rid):
            if e["event_type"] != "agent.response":
                continue
            p = e["payload"]
            p = json.loads(p) if isinstance(p, str) else p
            turns.append((e["turn"], e.get("agent_name") or "?", p.get("message") or ""))
        view = RunView(
            run_id=rid,
            name=run.get("name") or rid[:8],
            arm={
                "method": sel.get("method", "moderated"),
                "max_messages": cfg.get("max_messages"),
                **({"fairness": False} if sel.get("fairness") is False else {}),
                **({"stop_when_converged": True} if sel.get("stop_when_converged") else {}),
                **({"closing_round": True} if sel.get("closing_round") else {}),
                **({"hybrid_opening_rounds": sel["hybrid_opening_rounds"]}
                   if sel.get("hybrid_opening_rounds") and sel.get("method") == "hybrid"
                   else {}),
            },
            turns=turns,
        )
        view.metrics = measure(view)
        views.append(view)
    return views
