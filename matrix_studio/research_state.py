# SPDX-License-Identifier: Apache-2.0
"""The Research state: `docs/PERSONA-RESEARCH.md` §11 step 4.

`research.py` knows how to search, tier and ingest. This module knows **where a corpus is allowed to
go, and what happens when it cannot go anywhere** — which is ownership, binding and additive
failure, none of which belongs in the searcher.

Three jobs, and the split between the first two is the whole design:

- `allocate_targets` runs at **creation time**, inside `POST /api/runs`, and creates the ingest
  target for every scope: a NEW collection, marked as this pass's, whose id is appended to that
  scope's bindings before anything is searched (§5.1). Never a collection that was already bound.
- `run_research` runs **in the state machine**, minutes later, and does the searching. It re-checks
  every target it was handed — owned by the run's owner, created for THIS pass, holding nothing a
  person uploaded — because the targets travel in `config_json`, which came from a request body.
  `research_definition` is the half of it that takes a definition rather than a run id, because on
  the LOCAL path the run row does not exist yet — `run_simulation` writes it — and the searching
  must not care which.
- `_record` compresses the outcome to something small enough to live on the run row.

## Research writes only into collections of its own

Decided 2026-10-01, reversing §5.1 as first built. Research used to ingest into the collection
already bound at a scope whenever the caller owned it. That is safe for exactly one run. A run
created from another run's setup inherits that run's bindings, so its research went into the same
curated collections — and because a later pass replaces its predecessor's batch, the second run
silently deleted the first run's research and wrote its own. Every other run bound to those
collections, including no-research baselines, then retrieved web material it never asked for, and
curated passages lost their slots to found ones, more of them with every pass.

So every pass now creates its own collections, marked `research_for` on the KB row, and the state
refuses any target whose marker does not name the pass doing the writing. Curated material keeps a
retrieval slot of its own as a result — the source floor reserves one per COLLECTION — which is the
displacement §5.1 measured and had accepted as visible-but-unprevented. `docs/PERSONA-RESEARCH.md`
§5.1 records the reversal and what it costs.

## The corpus is embedded here, and nothing else would do it

`research.ingest` chunks and stores; a chunk with no vector is invisible to a k-NN query; and the
only OTHER caller of `embed_pending_kb_chunks` is the knowledge-base upload route. So without the
`_embed` step a pass would store twenty documents no turn could retrieve, and the symptom would read
as the researcher having found nothing useful — a judgement about quality rather than a missing step.
See `_embed`.

The vector INDEX, by contrast, is created at allocation time by the API, because the tenant role
holds `s3vectors:GetIndex` and deliberately not `CreateIndex`. The worker running this state cannot
create one. See `allocate_targets`.

## Why the state is unconditional rather than behind a Choice

The Research state is invoked for **every** run, and returns in milliseconds when research is off.
A `Choice` on a boolean in the execution input would save that invocation and would put the decision
in two places — the input the API wrote and the config the state reads. This project has shipped
three features inert because a value never arrived where it was read (cognition v0.2, the decline
streak, the closing round), and "the machine skipped a state the config asked for" is that failure
with no symptom at all: the conversation simply runs without the research, exactly as it did before.
The run row is authoritative, one invocation is the price of it being authoritative.

## Research never fails a run

§5.2. Every failure path here records what happened and returns; nothing raises. A search outage is
not a reason to lose a conversation somebody asked for, and `retrieval.disclose_unsupported` already
makes a persona say in-voice when retrieval found nothing. The state machine's catch sends a
*timeout* to `Prepare` for the same reason.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: The `config.research` key. Absent or `enabled: false` means no research, which is the default —
#: this feature costs money and searches the open web, so it is opt-in per run.
CONFIG_KEY = "research"

#: Status values recorded on the run. "Researched and found nothing" and "research failed" are
#: different facts (§5.2) and only one of them is worth retrying.
RESEARCHED = "researched"
FOUND_NOTHING = "found-nothing"
UNAVAILABLE = "unavailable"
FAILED = "failed"
SKIPPED = "skipped"

#: How long an error message may be on the run row. Long enough to name the provider and the status
#: code, short enough that a stack of them cannot threaten the item size limit.
MAX_ERROR_CHARS = 300

#: Name given to a knowledge base created for research: the run's (or ensemble's) name, then the
#: scope. Both, because every pass now creates its own set — one shared, one per persona, one per
#: consultant — and seven rows reading "Research — quiet-harbor" would not say which is whose.
NEW_KB_NAME = "Research — {label} · {scope}"


def for_run(run_id: str) -> str:
    """The `research_for` marker of a collection created for one run's research pass."""
    return f"run:{run_id}"


