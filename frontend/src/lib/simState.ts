// SPDX-License-Identifier: Apache-2.0
// Pure functions that fold the revealed event stream into view state.
//
// Playback (§5a) is implemented entirely here on the client: the full event
// buffer is received/persisted regardless of the viewer, and the UI chooses how
// many events to *reveal*. deriveState() is called with the slice of buffered
// events up to the current reveal cursor. Nothing here ever talks to the engine.

import type { AgentView, FeedMessage, Persona, SimEvent } from '../types'

export interface SimState {
  topic: string | null
  agents: Record<string, AgentView>
  order: string[] // stable agent display order
  feed: FeedMessage[]
  activeSpeaker: string | null
  thinking: boolean // between speaker.selected and its agent.response
  totalCost: number
  totalTokensIn: number
  totalTokensOut: number
  status: 'idle' | 'running' | 'complete' | 'failed'
  error: string | null
}

export function initialState(cast: Persona[] = []): SimState {
  const agents: Record<string, AgentView> = {}
  const order: string[] = []
  for (const p of cast) {
    agents[p.name] = {
      name: p.name,
      persona: p.persona,
      goals: p.goals || [],
      portrait: null,
      avatarResolved: false,
      messageCount: 0,
      tokensIn: 0,
      tokensOut: 0,
      costUsd: 0,
    }
    order.push(p.name)
  }
  return {
    topic: null,
    agents,
    order,
    feed: [],
    activeSpeaker: null,
    thinking: false,
    totalCost: 0,
    totalTokensIn: 0,
    totalTokensOut: 0,
    status: cast.length ? 'running' : 'idle',
    error: null,
  }
}

function ensureAgent(state: SimState, name: string) {
  if (!state.agents[name]) {
    state.agents[name] = {
      name,
      persona: '',
      goals: [],
      portrait: null,
      avatarResolved: false,
      messageCount: 0,
      tokensIn: 0,
      tokensOut: 0,
      costUsd: 0,
    }
    state.order.push(name)
  }
}

// Fold a single event into a (mutable copy of) state.
export function applyEvent(prev: SimState, e: SimEvent): SimState {
  const state: SimState = {
    ...prev,
    agents: { ...prev.agents },
    order: [...prev.order],
    feed: prev.feed,
  }

  switch (e.event_type) {
    case 'sim.started':
      state.topic = e.payload.topic ?? state.topic
      state.status = 'running'
      break
    case 'avatar.ready': {
      const name = e.payload.agent_name ?? e.agent_name
      if (name) {
        ensureAgent(state, name)
        state.agents[name] = {
          ...state.agents[name],
          portrait: e.payload.portrait_b64 ?? null,
          avatarResolved: true,
        }
      }
      break
    }
    case 'speaker.selected': {
      const name = e.payload.speaker ?? e.agent_name
      if (name) {
        ensureAgent(state, name)
        state.activeSpeaker = name
        state.thinking = true
      }
      break
    }
    case 'agent.response': {
      const name = e.payload.speaker ?? e.agent_name
      if (name) {
        ensureAgent(state, name)
        const a = state.agents[name]
        const tin = e.payload.tokens_in ?? 0
        const tout = e.payload.tokens_out ?? 0
        const cost = e.payload.cost_usd ?? 0
        state.agents[name] = {
          ...a,
          messageCount: a.messageCount + 1,
          tokensIn: a.tokensIn + tin,
          tokensOut: a.tokensOut + tout,
          costUsd: a.costUsd + cost,
        }
        state.feed = [
          ...state.feed,
          {
            turn: e.turn,
            seq: e.seq,
            speaker: name,
            content: e.payload.message ?? e.payload.content ?? '',
          },
        ]
        state.totalCost += cost
        state.totalTokensIn += tin
        state.totalTokensOut += tout
        state.thinking = false
        state.activeSpeaker = null
      }
      break
    }
    case 'sim.completed':
      state.status = 'complete'
      state.thinking = false
      state.activeSpeaker = null
      break
    case 'sim.failed':
      state.status = 'failed'
      state.error = e.payload.error ?? 'Simulation failed'
      state.thinking = false
      state.activeSpeaker = null
      break
  }
  return state
}

// Fold an ordered list of events from a base state.
export function deriveState(base: SimState, events: SimEvent[]): SimState {
  return events.reduce(applyEvent, base)
}
