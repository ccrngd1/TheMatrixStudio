// SPDX-License-Identifier: Apache-2.0
// Shared types mirroring the backend event/result contract (Phase 0 shapes).

export interface Persona {
  name: string
  persona: string
  goals: string[]
}

export interface SimEvent {
  run_id: string
  turn: number
  seq: number
  event_type:
    | 'sim.started'
    | 'avatar.ready'
    | 'speaker.selected'
    | 'agent.response'
    | 'sim.completed'
    | 'sim.failed'
    | 'sim.interrupted'
    // Terminal like the three above. `sim.capped` predates this list and was
    // simply missing, so a cost-capped run read as still running in the UI.
    | 'sim.stopped'
    | 'sim.capped'
    // Phase 5: the passages a speaker's turn retrieved, just before that speaker's message.
    | 'document.retrieved'
    // A consultant's answer to a persona's question (matrix_studio/experts.py). Not a participant's turn.
    | 'expert.answered'
    // A working assumption the room reasons from (matrix_studio/assumptions.py). Not speech.
    | 'assumption.made'
    | 'assumption.withdrawn'
    // A persona said its position moved (matrix_studio/shifts.py). Flag-only.
    | 'position.shift'
    | 'error'
  agent_name: string | null
  payload: Record<string, any>
}

export interface RunSummary {
  run_id: string
  name: string | null
  description: string | null
  slug: string | null
  topic: string
  status: string
  turn_count: number
  total_cost_usd: number
  created_at: number | null
  completed_at: number | null
  // Wall-clock (epoch seconds) of this run's most recent event; used to flag a
  // run still marked "running" that has gone quiet (stalled/orphaned).
  last_event_at?: number | null
  // Phase 2a: set on branch runs so history can flag lineage (both null on a
  // fresh/root run).
  parent_run_id?: string | null
  branch_turn?: number | null
  // Set on an ensemble's member runs (and the group label it was created under).
  ensemble_id?: string | null
  ensemble_cell?: string | null
  // For the run cards: the turn ceiling (for `LIVE 06/12`) and the cast (one hex token per persona).
  max_messages?: number | null
  cast_names?: string[]
  /** Where each persona ended (`matrix_studio/stance.py`): set once the run is summarised, never while live. */
  stance?: Record<string, StanceState> | null
}

export type StanceState = 'support' | 'conditional' | 'unstated' | 'holding'

/**
 * Why one persona has its stance (`matrix_studio/stance.py`, docs/MOBILE-UI.md §6.1).
 *
 * `source: 'closing'`: their closing statement decided it, and `quote` is their own words from it, checked to be
 * in the statement. `source: 'summary'`: the summary rule decided it, and `quote` is the summary's account of their
 * objection (holding), their own flagged shift sentence (support), or null (unstated); `fallback` says why the
 * closing statement did not decide.
 */
export interface StanceBasisEntry {
  stance: StanceState
  source: 'closing' | 'summary'
  /** The classifier's verdict on their closing statement, when there was one to give. */
  class: 'accepts' | 'accepts_with_conditions' | 'rejects' | 'unclear' | null
  quote: string | null
  fallback?:
    | 'no_closing_round'
    | 'no_statement'
    | 'classifier_failed'
    | 'no_verdict'
    | 'unclear'
    | 'unverified_quote'
  /** What the classifier claimed when its quote was not in the statement and the claim was discarded. */
  claimed?: string
}

export interface StanceBasis {
  personas: Record<string, StanceBasisEntry>
  /** The closing-statement classifier call, or null when the run had no closing statements. */
  classifier: {
    model: string | null
    tokens_in: number
    tokens_out: number
    cost_usd: number
    error: string | null
  } | null
}

export interface AgentResult {
  name: string
  persona: string
  goals: string[]
  total_tokens_in: number
  total_tokens_out: number
  total_cost_usd: number
  portrait_key: string | null
  portrait: string | null // legacy base64; null for runs recorded after the change
}

export interface RunDetail extends RunSummary {
  cast: Persona[]
  config: Record<string, any>
  result: {
    conversation: { speaker: string; content: string; turn: number }[]
    agents: Record<string, AgentResult>
    total_turns: number
    total_cost_usd: number
  } | null
  // Phase 1.5: model-generated / imported analysis summaries (may be null).
  summary?: { generated: StoredSummary | null; imported: StoredSummary | null }
  // Phase 2a: branch lineage — this run's parent (if it is a branch) and any
  // child branches forked from it.
  lineage?: RunLineage
  // PERSONA-RESEARCH.md §5.3: the account of a research pass nobody watched.
  // Null or absent for every run that did not ask for research.
  research?: ResearchRecord | null
  // Each model role -> the model it resolved to, from the stored config via the engine's own
  // ModelSet. Shown so a run cannot use a different model than it asked for without anyone seeing.
  models?: Record<string, string | null>
  // Why each persona has its stance. Absent for every run summarised before 2026-10-01 (not backfilled).
  stance_basis?: StanceBasis | null
}

