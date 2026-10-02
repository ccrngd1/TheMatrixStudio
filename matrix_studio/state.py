# SPDX-License-Identifier: Apache-2.0
"""
State models for TheMatrix Simulation Studio.

All state models carry type and schema_version fields for forward compatibility
and migration support.
"""

from typing import Any, Dict, List, Optional
import uuid
from pydantic import BaseModel, Field, field_validator

from .personas import StructuredPersona


class MemoryItem(BaseModel):
    """A single memory item in an agent's memory stream."""
    type: str = Field(default="MemoryItem", description="Type discriminator")
    schema_version: str = Field(default="1.0.0", description="Schema version for migration")
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, description="Stable memory id (for memory_refs)")
    timestamp: int = Field(description="Unix timestamp")
    content: str = Field(description="Memory content")
    importance: Optional[float] = Field(default=None, description="Importance score 0-1")
    tags: List[str] = Field(default_factory=list, description="Memory tags")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


THREAD_TYPES = ("setup", "promise", "faction-action", "deferred-consequence")


class PendingThread(BaseModel):
    """Phase 4b: one unresolved setup / promise / deferred consequence — the
    engine analogue of a Setups & Payoffs ledger entry. Lives in global sim
    state (``SimSnapshot.pending_threads``); open threads are retrieved into
    subsequent turn prompts so they causally influence generation (never a
    post-hoc annotation)."""
    type: str = Field(default="PendingThread", description="Type discriminator")
    schema_version: str = Field(default="1.0.0", description="Schema version for migration")
    # Short id so the generating model can reference it verbatim in prompts.
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12], description="Stable thread id")
    description: str = Field(description="What was planted / promised / deferred")
    thread_type: str = Field(default="setup", description="setup|promise|faction-action|deferred-consequence")
    origin_turn: int = Field(description="Turn the thread was opened")
    origin_agent: Optional[str] = Field(default=None, description="Agent that planted it")
    status: str = Field(default="open", description="open|resolved|abandoned")
    resolved_turn: Optional[int] = Field(default=None, description="Turn it was resolved/abandoned")


class AgentState(BaseModel):
    """Complete state for a single agent."""
    type: str = Field(default="AgentState", description="Type discriminator")
    schema_version: str = Field(default="1.0.0", description="Schema version for migration")
    name: str = Field(description="Agent name")
    persona: str = Field(description="Agent persona/system message")
    # Phase 6: optional structured identity — background, preferences (including
    # `dismisses`) and viewpoints with firmness. Additive to `persona`, never a
    # replacement: prose carries voice, structure carries commitments. None for
    # every pre-Phase-6 run and snapshot, so stored snapshots still parse.
    structured: Optional[StructuredPersona] = Field(
        default=None, description="Structured persona: convictions, not just goals (Phase 6)"
    )
    memory_stream: List[MemoryItem] = Field(default_factory=list, description="Agent's memory")
    goals: List[str] = Field(default_factory=list, description="Current goals")
    relationships: Dict[str, str] = Field(default_factory=dict, description="Relationships to other agents")
    conversation_history: List[Dict[str, Any]] = Field(default_factory=list, description="Recent conversation context")
    total_tokens_in: int = Field(default=0, description="Total input tokens consumed")
    total_tokens_out: int = Field(default=0, description="Total output tokens generated")
    total_cost_usd: float = Field(default=0.0, description="Total cost in USD")
    # DEPRECATED as a write target: inlining base64 here put the image into every
    # snapshot (measured: 99% of the largest snapshot in a real database was one
    # portrait). Kept readable so runs recorded before the change still render.
    portrait: Optional[str] = Field(default=None, description="Legacy base64 avatar (read-only)")
    # The blob key the avatar is stored under; see matrix_studio.blobs.
    portrait_key: Optional[str] = Field(default=None, description="Blob key for the avatar image")


