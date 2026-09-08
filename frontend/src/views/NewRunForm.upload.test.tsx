// SPDX-License-Identifier: Apache-2.0
/**
 * Uploading files as a persona's knowledge base, from the new-run form.
 *
 * The server extracts text and stores nothing, so an uploaded file becomes an ordinary
 * pasted-document entry and rides the existing create-run path. These tests pin the
 * two properties that make that safe: the extracted text is *shown* before it is used
 * (PDF extraction can silently produce nothing useful), and it is what actually gets
 * submitted.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { NewRunForm } from './NewRunForm'
import { api } from '../api'

const ALL_FORMATS = {
  formats: [
    { suffix: '.docx', media_type: 'docx', available: true, needs: null },
    { suffix: '.md', media_type: 'md', available: true, needs: null },
    { suffix: '.pdf', media_type: 'pdf', available: true, needs: null },
    { suffix: '.txt', media_type: 'txt', available: true, needs: null },
  ],
  max_upload_bytes: 10 * 1024 * 1024,
  max_document_chars: 400000,
}

vi.mock('../api', () => ({
  api: {
    createRun: vi.fn().mockResolvedValue({ run_id: 'r-new' }),
    getModels: vi.fn().mockResolvedValue({ models: [], default: '' }),
    getDocumentFormats: vi.fn(),
    extractDocument: vi.fn(),
    getRunSetup: vi.fn(),
    suggestPersonas: vi.fn(),
    suggestName: vi.fn().mockResolvedValue({ name: 'x', description: 'y' }),
  },
}))

const file = (name: string, body = 'x') =>
  new File([body], name, { type: 'application/octet-stream' })

/** Opens the first persona's document section and returns its file input. */
const openDocuments = async () => {
  render(<NewRunForm onStarted={() => {}} onCancel={() => {}} />)
  await waitFor(() => expect(api.getDocumentFormats).toHaveBeenCalled())
  fireEvent.click(screen.getAllByText(/Convictions, dismissals & documents|documents/i)[0])
  return screen.getAllByLabelText(/upload file/i)[0] as HTMLInputElement
}