// -------- Pre-conversation research (docs/PERSONA-RESEARCH.md) --------- //

export interface ResearchScope {
  // The persona's name, or the literal 'shared' for the researcher's own corpus.
  scope: string
  // Set when `scope` names a consultant's library rather than a persona's corpus.
  consultant?: boolean
  queries?: number
  documents?: number
  // Sources tiered as a statute, regulation, board ruling or decided case. Broken out
  // because this is the tier the retrieval floor reserves a prompt slot for: a pass that
  // found ninety sources and no controlling authority did not answer the question the
  // personas were asking.
  controlling?: number
  // URLs a search returned that could not be read, with the count only. Not a bug count —
  // a state board's own statute page answering 403 is a fact about the search, and it is
  // reported so an absence is never mistaken for "there is nothing there".
  unreadable?: number
  negative?: boolean
  // §9.3: negatives per SEARCH — written when the corpus found some controlling authority but a
  // particular query did not. Separate from `negative`, the corpus-level record, so a reader can
  // tell which mechanism fired.
  query_negatives?: number
  written?: number
  replaced?: number
  embedded?: number
  kb_id?: string
  // Set when a scope could not be stored: not owned, no target allocated, or the
  // collection is gone. The reason travels because each one implies a different fix.
  refused?: string
  // Set when documents were stored and could NOT be embedded, which means they exist and
  // no turn can retrieve them. The one failure here worth shouting about.
  embed_error?: string
}

export interface ResearchRecord {
  // researched | found-nothing | unavailable | failed | skipped. "Researched and found
  // nothing" and "research failed" are different facts and only one is worth retrying.
  status: string
  finished_at?: number
  cost_usd?: number
  batch?: string
  provider?: string
  scopes?: ResearchScope[]
  error?: string
}

// -------- Phase 2a: branch lineage + checkpoints --------- //

export interface RunLineage {
  parent: { run_id: string; name: string | null; branch_turn: number | null } | null
  branches: {
    run_id: string
    name: string | null
    branch_turn: number | null
    status: string
    created_at: number | null
  }[]
}

// A per-turn checkpoint descriptor (from GET .../snapshots).
export interface SnapshotInfo {
  turn: number
  status: string | null
  created_at: number | null
}

// The full reconstructed state at a turn (from GET .../snapshots/{turn}).
export interface SnapshotState {
  run_id: string
  turn: number
  status: string
  topic: string
  total_turns: number
  conversation: { speaker: string; content: string; turn: number }[]
  agents: Record<string, AgentResult>
}

// Response from POST .../branch — the new run resuming forward.
export interface BranchResponse {
  run_id: string
  name: string
  slug: string
  name_source: string | null
  description: string
  topic: string
  parent_run_id: string
  parent_name: string | null
  branch_turn: number
  status: string
  max_messages: number
  model: string | null
  mutation: Record<string, unknown> | null
}

export interface BranchTreeNode {
  id: string
  name: string | null
  slug: string | null
  status: string
  branch_turn: number | null
  parent_run_id: string | null
  created_at: number
  turn_count: number
  total_cost_usd: number
  mutation_kind: string | null
}

export interface BranchTreeResponse {
  root_id: string
  nodes: Record<string, BranchTreeNode>
}

// -------- Phase 1.5: post-run analysis (summary + aside threads) --------- //

// The structured summary payload. All list fields may be empty; a plain-text
// fallback lands in `overview` when strict JSON could not be produced.
export interface SummaryPayload {
  consensus?: string[]
  dissenters?: { speaker: string; position: string }[]
  key_ideas?: string[]
  open_questions?: string[]
  // One row per evidence request. A column the conversation never supplied is "not stated".
  evidence_plan?: {
    data: string
    asked_by: string
    decision: string
    moves_them: string
    best_guess: string
    cheapest_way: string
  }[]
  conditional_recommendation?: string
  overview?: string
  // Requested fields the analyst's reply did not supply (they hold their empty value). Present only
  // when there were any; summaries stored before 2026-10-01 never carry it.
  omitted?: string[]
}

export interface StoredSummary {
  id: number
  run_id: string
  kind: 'generated' | 'imported'
  payload: SummaryPayload
  tokens_in: number
  tokens_out: number
  cost_usd: number
  // The effective analyst-role instructions that created this summary; null
  // means the default framing was used (UI prefills default_instructions).
  instructions?: string | null
  created_at: number
  parsed?: boolean
}