class CognitionConfig(BaseModel):
    """Phase 2c cognition flags (per-run, read from ``config['cognition']``).

    All flags default to the pre-2c behavior: when ``enabled`` is False the
    engine's generation path is byte-for-byte identical to Phase 2b (no
    structured output, no new events, no extra token cost). ``enabled`` is the
    master switch; the sub-flags only take effect when it is True.
    """
    type: str = Field(default="CognitionConfig", description="Type discriminator")
    schema_version: str = Field(default="1.0.0", description="Schema version")
    enabled: bool = Field(default=False, description="Master switch for cognition")
    memory: bool = Field(default=True, description="Form + retrieve agent memories (on when enabled)")
    reflection_every: int = Field(
        default=4, ge=0,
        description="Reflect every N turns (0 disables); ON by default when enabled",
    )
    goals_dynamic: bool = Field(default=False, description="Allow agents to update their own goals")
    relationships: bool = Field(default=False, description="Track per-agent stance toward others")
    retrieval_k: int = Field(default=5, ge=0, description="Memories injected into each turn's prompt")
    # Phase 4b: pending-thread ledger (setups & payoffs). OFF by default so
    # cognition-enabled runs keep their pre-4b structured schema byte-for-byte
    # unless threads are explicitly turned on.
    threads: bool = Field(default=False, description="Track pending threads (setups/payoffs ledger)")
    thread_stale_after: int = Field(
        default=5, ge=1,
        description="Open threads older than this many turns are surfaced as dangling",
    )

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "CognitionConfig":
        """Parse from a run ``config`` dict. Missing/invalid -> all-off default.

        Accepts ``config['cognition']`` as a dict; anything else yields the
        disabled default so legacy/plain configs behave exactly as before.
        """
        raw = (config or {}).get("cognition")
        if not isinstance(raw, dict):
            return cls()
        return cls(**{k: v for k, v in raw.items() if k in cls.model_fields})


