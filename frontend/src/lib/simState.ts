// SPDX-License-Identifier: Apache-2.0
// Pure functions that fold the revealed event stream into view state.
//
// Playback (§5a) is implemented entirely here on the client: the full event
// buffer is received/persisted regardless of the viewer, and the UI chooses how
// many events to *reveal*. deriveState() is called with the slice of buffered
// events up to the current reveal cursor. Nothing here ever talks to the engine.

import { avatarUrl } from '../api'
import type { AgentView, FeedMessage, Persona, SimEvent, SourcePassage } from '../types'

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
  // Every terminal event the engine can emit. Missing one leaves a finished run
  // looking live: the Stop button stays offered and the analysis affordances stay
  // hidden until the page is reloaded.
  status: 'idle' | 'running' | 'complete' | 'failed' | 'stopped' | 'capped' | 'interrupted'
  error: string | null
  /** Each turn's retrieved passages, keyed `turn|speaker`, until that speaker's message claims them. */
  retrieved: Record<string, SourcePassage[]>
  /** Every passage retrieved so far in the run, keyed by its citation label (`title #ordinal`).
   *  A message may cite a source another persona surfaced; this is how that citation still opens. */
  sourceIndex: Record<string, SourcePassage>
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
      portraitKey: null,
      portraitUrl: null,
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
    retrieved: {},
    sourceIndex: {},
  }
}

function ensureAgent(state: SimState, name: string) {
  if (!state.agents[name]) {
    state.agents[name] = {
      name,
      persona: '',
      goals: [],
      portrait: null,
      portraitKey: null,
      portraitUrl: null,
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

  // Every model call's cost, not only the personas' replies. The engine records selection,
  // validation, rejected attempts, reflections, passes and avatars on their own events; counting
  // `agent.response` alone showed $4.25 for a run that Bedrock's own log put at ~$5.15.
  // `agent.response` is excluded here only because its case below already adds it.
  if (e.event_type !== 'agent.response' && typeof e.payload?.cost_usd === 'number') {
    state.totalCost += e.payload.cost_usd
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
        // `portrait_key` is the current shape; `portrait_b64` only appears on runs
        // recorded before avatars moved out of the event payload. Both are carried so
        // those runs keep rendering.
        const key = e.payload.portrait_key ?? null
        state.agents[name] = {
          ...state.agents[name],
          portrait: e.payload.portrait_b64 ?? null,
          portraitKey: key,
          // Built here because the event carries its own run_id, so no component
          // needs the run threaded down to it just to render a face.
          portraitUrl: avatarUrl(e.run_id, name, key),
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
    case 'document.retrieved': {
      // Retrieval precedes the message it feeds, on the same turn and by the same speaker, so the
      // passages are parked under that key until the message arrives and claims them.
      const name = e.payload.speaker ?? e.agent_name
      const passages: SourcePassage[] = Array.isArray(e.payload.passages) ? e.payload.passages : []
      if (name && passages.length) {
        state.retrieved = { ...state.retrieved, [`${e.turn}|${name}`]: passages }
        state.sourceIndex = { ...state.sourceIndex }
        for (const p of passages) state.sourceIndex[`${p.title} #${p.ordinal}`] = p
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
        // What was in the prompt: the turn's retrieval, narrowed to `document_refs` when the
        // engine recorded them (they are the causal set). Left undefined, not [], when there was
        // none, so a run without retrieval renders exactly as before.
        const key = `${e.turn}|${name}`
        const parked = state.retrieved[key]
        const refs: unknown = e.payload.document_refs
        const sources = parked
          ? Array.isArray(refs)
            ? parked.filter((p) => refs.includes(p.chunk_id))
            : parked
          : undefined
        if (parked) {
          const { [key]: _claimed, ...rest } = state.retrieved
          state.retrieved = rest
        }
        const citations = Array.isArray(e.payload.citation_provenance)
          ? e.payload.citation_provenance
          : undefined
        state.feed = [
          ...state.feed,
          {
            turn: e.turn,
            seq: e.seq,
            speaker: name,
            content: e.payload.message ?? e.payload.content ?? '',
            ...(sources && sources.length ? { sources } : {}),
            ...(citations && citations.length ? { citations } : {}),
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
    // Ended with a transcript, but not by finishing: stopped by the operator, cut
    // off by the cost cap, or orphaned by a process that died. Each is terminal and
    // each leaves real turns to inspect, so they are distinct rather than folded
    // into 'complete' or 'failed'.
    case 'sim.stopped':
      state.status = 'stopped'
      state.thinking = false
      state.activeSpeaker = null
      break
    case 'sim.capped':
      state.status = 'capped'
      state.thinking = false
      state.activeSpeaker = null
      break
    case 'sim.interrupted':
      state.status = 'interrupted'
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
