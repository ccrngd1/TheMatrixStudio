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
  portrait: string | null
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
  portrait: string | null // base64 png, or null (placeholder)
  avatarResolved: boolean // whether avatar.ready has fired (even if null)
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
