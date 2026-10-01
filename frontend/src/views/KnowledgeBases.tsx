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
// collection and the only symptom would be retrieval never finding it. The drop target
// is only a new way IN to the first step: dropping, tapping and choosing all end at the
// same review, and nothing is stored until "Add and embed".
//
// ## What the inside of a collection does not show, and why (docs/MOBILE-UI.md §4.10)
//
// The spec also draws live "indexing n/m" ticks and the runs a collection is bound to.
// Neither is here because the API does not say either:
//
// - Embedding happens INLINE in the add request, which either embeds every chunk or
//   fails with a 502 — there is no partially indexed state to poll, and the detail route
//   reports a document's `chunk_count` but not how many of them have vectors. What is
//   shown instead is the truth that is available: the request is in flight.
// - Bindings live on the run (`config.knowledge_bases`, a persona's `knowledge_bases`),
//   and nothing returns them from the collection's side. Deriving them would mean
//   reading every run's setup to answer a question about one collection.
//
// Both would be backend changes first; drawing them from guesses would be worse than
// leaving them out.

import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react'
import { api } from '../api'
import type { KbDocument, KnowledgeBaseDetail, KnowledgeBase } from '../types'
import { Btn, Label, Panel, Tag } from '../ui/primitives'
import { Icon } from '../ui/icons'
import './KnowledgeBases.css'

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

      {/* "Loading…" only while there is nothing to show. Adding or removing a document refreshes
          this list for its counts, and blanking it then unmounted the open collection — taking
          the note that said what was just embedded, or the error that said it was not, with it. */}
      {loading && rows.length === 0 ? (
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

const message = (err: unknown) => (err instanceof Error ? err.message : String(err))
const pad = (n: number) => String(n).padStart(2, '0')

/** "1 chunk", "6 chunks". Null is a row that carries no count (storage fills an absent field with
 *  null; every current write sets one): unknown, which is not the same as zero. */
function chunkLabel(n: number | null) {
  if (n == null) return 'chunks unknown'
  return `${n} chunk${n === 1 ? '' : 's'}`
}

/** One collection's documents and sharing, loaded when it is opened. */
function KbPanel({ kbId, onChanged }: { kbId: string; onChanged: () => void }) {
  const [detail, setDetail] = useState<KnowledgeBaseDetail | null>(null)
  // Two errors, not one. A failed LOAD has nothing to show; a failed REMOVE has the whole
  // collection to show, and replacing it with the error hid what was still there.
  const [loadError, setLoadError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)

  const load = useCallback(() => {
    api
      .getKnowledgeBase(kbId)
      .then((d) => {
        setDetail(d)
        setLoadError(null)
      })
      .catch((err) => setLoadError(message(err)))
  }, [kbId])

  useEffect(() => load(), [load])

  if (loadError && !detail) {
    return (
      <div className="cc-kb-inside" role="alert">
        <p className="cc-kb-err">{loadError}</p>
        <Btn size="sm" onClick={load}>Try again</Btn>
      </div>
    )
  }
  if (!detail) {
    return <p className="cc-kb-inside cc-muted">Loading…</p>
  }

  // `shared` decides what is OFFERED. The server decides what is allowed.
  const mine = !detail.shared

  const remove = async (doc: KbDocument) => {
    setRemoving(doc.id)
    setActionError(null)
    try {
      await api.deleteKbDocument(kbId, doc.id)
      load()
      onChanged()
    } catch (err) {
      setActionError(message(err))
    } finally {
      setRemoving(null)
    }
  }

  return (
    <div className={mine ? 'cc-kb-inside cc-kb-owner' : 'cc-kb-inside'}>
      {mine && <AddDocument kbId={kbId} onAdded={() => { load(); onChanged() }} />}

      <section className="cc-sec">
        <Label as="h3">Documents · {pad(detail.documents.length)}</Label>
        {actionError && <p className="cc-kb-err" role="alert">{actionError}</p>}
        {detail.documents.length === 0 ? (
          <p className="cc-muted">Nothing in this collection yet.</p>
        ) : (
          <ul className="cc-kb-rows">
            {detail.documents.map((doc) => (
              <li key={doc.id} className="cc-kb-row">
                <Icon name="doc" size={16} style={{ color: 'var(--accent)', marginTop: 2 }} />
                <div className="cc-grow">
                  <span className="cc-kb-name">{doc.title}</span>
                  <span className="cc-kb-sub">
                    <Tag tone="ok">{chunkLabel(doc.chunk_count)}</Tag>
                    {doc.char_count != null && (
                      <span className="cc-muted cc-num">{doc.char_count.toLocaleString()} chars</span>
                    )}
                  </span>
                </div>
                {mine && (
                  <Btn
                    variant="danger"
                    size="sm"
                    onClick={() => void remove(doc)}
                    disabled={removing !== null}
                    // Named per document: a list of identical "remove" buttons is a list of
                    // guesses to a screen reader.
                    aria-label={`Remove ${doc.title}`}
                    aria-description="Remove from this collection, including its embeddings"
                  >
                    {removing === doc.id ? 'removing…' : 'remove'}
                  </Btn>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      {mine && <Sharing kbId={kbId} detail={detail} onChanged={load} />}
      {!mine && (
        <p className="cc-muted">
          Shared with you — searchable from a conversation that binds it. Only its owner
          can add or remove documents.
        </p>
      )}
    </div>
  )
}

// ─── adding a document ────────────────────────────────────────────────────────────────────────────────────

// Every format the extractor knows (matrix_studio/documents.py SUPPORTED_SUFFIXES). Offered until the server
// says which of them THIS install can read: PDF and Word are optional extras there.
const KNOWN_SUFFIXES = ['.docx', '.markdown', '.md', '.pdf', '.text', '.txt']
// Extensions AND media types in `accept`: a phone's picker filters by type, and not every one maps an
// extension to a type, which would grey out files the server can read.
const MEDIA_TYPE: Record<string, string> = {
  '.txt': 'text/plain',
  '.text': 'text/plain',
  '.md': 'text/markdown',
  '.markdown': 'text/markdown',
  '.pdf': 'application/pdf',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}
const FORMAT_NAME: [string, string][] = [
  ['.pdf', 'PDF'], ['.docx', 'Word'], ['.md', 'Markdown'], ['.markdown', 'Markdown'], ['.txt', 'text'], ['.text', 'text'],
]

/** "PDF, Word, Markdown or text": the suffixes as a person would name them, in a fixed order. */
function formatNames(suffixes: string[]) {
  const names = [...new Set(FORMAT_NAME.filter(([s]) => suffixes.includes(s)).map(([, n]) => n))]
  return names.length < 2 ? names.join('') : `${names.slice(0, -1).join(', ')} or ${names[names.length - 1]}`
}

interface Formats {
  suffixes: string[]
  maxBytes: number
  /** Whether the list is the server's answer, rather than the fallback. */
  known: boolean
}

/** Which file types this install reads, from `/api/documents/formats`; the full list until it answers. */
function useDocumentFormats(): Formats {
  const [formats, setFormats] = useState<Formats>({ suffixes: KNOWN_SUFFIXES, maxBytes: 0, known: false })
  useEffect(() => {
    let alive = true
    // Async so that a missing or throwing method REJECTS rather than throwing inside the effect.
    const load = async () => api.getDocumentFormats()
    load()
      .then((body) => {
        const usable = (body?.formats ?? []).filter((f) => f.available).map((f) => f.suffix)
        if (alive && usable.length) {
          setFormats({ suffixes: usable, maxBytes: body.max_upload_bytes ?? 0, known: true })
        }
      })
      // Swallowed on purpose: the list only narrows the picker. Failing to fetch it must not stop
      // an upload, and the server's own answer arrives with the extraction anyway.
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])
  return formats
}

type Step = 'idle' | 'reading' | 'embedding'

/** Drop, tap or paste; review the text; then store it. */
function AddDocument({ kbId, onAdded }: { kbId: string; onAdded: () => void }) {
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const [step, setStep] = useState<Step>('idle')
  const [reading, setReading] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<{ text: string; warn?: boolean } | null>(null)
  const [over, setOver] = useState(false)
  // The title the last extraction filled in. A second file replaces it; a title the operator
  // typed is theirs and stays. Without this, dropping the wrong file and then the right one
  // kept the wrong file's name.
  const autoTitle = useRef('')
  const formats = useDocumentFormats()
  const busy = step !== 'idle'

  const accept = [
    ...new Set([...formats.suffixes, ...formats.suffixes.map((s) => MEDIA_TYPE[s]).filter(Boolean)]),
  ].join(',')
  const limitMb = formats.maxBytes ? Math.round(formats.maxBytes / (1024 * 1024)) : 0

  // Refused here only on the SERVER's word (`formats.known`). It saves sending a file the server
  // would refuse — over a phone connection the whole body goes up before the 422 comes back —
  // but the fallback list is this file's guess, and a guess must not refuse a format the
  // server has since learned to read.
  const refusal = (file: File): string | null => {
    if (!formats.known) return null
    const dot = file.name.lastIndexOf('.')
    const suffix = dot >= 0 ? file.name.slice(dot).toLowerCase() : ''
    if (!formats.suffixes.includes(suffix)) {
      return `${file.name} is not a format this can read. It reads ${formatNames(formats.suffixes)} files.`
    }
    if (formats.maxBytes && file.size > formats.maxBytes) {
      return `${file.name} is larger than the ${limitMb} MB limit for a single document.`
    }
    return null
  }

  const pick = async (file: File, others = 0) => {
    const refused = refusal(file)
    if (refused) {
      setError(refused)
      setNote(null)
      return
    }
    setStep('reading')
    setReading(file.name)
    setError(null)
    setNote(null)
    try {
      const extracted = await api.extractDocument(file, file.name)
      const next = extracted.title || file.name
      const previous = autoTitle.current
      autoTitle.current = next
      setTitle((current) => (!current || current === previous ? next : current))
      setText(extracted.text)
      // One at a time, because each file's text is reviewed before it is stored.
      const rest = others
        ? ` Only ${file.name} was read; add the other ${others} one at a time so each can be checked.`
        : ''
      // Shown rather than stored. A scanned PDF extracts to nothing, and storing that
      // silently puts an empty document in the collection whose only symptom is
      // retrieval never finding it.
      setNote(
        extracted.text.trim()
          ? { text: `Extracted ${extracted.text.length} characters — check it, then add.${rest}` }
          : {
              text: `Extraction produced NO text. A scanned PDF has no text layer; this would be an empty document.${rest}`,
              warn: true,
            },
      )
    } catch (err) {
      setError(message(err))
    } finally {
      setStep('idle')
    }
  }

  const add = async () => {
    setStep('embedding')
    setError(null)
    setNote(null)
    try {
      const result = await api.addKbDocument(kbId, title.trim() || 'untitled', text)
      setTitle('')
      setText('')
      autoTitle.current = ''
      setNote({ text: `Added and embedded ${chunkLabel(result.embedded)}.` })
      onAdded()
    } catch (err) {
      setError(message(err))
      // Refreshed on failure too. The server's 502 means the document WAS stored and could not
      // be embedded; listing it is how the owner finds it to remove it. The text stays in the
      // box, since it may be the only copy they have.
      onAdded()
    } finally {
      setStep('idle')
    }
  }

  // Drag-and-drop is an enhancement over the label below, which a tap or a keyboard opens.
  const dragging = (e: DragEvent<HTMLLabelElement>) => {
    e.preventDefault()
    if (!busy) setOver(true)
  }
  const left = (e: DragEvent<HTMLLabelElement>) => {
    // Leaving the icon for the text inside the target is not leaving the target.
    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOver(false)
  }
  const dropped = (e: DragEvent<HTMLLabelElement>) => {
    e.preventDefault()
    setOver(false)
    const files = Array.from(e.dataTransfer?.files ?? [])
    if (!busy && files.length) void pick(files[0], files.length - 1)
  }

  const status =
    step === 'reading' ? (
      <><Tag tone="live" pulse>reading</Tag> {reading}</>
    ) : step === 'embedding' ? (
      <><Tag tone="live" pulse>embedding</Tag> stored and indexed in one step; it is searchable once this finishes.</>
    ) : (
      note?.text
    )

  return (
    <section className="cc-sec cc-kb-add">
      <Label as="h3">Add a document</Label>
      <label
        className={['cc-drop', over && 'cc-kb-over', busy && 'cc-kb-busy'].filter(Boolean).join(' ')}
        onDragEnter={dragging}
        onDragOver={dragging}
        onDragLeave={left}
        onDrop={dropped}
      >
        <Icon name="upload" size={28} style={{ color: 'var(--accent)' }} />
        <span className="cc-kb-droptext">
          {step === 'reading' ? 'Reading…' : over ? 'Release to read it' : 'Choose file'}
        </span>
        <span className="cc-muted">
          or drop one here · {formatNames(formats.suffixes)}
          {limitMb > 0 && `, up to ${limitMb} MB`}
        </span>
        {/* A real file input, visually hidden rather than replaced by a button and click(): it stays
            keyboard-reachable, the label's text is its name, and on a phone it is how the Files app
            and the share sheet reach this. One file: each one's text is reviewed before it is stored. */}
        <input
          type="file"
          className="sr-only"
          accept={accept}
          disabled={busy}
          onChange={(e) => {
            const file = e.target.files?.[0]
            if (file) void pick(file)
            // Cleared so choosing the SAME file again fires change again.
            e.target.value = ''
          }}
        />
      </label>

      <input
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Title"
        aria-label="Document title"
        className="cc-field"
      />
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={5}
        placeholder="Paste text, or choose a file to extract it from…"
        aria-label="Document text"
        className="cc-field"
      />
      {/* Always mounted: a live region that appears with its content is often not announced. */}
      <p className={note?.warn && !busy ? 'cc-kb-note cc-kb-warn' : 'cc-kb-note'} role="status">
        {status}
      </p>
      {error && <p className="cc-kb-err" role="alert">{error}</p>}
      <Btn variant="primary" full onClick={() => void add()} disabled={busy || !text.trim()}>
        {step === 'embedding' ? 'Embedding…' : 'Add and embed'}
      </Btn>
    </section>
  )
}

// ─── sharing ──────────────────────────────────────────────────────────────────────────────────────────────

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
      setError(null)
      onChanged()
    } catch (err) {
      setError(message(err))
    }
  }

  const grants = detail.grants ?? []

  return (
    <section className="cc-sec">
      <Label as="h3">Shared with</Label>
      {grants.length === 0 ? (
        <p className="cc-muted">Nobody — this collection is private.</p>
      ) : (
        <ul className="cc-kb-rows">
          {grants.map((grant) => (
            <li key={`${grant.kind}:${grant.principal}`} className="cc-kb-row">
              {/* The kind in words: a user and a group are different namespaces, and a colour
                  alone would not say which one a revoke is about to act on. */}
              <Tag tone={grant.kind === 'group' ? 'ens' : undefined}>{grant.kind}</Tag>
              <span className="cc-grow cc-kb-name">{grant.principal}</span>
              <Btn
                variant="danger"
                size="sm"
                onClick={async () => {
                  try {
                    await api.revokeKb(
                      kbId,
                      grant.kind === 'group'
                        ? { group: grant.principal }
                        : { user: grant.principal },
                    )
                    setError(null)
                    onChanged()
                  } catch (err) {
                    setError(message(err))
                  }
                }}
                aria-label={`Revoke ${grant.kind} ${grant.principal}`}
                aria-description="Takes effect on the next turn of any conversation using it"
              >
                revoke
              </Btn>
            </li>
          ))}
        </ul>
      )}
      <div className="cc-kb-share">
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as 'user' | 'group')}
          aria-label="Share with a user or a group"
          className="cc-field"
        >
          <option value="user">User</option>
          <option value="group">Group</option>
        </select>
        <input
          value={principal}
          onChange={(e) => setPrincipal(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void share()}
          placeholder={kind === 'user' ? 'Cognito sub…' : 'Cognito group…'}
          aria-label={kind === 'user' ? 'User to share with' : 'Group to share with'}
          className="cc-field"
        />
        <Btn onClick={() => void share()} disabled={!principal.trim()}>
          Share
        </Btn>
      </div>
      {error && <p className="cc-kb-err" role="alert">{error}</p>}
    </section>
  )
}
