# SPDX-License-Identifier: Apache-2.0
"""What a run or an ensemble will cost, forecast from what this account's runs actually cost.

## Why from history and not from a price list

Two estimates in one week were 3–4× low. A §9 comparison was quoted at $2–3 and cost $9.50; an Opus
brainstorm was quoted at $2.50–3 and cost $4.25 (and Bedrock's own log put it at $5.15). Both scaled a
per-token price and missed whole kinds of spend: the image model's avatars, the ensemble report, the
research pass, speaker selection. The stored runs record what every one of those actually cost, so this
module prices a request from them and makes no model call.

## Built from parts, because most of the history is incomplete

Until 2026-09-26 (`d4f852c`) a run recorded only its voice and reflection calls. Those are still exact
and they are most of the spend, so the CONVERSATION part is priced from every finished run. The parts
those runs never recorded — speaker selection and the validation gate — are priced only from runs
created after the cutoff, and say how few there are. Avatars are a fixed price per image. The summary,
the research pass and the ensemble report are stored outside the event log, so their history is
complete for every run that had one.

## It says "not yet measured" rather than inventing a number

A part with no comparable history is reported as unmeasured and the total becomes "at least" the sum
of the parts that are measured. The rule is the one the decision brief keeps: no number without the
history it came from.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

#: Runs created at or after this recorded every model call's cost (`d4f852c`, 2026-09-26 16:17:30 UTC).
ATTRIBUTION_SINCE = 1790439450

#: Event kinds that make up the conversation itself: what each persona says, passes on, or reflects.
CONVERSATION_KINDS = ("agent.response", "agent.passed", "agent.reflected")
#: The per-turn machinery around it. Unrecorded before `ATTRIBUTION_SINCE`.
MODERATION_KINDS = ("speaker.selected", "validation.checked")

#: Runs that ended with a full transcript; a failed run's cost says nothing about a finished one.
FINISHED = frozenset({"complete", "stopped", "capped"})
#: How many runs at the requested cast size are enough to price from those alone.
SAME_CAST_MIN = 3


# --------------------------------------------------------------------------- #
# How many responses a config can produce
# --------------------------------------------------------------------------- #


def response_bounds(config: Mapping[str, Any], cast_size: int, default_max: int) -> Dict[str, int]:
    """The ceiling on persona responses, and how many of the turns involve speaker selection.

    `max_messages` counts TURNS, and what a turn is depends on the method: one speaker when the
    moderator picks (`moderated`), one round of everybody otherwise. Measured on stored runs: a
    simultaneous run at 8 produced 46–52 responses from 6 personas, a hybrid run at 36 produced 47.
    """
    m = int(config.get("max_messages") or default_max)
    sel = config.get("selection") or {}
    method = sel.get("method") or "moderated"
    cast = max(1, cast_size)
    if method in ("rotation", "simultaneous"):
        ceiling, moderated = m * cast, 0
    elif method == "hybrid":
        opening = min(int(sel.get("hybrid_opening_rounds", 2) or 0), m)
        ceiling, moderated = opening * cast + (m - opening), m - opening
    else:
        ceiling, moderated = m, m
    if sel.get("closing_round"):
        # Always a round, always blind — nobody is selected for it.
        ceiling += cast
    return {"ceiling": ceiling, "moderated": moderated}


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


@dataclass
class Observation:
    """One finished run, reduced to what a forecast needs."""

    voice_model: str
    summary_model: str
    cast: int
    cognition: bool
    converge: bool
    responses: int
    ceiling: int
    moderated: int
    conversation_cost: float
    #: None for a run from before `ATTRIBUTION_SINCE`, which did not record it.
    moderation_cost: Optional[float]
    summary_cost: Optional[float]


@dataclass
class History:
    runs: List[Observation] = field(default_factory=list)
    #: Research cost per collection built, from every run or ensemble that researched.
    research_per_collection: List[float] = field(default_factory=list)
    #: Ensemble report cost per member it covered.
    report_per_member: List[float] = field(default_factory=list)


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def observe(run: Mapping[str, Any], summary_cost: Optional[float], default_max: int) -> Optional[Observation]:
    """A stored run (a `list_runs` row, with its stats) as an observation, or None if it teaches nothing."""
    from matrix_studio.models import ModelSet
    from matrix_studio.settings import get_settings

    if run.get("status") not in FINISHED:
        return None
    by_kind = run.get("cost_by_kind") or {}
    conversation = sum(float(by_kind.get(k) or 0.0) for k in CONVERSATION_KINDS)
    responses = int(run.get("turn_count") or 0)
    if responses <= 0 or conversation <= 0:
        # Imported transcripts carry no cost; they would price every future run at zero.
        return None
    config = _json(run.get("config_json"), {}) or _json(run.get("config"), {})
    cast = len(_json(run.get("cast_json"), []) or _json(run.get("cast"), []))
    models = ModelSet.from_config(config)
    default_model = get_settings().litellm_model
    bounds = response_bounds(config, cast, default_max)
    full = int(run.get("created_at") or 0) >= ATTRIBUTION_SINCE
    return Observation(
        voice_model=models.resolve("voice") or default_model,
        summary_model=models.resolve("summary") or default_model,
        cast=cast,
        cognition=bool((config.get("cognition") or {}).get("enabled")),
        converge=bool((config.get("selection") or {}).get("stop_when_converged")),
        responses=responses,
        ceiling=bounds["ceiling"],
        moderated=min(bounds["moderated"], responses),
        conversation_cost=conversation,
        moderation_cost=(
            sum(float(by_kind.get(k) or 0.0) for k in MODERATION_KINDS) if full else None
        ),
        summary_cost=summary_cost,
    )


def research_rate(record: Any) -> Optional[float]:
    """Cost per collection from a stored research record, or None."""
    rec = _json(record, None)
    if not isinstance(rec, dict) or not rec.get("cost_usd"):
        return None
    scopes = len(rec.get("scopes") or []) or 1
    return float(rec["cost_usd"]) / scopes


async def load_history(store: Any, default_max: int) -> History:
    """Everything this owner's store can teach a forecast. Reads only; one pass."""
    history = History()
    for run in await store.list_runs(limit=1000):
        summary_cost: Optional[float] = None
        try:
            rows = await store.get_summaries(run["id"])
            gen = next((r for r in rows if r.get("kind") == "generated"), None)
            if gen and gen.get("cost_usd"):
                summary_cost = float(gen["cost_usd"])
        except Exception:  # noqa: BLE001 — a missing summary is simply no observation
            pass
        obs = observe(run, summary_cost, default_max)
        if obs:
            history.runs.append(obs)
        rate = research_rate(run.get("research_json"))
        if rate:
            history.research_per_collection.append(rate)
    list_ensembles = getattr(store, "list_ensembles", None)
    if list_ensembles:
        for ens in await list_ensembles():
            rate = research_rate(ens.get("research_json"))
            if rate:
                history.research_per_collection.append(rate)
            n = sum(int(c.get("n") or 0) for c in _json(ens.get("spec_json"), []))
            if ens.get("report_cost_usd") and n:
                history.report_per_member.append(float(ens["report_cost_usd"]) / n)
    return history


