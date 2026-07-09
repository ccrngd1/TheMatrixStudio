// SPDX-License-Identifier: Apache-2.0
// Thin REST client. Keys never touch the browser — all provider credentials
// live server-side; these endpoints only exchange run metadata and events.

import type {
  AsideTarget,
  RunDetail,
  RunSummary,
  SimEvent,
  StoredSummary,
  ThreadDetail,
  ThreadMessage,
  ThreadSummary,
} from './types'

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail || detail
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status}: ${detail}`)
  }
  return res.json() as Promise<T>
}

export interface CreateRunBody {
  topic: string
  cast: { name: string; persona: string; goals: string[] }[]
  config: { max_messages?: number; generate_avatars?: boolean }
  model?: string
  name?: string
  description?: string
  // Phase 1.5: optional summary config (omit → default: enabled + full fields).
  summary?: { enabled: boolean; fields?: string[]; focus?: string }
}

export interface CreateRunResponse {
  run_id: string
  name: string
  description: string
  slug: string
  name_source: string | null
  topic: string
  status: string
}

export const api = {
  listRuns: (q?: string) =>
    jsonFetch<{ runs: RunSummary[] }>(
      `/api/runs${q ? `?q=${encodeURIComponent(q)}` : ''}`,
    ).then((r) => r.runs),

  getRun: (ref: string) => jsonFetch<RunDetail>(`/api/runs/${encodeURIComponent(ref)}`),

  getEvents: (ref: string, afterSeq = -1) =>
    jsonFetch<{ run_id: string; events: SimEvent[] }>(
      `/api/runs/${encodeURIComponent(ref)}/events?after_seq=${afterSeq}`,
    ).then((r) => r.events),

  createRun: (body: CreateRunBody) =>
    jsonFetch<CreateRunResponse>('/api/runs', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  suggestName: (topic: string) =>
    jsonFetch<{ name: string; description: string; slug: string; source: string }>(
      `/api/name/suggest?topic=${encodeURIComponent(topic)}`,
    ),

  getModels: () =>
    jsonFetch<{ default: string; models: string[] }>('/api/models'),

  // -------- Phase 1.5: post-run analysis (read-only) -------- //

  getSummary: (ref: string) =>
    jsonFetch<{ run_id: string; generated: StoredSummary | null; imported: StoredSummary | null }>(
      `/api/runs/${encodeURIComponent(ref)}/summary`,
    ),

  generateSummary: (ref: string, body?: { fields?: string[]; focus?: string }) =>
    jsonFetch<{ run_id: string; generated: StoredSummary; imported: StoredSummary | null }>(
      `/api/runs/${encodeURIComponent(ref)}/summary`,
      { method: 'POST', body: JSON.stringify(body || {}) },
    ),

  listThreads: (ref: string) =>
    jsonFetch<{ run_id: string; threads: ThreadSummary[] }>(
      `/api/runs/${encodeURIComponent(ref)}/threads`,
    ).then((r) => r.threads),

  createThread: (ref: string, target: AsideTarget, personaName?: string) =>
    jsonFetch<ThreadSummary>(`/api/runs/${encodeURIComponent(ref)}/threads`, {
      method: 'POST',
      body: JSON.stringify({ target, persona_name: personaName }),
    }),

  getThread: (threadId: string) =>
    jsonFetch<ThreadDetail>(`/api/threads/${encodeURIComponent(threadId)}`),

  postThreadMessage: (threadId: string, content: string) =>
    jsonFetch<{ thread_id: string; reply: ThreadMessage; total_cost_usd: number }>(
      `/api/threads/${encodeURIComponent(threadId)}/messages`,
      { method: 'POST', body: JSON.stringify({ content }) },
    ),
}

// Build the WebSocket URL for a run's live stream.
export function streamUrl(runId: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/api/runs/${encodeURIComponent(runId)}/stream`
}