class SelectionConfig(BaseModel):
    """Next-speaker selection flags (per-run, read from ``config['selection']``).

    ``fairness`` adds two things to the moderator's prompt: each persona's turn count
    and how long since they last spoke (intervention A), and the run's length with the
    resulting fair share (intervention B).

    **It defaults to ON**, which is unlike every other config block in this file, and the
    reason is that the alternative is measured to be worse. Across 146 replays over four
    transcripts and two models (`docs/studies/SPEAKER-SELECTION-EVALUATION.md` §10–§11), the
    shipped prompt without it scores a Gini of turn share of 0.332 (Haiku 4.5) and 0.335
    (Sonnet 5), and starves at least one persona to **zero turns** in 6 of 24 replays. With
    it: 0.223 and 0.185, and nobody was ever starved on either model.

    A run that wants the old behaviour sets ``{"selection": {"fairness": false}}`` — which
    is also how the comparison gets re-measured later, since an intervention with no off
    switch cannot be A/B'd again after it ships.
    """

    type: str = Field(default="SelectionConfig", description="Type discriminator")
    schema_version: str = Field(default="1.0.0", description="Schema version")
    fairness: bool = Field(
        default=True,
        description="Show the moderator participation counts and the fair share",
    )
    #: How the next speaker is decided. Four answers, and they differ in two dimensions:
    #: whether one persona speaks per turn or all of them, and whether they can see each
    #: other while doing it.
    #:
    #:   moderated      One LLM call picks one speaker per turn. Everything §1–§17 of
    #:                  `docs/studies/SPEAKER-SELECTION-EVALUATION.md` measures. Turn share is a
    #:                  judgement, and making that judgement fair took eight interventions.
    #:   rotation       Everyone speaks once per round, in cast order, each SEEING what the
    #:                  earlier speakers in that round said. Turn share is equal by
    #:                  construction — it is what the moderated interventions approximate —
    #:                  and the conversation stays cumulative because nobody is blind.
    #:   simultaneous   Everyone is asked every round against the state as it stood when the
    #:                  round OPENED, so none of them can see the others' contributions that
    #:                  round. Genuinely concurrent, and measurably more parallel: run
    #:                  `36231059` has four personas opening a round by answering the same
    #:                  question, none acknowledging the others.
    #:   hybrid         `hybrid_opening_rounds` simultaneous rounds, then moderated. Measured
    #:                  motivation: the simultaneous run's opening was its best part (every
    #:                  position on the table by turn 1, no dyad lock) and its later rounds
    #:                  its worst (parallel restatement), while the moderated run's weakness
    #:                  is the opposite — it takes 8–11 turns to introduce the cast.
    #:
    #: In every method except `moderated` the selection apparatus is bypassed for the round
    #: phases: `fairness` does nothing there, because a rotation makes turn share equal
    #: outright, and a round where everybody passes IS convergence — measured rather than
    #: judged. `hybrid` gets both: rounds while it opens, then the full moderated machinery.
    method: str = Field(
        default="moderated",
        description="moderated | rotation | simultaneous | hybrid",
    )

    #: How many opening rounds `hybrid` runs before it switches to moderated selection.
    #:
    #: Two by default, from the one live run there is: in `36231059` round 1 put every
    #: position on the table and round 2 was the strongest of the eight (956 chars average,
    #: the peak); the parallel restatement starts at round 3, when four personas all answer
    #: the same question. Ignored unless the method is `hybrid`.
    hybrid_opening_rounds: int = Field(
        default=2, ge=1, description="Opening simultaneous rounds before moderated selection"
    )

    @field_validator("method")
    @classmethod
    def _known_method(cls, v: str) -> str:
        """Rejected rather than defaulted. A typo'd method silently falling back to
        `moderated` would look like the chosen mode simply not working."""
        if v not in ("moderated", "rotation", "simultaneous", "hybrid"):
            raise ValueError(
                f"unknown selection method {v!r}: "
                "moderated | rotation | simultaneous | hybrid"
            )
        return v

    #: One final round when the run hits its ceiling without finishing.
    #:
    #: Asked of everybody at once, in either method, and it asks for POSITIONS AND TERMS
    #: rather than agreement — see `_CLOSING` in the simulator for why the obvious wording
    #: ("work toward consensus") is the dangerous one.
    #:
    #: Runs at turn `max_messages + 1`, so it does not consume a round of the conversation,
    #: and only when the ceiling was reached: a converged run has already had every persona
    #: say it had nothing left, and asking again would contradict that.
    closing_round: bool = Field(
        default=False,
        description="One final round asking for final positions when the ceiling is hit",
    )

    #: Intervention H, and the other half of `fairness`.
    #:
    #: Fair rotation front-loads the substance: run `d7d739dd` stated every position by turn
    #: 25 of 40 and then spent fifteen turns on "confirmed, nothing to add" — and the padding
    #: was not misallocation, since the persona with the MOST turns padded too. So the
    #: moderator is allowed to answer "nobody", and that ends the run.
    #:
    #: Guarded twice in the turn loop, because the errors are not symmetric: fifteen turns of
    #: filler cost about $0.30, fifteen turns of argument cut short cost the run. It cannot
    #: fire until every persona has spoken at least once, and it needs two consecutive
    #: declines.
    #: **Default OFF, on purpose.** The pre-registered criterion in §14 says one premature
    #: convergence rejects the arm, and the closed-loop replay produced exactly that — turn 17
    #: of the 40-turn control, reproducibly. Open-loop replay does not (never honoured on the
    #: control, turn 26 on the fair run, which is the right answer), and open loop is the
    #: faithful protocol here because production's participation counts always agree with the
    #: transcript. But that protocol was chosen AFTER seeing which one favoured the arm, so it
    #: does not get to decide. A live run does; until one passes, this stays off and a caller
    #: opts in with `{"selection": {"stop_when_converged": true}}`.
    stop_when_converged: bool = Field(
        default=False,
        description="Let the moderator end the run when nobody has anything left to add",
    )

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "SelectionConfig":
        """Parse from a run ``config`` dict. Missing/invalid -> the default (fairness ON).

        Note the asymmetry with the other blocks: a missing ``selection`` key means the
        feature is ON, because it is the measured default rather than an opt-in.
        """
        raw = (config or {}).get("selection")
        if not isinstance(raw, dict):
            return cls()
        return cls(**{k: v for k, v in raw.items() if k in cls.model_fields})


