// SPDX-License-Identifier: Apache-2.0
// Knowledge bases: create a collection, put documents in it, share it, revoke.
//
// This is the half of Phase 6 that was missing. Storage, retrieval, bindings and grants
// all worked and were verified against the live account, and a collection could only be
// created by running a script — so the feature existed and nobody could reach it.
//
// ## What this component does NOT decide
//
// Whether the caller may write to a collection. The server answers that with a 404 from
// `_owned_kb`, and `shared` on each row is what this reads to decide whether to OFFER a
// control. A client-side ownership check would be a second opinion that can disagree
// with the one that actually enforces — and the disagreement would show up as a button
// that 404s, which reads as a broken app rather than a permission.
//
// ## Upload is a two-step flow, and that is deliberate
//
// A file becomes text at `/api/documents/extract` first, and the extracted text is shown
// before it is stored. That matters most for PDFs, where extraction quality varies and a
// scanned page yields nothing at all — storing silently would put an empty document in a
// collection and the only symptom would be retrieval never finding it.

import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { KnowledgeBaseDetail, KnowledgeBase } from '../types'
import { Panel, Tag } from '../ui/primitives'

interface Props {
  onBack: () => void
}

// `onBack` is kept for callers outside the shell; inside it the dock is the way out.
export function KnowledgeBases(_props: Props) {
  const [rows, setRows] = useState<KnowledgeBase[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    api
      .listKnowledgeBases()
      .then((body) => {
        setRows(body.knowledge_bases)
        setError(null)
      })
      .catch((err) => {
        // Not swallowed into an empty list: "you have no collections" and "the request
        // failed" look identical otherwise, and the first is a lie.
        setRows([])
        setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => load(), [load])

  const create = async () => {
    const name = newName.trim()
    if (!name) return
    setCreating(true)
    try {
      const kb = await api.createKnowledgeBase(name)
      setNewName('')
      load()
      setSelected(kb.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="cc-muted">
        A collection of documents, indexed once and searchable from any conversation that
        binds it. Share one and the recipient can search it without a copy being made.
      </p>

      <div className="flex gap-2">
        <input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void create()}
          placeholder="New collection name…"
          aria-label="New collection name"
          className="cc-field min-w-0 flex-1"
        />
        <button
          type="button"
          onClick={() => void create()}
          disabled={creating || !newName.trim()}
          className="cc-btn cc-primary"
        >
          Create
        </button>
      </div>

      {error && (
        <div className="cc-card" role="alert">
          <p className="text-sm text-red-300">{error}</p>
          <button
            onClick={load}
            className="mt-2 rounded border border-matrix-border px-3 py-1 text-xs text-slate-300"
          >
            Try again
          </button>
        </div>
      )}

      {loading ? (
        <p className="cc-empty">Loading…</p>
      ) : rows.length === 0 && !error ? (
        <p className="cc-empty">
          No collections yet. Create one above, then add documents to it.
        </p>
      ) : (
        <div className="cc-list">
          {rows.map((kb) => (
            <Panel key={kb.id} className={selected === kb.id ? 'cc-kb-open' : undefined}>
              <button
                type="button"
                onClick={() => setSelected(selected === kb.id ? null : kb.id)}
                aria-expanded={selected === kb.id}
                className="flex w-full items-start justify-between gap-3 text-left"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="cc-code">{kb.name}</span>
                    {kb.shared && (
                      <>
                        <Tag tone="ens">shared with you</Tag>
                        <span className="sr-only">
                          Someone else owns this and shared it with you. You can search it; you cannot change it.
                        </span>
                      </>
                    )}
                    <Tag>
                      {kb.document_count ?? 0} doc{kb.document_count === 1 ? '' : 's'}
                    </Tag>
                  </div>
                  {kb.description && <p className="cc-topic">{kb.description}</p>}
                  <p className="cc-meta">
                    <code>{kb.id}</code>
                  </p>
                </div>
                <span aria-hidden="true" className="cc-caret">{selected === kb.id ? '▾' : '▸'}</span>
              </button>
              {selected === kb.id && (
                <KbPanel kbId={kb.id} onChanged={load} />
              )}
            </Panel>
          ))}
        </div>
      )}
    </div>
  )
}

/** One collection's documents and sharing, loaded when it is opened. */
function KbPanel({ kbId, onChanged }: { kbId: string; onChanged: () => void }) {
  const [detail, setDetail] = useState<KnowledgeBaseDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api
      .getKnowledgeBase(kbId)
      .then((d) => {
        setDetail(d)
        setError(null)
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
  }, [kbId])

  useEffect(() => load(), [load])

  if (error) {
    return <p className="border-t border-matrix-border p-3 text-sm text-red-300">{error}</p>
  }
  if (!detail) {
    return <p className="border-t border-matrix-border p-3 text-sm text-slate-500">Loading…</p>
  }

  // `shared` decides what is OFFERED. The server decides what is allowed.
  const mine = !detail.shared

  return (
    <div className="border-t border-matrix-border p-3">
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
        Documents
      </h3>
      {detail.documents.length === 0 ? (
        <p className="mb-3 text-sm text-slate-500">Nothing in this collection yet.</p>
      ) : (
        <ul className="mb-3 space-y-1">
          {detail.documents.map((doc) => (
            <li
              key={doc.id}
              className="flex items-center justify-between rounded border border-matrix-border/60 px-2 py-1 text-sm"
            >
              <span className="truncate text-slate-300">{doc.title}</span>
              <span className="ml-3 flex items-center gap-3 whitespace-nowrap text-xs text-slate-500">
                <span>{doc.chunk_count ?? 0} chunk(s)</span>
                {mine && (
                  <button
                    onClick={async () => {
                      setBusy(true)
                      try {
                        await api.deleteKbDocument(kbId, doc.id)
                        load()
                        onChanged()
                      } catch (err) {
                        setError(err instanceof Error ? err.message : String(err))
                      } finally {
                        setBusy(false)
                      }
                    }}
                    disabled={busy}
                    className="text-red-400 hover:text-red-300 disabled:opacity-40"
                    aria-description="Remove from this collection, including its embeddings"
                  >
                    remove
                  </button>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}

      {mine && <AddDocument kbId={kbId} onAdded={() => { load(); onChanged() }} />}
      {mine && <Sharing kbId={kbId} detail={detail} onChanged={load} />}
      {!mine && (
        <p className="mt-2 text-xs text-slate-500">
          Shared with you — searchable from a conversation that binds it. Only its owner
          can add or remove documents.
        </p>
      )}
    </div>
  )
}

/** Paste or extract text, review it, then store it. */
function AddDocument({ kbId, onAdded }: { kbId: string; onAdded: () => void }) {
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)

  const pick = async (file: File) => {
    setBusy(true)
    setError(null)
    try {
      const extracted = await api.extractDocument(file, file.name)
      setTitle((current) => current || extracted.title || file.name)
      setText(extracted.text)
      // Shown rather than stored. A scanned PDF extracts to nothing, and storing that
      // silently puts an empty document in the collection whose only symptom is
      // retrieval never finding it.
      setNote(
        extracted.text.trim()
          ? `Extracted ${extracted.text.length} characters — check it, then add.`
          : 'Extraction produced NO text. A scanned PDF has no text layer; this would be an empty document.',
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const add = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.addKbDocument(kbId, title.trim() || 'untitled', text)
      setTitle('')
      setText('')
      setNote(`Added and embedded ${result.embedded} chunk(s).`)
      onAdded()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mt-2 rounded border border-matrix-border/60 p-2">
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
        Add a document
      </h4>
      <div className="mb-2 flex gap-2">
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Title"
          className="flex-1 rounded border border-matrix-border bg-matrix-bg p-1.5 text-sm"
        />
        <label className="cursor-pointer rounded border border-matrix-border px-3 py-1.5 text-xs text-slate-300 hover:text-slate-100">
          Choose file…
          <input
            type="file"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0]
              if (file) void pick(file)
            }}
          />
        </label>
      </div>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={4}
        placeholder="Paste text, or choose a file to extract it from…"
        className="w-full rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
      />
      {note && <p className="mt-1 text-xs text-slate-400">{note}</p>}
      {error && <p className="mt-1 text-xs text-red-300">{error}</p>}
      <button
        onClick={() => void add()}
        disabled={busy || !text.trim()}
        className="mt-2 rounded bg-matrix-accent px-3 py-1 text-xs font-semibold text-matrix-bg disabled:opacity-40"
      >
        {busy ? 'Working…' : 'Add and embed'}
      </button>
    </div>
  )
}

/** Grants: who else may search this collection. */
function Sharing({
  kbId,
  detail,
  onChanged,
}: {
  kbId: string
  detail: KnowledgeBaseDetail
  onChanged: () => void
}) {
  const [principal, setPrincipal] = useState('')
  const [kind, setKind] = useState<'user' | 'group'>('user')
  const [error, setError] = useState<string | null>(null)

  const share = async () => {
    const value = principal.trim()
    if (!value) return
    try {
      await api.grantKb(kbId, kind === 'user' ? { user: value } : { group: value })
      setPrincipal('')
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="mt-2 rounded border border-matrix-border/60 p-2">
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
        Shared with
      </h4>
      {(detail.grants ?? []).length === 0 ? (
        <p className="mb-2 text-sm text-slate-500">Nobody — this collection is private.</p>
      ) : (
        <ul className="mb-2 space-y-1">
          {(detail.grants ?? []).map((grant) => (
            <li
              key={`${grant.kind}:${grant.principal}`}
              className="flex items-center justify-between text-sm"
            >
              <span className="truncate text-slate-300">
                <span className="mr-2 text-xs uppercase text-slate-500">{grant.kind}</span>
                {grant.principal}
              </span>
              <button
                onClick={async () => {
                  try {
                    await api.revokeKb(
                      kbId,
                      grant.kind === 'group'
                        ? { group: grant.principal }
                        : { user: grant.principal },
                    )
                    onChanged()
                  } catch (err) {
                    setError(err instanceof Error ? err.message : String(err))
                  }
                }}
                className="ml-3 text-xs text-red-400 hover:text-red-300"
                aria-description="Takes effect on the next turn of any conversation using it"
              >
                revoke
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex gap-2">
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as 'user' | 'group')}
          className="rounded border border-matrix-border bg-matrix-bg p-1.5 text-sm"
        >
          <option value="user">User</option>
          <option value="group">Group</option>
        </select>
        <input
          value={principal}
          onChange={(e) => setPrincipal(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void share()}
          placeholder={kind === 'user' ? 'Cognito sub…' : 'Cognito group…'}
          className="flex-1 rounded border border-matrix-border bg-matrix-bg p-1.5 text-sm"
        />
        <button
          onClick={() => void share()}
          disabled={!principal.trim()}
          className="rounded border border-matrix-border px-3 py-1 text-xs text-slate-300 disabled:opacity-40"
        >
          Share
        </button>
      </div>
      {error && <p className="mt-1 text-xs text-red-300">{error}</p>}
    </div>
  )
}
