// SPDX-License-Identifier: Apache-2.0
// The knowledge-base view.
//
// What is worth testing here is NOT that buttons render. It is the one distinction the
// UI has to get right: a collection shared WITH you is searchable and not editable, so
// the controls that would 404 must not be offered.
//
// The server is the boundary — `_owned_kb` returns 404 for a grantee's write — so a bug
// here is not a security hole. It is worse in a different way: a delete button that
// always 404s reads as a broken app, and a user who cannot tell "not allowed" from
// "broken" stops trusting the whole thing.

import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { KnowledgeBases } from './KnowledgeBases'
import type { KnowledgeBase, KnowledgeBaseDetail } from '../types'

vi.mock('../api', () => ({
  api: {
    listKnowledgeBases: vi.fn(),
    getKnowledgeBase: vi.fn(),
    createKnowledgeBase: vi.fn(),
    addKbDocument: vi.fn(),
    deleteKbDocument: vi.fn(),
    grantKb: vi.fn(),
    revokeKb: vi.fn(),
    extractDocument: vi.fn(),
    // Called by the drop target to narrow its picker. Left returning nothing by default, which
    // is the "server did not answer" case: the picker offers every known format.
    getDocumentFormats: vi.fn(),
  },
}))
import { api } from '../api'

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>

function kb(over: Partial<KnowledgeBase> = {}): KnowledgeBase {
  return {
    id: 'kb-1',
    name: 'egress-policies',
    description: null,
    owner_sub: 'me',
    embedding_model: null,
    created_at: 1_700_000_000,
    shared: false,
    document_count: 0,
    ...over,
  }
}

function detail(over: Partial<KnowledgeBaseDetail> = {}): KnowledgeBaseDetail {
  return {
    ...kb(),
    documents: [],
    grants: [],
    ...over,
  }
}

beforeEach(() => vi.clearAllMocks())

async function open(rows: KnowledgeBase[], d: KnowledgeBaseDetail) {
  mocked.listKnowledgeBases.mockResolvedValue({ knowledge_bases: rows, count: rows.length })
  mocked.getKnowledgeBase.mockResolvedValue(d)
  render(<KnowledgeBases onBack={() => {}} />)
  await waitFor(() => expect(screen.getByText(rows[0].name)).toBeTruthy())
  fireEvent.click(screen.getByText(rows[0].name))
  await waitFor(() => expect(mocked.getKnowledgeBase).toHaveBeenCalled())
}

// --------------------------------------------------------------------------- //
// A shared collection offers no writes
// --------------------------------------------------------------------------- //

describe('a collection shared with you', () => {
  const shared = kb({ shared: true, owner_sub: 'someone-else', document_count: 1 })
  const sharedDetail = detail({
    ...shared,
    documents: [{ id: 'd1', title: 'policy.md', char_count: 100, chunk_count: 3, created_at: 1 }],
    // Null, as the server sends for a grantee: who else it is shared with names other
    // users' subs and is the owner's business.
    grants: null,
  })

  it('is marked as shared', async () => {
    await open([shared], sharedDetail)
    // Exact string, not a regex: the explanatory paragraph below the documents also
    // says "Shared with you — …", and a loose match found both and could not tell which
    // of the two had gone missing.
    expect(screen.getByText('shared with you')).toBeTruthy()
  })

  it('offers no remove button for its documents', async () => {
    await open([shared], sharedDetail)
    expect(screen.getByText('policy.md')).toBeTruthy()
    expect(screen.queryByText('remove')).toBeNull()
  })

  it('offers no way to add a document', async () => {
    await open([shared], sharedDetail)
    expect(screen.queryByText(/add a document/i)).toBeNull()
  })

  it('offers no sharing controls', async () => {
    // A grantee re-granting would spread read access without the owner's knowledge.
    await open([shared], sharedDetail)
    expect(screen.queryByText(/shared with$/i)).toBeNull()
    expect(screen.queryByRole('button', { name: /^share$/i })).toBeNull()
  })

  it('says why, rather than just omitting the controls', async () => {
    await open([shared], sharedDetail)
    expect(screen.getByText(/only its owner can add or remove/i)).toBeTruthy()
  })
})

// --------------------------------------------------------------------------- //
// Your own collection offers all of them
// --------------------------------------------------------------------------- //

