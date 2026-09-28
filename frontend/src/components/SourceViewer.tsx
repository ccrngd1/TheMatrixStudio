// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef, useState } from 'react'
import { api, type RunSource } from '../api'

interface Props {
  runId: string
  documentId: string
  /** The chunk the turn retrieved. Highlighted and scrolled to, so a reader lands on it. */
  ordinal?: number
  onClose: () => void
}

// The source behind a passage a persona retrieved — so a human can read what the persona read and
// judge whether it was used fairly. A `document.retrieved` event records which passage was used and
// not its text; this is where the text comes from.
//
// Two shapes, and the difference is said out loud rather than papered over:
//
//   owned   — the whole source, every chunk, with the cited one highlighted.
//   shared  — only the cited passage and its neighbours. A shared collection's full text belongs to
//             its owner, and PHASE6-KB-DESIGN.md §8.2 keeps it that way: a grantee retrieves
//             passages, they do not download the source. The server enforces it; this says why.
//
// Research made this necessary rather than merely convenient: a pass ingests a hundred sources
// nobody chose, and "a searcher found this commentary" and "this is the statute" have to be
// distinguishable at a glance — hence the origin and tier badges in the header.
export function SourceViewer({ runId, documentId, ordinal, onClose }: Props) {
  const [source, setSource] = useState<RunSource | null>(null)
  const [error, setError] = useState<string | null>(null)
  const cited = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    let live = true
    api
      .getRunSource(runId, documentId, ordinal)
      .then((s) => live && setSource(s))
      // Not swallowed into an empty modal: "this source is empty" and "you cannot open it" are
      // different, and the second is the one a reader needs to know about.
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      live = false
    }
  }, [runId, documentId, ordinal])

  useEffect(() => {
    // `scrollIntoView` is absent in jsdom, and the viewer must not crash where it is missing.
    cited.current?.scrollIntoView?.({ block: 'center' })
  }, [source])

  return (
    // stopPropagation on the backdrop too: the viewer opens INSIDE the dossier's own backdrop, and
    // a click that closes the viewer would otherwise bubble up and close the dossier with it.
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60"
      onClick={(e) => {
        e.stopPropagation()
        onClose()
      }}
    >
      <div
        role="dialog"
        aria-label="Source"
        className="flex max-h-[85vh] w-full max-w-3xl flex-col rounded-lg border border-matrix-border bg-matrix-panel"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start gap-3 border-b border-matrix-border p-4">
          <div className="flex-1">
            <h2 className="text-sm font-semibold text-slate-200">
              {source?.title ?? 'Loading source…'}
            </h2>
            {source && (
              <div className="mt-1 flex flex-wrap items-center gap-2 text-xs">
                {source.origin === 'researched' ? (
                  <span className="rounded bg-slate-700 px-1.5 py-0.5 text-slate-300">
                    found by research
                  </span>
                ) : (
                  <span className="rounded bg-sky-900/60 px-1.5 py-0.5 text-sky-300">
                    provided by a person
                  </span>
                )}
                {source.authority && source.authority !== 'unknown' && (
                  <span
                    className={`rounded px-1.5 py-0.5 ${
                      source.authority === 'controlling'
                        ? 'bg-emerald-900/60 text-emerald-300'
                        : 'bg-slate-800 text-slate-400'
                    }`}
                    title="How research tiered it: a statute, regulation, board ruling or decided case is controlling."
                  >
                    {source.authority}
                  </span>
                )}
                {source.kb_name && (
                  <span className="text-slate-500">in {source.kb_name}</span>
                )}
                {source.source_url && (
                  // The original page, so the copy can be checked rather than trusted.
                  <a
                    href={source.source_url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="text-matrix-accent hover:underline"
                  >
                    open the original ↗
                  </a>
                )}
              </div>
            )}
          </div>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-300" aria-label="Close source">
            ✕
          </button>
        </div>

        <div className="overflow-y-auto p-4 text-sm leading-relaxed text-slate-300">
          {error && <p className="text-rose-300">Could not open this source: {error}</p>}
          {source && !source.full && source.notice && (
            <p className="mb-3 rounded border border-amber-900/60 bg-amber-950/30 p-2 text-xs text-amber-300">
              {source.notice}
            </p>
          )}
          {source && source.chunks.length === 0 && !error && (
            <p className="text-slate-500">This source has no readable text.</p>
          )}
          {source?.chunks.map((c) => {
            const isCited = c.ordinal === source.cited_ordinal
            return (
              <div
                key={c.ordinal}
                ref={isCited ? cited : undefined}
                data-cited={isCited || undefined}
                className={`mb-3 whitespace-pre-wrap rounded p-2 ${
                  isCited ? 'border border-matrix-accent bg-sky-950/40' : ''
                }`}
              >
                {isCited && (
                  <div className="mb-1 text-[11px] uppercase tracking-wide text-matrix-accent">
                    the passage this turn retrieved
                  </div>
                )}
                {/* The cited passage in full — exactly what the persona read, overlap included. Every
                    other chunk without the overlap it repeats from the one before, so the document
                    reads once instead of stuttering at each boundary. */}
                {isCited ? c.text : (c.display ?? c.text)}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