// Response shape for the summary endpoints. `default_instructions` is the
// editable analyst-role framing; the guardrails (JSON schema, JSON-only,
// no-fabrication) are enforced automatically and are NOT part of it.
/** A working assumption the room reasons from (matrix_studio/assumptions.py) — not a fact. */
export interface WorkingAssumption {
  id: string
  statement: string
  basis: string
  source: string
  turn: number
  /** Set when a fork replaced an earlier assumption with this id: what it said before. */
  replaces?: string
}

export interface SummaryResponse {
  run_id: string
  generated: StoredSummary | null
  imported: StoredSummary | null
  default_instructions: string
}

// 'consultant': one of the run's consultants (matrix_studio/experts.py), asked directly.
export type AsideTarget = 'analyst' | 'persona' | 'room' | 'consultant'

export interface ThreadSummary {
  id: string
  run_id: string
  target: AsideTarget
  persona_name: string | null
  mode: string
  created_at: number
  message_count: number
  total_cost_usd: number
}

export interface ThreadMessage {
  id: number
  thread_id: string
  // 'error': the aside worker could not write a reply, and says so in the thread.
  role: 'user' | 'target' | 'error'
  speaker: string | null
  content: string
  tokens_in: number
  tokens_out: number
  cost_usd: number
  created_at: number
}

export interface ThreadDetail {
  id: string
  run_id: string
  target: AsideTarget
  persona_name: string | null
  mode: string
  created_at: number
  messages: ThreadMessage[]
  total_cost_usd: number
}

// Derived per-agent live state maintained in the client from the event stream.
export interface AgentView {
  name: string
  persona: string
  goals: string[]
  portrait: string | null    // LEGACY base64 png — only on runs predating blob storage
  portraitKey: string | null // blob key; content-addressed
  portraitUrl: string | null // derived from the key when the event is folded
  avatarResolved: boolean    // whether avatar.ready has fired (even if null)
  messageCount: number
  tokensIn: number
  tokensOut: number
  costUsd: number
}

/** A retrieved passage, as `document.retrieved` records it: enough to open it in the source viewer. */
export interface SourcePassage {
  chunk_id: number
  document_id: string
  title: string
  ordinal: number
  authority?: string
  origin?: string
}

/** One citation found in a message, as the engine judged it (`matrix_studio/citations.py`). */
export interface CitationMark {
  label: string
  title: string
  /** firsthand: in the speaker's own passages; secondhand: attributed to who surfaced it;
   *  anything else: nobody it was attributed to had it. */
  kind: string
  attributive?: boolean
  via?: string
  reason?: string
}

/** A verbatim run of words a message shares with an in-view passage (`matrix_studio/attribution.py`). */
export interface Quote {
  chunk_id: number
  document_id: string
  title: string
  ordinal: number
  /** The words as they appear in the message. */
  phrase: string
  content_words: number
  words: number
}

export interface FeedMessage {
  turn: number
  seq: number
  speaker: string
  content: string
  /** The passages that were in this message's prompt. Absent when retrieval gave it none. */
  sources?: SourcePassage[]
  /** The citations the engine found in the text, with their provenance. */
  citations?: CitationMark[]
  /** Set when this is a consultant's answer, not a participant's turn. */
  consultant?: { expert: string; askedBy: string; question: string }
  /** Put into the conversation by the operator (a branch or a scheduled message), not generated. */
  injected?: boolean
  /** Set when the persona said its position moved in this message (matrix_studio/shifts.py). */
  shift?: PositionShift
}

export interface PositionShift {
  sentences: string[]
  credits: { kind: string; name: string }[]
  conditions: { position: string; firmness: string; condition: string }[]
  matched_conditions: string[]
  no_listed_condition: boolean
}

// -------- Phase 2c: cognition / introspection -------- //

export interface CognitionSettings {
  enabled: boolean
  memory?: boolean
  reflection_every?: number
  goals_dynamic?: boolean
  relationships?: boolean
  retrieval_k?: number
}

export interface DossierMemory {
  id: string
  content: string
  importance: number | null
  tags: string[]
  timestamp: number
}

/** Phase 5: a document attached to this persona (or to the whole cast). */
export interface DossierDocument {
  document_id: string
  title: string
  media_type: string | null
  char_count: number
  chunk_count: number
  cast_wide: boolean
}