describe('a collection you own', () => {
  const mine = kb({ shared: false, document_count: 1 })
  const mineDetail = detail({
    documents: [{ id: 'd1', title: 'policy.md', char_count: 100, chunk_count: 3, created_at: 1 }],
    grants: [{ kb_id: 'kb-1', principal: 'sub-them', kind: 'user', granted_by: 'me', created_at: 1 }],
  })

  it('is not marked as shared', async () => {
    await open([mine], mineDetail)
    expect(screen.queryByText(/shared with you/i)).toBeNull()
  })

  it('offers remove, add and sharing', async () => {
    await open([mine], mineDetail)
    expect(screen.getByText('remove')).toBeTruthy()
    expect(screen.getByText(/add a document/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /^share$/i })).toBeTruthy()
  })

  it('lists who it is shared with, and revokes', async () => {
    await open([mine], mineDetail)
    expect(screen.getByText('sub-them')).toBeTruthy()

    mocked.revokeKb.mockResolvedValue({ revoked: 'sub-them' })
    fireEvent.click(screen.getByText('revoke'))
    await waitFor(() =>
      expect(mocked.revokeKb).toHaveBeenCalledWith('kb-1', { user: 'sub-them' }),
    )
  })

  it('revokes a GROUP grant as a group, not as a user', async () => {
    // The two namespaces are distinct in the grant's key — a group could otherwise be
    // named to collide with a sub — so revoking the wrong kind silently does nothing
    // and the grant stays live.
    await open(
      [mine],
      detail({
        grants: [
          { kb_id: 'kb-1', principal: 'platform', kind: 'group', granted_by: 'me', created_at: 1 },
        ],
      }),
    )
    mocked.revokeKb.mockResolvedValue({ revoked: 'platform' })
    fireEvent.click(screen.getByText('revoke'))
    await waitFor(() =>
      expect(mocked.revokeKb).toHaveBeenCalledWith('kb-1', { group: 'platform' }),
    )
  })
})

// --------------------------------------------------------------------------- //
// Extraction is shown before it is stored
// --------------------------------------------------------------------------- //

describe('adding a document', () => {
  const mine = kb()

  it('warns when extraction produced no text at all', async () => {
    // A scanned PDF has no text layer. Storing that silently puts an empty document in
    // the collection whose only symptom is retrieval never finding it.
    await open([mine], detail())
    mocked.extractDocument.mockResolvedValue({ title: 'scan.pdf', text: '   ', chars: 3 })

    const input = screen.getByLabelText(/choose file/i, { selector: 'input' })
    fireEvent.change(input, {
      target: { files: [new File(['x'], 'scan.pdf', { type: 'application/pdf' })] },
    })

    await waitFor(() => expect(screen.getByText(/NO text/i)).toBeTruthy())
    expect(mocked.addKbDocument).not.toHaveBeenCalled()
  })

  it('does not store what it extracted until asked', async () => {
    await open([mine], detail())
    mocked.extractDocument.mockResolvedValue({
      title: 'policy.pdf', text: 'egress inspection rules', chars: 23,
    })

    const input = screen.getByLabelText(/choose file/i, { selector: 'input' })
    fireEvent.change(input, {
      target: { files: [new File(['x'], 'policy.pdf', { type: 'application/pdf' })] },
    })
    await waitFor(() => expect(screen.getByText(/check it, then add/i)).toBeTruthy())
    expect(mocked.addKbDocument).not.toHaveBeenCalled()

    mocked.addKbDocument.mockResolvedValue({
      document_id: 'd9', kb_id: 'kb-1', embedded: 2, cost_usd: 0, model: 'm',
    })
    fireEvent.click(screen.getByRole('button', { name: /add and embed/i }))
    await waitFor(() =>
      expect(mocked.addKbDocument).toHaveBeenCalledWith(
        'kb-1', 'policy.pdf', 'egress inspection rules',
      ),
    )
  })

  it('surfaces a failed upload rather than appearing to succeed', async () => {
    await open([mine], detail())
    mocked.addKbDocument.mockRejectedValue(
      new Error('502: stored as d9 but could not be embedded'),
    )

    fireEvent.change(screen.getByPlaceholderText(/paste text/i), {
      target: { value: 'some material' },
    })
    fireEvent.click(screen.getByRole('button', { name: /add and embed/i }))

    await waitFor(() => expect(screen.getByText(/could not be embedded/i)).toBeTruthy())
  })
})

// --------------------------------------------------------------------------- //
// The list
// --------------------------------------------------------------------------- //

describe('the list', () => {
  it('shows an error rather than an empty account when the request fails', async () => {
    mocked.listKnowledgeBases.mockRejectedValue(new Error('503: Service Unavailable'))
    render(<KnowledgeBases onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/503/)).toBeTruthy())
    expect(screen.queryByText(/no collections yet/i)).toBeNull()
  })

  it('distinguishes a genuinely empty account', async () => {
    mocked.listKnowledgeBases.mockResolvedValue({ knowledge_bases: [], count: 0 })
    render(<KnowledgeBases onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/no collections yet/i)).toBeTruthy())
  })

  it('refuses to create a collection with a blank name', async () => {
    mocked.listKnowledgeBases.mockResolvedValue({ knowledge_bases: [], count: 0 })
    render(<KnowledgeBases onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/no collections yet/i)).toBeTruthy())

    fireEvent.change(screen.getByPlaceholderText(/new collection name/i), {
      target: { value: '   ' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^create$/i }))
    expect(mocked.createKnowledgeBase).not.toHaveBeenCalled()
  })
})