class RetrievalConfig(BaseModel):
    """Phase 5 document-retrieval flags (per-run, read from ``config['retrieval']``).

    Deliberately NOT part of ``CognitionConfig``: attaching background documents
    to a persona is useful with cognition off, so this is an independent switch.
    Defaults reproduce pre-Phase-5 behavior exactly — a run with no ``retrieval``
    block never queries the index and never adds a prompt block.

    ``max_chars`` is the feature, not a safety valve: it is the hard ceiling on
    how much document text any single call can carry, which is what keeps a
    forty-page attachment out of the per-call context.
    """
    type: str = Field(default="RetrievalConfig", description="Type discriminator")
    schema_version: str = Field(default="1.0.0", description="Schema version")
    enabled: bool = Field(default=False, description="Master switch for document retrieval")
    k: int = Field(default=3, ge=0, description="Max passages injected per turn")
    max_chars: int = Field(
        default=1200, ge=0,
        description="Hard ceiling on retrieved document characters per turn",
    )
    recent_turns: int = Field(
        default=3, ge=1,
        description="How many recent messages contribute terms to the query",
    )
    # Retrieval mode. "fts" is lexical BM25 only, "vector" is embeddings only,
    # "hybrid" fuses both by Reciprocal Rank Fusion.
    #
    # **The default is now "vector".** It was "fts" because vector/hybrid needed a
    # local vector extension (the since-deleted `sqlite-vec` extra) AND an embedding
    # provider that a local user might
    # not have — a real reason then, and simply untrue on the AWS target: the vector
    # store is a managed service that is always present, and Bedrock is already a hard
    # dependency because generation uses it. Nothing is opt-in about a capability every
    # deployment has.
    #
    # What settles the value is measurement, not availability. Re-measured against
    # S3 Vectors on this project's own docs (22 files, 666 chunks, n=40 — see
    # `docs/project/PHASE3-RECALL-MEASUREMENT.md`), on the **diluted** arm that models the
    # engine's actual query shape (a question buried in conversational filler):
    #
    #                     recall@1   recall@5
    #     fts (BM25)         0.125      0.650
    #     vector             0.650      0.825
    #     hybrid             0.450      0.875
    #
    # `recall@1` is the figure that matters: a turn injects only `k` passages and the
    # first one dominates the prompt. Lexical put the right passage first 1 turn in 8.
    #
    # Pure vector beats hybrid at k=1 by a wide margin, and that ordering also held in
    # the earlier sqlite-vec measurement, so it is reproduced rather than a one-off:
    # equal-weight RRF lets a confident lexical wrong answer outrank a correct semantic
    # one. Hybrid's win on recall@5 does not buy back the top slot.
    #
    # Cost of the change is one embedding call per turn (~$1e-7). A run whose chunks
    # were never embedded degrades to lexical rather than returning nothing.
    #
    # "fts" is kept as a name for compatibility with stored `config_json`, though it is
    # in-process BM25 now and there is no FTS5 anywhere in the system.
    mode: str = Field(
        default="vector", description="Retrieval mode: fts | vector | hybrid",
    )
    embedding_model: str = Field(
        default="", description="LiteLLM embedding model ('' = module default)",
    )
    rrf_k: int = Field(
        default=60, ge=1,
        description="Reciprocal Rank Fusion constant for hybrid mode",
    )
    # Phase 5h: absolute similarity floor for vector/hybrid, as COSINE (1.0 =
    # identical, 0.0 = unrelated). Calibrated in
    # docs/project/PHASE5-RETRIEVAL-MEASUREMENT.md over 180 real retrievals.
    #
    # It is an OFF-TOPIC GUARD, not a relevance filter, and the distinction is
    # measured rather than assumed: correct and incorrect retrievals overlap
    # almost completely (hits 0.228-0.870, misses 0.166-0.699), so no threshold
    # can tell a right passage from a wrong one. What IS cleanly separable is a
    # query with nothing to do with the corpus at all, which scores ~0.0-0.07.
    #
    # 0.15 sits below the lowest observed genuine hit (0.228) with margin, so it
    # costs 0 of 137 measured hits while still rejecting unrelated queries.
    # Set 0.0 to disable.
    min_similarity: float = Field(
        default=0.15, ge=0.0, le=1.0,
        description="Reject vector matches below this cosine (0 = off; off-topic guard)",
    )
    # Phase 5g: when retrieval ran and found nothing, ask the persona to say — in
    # its own voice — that it is speaking from experience rather than a source.
    # Deliberately about PROVENANCE, not evidentiary support: the engine knows
    # only that nothing was retrieved, and at ~0.82 recall the supporting passage
    # may well exist in the corpus and simply have been missed. Claiming "no
    # documentation supports this" would be wrong ~18% of the time.
    disclose_unsupported: bool = Field(
        default=False,
        description="Ask the persona to flag in-voice when it has no retrieved source",
    )
    # Ask the persona to end a claim with the bracketed label of the passage it relies on.
    # OFF here on purpose, although new runs have it ON: the API request model defaults it to true
    # and stores that in the run's config (docs/studies/CITE-INLINE.md, decided 2026-09-28). A config with
    # no value is a run created before then, and resuming or branching it must not change its prompt.
    cite_inline: bool = Field(
        default=False,
        description="Ask personas to cite each passage they rely on inline, as [title #n]",
    )
    # Reserved slots for a CONTROLLING authority — a statute, a regulation, a board opinion,
    # a decided case. `docs/PERSONA-RESEARCH.md` §3, and `vectors.apply_floors` composes it
    # with the per-collection floor.
    #
    # **Default 0, meaning off, and that is byte-identical to the previous behaviour.** A run
    # without research has nothing tiered as controlling, so this could have defaulted to 1
    # harmlessly — and defaulting it on would still be wrong, because it changes what reaches
    # a prompt and every change of that kind in this project is supposed to arrive with a
    # measurement behind it. The one that decides this value is §9's comparison.
    #
    # 1 rather than a larger number when it is on: the floor buys the room ONE slot for the
    # law, and the rest of the prompt is still decided by relevance. A floor of 2 or 3 would
    # start crowding out the conversation's own material with authorities nobody asked about.
    authority_floor: int = Field(
        default=0, ge=0,
        description="Reserved retrieval slots for controlling authority (0 = off)",
    )
    # PERSONA-RESEARCH.md §9.5: also search each turn with the speaker's own
    # `evidence_that_shifts`, merged into the same pool (k unchanged). Default OFF: it changes
    # what reaches a prompt, and it arrives with its own pre-registered measurement (§9 run 3).
    standing_query: bool = Field(
        default=False,
        description="Also retrieve with the speaker's evidence_that_shifts each turn",
    )

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        """Reject an unknown mode rather than silently retrieving nothing."""
        allowed = {"fts", "vector", "hybrid"}
        if v not in allowed:
            raise ValueError(f"mode must be one of {sorted(allowed)}, got {v!r}")
        return v

    # Experimental query/ranking knobs, both DEFAULT OFF because they were
    # MEASURED AS HARMFUL. docs/project/PHASE5-RETRIEVAL-MEASUREMENT.md A/B'd them across
    # three arms and recall fell in every one — worst on the "diluted" arm that
    # models the engine's real conversation-window query (recall@5 0.339 tuned vs
    # 0.509 baseline). Kept only so the measurement harness can re-evaluate them;
    # do not enable without re-running that comparison.
    term_limit: int = Field(
        default=0, ge=0,
        description="Max discriminative query terms (0 = OFF; measured harmful when on)",
    )
    max_df_ratio: float = Field(
        default=0.5, gt=0.0, le=1.0,
        description="Drop query terms appearing in more than this fraction of chunks",
    )
    score_ratio: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="Drop matches weaker than this fraction of the best score (0 = OFF; measured harmful when on)",
    )

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "RetrievalConfig":
        """Parse from a run ``config`` dict. Missing/invalid -> disabled default."""
        raw = (config or {}).get("retrieval")
        if not isinstance(raw, dict):
            return cls()
        return cls(**{k: v for k, v in raw.items() if k in cls.model_fields})


