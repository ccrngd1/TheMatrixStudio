// SPDX-License-Identifier: Apache-2.0
//
// The viewer's job is to let a human read what a persona was actually given. Two shapes, and the
// tests are mostly that the difference is SAID rather than hidden: a source the reader owns comes
// back whole, and one shared with them comes back as the cited passage and its neighbours, with a
// notice — because a grantee retrieves passages and does not download the source (§8.2).
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { SourceViewer } from './SourceViewer'
import { api } from '../api'

vi.mock('../api', () => ({ api: { getRunSource: vi.fn() } }))
const mocked = api as unknown as { getRunSource: ReturnType<typeof vi.fn> }

const base = {
  document_id: 'd1', title: 'Iowa Admin Code Ch. 811', scope: 'knowledge_base', kb_id: 'kb1',
  kb_name: 'statutes', owned: true, full: true, origin: 'researched', authority: 'controlling',
  source_url: 'https://www.legis.iowa.gov/docs/iac/chapter/811.10.pdf', cited_ordinal: 1,
  chunk_count: 3,
  chunks: [
    { ordinal: 0, text: 'Definitions.' },
    { ordinal: 1, text: 'The board shall require an examination.' },
    { ordinal: 2, text: 'Exceptions.' },
  ],
}

describe('SourceViewer', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows the whole source and marks the passage the turn retrieved', async () => {
    mocked.getRunSource.mockResolvedValue(base)
    render(<SourceViewer runId="r1" documentId="d1" ordinal={1} onClose={vi.fn()} />)

    expect(await screen.findByText('Iowa Admin Code Ch. 811')).toBeInTheDocument()
    expect(screen.getByText('Definitions.')).toBeInTheDocument()
    expect(screen.getByText('Exceptions.')).toBeInTheDocument()
    const cited = screen.getByText('The board shall require an examination.')
    expect(cited.closest('[data-cited]')).not.toBeNull()
    expect(screen.getByText(/the passage this turn retrieved/)).toBeInTheDocument()
    expect(mocked.getRunSource).toHaveBeenCalledWith('r1', 'd1', 1)
  })

  it('says a searcher found it, what tier it is, and links the original', async () => {
    // "A searcher found this commentary" and "this is the statute" must be distinguishable at a
    // glance — research ingests a hundred sources nobody chose.
    mocked.getRunSource.mockResolvedValue(base)
    render(<SourceViewer runId="r1" documentId="d1" ordinal={1} onClose={vi.fn()} />)

    expect(await screen.findByText('found by research')).toBeInTheDocument()
    expect(screen.getByText('controlling')).toBeInTheDocument()
    const link = screen.getByText(/open the original/)
    expect(link.getAttribute('href')).toBe(base.source_url)
    expect(link.getAttribute('rel')).toContain('noopener')
  })

  it('says a person provided an upload, and offers no link for a filename', async () => {
    mocked.getRunSource.mockResolvedValue({
      ...base, origin: null, authority: null, source_url: null,
    })
    render(<SourceViewer runId="r1" documentId="d1" onClose={vi.fn()} />)
    expect(await screen.findByText('provided by a person')).toBeInTheDocument()
    expect(screen.queryByText(/open the original/)).not.toBeInTheDocument()
  })

  it('says plainly when only passages are shown, and why', async () => {
    mocked.getRunSource.mockResolvedValue({
      ...base, owned: false, full: false,
      notice: 'This collection is shared with you… it belongs to another account.',
    })
    render(<SourceViewer runId="r1" documentId="d1" ordinal={1} onClose={vi.fn()} />)
    expect(await screen.findByText(/belongs to another account/)).toBeInTheDocument()
  })

  it('reports a refusal rather than showing an empty source', async () => {
    // "This source is empty" and "you cannot open it" are different, and only one is true here.
    mocked.getRunSource.mockRejectedValue(new Error('404: Source not found for this run'))
    render(<SourceViewer runId="r1" documentId="d1" onClose={vi.fn()} />)
    expect(await screen.findByText(/Could not open this source: 404/)).toBeInTheDocument()
    expect(screen.queryByText(/no readable text/)).not.toBeInTheDocument()
  })

  it('closing it does not also close whatever it was opened from', async () => {
    mocked.getRunSource.mockResolvedValue(base)
    const onClose = vi.fn()
    const parent = vi.fn()
    render(
      <div onClick={parent}>
        <SourceViewer runId="r1" documentId="d1" onClose={onClose} />
      </div>,
    )
    await screen.findByText('Iowa Admin Code Ch. 811')
    fireEvent.click(screen.getByRole('dialog').parentElement!)
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(parent).not.toHaveBeenCalled()
  })
})


describe('SourceViewer overlap', () => {
  it('shows each chunk once, and the cited passage in full', async () => {
    const { api } = await import('../api')
    ;(api.getRunSource as any).mockResolvedValue({
      ...base,
      chunks: [
        { ordinal: 0, text: 'First part.', display: 'First part.' },
        { ordinal: 1, text: 'First part.\n\nThe board shall require an examination.', display: 'The board shall require an examination.' },
        { ordinal: 2, text: 'an examination.\n\nExceptions.', display: 'Exceptions.' },
      ],
    })
    render(<SourceViewer runId="r1" documentId="d1" ordinal={1} onClose={vi.fn()} />)
    const cited = await screen.findByText(/The board shall require an examination\./)
    expect(cited.closest('[data-cited]')).toHaveTextContent('First part.')
    expect(screen.getByText('Exceptions.')).toBeInTheDocument()
    expect(screen.queryByText(/an examination\.\s+Exceptions/)).not.toBeInTheDocument()
  })
})
