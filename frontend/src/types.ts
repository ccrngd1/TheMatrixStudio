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
  overview?: string
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
export interface SummaryResponse {
  run_id: string
  generated: StoredSummary | null
  imported: StoredSummary | null
  default_instructions: string
}

export type AsideTarget = 'analyst' | 'persona' | 'room'

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
  role: 'user' | 'target'
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

export interface FeedMessage {
  turn: number
  seq: number
  speaker: string
  content: string
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
  }[]
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

export interface AgentDossier {
  run_id: string
  agent: string
  persona: string
  goals: string[]
  memory_stream: DossierMemory[]
  beliefs: DossierMemory[]
  relationships: Record<string, string>
  // Phase 5. Optional so a dossier from an older backend still parses.
  documents?: DossierDocument[]
  document_retrievals?: DossierRetrieval[]
  // Phase 6. Null for a run that used no structured personas. The backend has
  // already stripped `underlying_concern` and `validity` — both are private to
  // the operator by design, so they are absent from this type on purpose rather
  // than by omission. No UI renders this yet (see docs/BACKLOG.md).
  structured?: StructuredPersona | null
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
  utterance?: string
  rationale?: string
  goal_served?: string
  memory_refs?: string[]
  memories?: { id: string; content: string; importance: number | null; tags: string[] }[]
}
