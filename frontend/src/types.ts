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
