// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from 'react'
import { api } from '../api'
import { downloadText, slugify } from '../lib/download'

interface Props {
  kind: 'run' | 'ensemble'
  id: string
  name: string
}

// The one-page decision brief: a button beside Export that opens it in a modal, with downloads.
//
// **The preview IS the export.** The modal shows the server-rendered HTML — the same bytes a download
// saves — so there is no second renderer to disagree with the file. The rules the brief keeps live in
// `matrix_studio/brief.py`: no number without its group and replicate count ("not yet measured"
// otherwise), per-group counts never pooled, one page with every cut stated.
//
// Shown in a SANDBOXED frame (`sandbox=""`: no scripts, no same-origin access). The brief is model
// text, already escaped server-side; the sandbox means even an escaping bug could not run anything in
// the app's origin, where the user's token lives.
export function BriefButton({ kind, id, name }: Props) {
  const [open, setOpen] = useState(false)
  const [html, setHtml] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let live = true
    setHtml(null)
    setError(null)
    api
      .briefText(kind, id, 'html')
      .then((h) => live && setHtml(h))
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      live = false
    }
  }, [open, kind, id])

  const save = async (format: 'md' | 'html') => {
    setBusy(format)
    try {
      // HTML is saved from what is already on screen, so the file is exactly what was read.
      const text = format === 'html' && html ? html : await api.briefText(kind, id, format)
      downloadText(text, `${slugify(name, kind)}-${kind}-brief.${format}`, format)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const btn =
    'rounded border border-matrix-border px-2 py-1 text-xs text-slate-300 hover:border-matrix-accent disabled:opacity-50'

  return (
    <>
      <button
        className="rounded border border-matrix-accent px-2 py-1 text-xs text-matrix-accent hover:bg-sky-950/40"
        onClick={() => setOpen(true)}
        aria-description="A one-page decision brief: what was concluded, how consistently, and what is still open."
      >
        Brief
      </button>
      {open && (
        <div
          className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60"
          onClick={() => setOpen(false)}
        >
          <div
            role="dialog"
            aria-label="Decision brief"
            className="flex h-[88vh] w-full max-w-4xl flex-col rounded-lg border border-matrix-border bg-matrix-panel"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2 border-b border-matrix-border p-3">
              <h2 className="flex-1 text-sm font-semibold text-slate-200">Decision brief</h2>
              <button className={btn} disabled={busy !== null || !html} onClick={() => void save('html')}>
                {busy === 'html' ? 'Saving…' : 'Download HTML'}
              </button>
              <button className={btn} disabled={busy !== null} onClick={() => void save('md')}>
                {busy === 'md' ? 'Saving…' : 'Download Markdown'}
              </button>
              <button
                onClick={() => setOpen(false)}
                className="px-1 text-slate-500 hover:text-slate-300"
                aria-label="Close brief"
              >
                ✕
              </button>
            </div>
            <div className="flex-1 overflow-hidden bg-white">
              {error && <p className="p-4 text-sm text-rose-600">Could not load the brief: {error}</p>}
              {!error && !html && <p className="p-4 text-sm text-slate-500">Preparing the brief…</p>}
              {html && (
                <iframe
                  title="Decision brief"
                  sandbox=""
                  srcDoc={html}
                  className="h-full w-full border-0"
                />
              )}
            </div>
          </div>
        </div>
      )}
    </>
  )
}