def for_ensemble(ensemble_id: str) -> str:
    """The marker of a collection created for an ensemble's single pass (§6).

    The ENSEMBLE, not a member: the pass happens once, before any member exists, and every member
    reads the same collections. A member never writes — `run_research` skips it — so no member id
    ever needs to match.
    """
    return f"ensemble:{ensemble_id}"


class ResearchRefused(ValueError):
    """A scope's research cannot be stored, with the reason. Never fatal to the run.

    A distinct type because the reasons are *policy* — not owned, not bound, no target — and each
    has to reach the run record in words an operator can act on. Raised and caught within this
    module only.
    """


@dataclass
class Settings:
    """`config.research`, parsed. Unknown keys are ignored rather than refused."""

    enabled: bool = False
    #: The shared corpus, built by the researcher for every persona to search (§2).
    shared: bool = True
    #: A private corpus per persona: their stance AND the evidence they said would change their
    #: mind (§2.2).
    personas: bool = True
    #: A library per consultant (`experts.py`), searched from their stated expertise. A consultant
    #: answers only from its sources, so this is what lets one "look things up".
    consultants: bool = True
    results_per_query: Optional[int] = None
    fetch_per_query: Optional[int] = None
    provider: Optional[str] = None
    #: `{"shared": kb_id, "personas": {name: kb_id}, "consultants": {name: kb_id}}`, resolved by `allocate_targets` at creation.
    #: Never trusted as it arrives — see `_verified_target`.
    targets: Dict[str, Any] = field(default_factory=dict)

    def target_for(self, persona: Optional[str], *, consultant: bool = False) -> Optional[str]:
        if persona is None:
            value = self.targets.get("shared")
        else:
            # Separate maps, so a consultant's library can never be resolved to a persona's KB (or
            # the reverse) even if a stored config carried the same name in both.
            value = (self.targets.get("consultants" if consultant else "personas") or {}).get(persona)
        return str(value) if value else None


def _int(value: Any) -> Optional[int]:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def settings_from(config: Optional[Dict[str, Any]]) -> Settings:
    """Parse `config.research`. A malformed value reads as "off" rather than raising.

    Off rather than an error because this is read on the turn-loop side of the boundary, where the
    config has already been accepted; refusing it here would fail a run over a field that only ever
    adds to it.
    """
    raw = (config or {}).get(CONFIG_KEY)
    if raw is True:
        # `research: true` is a reasonable thing for a hand-written definition to say, and rejecting
        # it as "not an object" would be pedantry about JSON rather than about research.
        return Settings(enabled=True)
    if not isinstance(raw, dict):
        return Settings()
    targets = raw.get("targets")
    return Settings(
        enabled=bool(raw.get("enabled")),
        shared=bool(raw.get("shared", True)),
        personas=bool(raw.get("personas", True)),
        consultants=bool(raw.get("consultants", True)),
        results_per_query=_int(raw.get("results_per_query")),
        fetch_per_query=_int(raw.get("fetch_per_query")),
        provider=(str(raw["provider"]) if raw.get("provider") else None),
        targets=dict(targets) if isinstance(targets, dict) else {},
    )


def enabled_for(run: Dict[str, Any]) -> Settings:
    """The run row's research settings, read out of `config_json`."""
    from matrix_studio.bindings import _config

    return settings_from(_config(run))


# --------------------------------------------------------------------------- #
# Creation time: deciding where each corpus may be stored
# --------------------------------------------------------------------------- #


def _persona_kbs(member: Dict[str, Any]) -> List[str]:
    from matrix_studio.bindings import _clean

    return _clean(member.get("knowledge_bases"))


