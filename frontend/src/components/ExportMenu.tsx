// SPDX-License-Identifier: Apache-2.0
import { useState } from 'react'
import { api, type ExportFormat } from '../api'

interface Props {
  kind: 'run' | 'ensemble'
  id: string
  /** Used for the downloaded file's name. The server also names it; this is the fallback. */
  name: string
}

// Export this conversation or ensemble as Markdown, HTML or PDF.
//
// The rules an export must keep — no pooled claim totals, analysis labelled in words, operator-private
// persona fields omitted, model text escaped — are enforced server-side in `matrix_studio/export.py`,
// so every format carries them and this component only moves bytes.
//
// PDF is the HTML export printed by the browser: it carries its own print stylesheet, and there is no
// headless browser in the backend. The print window is opened SYNCHRONOUSLY inside the click, before
// the fetch, because a window opened after an `await` is treated as a popup and blocked.
export function ExportMenu({ kind, id, name }: Props) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const slug = (name || kind).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')

  const download = async (format: ExportFormat) => {
    setBusy(format)
    setError(null)
    try {
      const text = await api.exportText(kind, id, format)
      const blob = new Blob([text], {
        type: format === 'md' ? 'text/markdown;charset=utf-8' : 'text/html;charset=utf-8',
      })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${slug}-${kind}.${format}`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const printPdf = async () => {
    const win = window.open('', '_blank')
    if (!win) {
      setError('The print window was blocked. Allow popups for this site, or download HTML and print it.')
      return
    }
    setBusy('pdf')
    setError(null)
    try {
      const html = await api.exportText(kind, id, 'html')
      // The export is fully escaped server-side and carries no script, so writing it into a window
      // of our own origin cannot run anything that was in the transcript.
      win.document.open()
      win.document.write(html)
      win.document.close()
      win.focus()
      win.print()
    } catch (e) {
      win.close()
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const btn =
    'rounded border border-matrix-border px-2 py-1 text-xs text-slate-300 hover:border-matrix-accent disabled:opacity-50'

  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-xs text-slate-500">Export</span>
      <button className={btn} disabled={busy !== null} onClick={() => void download('md')}>
        {busy === 'md' ? 'Exporting…' : 'Markdown'}
      </button>
      <button className={btn} disabled={busy !== null} onClick={() => void download('html')}>
        {busy === 'html' ? 'Exporting…' : 'HTML'}
      </button>
      <button
        className={btn}
        disabled={busy !== null}
        onClick={() => void printPdf()}
        title="Opens the HTML export and your browser's print dialog — choose “Save as PDF”."
      >
        {busy === 'pdf' ? 'Preparing…' : 'PDF'}
      </button>
      {error && <span className="text-xs text-rose-300">Export failed: {error}</span>}
    </div>
  )
}