class PersonaConfig(BaseModel):
    """Phase 6 structured-persona flags (per-run, read from ``config['personas']``).

    Deliberately NOT part of ``CognitionConfig``, for the same reason
    ``RetrievalConfig`` is not: convictions are useful with cognition off, and the
    premise validation that justified this feature ran with cognition **off** in
    all three arms, so that is the configuration the evidence actually covers.

    Defaults reproduce pre-Phase-6 behavior exactly: with ``enabled`` False, any
    ``structured`` block on a cast member is ignored and every prompt is
    byte-identical to before.
    """

    type: str = Field(default="PersonaConfig", description="Type discriminator")
    schema_version: str = Field(default="1.0.0", description="Schema version")
    enabled: bool = Field(default=False, description="Master switch for structured personas")
    # When False the underlying concern is rendered as freely sayable. Default
    # True because withholding it is the point: per the source spec, drawing the
    # real concern out of a stakeholder is the skill being exercised, and a
    # concern volunteered in turn 1 cannot be drawn out.
    withhold_concerns: bool = Field(
        default=True, description="Keep `underlying_concern` unsaid until asked"
    )
    # Which dismissal-rule wording to render alongside `dismisses`. A NAMED variant
    # rather than on/off, because both failure modes are measured and sit on either
    # side of the target:
    #   blunt     -> dismissals reliably, but parallel monologues (Arm C, talk-past 4/5)
    #   retuned   -> talk-past back to 2, but dismissal suppressed to the CONTROL's
    #                rate of 0.067 across 3 runs, two of them with none at all
    #   mandatory -> the current candidate; see matrix_studio/personas.py
    #   off       -> no rule AND no `dismisses` list
    # Booleans are still accepted (True -> "mandatory", False -> "off") so configs
    # and snapshots written against the previous boolean field keep working.
    dismissal_rule: str = Field(
        default="mandatory",
        description="Dismissal rule wording: mandatory | retuned | blunt | off (bool accepted)",
    )

    # docs/studies/EVIDENCE-LEAN.md: a persona asking for evidence must also say what it expects the evidence to
    # show and which way it leans today. ON by default since 2026-09-29, by operator decision after
    # docs/studies/EVIDENCE-LEAN-2.md met its pre-registered criterion (lean 3/3 vs 0/3).
    evidence_lean: bool = Field(
        default=True, description="Require a best guess and a current lean with every evidence request"
    )

    @field_validator("dismissal_rule", mode="before")
    @classmethod
    def _check_dismissal_rule(cls, v: Any) -> str:
        """Coerce bool -> variant name and reject unknown names.

        Rejecting rather than defaulting: a typo'd variant silently falling back to
        the default would make a measurement arm quietly test the wrong wording,
        which is exactly the class of error this whole re-tune exists to correct.
        """
        from .personas import normalise_dismissal_rule

        return normalise_dismissal_rule(v)

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "PersonaConfig":
        """Parse from a run ``config`` dict. Missing/invalid -> disabled default."""
        raw = (config or {}).get("personas")
        if not isinstance(raw, dict):
            return cls()
        return cls(**{k: v for k, v in raw.items() if k in cls.model_fields})


