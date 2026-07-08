// SPDX-License-Identifier: Apache-2.0
// Thin REST client. Keys never touch the browser — all provider credentials
// live server-side; these endpoints only exchange run metadata and events.

import type { RunDetail, RunSummary, SimEvent } from './types'

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
}

// Build the WebSocket URL for a run's live stream.
export function streamUrl(runId: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/api/runs/${encodeURIComponent(runId)}/stream`
}