// --------------------------------------------------------------------------- //
// The inside of a collection (docs/MOBILE-UI.md §4.10)
// --------------------------------------------------------------------------- //

const pdf = (name = 'policy.pdf') => new File(['x'], name, { type: 'application/pdf' })
const picker = () => screen.getByLabelText(/choose file/i, { selector: 'input' }) as HTMLInputElement
const dropTarget = () => picker().closest('label') as HTMLLabelElement

describe('the drop target', () => {
  const mine = kb()

  it('starts the extraction when a file is dropped on it, and stores nothing yet', async () => {
    await open([mine], detail())
    mocked.extractDocument.mockResolvedValue({ title: 'policy.pdf', text: 'egress rules', chars: 12 })

    const file = pdf()
    fireEvent.drop(dropTarget(), { dataTransfer: { files: [file] } })

    await waitFor(() => expect(mocked.extractDocument).toHaveBeenCalledWith(file, 'policy.pdf'))
    // The same review the picker leads to: the text is shown, and storing waits to be asked.
    await waitFor(() => expect(screen.getByDisplayValue('egress rules')).toBeTruthy())
    expect(mocked.addKbDocument).not.toHaveBeenCalled()
  })

  it('says, in words, that a held file will be read on release', async () => {
    // The highlight alone would be colour alone.
    await open([mine], detail())
    // Held once: while a file is over it the target's words, and so its label, change.
    const target = dropTarget()
    fireEvent.dragEnter(target)
    expect(screen.getByText(/release to read it/i)).toBeTruthy()
    fireEvent.dragLeave(target)
    expect(screen.queryByText(/release to read it/i)).toBeNull()
  })

  it('reads one of several dropped files, and says what happened to the rest', async () => {
    // One at a time because each one's text is reviewed before it is stored.
    await open([mine], detail())
    mocked.extractDocument.mockResolvedValue({ title: 'a.pdf', text: 'first', chars: 5 })
    fireEvent.drop(dropTarget(), { dataTransfer: { files: [pdf('a.pdf'), pdf('b.pdf'), pdf('c.pdf')] } })

    await waitFor(() => expect(screen.getByText(/other 2 one at a time/i)).toBeTruthy())
    expect(mocked.extractDocument).toHaveBeenCalledTimes(1)
  })

  it('is a real, labelled, keyboard-reachable file input that a tap on the target opens', async () => {
    await open([mine], detail())
    const input = picker()
    expect(input.type).toBe('file')
    // Visually hidden, not `display: none`: a hidden input is out of the tab order.
    expect(input).not.toHaveClass('hidden')
    expect(input.disabled).toBe(false)
    // Every format the server extracts, so a phone's picker does not grey them out.
    for (const suffix of ['.txt', '.md', '.pdf', '.docx']) expect(input.accept).toContain(suffix)

    const clicked = vi.fn()
    input.addEventListener('click', clicked)
    fireEvent.click(dropTarget())
    expect(clicked).toHaveBeenCalled()
  })

  it('offers only what this server can read, and refuses the rest before uploading it', async () => {
    // Once: `clearAllMocks` does not reset implementations, and a persistent "no PDFs here"
    // would quietly refuse every PDF in the tests after this one.
    mocked.getDocumentFormats.mockResolvedValueOnce({
      formats: [
        { suffix: '.md', media_type: 'md', available: true, needs: null },
        { suffix: '.pdf', media_type: 'pdf', available: false, needs: 'pypdf' },
        { suffix: '.txt', media_type: 'txt', available: true, needs: null },
      ],
      max_upload_bytes: 10 * 1024 * 1024,
      max_document_chars: 400000,
    })
    await open([mine], detail())
    await waitFor(() => expect(picker().accept).not.toContain('.pdf'))
    expect(screen.getByText(/markdown or text, up to 10 MB/i)).toBeTruthy()

    // A drop bypasses `accept`, so the drop is checked too.
    fireEvent.drop(dropTarget(), { dataTransfer: { files: [pdf('scan.pdf')] } })
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/not a format this can read/i))
    expect(mocked.extractDocument).not.toHaveBeenCalled()
  })

  it('replaces a title the last file filled in, but not one the operator typed', async () => {
    await open([mine], detail())
    mocked.extractDocument.mockResolvedValueOnce({ title: 'wrong.pdf', text: 'one', chars: 3 })
    fireEvent.drop(dropTarget(), { dataTransfer: { files: [pdf('wrong.pdf')] } })
    await waitFor(() => expect(screen.getByDisplayValue('wrong.pdf')).toBeTruthy())

    mocked.extractDocument.mockResolvedValueOnce({ title: 'right.pdf', text: 'two', chars: 3 })
    fireEvent.drop(dropTarget(), { dataTransfer: { files: [pdf('right.pdf')] } })
    await waitFor(() => expect(screen.getByDisplayValue('right.pdf')).toBeTruthy())

    fireEvent.change(screen.getByLabelText(/document title/i), { target: { value: 'Egress policy' } })
    mocked.extractDocument.mockResolvedValueOnce({ title: 'third.pdf', text: 'three', chars: 5 })
    fireEvent.drop(dropTarget(), { dataTransfer: { files: [pdf('third.pdf')] } })
    await waitFor(() => expect(screen.getByDisplayValue('three')).toBeTruthy())
    expect(screen.getByDisplayValue('Egress policy')).toBeTruthy()
  })

  it('says it is embedding while the add is in flight, without inventing a count', async () => {
    // The add route embeds inline and reports nothing until it is done, so there is no n/m.
    await open([mine], detail())
    let finish: (v: unknown) => void = () => {}
    mocked.addKbDocument.mockReturnValue(new Promise((resolve) => { finish = resolve }))
    fireEvent.change(screen.getByPlaceholderText(/paste text/i), { target: { value: 'some material' } })
    fireEvent.click(screen.getByRole('button', { name: /add and embed/i }))

    await waitFor(() => expect(screen.getByRole('button', { name: /embedding/i })).toBeDisabled())
    expect(screen.getByRole('status').textContent).toMatch(/embedding/i)
    expect(screen.queryByRole('meter')).toBeNull()

    finish({ document_id: 'd9', kb_id: 'kb-1', embedded: 2, cost_usd: 0, model: 'm' })
    // The note outlives the refresh of the list that adding triggers.
    await waitFor(() => expect(mocked.listKnowledgeBases).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByText(/added and embedded 2 chunks/i)).toBeTruthy())
  })

  it('refreshes after a failed embed, so the stored document can be found, and keeps the text', async () => {
    // A 502 here means the document WAS stored and could not be embedded.
    await open([mine], detail())
    mocked.addKbDocument.mockRejectedValue(new Error('502: stored as d9 but could not be embedded'))
    fireEvent.change(screen.getByPlaceholderText(/paste text/i), { target: { value: 'only copy' } })
    fireEvent.click(screen.getByRole('button', { name: /add and embed/i }))

    await waitFor(() => expect(screen.getByText(/could not be embedded/i)).toBeTruthy())
    await waitFor(() => expect(mocked.getKnowledgeBase).toHaveBeenCalledTimes(2))
    expect(screen.getByDisplayValue('only copy')).toBeTruthy()
  })
})

