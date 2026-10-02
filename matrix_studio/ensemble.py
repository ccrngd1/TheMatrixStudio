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

import asyncio
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
        # The outcome broken into its separate decisions, so they can be counted ACROSS runs.
        # Not required: an extraction from before this field existed, or one where the group
        # decided nothing, has none — and "none" is itself the finding in that case.
        "conclusions": {"type": "array", "items": {"type": "string"}},
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
  conclusions     the same decisions as separate items, ONE decision per item, each worded
                  plainly enough that the same decision reached in a different discussion
                  would read the same (e.g. "exclude California from launch", not "they agreed
                  Morgan's revised scope was acceptable")
  unresolved      questions left open, deferred, or escalated rather than settled

Rules that matter more than completeness:
- A participant who never conceded has an EMPTY concessions list. Do not manufacture movement.
- Do not smooth disagreement into agreement. If two participants agreed to different things, \
record two different things.
- If the discussion did not reach a decision, say so in `outcome`. "Escalated without \
deciding" is a real outcome and the most common one in short conversations.
- A conclusion is something the GROUP reached, not one participant's view. If they decided \
nothing, `conclusions` is EMPTY — do not promote a proposal, a preference or a majority lean \
into a conclusion.

Transcript:
{transcript}

Return ONLY a JSON object of exactly this shape, with no prose around it and no code fence:

{{"personas": [{{"name": "<participant>",
                "final_position": "<one or two sentences>",
                "demands": ["<concrete condition>"],
                "concessions": [{{"gave_up": "<what>", "because": "<what moved them>"}}],
                "refusals": ["<what they would not accept>"]}}],
 "outcome": "<what the group decided or committed to, in two sentences>",
 "conclusions": ["<one decision the group reached>"],
 "unresolved": ["<question left open, deferred or escalated>"]}}
"""


#: Output budget for one member's position extraction. Until 2026-10-02 it had no budget of its
#: own and inherited the summary's, `settings.summary_max_tokens`. That went from 8,000 to 16,000
#: for summaries with a long focus, which this call does not have. Kept at 8,000 so that change
#: does not also lengthen the ensemble report, which runs closest to Lambda's 15 minutes.
EXTRACT_MAX_TOKENS = 8000


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
        max_tokens=EXTRACT_MAX_TOKENS,
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


#: `(kind, text) -> (cluster key, canonical label)`, from `apply_clusters`.
#:
#: Threaded into every section that COUNTS, so the claim table, the per-persona view and the
#: unresolved tally all group claims the same way. They did not, briefly, and the result was a
#: table reporting a unanimous finding beside a persona section reporting zero invariant
#: demands — from the same extractions.
Canonical = Dict[Tuple[str, str], Tuple[str, str]]


def _keyed(canonical: Optional[Canonical], kind: str, text: str) -> Tuple[str, str]:
    """The key and label for one claim: clustered where available, text-matched otherwise."""
    if canonical is not None:
        got = canonical.get((kind, text))
        if got is not None:
            return got
    return _normalise(text), text


def per_persona(
    views: Sequence[RunView], canonical: Optional[Canonical] = None
) -> Dict[str, Dict[str, Any]]:
    """Each persona across every run: what they always demanded, and what varied.

    This is the artefact a persona AUTHOR needs. A conviction that appears in 9 of 9 runs is
    load-bearing; one that appears in 3 of 9 is either a persona that is genuinely persuadable
    or a persona whose position depends on who else got a turn — and the run list attached to
    each claim is what tells those two apart.

    `canonical` is what makes `invariant_demands` mean anything. Without it this keyed on
    `_normalise`, and on the first live ensemble EVERY persona reported zero invariant demands
    across five runs of an identical brief — a persona who demanded the same thing five times in
    five different sentences looked like one who had changed their mind five times.
    """
    out: Dict[str, Dict[str, Any]] = {}
    runs_with = defaultdict(lambda: defaultdict(list))   # persona -> key -> [run names]
    text_of: Dict[Tuple[str, str], str] = {}
    positions: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    concessions: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
    refusals = defaultdict(lambda: defaultdict(list))
    spoke = Counter()
    # Runs in which this persona named at least one demand. The DENOMINATOR for `invariant`,
    # and not the same thing as the runs they appeared in — see the split below.
    demanded_in: Dict[str, set] = defaultdict(set)

    for view in views:
        for p in (view.positions.get("personas") or []):
            name = str(p.get("name") or "").strip()
            if not name:
                continue
            spoke[name] += 1
            positions[name].append((view.name, str(p.get("final_position") or "")))
            for d in p.get("demands") or []:
                key, label = _keyed(canonical, "demand", str(d))
                if key:
                    runs_with[name][key].append(view.name)
                    text_of.setdefault((name, key), label)
                    demanded_in[name].add(view.name)
            for c in p.get("concessions") or []:
                if isinstance(c, dict) and c.get("gave_up"):
                    concessions[name].append(
                        (view.name, str(c.get("gave_up")), str(c.get("because") or ""))
                    )
            for r in p.get("refusals") or []:
                key, label = _keyed(canonical, "refusal", str(r))
                if key:
                    refusals[name][key].append(view.name)
                    text_of.setdefault((name, key), label)

    total = len(views)
    for name in sorted(spoke, key=lambda n: -spoke[n]):
        demands = runs_with[name]
        out[name] = {
            "appears_in_runs": spoke[name],
            "of_runs": total,
            # The denominator `invariant` is judged against, reported so a reader can see it.
            # "Invariant across 2 runs" and "invariant across 5" are different claims.
            "demanded_in_runs": len(demanded_in[name]),
            # Split at "every run in which this persona named ANY demand", not at the runs they
            # appeared in and not at the ensemble size.
            #
            # The earlier denominator was runs-appeared-in, and it made the metric unreachable
            # for real personas: measured on a 5-run ensemble, Jordan was extracted in all five and
            # had demands recorded in only two, so no demand of his could ever be invariant
            # however well the clustering worked. That is not a persona who changed his mind —
            # the extractor recorded no demand for him in three runs, and silence is not a
            # retraction. The old comment reasoned about a persona who never spoke and stopped
            # one step short of this case.
            "invariant_demands": [
                {"claim": text_of[(name, k)], "runs": v}
                for k, v in sorted(demands.items(), key=lambda kv: -len(kv[1]))
                if demanded_in[name] and len(v) == len(demanded_in[name])
            ],
            "situational_demands": [
                {"claim": text_of[(name, k)], "runs": v}
                for k, v in sorted(demands.items(), key=lambda kv: -len(kv[1]))
                if not demanded_in[name] or len(v) < len(demanded_in[name])
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


def agreements_and_dissents(
    views: Sequence[RunView], canonical: Optional[Canonical] = None
) -> Dict[str, Any]:
    """What every run's outcome shared, and what was left standing.

    A dissent here is defined mechanically: a demand or refusal that its holder carried into
    their final position while the run's `outcome` did not adopt it. That is deliberately
    narrower than "somebody disagreed" — a disagreement that got resolved is not a dissent,
    and the interesting artefact is the objection that survived being answered.

    `canonical` groups the unresolved questions. Without it every one of them counted 1 on the
    first live ensemble, so "the same question was left open in all five runs" — the single most
    useful thing this section can say — was unsayable.
    """
    outcomes = [(v.name, str(v.positions.get("outcome") or "")) for v in views]
    unresolved = defaultdict(list)
    for v in views:
        for u in v.positions.get("unresolved") or []:
            key, label = _keyed(canonical, "unresolved", str(u))
            if key:
                unresolved[key].append((v.name, label))

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
# Canonical labels — making the counts mean something
# --------------------------------------------------------------------------- #
#
# `_normalise` keys on sorted unique non-stopword tokens, so ANY wording difference is a
# different claim. Measured on the first live 5-replicate ensemble: 128 of 128 claims counted
# 1/5, every persona reported zero invariant demands, and all 24 unresolved groups had count 1.
# The computed half of the report produced no signal at all, while the synthesis — reading the
# same extractions — found four conclusions held in 5 of 5. Two halves of one report
# contradicting each other, with the numeric half looking the more authoritative.
#
# So the counts need a model. The risk is exactly the one `_normalise` was crude to avoid:
# **over-merging deletes a dissent**, and that failure is invisible in the output. Three things
# hold it in check, and none of them is the prompt alone:
#
#   bias        the prompt's rule is "same REQUIREMENT, not same topic", and when in doubt
#               keep separate. A cluster of one is an acceptable answer.
#   audit       every cluster carries its member phrasings and the runs they came from, so a
#               reader can see what was merged and disagree. A clustering nobody can inspect
#               replaces one wrong number with a different wrong number.
#   arithmetic  `apply_clusters` REFUSES a clustering that drops, duplicates or invents a
#               claim, or that mixes a demand with a refusal. Those are checkable in code, and
#               a model that silently drops claims would under-report exactly like the bug
#               this replaces.

_CLUSTER_PROMPT = """You are grouping claims extracted from several independent runs of the \
SAME discussion, so they can be counted. Different participants in different runs often \
express the same requirement in different words; the count is meaningless unless those are \
recognised as one claim.

Group the numbered claims below into clusters. Each cluster is ONE requirement.

THE RULE, and it decides every hard case: group two claims only when satisfying one would \
satisfy the other. Same TOPIC is not enough. Same requirement at a different threshold, \
scope, or trigger is a DIFFERENT claim.

Worked examples of what must stay apart:
  "a state statute defining plans as regulated products" vs "a state practice act that \
reaches treatment, not just drugs" — different legal triggers. Satisfying one does not \
satisfy the other. SEPARATE.
  "a 90-day purchase window" vs "a 45-day window" — same mechanism, different threshold. \
SEPARATE.
  "records pull before approval" vs "records pull before approval for net-new only" — \
different scope. SEPARATE.

Worked examples of what should group:
  "verified weight or a ordering note" / "a confirmed weight or the original ordering \
note" — same requirement, different words. GROUP.
  "she will not sign off without a synchronous exam" / "refuses to approve absent a live \
examination" — same requirement. GROUP.

When you are unsure, DO NOT GROUP. A cluster containing one claim is a correct answer and \
costs nothing. Merging two claims that differ deletes a disagreement from the report, and \
nobody reading the report can tell that it happened.

Never put a `demand` and a `refusal` in the same cluster: requiring something and refusing \
something are different acts even when they concern the same subject.

Each claim is tagged with the participant who made it, where that is known. **Claims from \
DIFFERENT participants are usually different requirements even when they sound alike**, because \
each person wants a thing for their own reasons and would be satisfied by different evidence. \
You are looking for one person restating themselves across runs. Two claims made by different \
people are strong evidence of two requirements — group them only if you would still group them \
with the names hidden.

Give each cluster a `label`: the requirement in under 15 words, in the participants' own \
vocabulary, neutral as to whether anyone agreed.

Every claim number must appear in exactly one cluster. Do not omit any. Do not invent any.

Reply with ONLY a JSON object, no prose:
{{"clusters": [{{"label": "<under 15 words>", "members": [<claim numbers>]}}]}}

The claims:
{claims}
"""


def _cluster_input(claims: Sequence[Sequence[str]]) -> str:
    """Claims as a numbered list for the prompt, with the speaker when it is known.

    The speaker matters more than it looks. Over-merging showed up repeatedly as two claims
    from DIFFERENT participants in the SAME run being combined — "will not accept 'no complaint
    history' as an answer to HER mechanism question" merged with "will not extend HIS
    absence-of-complaints data to a broader model". Two people, two requirements, one row.

    Two claims in one run cannot be one participant restating themselves across runs, which is
    what the clustering is for; and a name is the cheapest possible signal of that.
    """
    lines = []
    for i, claim in enumerate(claims):
        kind, text = claim[0], claim[1]
        who = claim[2] if len(claim) > 2 else None
        lines.append(f"{i}. [{kind}{f' / {who}' if who else ''}] {text}")
    return "\n".join(lines)


#: Output budget per clustering call.
#:
#: **Size this for the model's REASONING, not for the answer.** Measured on 65 refusal claims
#: from the first live ensemble: the JSON reply was 4,332 characters — about 1,100 tokens — and
#: `tokens_out` was **29,260**. Roughly 28,000 output tokens were reasoning, which is billed and
#: counted against `max_tokens`. So the size of the answer tells you nothing about the budget
#: needed, and inspecting the reply to estimate one is actively misleading.
#:
#: The failure is silent, which is what makes it expensive: a reply cut off by `max_tokens`
#: comes back EMPTY rather than partial. 8000 and then 24000 both produced
#: `finish_reason=length` with no content and a real bill ($0.09, then $0.25). 60000 returned
#: `finish=stop` on the same input, so 64000 is that with headroom rather than another guess.
#:
#: Fourth occurrence of the empty-truncated-reply trap in this project (120-token speaker
#: selection, the closing round, the 8000-token synthesis, and this). `finish_reason` is the
#: only thing that distinguishes it from a refusal.
CLUSTER_MAX_TOKENS = 64000

#: Claims per clustering call, within one kind. `None` would mean one call per kind.
#:
#: Measured: Haiku 4.5 left 5 of 72 refusal claims UNASSIGNED in a single call — the fatal
#: class — and completed cleanly at 24 per call, three times out of three. So the limit is how
#: many items it carries through one reply, not capability. Sonnet 5 completes 72 but spends
#: ~29,000 output tokens of reasoning doing it, which is minutes of wall-clock inside a Lambda.
#:
#: A batched call can only group WITHIN its batch, so batching makes the label-merge pass
#: load-bearing rather than optional — `cluster_claims_twice` is the complete operation.
CLUSTER_BATCH_SIZE = 24


async def _cluster_one_kind(
    kind: str,
    indexed: Sequence[Tuple[int, str, str]],
    *,
    model: Optional[Any],
    call: Any,
    max_tokens: int,
) -> Optional[Tuple[List[Dict[str, Any]], float]]:
    """Cluster one kind's claims. Returns `(clusters over GLOBAL indices, cost)` or None.

    `indexed` is `[(global index, text, speaker)]`. The speaker is passed through to the prompt
    rather than dropped here — it is the signal that two similar-sounding claims belong to two
    people and are therefore two requirements.
    """
    local = [(kind, text, who) for _, text, who in indexed]
    result = await call(
        [{"role": "user", "content": _CLUSTER_PROMPT.format(claims=_cluster_input(local))}],
        model=model,
        # 0.0 is REQUESTED and, on the deployed path, silently DROPPED: this role resolves to
        # Sonnet 5, which accepts only `temperature=1` (`models.py:11`), and litellm's
        # `drop_params` discards the rest without erroring. So do not read this as a
        # determinism guarantee — regenerating a report can legitimately produce different
        # groupings, and the `variants` audit trail is what compensates for that rather than
        # this argument.
        #
        # Asked for anyway, because the role is overridable: a caller who points clustering at
        # a temperature-honouring model gets the reproducibility, and dropping the argument
        # would silently deny it to them.
        temperature=0.0,
        max_tokens=max_tokens,
    )
    cost = float(result.get("cost_usd") or 0.0)
    content = (result.get("content") or "").strip()
    if not content:
        logger.error(
            "Clustering %d %s claim(s) returned no content: %d in, %d out, $%.4f, finish=%s. "
            "A truncated reply comes back EMPTY rather than partial, so check the output "
            "budget (max_tokens=%d) before assuming a refusal.",
            len(local), kind, result.get("tokens_in", 0), result.get("tokens_out", 0),
            cost, result.get("finish_reason"), max_tokens,
        )
        return None

    parsed = extract_json_object(content)
    clusters = (parsed or {}).get("clusters")
    if not isinstance(clusters, list):
        logger.warning(
            "Clustering %s claims did not return a `clusters` list; counts will fall back to "
            "text matching and the report will say so.", kind,
        )
        return None

    # Local indices back to global ones. The model is shown one kind at a time numbered from
    # zero, so this mapping is the whole reason the split is safe.
    out: List[Dict[str, Any]] = []
    for c in clusters:
        if not isinstance(c, dict):
            continue
        members = c.get("members")
        if not isinstance(members, list):
            # Passed through unmapped so `apply_clusters` refuses it by its own rules rather
            # than this function inventing a repair.
            out.append({"label": c.get("label"), "members": members})
            continue
        mapped = []
        for raw in members:
            try:
                i = int(raw)
            except (TypeError, ValueError):
                mapped.append(raw)
                continue
            mapped.append(indexed[i][0] if 0 <= i < len(indexed) else -1)
        out.append({"label": c.get("label"), "members": mapped})
    return out, cost


async def cluster_claims(
    claims: Sequence[Tuple[str, str]],
    *,
    model: Optional[Any] = None,
    call: Optional[Any] = None,
    max_tokens: int = CLUSTER_MAX_TOKENS,
    batch_size: Optional[int] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Group `[(kind, text)]` into canonical clusters. `None` if it could not be done.

    **One call per kind.** Demands and refusals are clustered separately, which makes the
    "never merge a demand with a refusal" rule structural instead of only a check afterwards —
    the model is never shown a mixture, so it cannot propose one. It also halves each call's
    output, which is what the 8000-token failure was really about.

    **`batch_size` splits a kind further**, and a batched call can only group claims WITHIN its
    batch — joining across batches is the merge pass's job, so `cluster_claims_twice` is the
    complete operation when batching. Measured reason to batch: Haiku 4.5 left 5 of 72 refusal
    claims unassigned in one call and completed cleanly at 24 per call, so the limit is task size
    rather than capability. Sonnet 5 completes 72 but spends ~29,000 output tokens of reasoning
    doing it, which is minutes of wall-clock.

    A kind with a single claim is given its own cluster without a call: there is nothing to
    group, and paying to be told so is waste.

    `None` rather than a guess, and the caller falls back to `_normalise` and says so in the
    report's caveats. If ANY kind fails, the whole clustering is abandoned: clustered demands
    beside unclustered refusals would make half the table trustworthy with no way for a reader
    to tell which half.
    """
    if len(claims) < 2:
        return None
    if call is None:
        from matrix_studio.analysis import _acompletion as call  # type: ignore

    by_kind: Dict[str, List[Tuple[int, str, str]]] = defaultdict(list)
    for i, claim in enumerate(claims):
        kind, text = claim[0], claim[1]
        who = claim[2] if len(claim) > 2 else ""
        by_kind[kind].append((i, text, who))

    combined: List[Dict[str, Any]] = []
    cost = 0.0

    # Kinds with one claim need no call at all; grouping a single item is nothing to ask for.
    # Everything else is split into batches — see `CLUSTER_BATCH_SIZE`.
    to_call: List[Tuple[str, List[Tuple[int, str]]]] = []
    for kind in sorted(by_kind):
        indexed = by_kind[kind]
        if len(indexed) == 1:
            i, text, _who = indexed[0]
            combined.append({"label": text, "members": [i]})
            continue
        size = batch_size or len(indexed)
        for start in range(0, len(indexed), size):
            to_call.append((kind, indexed[start:start + size]))

    # Concurrently, across kinds AND batches. Every call is independent, and each one spends
    # minutes on reasoning: measured at ~29,000 output tokens to produce a ~1,100-token answer.
    # Running them in sequence doubled the report's wall-clock for nothing, and wall-clock is
    # what has to fit inside a Lambda.
    results = await asyncio.gather(*(
        _cluster_one_kind(kind, indexed, model=model, call=call, max_tokens=max_tokens)
        for kind, indexed in to_call
    ))
    for got in results:
        # All or nothing: clustered demands beside unclustered refusals would leave half the
        # table trustworthy with no way for a reader to tell which half. The same applies across
        # batches of one kind — a batch that failed would leave its claims ungrouped while its
        # neighbours were grouped, and nothing in the output would say which was which.
        if got is None:
            return None
        clusters, spent = got
        combined.extend(clusters)
        cost += spent

    if not combined:
        return None
    combined[0]["_cost_usd"] = cost
    return combined


_MERGE_PROMPT = """These are groups of claims from several independent runs of the same \
discussion. Each group is already meant to be ONE requirement, but the same requirement may \
have been split across several groups because the runs worded it differently.

Find only the groups that are the SAME requirement and should be one.

THE RULE is unchanged and still decides every hard case: merge two groups only when satisfying \
one would satisfy the other. Same topic is not enough. A different threshold, scope or trigger \
is a DIFFERENT requirement and must stay separate.

  "verified weight before approval" + "confirmed weight required first" — same. MERGE.
  "90-day purchase window" + "45-day window" — different threshold. SEPARATE.
  "statute defining plans as regulated" + "practice act reaching treatment" — different \
legal triggers. SEPARATE.

Most groups will merge with nothing. That is the expected answer, not a failure — leave them \
out of your reply entirely rather than listing them alone.

Reply with ONLY a JSON object, no prose. `merge` lists sets of group numbers that are one \
requirement, and `label` is that requirement in under 15 words:
{{"merge": [{{"label": "<under 15 words>", "groups": [<group numbers>]}}]}}

If nothing should merge, reply {{"merge": []}}.

The groups:
{groups}
"""


async def _merge_clusters(
    clusters: Sequence[Dict[str, Any]],
    *,
    model: Optional[Any] = None,
    call: Optional[Any] = None,
    max_tokens: int = CLUSTER_MAX_TOKENS,
) -> Optional[Tuple[List[Dict[str, Any]], float]]:
    """A second pass over cluster LABELS. Returns `(merged clusters, cost)` or None.

    The first pass compares long sentences and is deliberately reluctant, which leaves one
    requirement spread over several groups: measured on the live ensemble at zero demand
    clusters spanning all five runs, so no persona had a single invariant demand across five
    runs of an identical brief.

    Comparing the LABELS is a different and much easier task — 86 short phrases rather than 150
    long sentences — so the same reluctance costs less recall. The rule is identical, because
    loosening it is the one change that would delete a dissent invisibly.

    Merging is done here in code from the model's group numbers; the model never restates a
    claim, so it cannot drop or invent one. Anything it fails to mention stays exactly as the
    first pass left it, which makes "no merges" a safe answer rather than a lost result.
    """
    if len(clusters) < 2:
        return None
    if call is None:
        from matrix_studio.analysis import _acompletion as call  # type: ignore

    listing = "\n".join(
        f"{i}. {c.get('label')}" for i, c in enumerate(clusters)
    )
    result = await call(
        [{"role": "user", "content": _MERGE_PROMPT.format(groups=listing)}],
        model=model,
        temperature=0.0,
        max_tokens=max_tokens,
    )
    cost = float(result.get("cost_usd") or 0.0)
    content = (result.get("content") or "").strip()
    if not content:
        logger.error(
            "The cluster merge pass returned no content: %d in, %d out, $%.4f, finish=%s. "
            "A truncated reply comes back EMPTY rather than partial — check the output budget "
            "(max_tokens=%d).",
            result.get("tokens_in", 0), result.get("tokens_out", 0), cost,
            result.get("finish_reason"), max_tokens,
        )
        return None

    parsed = extract_json_object(content)
    merges = (parsed or {}).get("merge")
    if not isinstance(merges, list):
        logger.warning("The cluster merge pass did not return a `merge` list; keeping pass one.")
        return None

    # Apply in code. A group named twice is dropped from the second set rather than merging two
    # sets transitively: transitive merging is how "A is like B, B is like C" quietly unites A
    # and C, which nobody asserted and which is the over-merge this whole design resists.
    taken: set = set()
    out: List[Dict[str, Any]] = []
    for m in merges:
        if not isinstance(m, dict):
            continue
        group_ids = m.get("groups")
        label = str(m.get("label") or "").strip()
        if not isinstance(group_ids, list) or len(group_ids) < 2 or not label:
            continue
        members: List[Any] = []
        used: List[int] = []
        for raw in group_ids:
            try:
                g = int(raw)
            except (TypeError, ValueError):
                continue
            if not 0 <= g < len(clusters) or g in taken:
                continue
            used.append(g)
            members.extend(clusters[g].get("members") or [])
        if len(used) < 2 or not members:
            continue
        taken.update(used)
        out.append({"label": label, "members": members})

    for i, c in enumerate(clusters):
        if i not in taken:
            out.append({"label": c.get("label"), "members": c.get("members")})
    return out, cost


async def cluster_claims_twice(
    claims: Sequence[Tuple[str, str]],
    *,
    model: Optional[Any] = None,
    call: Optional[Any] = None,
    batch_size: Optional[int] = CLUSTER_BATCH_SIZE,
) -> Optional[Tuple[List[Dict[str, Any]], float]]:
    """`cluster_claims` followed by a merge pass over the labels. `(clusters, cost)` or None.

    With `batch_size` set — the default — pass one can only group within a batch, so the merge
    pass is what joins equivalent claims across batches. It is therefore load-bearing here in a
    way it is not for a single unbatched call, and its failure costs real recall rather than a
    little extra.

    The merge pass failing is still not FATAL: pass one's result is returned unchanged, because
    even a batch-local grouping is far better than text matching, which grouped nothing at all.
    """
    first = await cluster_claims(
        claims, model=model, call=call, batch_size=batch_size,
    )
    if not first:
        return None
    cost = float(first[0].get("_cost_usd") or 0.0)

    # Partitioned by kind, and merged one kind at a time. The first pass gets the no-mixing rule
    # structurally by never showing the model a mixture; the merge pass sees only LABELS, from
    # which the kind is invisible — so shown all of them together it merged a demand group with a
    # refusal group on the first live attempt. `apply_clusters` caught it and refused the whole
    # clustering, which is the arithmetic net doing its job, but the right fix is to make the
    # rule structural here too rather than to ask the model to respect a distinction it cannot
    # see.
    by_kind: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for cluster in first:
        members = cluster.get("members") or []
        kinds = {claims[i][0] for i in members if isinstance(i, int) and 0 <= i < len(claims)}
        # A cluster whose kind is ambiguous is left out of the merge pass entirely rather than
        # assigned a guess; it survives as the first pass produced it.
        by_kind[next(iter(kinds)) if len(kinds) == 1 else ""].append(cluster)

    merged_all: List[Dict[str, Any]] = list(by_kind.pop("", []))
    spent_all = 0.0
    kinds_to_merge = sorted(by_kind)
    results = await asyncio.gather(*(
        _merge_clusters(by_kind[k], model=model, call=call) for k in kinds_to_merge
    ))
    for kind, got in zip(kinds_to_merge, results):
        if got is None:
            # Not fatal for this kind or any other: the point of the second pass is extra
            # recall, so its failure returns pass one's grouping rather than nothing.
            logger.info(
                "The merge pass produced nothing usable for %s claims; keeping pass one.", kind,
            )
            merged_all.extend(by_kind[kind])
            continue
        merged, spent = got
        merged_all.extend(merged)
        spent_all += spent

    if merged_all:
        merged_all[0]["_cost_usd"] = cost + spent_all
    return merged_all, cost + spent_all


class ClusteringRejected(ValueError):
    """A clustering that would change the counts rather than only group them."""


#: Share of claims that may be assigned twice before the clustering is refused outright.
#:
#: A duplicate is repaired rather than fatal (see `apply_clusters`), but a model producing them
#: in bulk is not doing the task, and repairing 40% of its answer would be pretending otherwise.
MAX_DUPLICATE_SHARE = 0.1


def apply_clusters(
    claims: Sequence[Tuple[str, str]], clusters: Sequence[Dict[str, Any]]
) -> Dict[int, Tuple[str, str]]:
    """`claim index -> (cluster key, label)`, or raise if the clustering is unsound.

    The checks are arithmetic, not taste, and they are the reason this is safe to trust at all:
    a model asked to group 128 claims can quietly drop twenty, and the result would under-count
    exactly like the bug being fixed here — while looking like a fix.

    **Two classes of fault, and they are not equivalent.**

    *Fatal* — a claim unassigned, invented, or a cluster mixing a demand with a refusal. Each
    changes the counts in the direction that misleads: a dropped claim under-counts exactly like
    the text matching this replaces, and a mixed cluster asserts that requiring something and
    refusing it are one act.

    *Repaired* — a claim assigned to TWO clusters. First assignment wins. This is safe in the
    only direction that matters here: the losing cluster ends up one member short, so the worst
    case is a merge that does not happen. Nothing is deleted and no count is inflated. Rejecting
    the whole clustering for it would throw away a good grouping over a bookkeeping slip, and it
    is a slip small models actually make — Haiku 4.5 duplicated one claim of 153 while being
    otherwise reproducible, where a rejection cost the entire result.

    Beyond `MAX_DUPLICATE_SHARE` the repair stops being a repair and the clustering is refused.
    """
    duplicates: List[int] = []
    seen: Dict[int, Tuple[str, str]] = {}
    for n, cluster in enumerate(clusters):
        members = cluster.get("members")
        label = str(cluster.get("label") or "").strip()
        if not isinstance(members, list) or not members:
            raise ClusteringRejected(f"cluster {n} has no members")
        if not label:
            raise ClusteringRejected(f"cluster {n} has no label")

        kinds = set()
        for raw in members:
            try:
                i = int(raw)
            except (TypeError, ValueError):
                raise ClusteringRejected(f"cluster {n} has a non-numeric member {raw!r}")
            if not 0 <= i < len(claims):
                raise ClusteringRejected(
                    f"cluster {n} cites claim {i}, which does not exist "
                    f"(there are {len(claims)})"
                )
            if i in seen:
                # First assignment wins. Recorded rather than silently tolerated, because a
                # rising count is how a model drifting off-task would show itself.
                duplicates.append(i)
                continue
            kinds.add(claims[i][0])
            seen[i] = (f"c{n}", label)
        if len(kinds) > 1:
            raise ClusteringRejected(
                f"cluster {n} mixes {sorted(kinds)} — requiring something and refusing "
                "something are different acts"
            )

    # `max(1, ...)`, so ONE duplicate is always repairable however few claims there are. A
    # proportion alone would make a single slip fatal on a small ensemble and tolerable on a
    # large one, which is backwards: the slip is the same slip, and it costs at most one merge.
    allowed = max(1, int(MAX_DUPLICATE_SHARE * len(claims)))
    if len(duplicates) > allowed:
        raise ClusteringRejected(
            f"{len(duplicates)} of {len(claims)} claim(s) were assigned to more than one "
            f"cluster, over the repair limit of {allowed}. At this rate the grouping is not "
            "being done, and repairing it would be pretending otherwise."
        )
    if duplicates:
        logger.info(
            "Repaired %d duplicate assignment(s) in the clustering (first wins): claims %s. "
            "The effect is at most a merge that does not happen.",
            len(duplicates), sorted(set(duplicates))[:8],
        )

    missing = [i for i in range(len(claims)) if i not in seen]
    if missing:
        raise ClusteringRejected(
            f"{len(missing)} claim(s) were not assigned to any cluster "
            f"(first few: {missing[:5]}). Dropping claims under-counts exactly like the "
            "text matching this replaces."
        )
    return seen


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