describe('knowledge-base file upload', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.getDocumentFormats as any).mockResolvedValue(ALL_FORMATS)
  })

  it('shows the extracted text so it can be checked before use', async () => {
    // The point of showing it: a scanned PDF or a bad extraction is invisible
    // otherwise, and the persona would cite material that is not there.
    ;(api.extractDocument as any).mockResolvedValue({
      title: 'constraints.pdf',
      media_type: 'pdf',
      text: 'No feature may add an external service.',
      char_count: 39,
      chunk_count: 1,
      stored: false,
    })
    const input = await openDocuments()
    fireEvent.change(input, { target: { files: [file('constraints.pdf')] } })

    await waitFor(() =>
      expect(screen.getByDisplayValue('No feature may add an external service.')).toBeInTheDocument(),
    )
    // Titled from the file, and editable like any pasted document.
    expect(screen.getByDisplayValue('constraints.pdf')).toBeInTheDocument()
  })

  it('submits the extracted text as the persona document', async () => {
    ;(api.extractDocument as any).mockResolvedValue({
      title: 'brief.docx', media_type: 'docx', text: 'The brief in full.',
      char_count: 18, chunk_count: 1, stored: false,
    })
    const input = await openDocuments()
    fireEvent.change(input, { target: { files: [file('brief.docx')] } })
    await waitFor(() => expect(screen.getByDisplayValue('The brief in full.')).toBeInTheDocument())

    fireEvent.change(screen.getByPlaceholderText(/What should the cast discuss/), {
      target: { value: 'A topic' },
    })
    fireEvent.change(screen.getAllByPlaceholderText('Name')[0], { target: { value: 'Priya' } })
    fireEvent.change(screen.getAllByPlaceholderText(/Persona description/)[0], {
      target: { value: 'An engineer' },
    })
    fireEvent.click(screen.getByRole('button', { name: /run simulation/i }))

    await waitFor(() => expect(api.createRun).toHaveBeenCalled())
    const body = (api.createRun as any).mock.calls[0][0]
    expect(body.cast[0].document_texts).toEqual([
      { title: 'brief.docx', text: 'The brief in full.' },
    ])
    // Uploading a document is what turns retrieval on; without it the text would be
    // ingested and never consulted.
    expect(body.config.retrieval).toMatchObject({ enabled: true })
  })

  it('uploads several files at once without losing any', async () => {
    // Each append must be a functional state update; building the next list from a
    // captured value keeps only the last file, which looks like a flaky upload.
    ;(api.extractDocument as any)
      .mockResolvedValueOnce({ title: 'one.txt', media_type: 'txt', text: 'First doc.',
                               char_count: 10, chunk_count: 1, stored: false })
      .mockResolvedValueOnce({ title: 'two.txt', media_type: 'txt', text: 'Second doc.',
                               char_count: 11, chunk_count: 1, stored: false })

    const input = await openDocuments()
    fireEvent.change(input, { target: { files: [file('one.txt'), file('two.txt')] } })

    await waitFor(() => expect(screen.getByDisplayValue('Second doc.')).toBeInTheDocument())
    expect(screen.getByDisplayValue('First doc.')).toBeInTheDocument()
  })

  it('keeps the files that worked when one fails, and names the failure', async () => {
    ;(api.extractDocument as any)
      .mockRejectedValueOnce(new Error('422: No extractable text in scan.pdf'))
      .mockResolvedValueOnce({ title: 'good.txt', media_type: 'txt', text: 'Readable content.',
                               char_count: 17, chunk_count: 1, stored: false })

    const input = await openDocuments()
    fireEvent.change(input, { target: { files: [file('scan.pdf'), file('good.txt')] } })

    await waitFor(() => expect(screen.getByDisplayValue('Readable content.')).toBeInTheDocument())
    expect(screen.getByText(/scan\.pdf/)).toBeInTheDocument()
    expect(screen.getByText(/No extractable text/)).toBeInTheDocument()
  })

  it('offers only the file types this server can actually read', async () => {
    ;(api.getDocumentFormats as any).mockResolvedValue({
      ...ALL_FORMATS,
      formats: [
        { suffix: '.txt', media_type: 'txt', available: true, needs: null },
        { suffix: '.pdf', media_type: 'pdf', available: false, needs: 'pypdf' },
        { suffix: '.docx', media_type: 'docx', available: false, needs: 'python-docx' },
      ],
    })
    const input = await openDocuments()
    // A picker that lists .pdf on a server that cannot read it produces an error the
    // operator cannot act on from the file dialog.
    expect(input.accept).toBe('.txt')
    expect(input.accept).not.toMatch(/pdf|docx/)
    // And it says what is missing, plus the workaround, rather than staying silent.
    expect(screen.getByText(/pypdf and python-docx/)).toBeInTheDocument()
    expect(screen.getByText(/Paste the text instead/i)).toBeInTheDocument()
  })

  it('says nothing about missing packages when everything is available', async () => {
    await openDocuments()
    expect(screen.queryByText(/pip install/)).toBeNull()
  })

  it('states the accepted types and the size limit up front', async () => {
    await openDocuments()
    const tips = screen.getAllByRole('tooltip').map((t) => t.textContent ?? '').join(' ')
    expect(tips).toMatch(/\.pdf/)
    expect(tips).toMatch(/10 MB each/)
    // And that the file itself is not kept — the text is the artefact.
    expect(tips).toMatch(/converted to text/i)
    expect(tips).toMatch(/not\s+stored/i)
  })

  it('can upload again after a failure rather than staying stuck', async () => {
    // Guards the `finally` that clears the in-progress persona: if that were missed,
    // the input would stay disabled after any error and the only way to retry would
    // be to reload the form and re-enter the whole cast.
    ;(api.extractDocument as any).mockRejectedValueOnce(new Error('422: nope'))
    const input = await openDocuments()

    fireEvent.change(input, { target: { files: [file('a.txt')] } })
    await waitFor(() => expect(screen.getByText(/nope/)).toBeInTheDocument())
    expect(input.disabled).toBe(false)

    ;(api.extractDocument as any).mockResolvedValueOnce({
      title: 'a.txt', media_type: 'txt', text: 'Second attempt worked.',
      char_count: 22, chunk_count: 1, stored: false,
    })
    fireEvent.change(input, { target: { files: [file('a.txt')] } })
    await waitFor(() =>
      expect(screen.getByDisplayValue('Second attempt worked.')).toBeInTheDocument(),
    )
    expect(api.extractDocument).toHaveBeenCalledTimes(2)
  })
})