# A forecast is asked for on every change to the launch form. Its history is the whole run list, so it
# is kept for a few minutes per owner rather than re-read on each keystroke. Stale by at most one run.
_CACHE: Dict[str, tuple] = {}
CACHE_SECONDS = 300


async def cached_history(owner: str, store: Any, default_max: int) -> History:
    hit = _CACHE.get(owner)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    history = await load_history(store, default_max)
    _CACHE[owner] = (time.monotonic(), history)
    return history


# --------------------------------------------------------------------------- #
# Forecasting
# --------------------------------------------------------------------------- #


def _spread(values: Sequence[float]) -> Dict[str, float]:
    """The range the comparable runs actually covered. Min–max rather than a percentile band: the
    point of the forecast is "what past runs like this cost", and a band that hides the dearest one
    under-forecasts exactly the run somebody is about to be surprised by."""
    xs = sorted(values)
    return {"low": xs[0], "typical": statistics.median(xs), "high": xs[-1]}


def _short(model: str) -> str:
    return model.rsplit("/", 1)[-1].replace("global.anthropic.", "").replace("anthropic.", "")


def _part(
    name: str, low: Optional[float], high: Optional[float], basis: str,
    typical: Optional[float] = None,
) -> Dict[str, Any]:
    """`typical` is what the median comparable run cost; the range is what they all covered."""
    measured = low is not None and high is not None
    if measured and typical is None:
        typical = (low + high) / 2
    return {
        "part": name,
        "low": round(low, 4) if measured else None,
        "typical": round(typical, 4) if measured else None,
        "high": round(high, 4) if measured else None,
        "measured": measured,
        "basis": basis,
    }


