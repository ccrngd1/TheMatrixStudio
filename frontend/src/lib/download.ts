// SPDX-License-Identifier: Apache-2.0
// Save text as a file in the browser. Shared by the export menu and the decision brief, so the two
// cannot drift in how they name or encode a download.

export function slugify(name: string, fallback = 'download'): string {
  return (name || fallback).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || fallback
}

export function downloadText(text: string, filename: string, format: 'md' | 'html'): void {
  const blob = new Blob([text], {
    type: format === 'md' ? 'text/markdown;charset=utf-8' : 'text/html;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