async def allocate_targets(
    db: Any,
    request: Dict[str, Any],
    *,
    owner_sub: str,
    label: str,
    research_for: str,
    privileged: Any = None,
) -> Dict[str, Any]:
    """Create every research target and bind it into the request. Mutates and returns it.

    Called from `POST /api/runs` **before the run row is written**, because a run's bindings live in
    `config_json`, which is written once. "Research, then associate the KB" would be a
    read-modify-write of a JSON blob from a state the machine can retry; the codebase already
    refuses that shape once, for `budget`, with a comment saying why. Same pattern as the ensemble
    parent row, which lists member ids before any member exists.

    **One outcome per scope: a new collection**, owned by the caller, named for this run and scope,
    marked ``research_for`` on its row, and appended to that scope's bindings AFTER whatever was
    already bound there. Nothing that was bound is reused as a target, and nothing is dropped: a
    curated collection stays bound and is read exactly as before, and research never writes into
    it. ``research_for`` is `for_run(run_id)` or `for_ensemble(ensemble_id)`, and it is what the
    Research state checks before it writes a single document (`_verified_target`).

    ## Why never the collection that is already bound

    This function used to reuse the first bound collection the caller owned, on §5.1's argument
    that a persona with a curated collection and a research collection has two places to look for
    the same kind of thing. That holds for one run. It fails for the second: a run created from
    another run's setup ("Start fresh", the setup route, a hand-copied definition) inherits that
    run's bindings, so reuse pointed its research at the SAME collections — and a pass replaces its
    predecessor's batch, so the second run deleted the first run's research and wrote its own.
    Every run bound to those collections, including no-research baselines, then read web material it
    never asked for, and curated passages lost their slots to found ones. Measured on the deployed
    system 2026-09-30/10-01; see `docs/PERSONA-RESEARCH.md` §5.1.

    The cost is the one §5.1 rejected this for: a persona bound to a curated collection now has two
    collections, and the source floor reserves a slot for each before rank decides the rest. That is
    also the fix for the displacement §5.1 measured — the curated collection's reservation is no
    longer one its own research can win.

    A persona's collection is bound to **their own** scope only, never the run-level one.
    `bindings.bound_kbs` returns the union, which is the right answer for a *turn* — a persona may
    search the cast-wide collection — and the wrong one here: it would put one persona's private
    research, including the opposition's case, into the collection every other persona reads.

    Returns the request. `config.research.targets` is **overwritten**, never merged: it arrives from
    a request body, and a caller naming somebody else's KB as a target must not be able to make this
    the thing that resolves it. `run_research` re-checks every target anyway.

    ## Idempotence

    This runs once per run, in the request, and the Research state never creates a collection — so
    a retried Research Lambda writes into the collections allocated here and nowhere else, and a
    retry's batch replaces the failed attempt's inside them. A retried REQUEST is a new run with a
    new id, and gets new collections, which is correct: it is a different run.

    ## The vector index is created HERE, and it has to be

    ``privileged`` is the **unscoped** store, used for nothing but `ensure_index_for_kb`. The tenant
    role is granted `s3vectors:GetIndex` and deliberately not `CreateIndex`, so the worker running
    the Research state physically cannot create an index — it would ingest documents into a
    collection with nowhere to put their vectors, and the corpus would be listed and unretrievable.
    That is the API's job because the API is where the privileged client is, and it is done at
    allocation time rather than on first write for the reason `POST /api/knowledge-bases` gives: a
    collection whose index appears later has a window in which it exists and cannot be written to.
    """
    config = dict(request.get("config") or {})
    settings = settings_from(config)
    if not settings.enabled:
        return request
    if not research_for:
        # Refused rather than defaulted: an unmarked collection is one the Research state will
        # refuse to write into, so allocating it would create a run that searches, pays, and
        # stores nothing — the silent failure this module exists to prevent.
        raise ValueError("allocate_targets needs the pass it allocates for (research_for)")

    targets: Dict[str, Any] = {"personas": {}, "consultants": {}}

    async def ensure_index(kb_id: str) -> None:
        if privileged is None:
            # A caller that did not supply one — a test, or the local store. Logged rather than
            # silent: on the deployed path this means the corpus will not be retrievable, and
            # "research found 20 documents and no turn saw them" must not be a mystery.
            logger.warning(
                "No privileged store was supplied, so KB %s's vector index was not ensured. "
                "If this is a deployment with S3 Vectors, research will not be retrievable.",
                kb_id,
            )
            return
        from matrix_studio.storage.vectors import ensure_index_for_kb

        try:
            await ensure_index_for_kb(privileged, kb_id)
        except Exception as exc:  # noqa: BLE001
            # Not fatal to the run: `ensure_kb_index` is idempotent and the index may exist.
            # Loud, because the failure it cannot rule out is the unretrievable one.
            logger.warning("Could not ensure a vector index for KB %s: %s", kb_id, exc)

    async def create(bound: List[str], scope: str, short: str) -> Tuple[str, List[str]]:
        created = await db.create_knowledge_base(
            NEW_KB_NAME.format(label=label, scope=short)[:120],
            owner_sub=owner_sub,
            description=f"Documents found by pre-conversation research for {scope}.",
            research_for=research_for,
        )
        kb_id = str(created["id"])
        await ensure_index(kb_id)
        logger.info("Research will ingest %s into a new KB %s (%s)", scope, kb_id, research_for)
        # Appended AFTER the existing bindings, which are kept whole: the curated collections
        # stay where the operator put them, and `bound_kbs`' deterministic order is preserved.
        return kb_id, bound + [kb_id]

    if settings.shared:
        from matrix_studio.bindings import _clean

        kb_id, bound = await create(
            _clean(config.get("knowledge_bases")), "the whole cast", "shared",
        )
        targets["shared"] = kb_id
        config["knowledge_bases"] = bound

    if settings.personas:
        cast: List[Dict[str, Any]] = []
        for member in request.get("cast") or []:
            member = dict(member)
            name = str(member.get("name") or "").strip()
            viewpoints = ((member.get("structured") or {}).get("viewpoints")) or []
            # A persona with no viewpoint has no stance to research and no opposition to find, so
            # they get no KB. Creating one would leave an empty collection bound to them for ever.
            if name and viewpoints:
                kb_id, bound = await create(_persona_kbs(member), name, name)
                targets["personas"][name] = kb_id
                member["knowledge_bases"] = bound
            cast.append(member)
        request["cast"] = cast

    if settings.consultants:
        experts: List[Dict[str, Any]] = []
        for raw in config.get("experts") or []:
            expert = dict(raw) if isinstance(raw, dict) else raw
            name = str((expert or {}).get("name") or "").strip() if isinstance(expert, dict) else ""
            if name:
                # The consultant's OWN bindings only, as for a persona: its library must not land in
                # a collection the cast reads, or every persona would hold the consultant's sources.
                kb_id, bound = await create(
                    _persona_kbs(expert), f"consultant {name}", f"consultant {name}",
                )
                targets["consultants"][name] = kb_id
                expert["knowledge_bases"] = bound
            experts.append(expert)
        config["experts"] = experts

    research = dict(config.get(CONFIG_KEY)) if isinstance(config.get(CONFIG_KEY), dict) else {}
    research["enabled"] = True
    research["targets"] = targets
    config[CONFIG_KEY] = research
    request["config"] = config
    return request