def _comparable(history: History, voice: str, cast: int, cognition: bool, responses: int) -> tuple:
    """The runs to price a conversation from, and a description of the choice.

    Narrowed by cast size and by LENGTH, most specific first. Length matters because every response
    reads the transcript so far: measured, a 4-turn run cost ~$0.012 a response and a 40-turn run
    ~$0.024, so pricing a short run from long ones doubled it.
    """
    same_model = [o for o in history.runs if o.voice_model == voice]
    similar = lambda o: responses / 2 <= o.responses <= responses * 2  # noqa: E731
    tiers = (
        (lambda o: o.cast == cast and o.cognition == cognition and similar(o), f"{cast} personas, similar length"),
        (lambda o: o.cast == cast and similar(o), f"{cast} personas, similar length"),
        (lambda o: similar(o), "similar length"),
        (lambda o: o.cast == cast, f"{cast} personas"),
    )
    for keep, label in tiers:
        pick = [o for o in same_model if keep(o)]
        if len(pick) >= SAME_CAST_MIN:
            return pick, label
    return same_model, "any cast size or length"


def forecast_run(
    request: Mapping[str, Any],
    history: History,
    *,
    default_model: str,
    default_max: int,
    avatars_default: bool,
    avatar_price: float,
) -> Dict[str, Any]:
    """The cost of ONE run of `request` (a CreateRunModel body, as a dict)."""
    from matrix_studio.models import ModelSet

    config = dict(request.get("config") or {})
    if request.get("model") and not config.get("model"):
        config["model"] = request["model"]
    cast = request.get("cast") or []
    size = len(cast)
    models = ModelSet.from_config(config)
    voice = models.resolve("voice") or default_model
    summary_model = models.resolve("summary") or default_model
    sel = config.get("selection") or {}
    bounds = response_bounds(config, size, default_max)
    cognition = bool((config.get("cognition") or {}).get("enabled"))

    # Responses. A run without early stopping runs to its ceiling (measured: every such stored run did).
    high_n = bounds["ceiling"]
    low_n = typical_n = high_n
    converge_note = ""
    if sel.get("stop_when_converged"):
        ratios = [o.responses / o.ceiling for o in history.runs if o.converge and o.ceiling]
        if len(ratios) >= SAME_CAST_MIN:
            low_n = max(size, int(high_n * min(ratios)))
            typical_n = max(low_n, round(high_n * statistics.median(ratios)))
            converge_note = f"; may stop early — past runs stopped as soon as {min(ratios):.0%} of the limit"
        else:
            low_n = min(high_n, size + 1)
            typical_n = high_n
            converge_note = "; may stop early once everyone has spoken"

    parts: List[Dict[str, Any]] = []

    pool, label = _comparable(history, voice, size, cognition, high_n)
    if pool:
        rate = _spread([o.conversation_cost / o.responses for o in pool])
        parts.append(_part(
            "conversation", low_n * rate["low"], high_n * rate["high"],
            f"{low_n}–{high_n} responses" if low_n != high_n else f"{high_n} responses",
            typical=typical_n * rate["typical"],
        ))
        parts[-1]["basis"] += (
            f" × ${rate['low']:.4f}–{rate['high']:.4f} each, from {len(pool)} past "
            f"{_short(voice)} run(s) with {label}{converge_note}"
        )
    else:
        parts.append(_part(
            "conversation", None, None,
            f"not yet measured — no finished run has used {_short(voice)} for the personas' voice",
        ))

    moderated = bounds["moderated"]
    if moderated:
        full = [o for o in history.runs if o.moderation_cost is not None and o.moderated]
        if full:
            rate = _spread([o.moderation_cost / o.moderated for o in full])
            low_m = min(moderated, low_n)
            parts.append(_part(
                "speaker selection and checks", low_m * rate["low"], moderated * rate["high"],
                f"{moderated} moderated turn(s), from {len(full)} run(s) that recorded it",
                typical=min(moderated, typical_n) * rate["typical"],
            ))
        else:
            parts.append(_part(
                "speaker selection and checks", None, None,
                "not yet measured — no run since 2026-09-26 has recorded it",
            ))

    if config.get("generate_avatars", avatars_default):
        cost = size * avatar_price
        parts.append(_part("avatars", cost, cost, f"{size} image(s) × ${avatar_price:.2f}, a fixed price"))

    if (request.get("summary") or {}).get("enabled", True):
        seen = [o.summary_cost for o in history.runs if o.summary_model == summary_model and o.summary_cost]
        if seen:
            s = _spread(seen)
            parts.append(_part(
                "summary", s["low"], s["high"], f"from {len(seen)} past {_short(summary_model)} summaries",
                typical=s["typical"],
            ))
        else:
            parts.append(_part(
                "summary", None, None, f"not yet measured for {_short(summary_model)}",
            ))

    return {
        "voice_model": voice,
        "responses": {"low": low_n, "typical": typical_n, "high": high_n},
        "parts": parts,
        **_total(parts),
    }


