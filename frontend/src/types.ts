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
  created_at: number
  parsed?: boolean
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