class SimSnapshot(BaseModel):
    """Complete simulation state snapshot."""
    type: str = Field(default="SimSnapshot", description="Type discriminator")
    schema_version: str = Field(default="1.0.0", description="Schema version for migration")
    run_id: str = Field(description="Unique run identifier")
    turn: int = Field(description="Turn number of this snapshot")
    topic: str = Field(description="Simulation topic")
    agents: Dict[str, AgentState] = Field(description="Agent states keyed by name")
    conversation: List[Dict[str, Any]] = Field(default_factory=list, description="Full conversation transcript")
    # Phase 4b: global pending-thread ledger. Default [] so pre-4b stored
    # snapshots still parse; rides every snapshot automatically (2c pattern).
    pending_threads: List[PendingThread] = Field(
        default_factory=list, description="Global pending-thread ledger (Phase 4b)"
    )
    # Phase 5i's first-hand citation ledger: ``[[speaker, document_title], ...]`` for
    # every first-hand citation so far. It is what makes a SECOND-hand attribution
    # verifiable — "Priya cited X as saying Y" is accepted only if Priya really cited
    # X first-hand earlier — so losing it does not merely forget history, it turns
    # legitimate crediting into an unverifiable claim.
    #
    # It lives on the snapshot because it is cross-turn state and every other piece of
    # cross-turn state already does (2c pattern, same as pending_threads). It did not,
    # and the omission was a live bug: `_run_turns` initialises the ledger to `[]` at
    # both call sites, so a resumed or branched run started with an empty one. Rare
    # enough to go unnoticed — until Phase 5, where every turn is a resume and the
    # ledger would be empty on every turn but the first, permanently.
    #
    # A list of pairs rather than a dict because JSON keys must be strings and the
    # natural key here is the pair itself; `CitationContext.build` consumes exactly
    # this shape. Default [] so every snapshot written before the field parses.
    firsthand_citations: List[List[str]] = Field(
        default_factory=list,
        description="[[speaker, document_title], ...] first-hand citations so far",
    )
    # Consecutive un-honoured declines by the moderator immediately before this turn
    # (intervention H). Cross-turn state, so it belongs here for the same reason
    # `pending_threads` and `firsthand_citations` do — and for a sharper one: the deployed
    # turn loop runs ONE turn per Lambda invocation, so a counter held in a local variable
    # is re-initialised on every turn and the "two declines in a row" guard can never be
    # satisfied. That shipped, and the live run `28235cec` declined eleven times — three on
    # consecutive turns — with every event recording `consecutive: 1`.
    #
    # Defaulted, so every snapshot written before 2026-09-16 still parses.
    decline_streak: int = Field(
        default=0, description="Consecutive un-honoured moderator declines (intervention H)"
    )
    status: str = Field(description="Simulation status: pending|running|complete|failed")
    created_at: int = Field(description="Unix timestamp of snapshot creation")
    completed_at: Optional[int] = Field(default=None, description="Unix timestamp of completion")
    total_turns: int = Field(description="Total turns executed")
    error_message: Optional[str] = Field(default=None, description="Error message if failed")