def research_collections(request: Mapping[str, Any]) -> int:
    """How many collections a research pass would build: the shared one, plus one per persona
    with a viewpoint to research. The same count the launch button shows."""
    research = (request.get("config") or {}).get("research") or {}
    if not research.get("enabled"):
        return 0
    n = 1 if research.get("shared", True) else 0
    if research.get("personas", True):
        n += sum(
            1 for c in request.get("cast") or []
            if ((c.get("structured") or {}).get("viewpoints") or [])
        )
    return n


def research_part(request: Mapping[str, Any], history: History) -> Optional[Dict[str, Any]]:
    n = research_collections(request)
    if not n:
        return None
    if not history.research_per_collection:
        return _part("research", None, None, f"{n} collection(s); not yet measured")
    r = _spread(history.research_per_collection)
    return _part(
        "research", n * r["low"], n * r["high"],
        f"{n} collection(s), from {len(history.research_per_collection)} past research pass(es); "
        "runs once, before the conversation",
        typical=n * r["typical"],
    )


def _total(parts: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    parts = list(parts)
    measured = [p for p in parts if p["measured"]]
    return {
        "low": round(sum(p["low"] for p in measured), 4),
        "typical": round(sum(p["typical"] for p in measured), 4),
        "high": round(sum(p["high"] for p in measured), 4),
        # False means the total is "at least": something it would spend on is not yet priced.
        "complete": len(measured) == len(parts),
        "unmeasured": [p["part"] for p in parts if not p["measured"]],
    }


def combine(
    run_forecasts: Sequence[Mapping[str, Any]],
    extra_parts: Sequence[Optional[Mapping[str, Any]]],
) -> Dict[str, Any]:
    """Several runs plus once-only parts (research, the ensemble report) as one forecast.

    Parts with the same name are summed across runs, so an ensemble reads as "conversations ×N", not
    as N copies of a single-run forecast.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for f in run_forecasts:
        for p in f["parts"]:
            m = merged.setdefault(p["part"], {**p, "low": 0.0, "typical": 0.0, "high": 0.0, "runs": 0})
            m["runs"] += 1
            if p["measured"] and m["measured"]:
                for k in ("low", "typical", "high"):
                    m[k] += p[k]
            else:
                m.update(measured=False, low=None, typical=None, high=None, basis=p["basis"])
    parts = []
    for p in merged.values():
        if p["measured"]:
            for k in ("low", "typical", "high"):
                p[k] = round(p[k], 4)
        if p["runs"] > 1:
            p["basis"] = f"×{p['runs']} runs; each: {p['basis']}"
        parts.append(p)
    parts += [p for p in extra_parts if p]
    return {"runs": len(run_forecasts), "parts": parts, **_total(parts)}


def report_part(members: int, history: History) -> Dict[str, Any]:
    if not history.report_per_member:
        return _part("ensemble report", None, None, "not yet measured")
    r = _spread(history.report_per_member)
    return _part(
        "ensemble report", members * r["low"], members * r["high"],
        f"{members} run(s) × ${r['low']:.3f}–{r['high']:.3f} each, from {len(history.report_per_member)} past report(s)",
        typical=members * r["typical"],
    )
