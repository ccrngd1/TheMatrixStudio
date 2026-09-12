// SPDX-License-Identifier: Apache-2.0
// Thin REST client. Keys never touch the browser — all provider credentials
// live server-side; these endpoints only exchange run metadata and events.

import type {
  AgentDossier,
  AsideTarget,
  BranchResponse,
  BranchTreeResponse,
  CognitionSettings,
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
  TurnTrace,
} from './types'

// Set once at start-up, so `jsonFetch` can attach a token without every call site
// knowing about auth. A function rather than a stored token: `validToken` refreshes when
// the current one is near expiry, and a copy captured here would go stale after an hour.
let tokenProvider: (() => Promise<string | null>) | null = null

export function setTokenProvider(
  provider: (() => Promise<string | null>) | null,
): void {
  tokenProvider = provider
}

// Called when the API rejects a token the SPA believed was good — a revoked session, or a
// pool the token was not issued for. The app sends the user back to the Hosted UI rather
// than showing a 401 for every panel.
let onUnauthorized: (() => void) | null = null

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler
}

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  // Attached HERE and nowhere else, so no request can be written that forgets it — the
  // same reasoning that makes `_slice_filter` and `may_read_kb` single chokepoints
  // server-side.
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((init?.headers as Record<string, string>) ?? {}),
  }
  if (tokenProvider) {
    const token = await tokenProvider()
    if (token) headers.Authorization = `Bearer ${token}`
  }
  const res = await fetch(url, { ...init, headers })
  if (res.status === 401 && onUnauthorized) {
    // Before the handler existed a 401 surfaced as "401: Unauthorized" in whichever
    // panel happened to fetch first, which reads as a broken app rather than an expired
    // session.
    onUnauthorized()
  }
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

/** One drafted persona from the wizard, in the same shape `createRun` accepts. */
export interface SuggestedPersona {
  name: string
  persona: string
  goals: string[]
  structured: {
    role?: string
    background?: { formative_events?: { year?: number | null; event: string; lesson?: string }[] }
    preferences?: { optimises_for?: string[]; dismisses?: string[]; persuaded_by?: string[] }
    viewpoints: {
      position: string
      firmness: string
      evidence_that_shifts?: string[]
      underlying_concern?: string
    }[]
  }
}

