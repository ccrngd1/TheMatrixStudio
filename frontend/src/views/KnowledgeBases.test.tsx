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