/** Phase 5: one turn's retrieval, straight from the document.retrieved event. */
export interface DossierRetrieval {
  turn: number
  query: string | null
  total_chars: number | null
  passages: {
    chunk_id: number
    document_id: string
    title: string
    ordinal: number
    score: number
    chars: number
    // What KIND of source, and whether a human chose it. Both absent unless the passage
    // came from a knowledge base that has been researched into — see below.
    authority?: string
    origin?: string
  }[]
  // How many of this turn's passages a searcher found rather than a human chose. Absent
  // when none did, which is every run without research.
  //
  // Recorded per turn because the problem it exposes is invisible otherwise
  // (PERSONA-RESEARCH.md §5.1): the retrieval floor reserves a slot for a COLLECTION, not
  // for a kind of thing in one, so once research writes into a curated collection the
  // operator's own document competes with the searcher's finds and can lose every slot.
  researched_passages?: number
}

// Phase 6 structured persona, as the dossier returns it. Mirrors
// `matrix_studio/personas.py` MINUS the two operator-private fields.
export interface StructuredViewpoint {
  position: string
  formed_by?: string
  firmness: 'negotiable' | 'firm' | 'non-negotiable' | 'requires-escalation'
  evidence_that_shifts?: string[]
}

export interface StructuredPersona {
  role?: string
  background?: {
    tenure_years?: number | null
    prior_roles?: string[]
    formative_events?: { year?: number | null; event: string; lesson?: string }[]
  }
  preferences?: {
    optimises_for?: string[]
    dismisses?: string[]
    persuaded_by?: string[]
  }
  viewpoints?: StructuredViewpoint[]
}

/** Phase 4b: a setup, promise or deferred consequence this persona opened (`matrix_studio/state.py`). */
export interface DossierThread {
  id: string
  description: string
  thread_type: string
  origin_turn: number
  status: string
  resolved_turn: number | null
  /** Still open after the run's `thread_stale_after` turns: planted and never paid off. */
  stale?: boolean
}

export interface AgentDossier {
  run_id: string
  agent: string
  persona: string
  goals: string[]
  memory_stream: DossierMemory[]
  beliefs: DossierMemory[]
  relationships: Record<string, string>
  // Only the threads this persona opened. Optional because the fixtures and any older backend omit it,
  // and threads are tracked only when cognition is on, so absent and empty mean the same thing.
  pending_threads?: DossierThread[]
  // Phase 5. Optional so a dossier from an older backend still parses.
  documents?: DossierDocument[]
  /** Knowledge bases this persona searches: the run's and its own. `readable` is re-checked on
   *  every read, so a revoked grant shows as unreadable (and unnamed). */
  knowledge_bases?: { id: string; name: string | null; scope: 'run' | 'persona'; readable: boolean }[]
  document_retrievals?: DossierRetrieval[]
  // Phase 6. Null for a run that used no structured personas. The backend has
  // already stripped `underlying_concern` and `validity` — both are private to
  // the operator by design, so they are absent from this type on purpose rather
  // than by omission. The dossier's Convictions tab renders the rest.
  structured?: StructuredPersona | null
  // Whether cognition was CONFIGURED on the run, which is not the same question as
  // whether it produced anything. Optional so a dossier from an older backend parses;
  // `undefined` means "the backend cannot tell us", and the UI must not read that as
  // false — it said "this run was created without cognition" about a run created with it.
  cognition_enabled?: boolean
  // Turns where the structured reply was discarded (see jsonio's strict-parse fix).
  cognition_lost_turns?: number
  tokens_in: number
  tokens_out: number
  cost_usd: number
  portrait_key: string | null
}

export interface TurnTrace {
  run_id: string
  turn: number
  available: boolean
  speaker?: string
  selection_reason?: string | null
  /** Set only when nobody chose this speaker: `call_failed` or `unresolved`. */
  selection_fallback?: string | null
  utterance?: string
  rationale?: string
  goal_served?: string
  memory_refs?: string[]
  memories?: { id: string; content: string; importance: number | null; tags: string[] }[]
}

/** A knowledge base, as the list and detail routes return it. */
export interface KnowledgeBase {
  id: string
  name: string
  description: string | null
  owner_sub: string
  embedding_model: string | null
  created_at: number | null
  /** True when someone else owns it and it was shared with you. Reported by the
   *  server rather than derived from `owner_sub`, so the UI's notion of "mine"
   *  cannot drift from the one the write routes enforce. */
  shared: boolean
  document_count?: number
}

export interface KbDocument {
  id: string
  title: string
  char_count: number | null
  chunk_count: number | null
  created_at: number | null
}

export interface KbGrant {
  kb_id: string
  principal: string
  kind: string
  granted_by: string | null
  created_at: number | null
}

export interface KnowledgeBaseDetail extends KnowledgeBase {
  documents: KbDocument[]
  /** Null for a grantee: who else a collection is shared with is the owner's
   *  business, and grants name other users' subs. */
  grants: KbGrant[] | null
}