describe('the documents in a collection', () => {
  const docs = detail({
    documents: [
      { id: 'd1', title: 'policy.md', char_count: 1200, chunk_count: 3, created_at: 2 },
      { id: 'd2', title: 'short.txt', char_count: 80, chunk_count: 1, created_at: 1 },
      { id: 'd3', title: 'legacy.txt', char_count: null, chunk_count: null, created_at: 0 },
    ],
  })

  it('shows each document with its chunk count', async () => {
    await open([kb({ document_count: 3 })], docs)
    expect(screen.getByText('Documents · 03')).toBeTruthy()
    expect(screen.getByText('3 chunks')).toBeTruthy()
    expect(screen.getByText('1 chunk')).toBeTruthy()
    // A row from before counts were stored is unknown, which is not the same as empty.
    expect(screen.getByText('chunks unknown')).toBeTruthy()
  })

  it('names each remove button after its document', async () => {
    await open([kb({ document_count: 3 })], docs)
    expect(screen.getByRole('button', { name: 'Remove short.txt' })).toBeTruthy()
  })

  it('keeps the documents on screen when a remove fails', async () => {
    // The failure used to replace the whole collection with the error.
    await open([kb({ document_count: 3 })], docs)
    mocked.deleteKbDocument.mockRejectedValue(new Error('503: Service Unavailable'))
    fireEvent.click(screen.getByRole('button', { name: 'Remove policy.md' }))

    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/503/))
    expect(screen.getByText('policy.md')).toBeTruthy()
    expect(screen.getByText('short.txt')).toBeTruthy()
  })
})