export interface CreateRunBody {
  topic: string
  cast: {
    name: string
    persona: string
    goals: string[]
    // Phase 6 convictions. `validity` is deliberately absent — it is the operator's
    // private calibration note and is never rendered into any prompt.
    structured?: {
      viewpoints: {
        position: string
        firmness: string
        evidence_that_shifts?: string[]
        // Withheld from the conversation, NOT from the operator: the persona knows it
        // and says it only when asked why it holds the position.
        underlying_concern?: string
      }[]
      preferences?: { dismisses: string[] }
    }
    // Phase 5 background documents pasted inline. The cast-level `documents` field
    // takes SERVER-readable paths, which a browser cannot supply, so inline text is
    // the only workable browser flow — and it must be sent at creation, because the
    // engine ingests cast documents before turn 1.
    document_texts?: { title: string; text: string }[]
  }[]
  config: {
    max_messages?: number
    generate_avatars?: boolean
    cognition?: CognitionSettings
    // Phase 6 / Phase 5. Sent only when the cast actually authored the relevant
    // content; enabling a feature nobody configured would cost tokens for an empty
    // prompt block.
    personas?: { enabled: boolean; withhold_concerns?: boolean; dismissal_rule?: string }
    retrieval?: { enabled: boolean; mode?: string; k?: number; max_chars?: number }
  }
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

/**
 * URL for a persona's avatar image.
 *
 * `key` is content-addressed, so passing it as `v` makes the URL change whenever the
 * image does — which is what lets the server mark the response `immutable`.
 */
export function avatarUrl(runId: string, name: string, key: string | null): string | null {
  if (!key) return null
  return `/api/runs/${encodeURIComponent(runId)}/agents/${encodeURIComponent(name)}` +
    `/avatar?v=${encodeURIComponent(key)}`
}

export const api = {
  listRuns: (q?: string) =>
    jsonFetch<{ runs: RunSummary[] }>(
      `/api/runs${q ? `?q=${encodeURIComponent(q)}` : ''}`,
    ).then((r) => r.runs),

  getRun: (ref: string) => jsonFetch<RunDetail>(`/api/runs/${encodeURIComponent(ref)}`),

  /**
   * This run's setup, shaped as a create-run body, for starting a fresh conversation
   * from it. Loaded into the new-run form to edit — NOT run directly, since the point
   * is to change something before running it again.
   */
  getRunSetup: (ref: string) =>
    jsonFetch<{ run_id: string; setup: CreateRunBody; warnings: string[] }>(
      `/api/runs/${encodeURIComponent(ref)}/setup`,
    ),

  getEvents: (ref: string, afterSeq = -1) =>
    jsonFetch<{ run_id: string; events: SimEvent[] }>(
      `/api/runs/${encodeURIComponent(ref)}/events?after_seq=${afterSeq}`,
    ).then((r) => r.events),

  /** Which knowledge-base file types this server can read, and the size limits. */
  getDocumentFormats: () =>
    jsonFetch<{
      formats: { suffix: string; media_type: string; available: boolean; needs: string | null }[]
      max_upload_bytes: number
      max_document_chars: number
    }>('/api/documents/formats'),

  /**
   * Extract text from an uploaded knowledge-base file. Stores nothing: the caller
   * submits the returned text as a persona's `document_texts`, so an uploaded file
   * and pasted text share one ingest path — and the operator can read what was
   * actually extracted before it becomes a persona's knowledge base.
   *
   * No Content-Type header: the browser must set the multipart boundary itself.
   */
  extractDocument: (file: File, title?: string) => {
    const form = new FormData()
    form.append('file', file)
    if (title) form.append('title', title)
    return jsonFetch<{
      title: string
      media_type: string
      text: string
      char_count: number
      chunk_count: number
      stored: boolean
    }>('/api/documents/extract', { method: 'POST', body: form, headers: {} })
  },

  createRun: (body: CreateRunBody) =>
    jsonFetch<CreateRunResponse>('/api/runs', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /**
   * Draft a cast from a short brief. AUTHORING ASSISTANCE — the result is a draft
   * the operator edits in the form; it never starts a run by itself.
   */
  suggestPersonas: (brief: string, count: number, model?: string) =>
    jsonFetch<{ cast: SuggestedPersona[]; count: number }>('/api/personas/suggest', {
      method: 'POST',
      body: JSON.stringify({ brief, count, model }),
    }),

  suggestName: (topic: string) =>
    jsonFetch<{ name: string; description: string; slug: string; source: string }>(
      `/api/name/suggest?topic=${encodeURIComponent(topic)}`,
    ),

  getModels: () =>
    jsonFetch<{ default: string; models: { id: string; label: string }[] }>('/api/models'),

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

  getRunTree: (ref: string) =>
    jsonFetch<BranchTreeResponse>(`/api/runs/${encodeURIComponent(ref)}/tree`),

  // -------- Phase 2c: introspection (read-only) -------- //

  getDossier: (ref: string, name: string) =>
    jsonFetch<AgentDossier>(
      `/api/runs/${encodeURIComponent(ref)}/agents/${encodeURIComponent(name)}/dossier`,
    ),

  getTurnTrace: (ref: string, turn: number) =>
    jsonFetch<TurnTrace>(
      `/api/runs/${encodeURIComponent(ref)}/turns/${turn}/trace`,
    ),

  // Error-recovery: resume an interrupted/failed run forward in place.
  /**
   * Ask a live run to stop after the turn it is generating. 202 = request
   * registered; the effect lands one turn later, so the UI must not assume the run
   * is already over.
   */
  stopRun: (ref: string) =>
    jsonFetch<{ run_id: string; status: string; stop_requested: boolean }>(
      `/api/runs/${encodeURIComponent(ref)}/stop`,
      { method: 'POST' },
    ),

  resumeRun: (ref: string) =>
    jsonFetch<{ run_id: string; name: string | null; status: string }>(
      `/api/runs/${encodeURIComponent(ref)}/resume`,
      { method: 'POST' },
    ),

  // Regenerate avatar for a specific agent
  regenerateAvatar: (ref: string, name: string) =>
    jsonFetch<{ run_id: string; agent: string; portrait_key: string }>(
      `/api/runs/${encodeURIComponent(ref)}/agents/${encodeURIComponent(name)}/regenerate-avatar`,
      { method: 'POST' },
    ),
}

// Build the WebSocket URL for a run's live stream.
export function streamUrl(runId: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/api/runs/${encodeURIComponent(runId)}/stream`
}
