// SPDX-License-Identifier: Apache-2.0
// Thin REST client. Keys never touch the browser — all provider credentials
// live server-side; these endpoints only exchange run metadata and events.

import type {
  AsideTarget,
  BranchResponse,
  RunDetail,
  RunSummary,
  SimEvent,
  SnapshotInfo,
  SnapshotState,
  StoredSummary,
  SummaryResponse,
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
    jsonFetch<SummaryResponse>(`/api/runs/${encodeURIComponent(ref)}/summary`),

  // `instructions` REPLACES the default analyst-role framing (guardrails always
  // remain, enforced server-side). Omit → default framing.
  generateSummary: (
    ref: string,
    body?: { fields?: string[]; focus?: string; instructions?: string; model?: string },
  ) =>
    jsonFetch<SummaryResponse & { generated: StoredSummary }>(
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

  postThreadMessage: (threadId: string, content: string, model?: string) =>
    jsonFetch<{ thread_id: string; reply: ThreadMessage; total_cost_usd: number }>(
      `/api/threads/${encodeURIComponent(threadId)}/messages`,
      { method: 'POST', body: JSON.stringify({ content, model }) },
    ),

  // -------- Phase 2a: checkpoints + branching -------- //

  listSnapshots: (ref: string) =>
    jsonFetch<{ run_id: string; snapshots: SnapshotInfo[] }>(
      `/api/runs/${encodeURIComponent(ref)}/snapshots`,
    ).then((r) => r.snapshots),

  getSnapshot: (ref: string, turn: number) =>
    jsonFetch<SnapshotState>(
      `/api/runs/${encodeURIComponent(ref)}/snapshots/${turn}`,
    ),

  // Fork a run at `fromTurn` into a new run that resumes forward. The parent is
  // never modified. Returns immediately with the new run's id + codename.
  branchRun: (
    ref: string,
    fromTurn: number,
    opts?: { name?: string; description?: string; model?: string; mutation?: Record<string, unknown> },
  ) =>
    jsonFetch<BranchResponse>(`/api/runs/${encodeURIComponent(ref)}/branch`, {
      method: 'POST',
      body: JSON.stringify({ from_turn: fromTurn, ...opts }),
    }),

  // Error-recovery: resume an interrupted/failed run forward in place.
  resumeRun: (ref: string) =>
    jsonFetch<{ run_id: string; name: string | null; status: string }>(
      `/api/runs/${encodeURIComponent(ref)}/resume`,
      { method: 'POST' },
    ),
}

// Build the WebSocket URL for a run's live stream.
export function streamUrl(runId: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/api/runs/${encodeURIComponent(runId)}/stream`
}