# --------------------------------------------------------------------------- #
# Copying a setup: what a run's research added is not part of its setup
# --------------------------------------------------------------------------- #


def _target_ids(settings: Settings) -> List[str]:
    from matrix_studio.bindings import _clean

    ids: List[Any] = [settings.targets.get("shared")]
    for key in ("personas", "consultants"):
        scoped = settings.targets.get(key)
        if isinstance(scoped, dict):
            ids.extend(scoped.values())
    return _clean(ids)


async def research_collections_of(db: Any, config: Optional[Dict[str, Any]]) -> List[str]:
    """The collections a run's research pass wrote into and that hold nothing a person put there.

    For the setup route ("Start fresh from this setup"), which must not carry them into the new run.
    They are an OUTPUT of the earlier run, like its transcript, not part of what the operator set up:
    allocation appended them to the bindings, the operator never chose them. Carried forward, a run
    that researches itself would read two research passes — the earlier one stale — and spend a
    reserved retrieval slot on each; a run that does not research would read web material it never
    asked for, which is exactly what made the no-research baselines of 2026-09-30 unreliable. An
    operator who does want the earlier corpus can bind it by name in the KB picker; it is then a
    choice, and it is read-only to the new run's research either way (`_verified_target`).

    Read from `config.research.targets`, the only record of what a pass wrote into, and kept to the
    targets holding no uploaded document. That second condition is what makes it right for a run
    created BEFORE 2026-10-01, whose targets may BE the curated collections it was bound to: those
    hold the operator's own documents, so they stay in the setup as the bindings the operator chose.
    A collection research created for itself holds only researched documents, before the fix or
    after it, and is dropped either way. One that cannot be read is kept — it will be re-validated
    when the new run is created, and keeping a binding is the behaviour this route had before.
    """
    out: List[str] = []
    for kb_id in _target_ids(settings_from(config)):
        try:
            docs = await db.list_kb_documents(kb_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read KB %s while copying a setup: %s", kb_id, exc)
            continue
        if all(str(d.get("origin") or "") == "researched" for d in docs):
            out.append(kb_id)
    return out


# --------------------------------------------------------------------------- #
# State time: doing the research
# --------------------------------------------------------------------------- #


async def _verified_target(
    db: Any,
    settings: Settings,
    persona: Optional[str],
    owner_sub: str,
    *,
    research_for: str,
    consultant: bool = False,
) -> str:
    """The ingest target for one scope, or `ResearchRefused` with the reason.

    Three checks, each closing a different way research could write somewhere it must not. They are
    repeated here rather than trusted from `allocate_targets` because the targets were stored in
    `config_json`, which is built from a request body — so between allocation and use there is a
    JSON blob a caller controls, and a run row written before these rules existed.

    - **Owned by the run's owner.** A read grant is not a write grant; without this, naming another
      user's KB in `config.research.targets` would write research into their collection.
    - **Created for THIS pass** — its `research_for` marker names the run (or ensemble) researching.
      This is the check the 2026-09-30 incident needed. A run created from another run's setup
      carried targets — or, before allocation stopped reusing bindings, was allocated targets —
      that were the curated collections it inherited, and the replace step then deleted the earlier
      run's research from them. A curated collection carries no marker and another run's carries
      that run's, so neither passes. It also stops a run created BEFORE the fix, whose stored
      targets are curated collections, from writing into them if its Research state ever runs again.
    - **Holding nothing a person uploaded.** A research collection is a normal collection, and an
      operator may add their own document to it; from then on it is partly curated, and the replace
      step must not run against it. Rare, and the refusal says why.

    It is a refusal rather than a silent fallback to a new KB: a run whose target was rejected should
    say so, and creating one here is not possible anyway — the worker cannot create a vector index.
    """
    kb_id = settings.target_for(persona, consultant=consultant)
    who = (f"consultant {persona}" if consultant else persona) or "the shared corpus"
    if not kb_id:
        raise ResearchRefused(
            f"no ingest target was allocated for {who}, so there is nowhere to put what was found"
        )
    row = await db.get_knowledge_base(kb_id)
    if row is None:
        raise ResearchRefused(f"the knowledge base {kb_id} allocated for {who} no longer exists")
    if str(row.get("owner_sub") or "") != owner_sub:
        raise ResearchRefused(
            f"{kb_id} is not owned by this run's owner, and a read grant is not a write grant"
        )
    if str(row.get("research_for") or "") != research_for:
        raise ResearchRefused(
            f"{kb_id} was not created for this research pass, and research writes only into "
            "collections created for it — never one a run inherited, somebody curated, or "
            "another run researched into"
        )
    uploaded = [
        d for d in await db.list_kb_documents(kb_id)
        if str(d.get("origin") or "") != "researched"
    ]
    if uploaded:
        raise ResearchRefused(
            f"{kb_id} holds {len(uploaded)} document(s) somebody uploaded, so research leaves it "
            "alone rather than replace anything in it"
        )
    return kb_id


def _record(
    status: str,
    *,
    batch: Optional[str] = None,
    provider: Optional[str] = None,
    scopes: Optional[List[Dict[str, Any]]] = None,
    cost_usd: float = 0.0,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """The run row's account of a research pass. Counts, not contents.

    Bounded by the size of the cast by construction, so there is no truncation step to get wrong.
    The *contents* are reviewable where they belong — the corpus is in the KB and the KB view lists
    a collection's documents (§5.3) — and duplicating passages onto the run row would put a 400 KB
    item limit between a run and a large research pass.
    """
    out: Dict[str, Any] = {
        "status": status,
        "finished_at": int(time.time()),
        "cost_usd": round(cost_usd, 6),
    }
    if batch:
        out["batch"] = batch
    if provider:
        out["provider"] = provider
    if scopes is not None:
        out["scopes"] = scopes
    if error:
        out["error"] = str(error)[:MAX_ERROR_CHARS]
    return out


async def run_research(
    db: Any,
    run_id: str,
    *,
    owner_sub: str,
    mode: str = "fresh",
) -> Dict[str, Any]:
    """Research one run from its stored row and ingest what is found. Never raises.

    The state machine's entry point. Everything it does beyond reading the row and deciding
    whether to proceed is `research_definition`, which the local path calls directly — a run's
    row does not exist yet there, and the searching must not care which.

    Skipped, with a reason recorded, in three cases:

    - research is not enabled, which is every run today;
    - `mode` is not `fresh`. A branch or a resume inherits the parent's bindings and READS the
      parent's research, which is what the conversation up to the branch point was informed by. A
      branch exists to vary *one* thing from a run that happened, and re-searching would vary the
      evidence too. (Were it ever to search, `_verified_target` would still refuse to write: the
      collections are marked for the parent, not for the branch.)
    - the run is an **ensemble member**. §6: an ensemble researches ONCE, before its members exist,
      and every member binds the same KBs. Live search per member would give replicates different
      inputs, and `ENSEMBLE-CONVERSATIONS.md` §2 rests on the opposite — same config, same brief, so
      divergence is evidence about the brief. Independent searches would make it unattributable.
    """
    from matrix_studio.bindings import _cast, _config

    run = await db.get_run(run_id)
    if run is None:
        # Not a refusal to record: there is no row to record it on.
        return _record(SKIPPED, error=f"run {run_id} does not exist")

    settings = enabled_for(run)
    if not settings.enabled:
        return _record(SKIPPED, error="research is not enabled for this run")
    if mode != "fresh":
        return await _store(db, run_id, owner_sub, _record(
            SKIPPED, error=f"a {mode} inherits its parent's research rather than repeating it",
        ))
    if run.get("ensemble_id"):
        return await _store(db, run_id, owner_sub, _record(
            SKIPPED,
            error="an ensemble researches once, before its members, so replicates stay replicates",
        ))

    record = await research_definition(
        db,
        topic=str(run.get("topic") or ""),
        cast=_cast(run),
        settings=settings,
        owner_sub=owner_sub,
        research_for=for_run(run_id),
        label=run_id,
        experts=_config(run).get("experts") or [],
    )
    return await _store(db, run_id, owner_sub, record)


def research_function_name() -> str:
    """The research worker's own name, or "" when there is none.

    Empty is the LOCAL case rather than a misconfiguration: one long-lived process can await a
    research pass in a background task, and `ensemble_reporting.report_function_name` carries the
    same reasoning for the same reason. Its presence is what selects between dispatching and doing
    the work inline, exactly as `TURN_LOOP_ARN` does for the turn loop.
    """
    import os

    return os.environ.get("RESEARCH_FUNCTION", "").strip()


async def dispatch_ensemble(db: Any, ensemble_id: str) -> bool:
    """Ask the research worker to research this ensemble and then fan it out. True if dispatched.

    Asynchronous (`InvocationType="Event"`), because the caller is `POST /api/ensembles` behind API
    Gateway's 29-second integration timeout and a research pass is minutes. The same shape, and the
    same reason, as `ensemble_reporting.dispatch`.

    Only the ensemble id and the owner travel. Not the request: a cast with attached document text
    is easily over Lambda's 256 KB payload limit, and the parent row already holds everything the
    worker needs — which is why it carries `cast_json`.

    Returns False when no research function is configured, telling the caller to do the work itself.
    """
    name = research_function_name()
    if not name:
        return False

    import asyncio as _asyncio
    import os

    parent = await db.get_ensemble(ensemble_id)
    owner = (parent or {}).get("owner_sub")
    if not owner:
        logger.warning(
            "Ensemble %s has no owner on its row, so research cannot be dispatched — the worker "
            "assumes the tenant role per invocation and there is no identity to assume it for.",
            ensemble_id,
        )
        return False

    def _invoke() -> None:
        import boto3

        boto3.client("lambda", region_name=os.environ.get("AWS_REGION")).invoke(
            FunctionName=name,
            InvocationType="Event",
            Payload=json.dumps(
                {"ensemble_id": ensemble_id, "owner_sub": str(owner)}
            ).encode(),
        )

    try:
        await _asyncio.to_thread(_invoke)
    except Exception as exc:  # noqa: BLE001
        # Reported as NOT dispatched so the caller falls back to doing it inline. Returning True
        # here would leave an ensemble at `researching` for ever with nothing working on it.
        logger.warning("Could not dispatch research for ensemble %s: %s", ensemble_id, exc)
        return False
    logger.info("Dispatched research for ensemble %s to %s", ensemble_id, name)
    return True


async def research_ensemble(db: Any, ensemble_id: str, *, owner_sub: str) -> Dict[str, Any]:
    """Research an ensemble ONCE, before any member exists. Records on the parent; never raises.

    §6, and the reason it is not simply "each member researches": live search per member would
    give replicates different inputs, and `ENSEMBLE-CONVERSATIONS.md` §2 rests on the opposite —
    same config, same brief, so divergence is evidence about the BRIEF. Independent searches make
    divergence unattributable, which destroys the only thing an ensemble is for. One snapshot, N
    conversations: cheaper, and it keeps replicates being replicates.

    Reads everything from the parent row, which is why the row carries `cast_json`: this runs in a
    worker that has an ensemble id and nothing else.

    The record goes on the PARENT rather than on each member. A copy per member would say N passes
    happened, which is the one thing that must not be true.
    """
    from matrix_studio.bindings import _config

    parent = await db.get_ensemble(ensemble_id)
    if parent is None:
        return _record(SKIPPED, error=f"ensemble {ensemble_id} does not exist")

    base_config = _config({"config_json": parent.get("base_config_json")})
    settings = settings_from(base_config)
    if not settings.enabled:
        return _record(SKIPPED, error="research is not enabled for this ensemble")

    try:
        cast = json.loads(parent.get("cast_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        cast = []
    if not isinstance(cast, list):
        cast = []

    record = await research_definition(
        db,
        topic=str(parent.get("topic") or ""),
        cast=cast,
        settings=settings,
        owner_sub=owner_sub,
        # The ENSEMBLE's marker: `create_ensemble` allocated these collections for the parent, and
        # every member reads them without ever writing (§6).
        research_for=for_ensemble(ensemble_id),
        label=f"ensemble {ensemble_id}",
        experts=base_config.get("experts") or [],
    )
    try:
        await db.update_ensemble(ensemble_id, research=record, owner_sub=owner_sub)
    except Exception as exc:  # noqa: BLE001
        # The corpus is already in the knowledge bases by now, so losing the record loses the
        # account of the pass rather than the pass. The members must still be created.
        logger.warning("Could not record research on ensemble %s: %s", ensemble_id, exc)
    return record


async def research_definition(
    db: Any,
    *,
    topic: str,
    cast: Sequence[Dict[str, Any]],
    settings: Settings,
    owner_sub: str,
    research_for: str,
    label: str = "",
    experts: Sequence[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """Search, tier, ingest and embed every corpus this definition calls for. Never raises.

    Takes a DEFINITION rather than a run id, and that is what lets the deployed path and the local
    path share it: in the state machine the run row exists and is read first, while locally the row
    is written by `run_simulation` and does not exist until the conversation starts. Making this
    depend on the row would have meant either a second implementation or a synthesised row, and a
    synthesised row is a lie that tests would then be written against.

    ``research_for`` names the pass — `for_run(run_id)` or `for_ensemble(ensemble_id)` — and a
    target is written only if its collection was created for exactly that (`_verified_target`).
    Required, with no default, because every caller knows which pass it is and a default would be
    the one place a pass could write into somebody else's collection without saying so.

    Every failure path records what happened and returns (§5.2). A search outage is not a reason to
    lose a conversation somebody asked for.
    """
    from matrix_studio import research as rs
    from matrix_studio import webfetch, websearch
    from matrix_studio.analysis import _acompletion

    try:
        provider = websearch.select(settings.provider)
    except websearch.SearchUnavailable as exc:
        # UNAVAILABLE, not FAILED: nothing went wrong, the deployment has no search key. Worth
        # distinguishing because the fix is a configuration change rather than a retry.
        logger.warning("Research was asked for and no search provider is configured: %s", exc)
        return _record(UNAVAILABLE, error=str(exc))

    brief = rs.brief_for(topic, cast)
    batch = uuid.uuid4().hex[:12]

    async def search(query: str, count: int):
        return await provider.search(query, count=count)

    async def fetch(url: str):
        return await webfetch.fetch(url)

    kwargs: Dict[str, Any] = {}
    if settings.results_per_query is not None:
        kwargs["results_per_query"] = settings.results_per_query
    if settings.fetch_per_query is not None:
        kwargs["fetch_per_query"] = settings.fetch_per_query

    try:
        corpora = await rs.plan_and_gather(
            brief,
            # The tiers are switched off by giving the planner nothing to plan, so a tier that is
            # off is never searched. Filtering the corpora afterwards would have paid for the
            # searching and then thrown the results away.
            cast if settings.personas else [],
            shared=settings.shared,
            experts=[e for e in experts if isinstance(e, dict)] if settings.consultants else [],
            search=search,
            fetch=fetch,
            call=_acompletion,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        # `gather` already survives a failed query and `_ask` already survives a Bedrock 503, so
        # reaching here means something structural. It still must not fail the run.
        logger.exception("Research for %s failed", label or "a definition")
        return _record(FAILED, batch=batch, provider=provider.name, error=str(exc))

    scopes: List[Dict[str, Any]] = []
    sources = 0
    for corpus in corpora:
        scope: Dict[str, Any] = {
            "scope": corpus.persona or "shared",
            **({"consultant": True} if corpus.consultant else {}),
            "queries": len(corpus.queries),
            "documents": len(corpus.documents),
            "controlling": len(corpus.controlling),
            "unreadable": len(corpus.unreadable),
            "negative": bool(corpus.negative),
            # §9.3: negatives per QUERY, written when the corpus found some controlling authority
            # but a particular search did not. Counted separately from `negative`, which stays the
            # corpus-level record, so a reader can tell which mechanism fired.
            "query_negatives": len(corpus.query_negatives),
        }
        try:
            kb_id = await _verified_target(
                db, settings, corpus.persona, owner_sub,
                research_for=research_for, consultant=corpus.consultant,
            )
        except ResearchRefused as exc:
            scope["refused"] = str(exc)[:MAX_ERROR_CHARS]
            scopes.append(scope)
            continue
        scope["kb_id"] = kb_id
        try:
            result = await rs.ingest(db, corpus, kb_id, batch=batch, owner_sub=owner_sub)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not ingest %s research into KB %s: %s", scope["scope"], kb_id, exc)
            scope["refused"] = str(exc)[:MAX_ERROR_CHARS]
            scopes.append(scope)
            continue
        scope["written"] = len(result["written"])
        scope["replaced"] = len(result["replaced"])
        # Counted from the CORPUS, not from the rows written. Every pass writes at least one row
        # — the documented negative is itself a document — so counting writes would report
        # `researched` for a pass that found no source at all, and the one distinction §5.2 asks
        # for would be the one the status could not make.
        sources += len(corpus.documents)
        if result["written"]:
            scope.update(await _embed(db, kb_id))
        scopes.append(scope)

    cost = sum(c.cost_usd for c in corpora) + sum(
        float(s.get("embed_cost_usd") or 0.0) for s in scopes
    )
    await _record_spend(db, owner_sub, cost)
    # `FOUND_NOTHING` is a success. A brief whose authorities are not on the open web is a real
    # answer, and §4's documented negative is the artefact that says so — the conversation can then
    # tell "nobody looked" from "we looked and there is nothing".
    status = RESEARCHED if sources else FOUND_NOTHING
    logger.info("Research for %s: %s, %d source(s) found, $%.4f",
                label or "a definition", status, sources, cost)
    return _record(
        status, batch=batch, provider=provider.name, scopes=scopes, cost_usd=cost,
    )


async def _record_spend(db: Any, owner_sub: str, cost: float) -> None:
    """Add the research pass's model spend to the owner's monthly total.

    Without this, research is UNMETERED. A conversation's own spend is recorded by
    `execute_slice`, but the research pass happens in a different state and makes model calls of
    its own — query generation per persona, tiering per corpus, one embedding call per batch. On a
    six-persona cast that is a dozen calls before turn 1, charged nowhere, and the cap's whole job
    is to refuse the NEXT thing.

    Never raises, for the reason `orchestration.record_spend` does not: a missed increment delays
    the cap rather than losing a corpus that has already been paid for and stored.
    """
    if cost <= 0:
        return
    try:
        await db.add_user_spend(cost, owner_sub=owner_sub)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not record $%.4f of research spend for %s (%s). The monthly total now "
            "UNDER-reports, so the cap will refuse later than it should.", cost, owner_sub, exc,
        )


async def _embed(db: Any, kb_id: str) -> Dict[str, Any]:
    """Embed what was just ingested. **Without this the corpus is stored and unretrievable.**

    Not an optimisation and not deferred work. `add_kb_document` chunks the text and writes the
    rows; a chunk with no vector is invisible to a k-NN query, and nothing else in the system
    would ever come along and embed it — the only other caller of `embed_pending_kb_chunks` is
    the upload ROUTE, so a document that did not arrive through it is never embedded at all.

    This is the exact shape of the three features that shipped inert in this project: the work
    was done, the value never reached the place that reads it, and every symptom pointed
    somewhere else. Here it would have read as "research found nothing useful" — a judgement
    about the researcher's quality — when the corpus was sitting in the collection unvectorised.

    The width is stated rather than left to the provider's default, for the reason the upload
    route gives: a KB index is created at `EMBEDDING_DIMENSION` and its dimension is immutable,
    so a default that happens to match today is a silent dependency on it not changing.
    """
    from matrix_studio.retrieval import embed_pending_kb_chunks
    from matrix_studio.storage.vectors import EMBEDDING_DIMENSION

    out: Dict[str, Any] = {}
    try:
        result = await embed_pending_kb_chunks(db, kb_id, dimensions=EMBEDDING_DIMENSION)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not embed research in KB %s: %s", kb_id, exc)
        return {"embed_error": str(exc)[:MAX_ERROR_CHARS]}
    out["embedded"] = int(result.get("embedded") or 0)
    if result.get("error"):
        # Reported, not raised. The documents are stored and a model mismatch is fixed by
        # configuration rather than by re-running the search — so the pass keeps whatever it
        # found and says plainly that it is not retrievable yet.
        out["embed_error"] = str(result["error"])[:MAX_ERROR_CHARS]
        logger.warning("Research in KB %s is not retrievable: %s", kb_id, result["error"])
    if result.get("cost_usd"):
        out["embed_cost_usd"] = float(result["cost_usd"])
    return out


async def _store(
    db: Any, run_id: str, owner_sub: str, record: Dict[str, Any]
) -> Dict[str, Any]:
    """Persist the record on the run row, and return it either way.

    A failure to *record* research must not fail the run either — the corpus is already in the KB by
    this point, so losing the record loses the account of the pass, not the pass.
    """
    try:
        await db.set_run_research(run_id, record, owner_sub=owner_sub)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record research on run %s: %s", run_id, exc)
    return record
