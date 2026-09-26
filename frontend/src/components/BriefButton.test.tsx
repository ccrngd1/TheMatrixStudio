// SPDX-License-Identifier: Apache-2.0
//
// The brief's content rules live server-side (matrix_studio/brief.py). These test what the modal
// owns: it fetches only when opened, previews in a frame that cannot run script, and saves what was
// previewed.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { BriefButton } from './BriefButton'
import { api } from '../api'

vi.mock('../api', () => ({ api: { briefText: vi.fn() } }))
const mocked = api as unknown as { briefText: ReturnType<typeof vi.fn> }

describe('BriefButton', () => {
  let clicked: string[]
  beforeEach(() => {
    vi.clearAllMocks()
    URL.createObjectURL = vi.fn(() => 'blob:x')
    URL.revokeObjectURL = vi.fn()
    clicked = []
    HTMLAnchorElement.prototype.click = function () {
      clicked.push((this as HTMLAnchorElement).download)
    }
  })

  it('does not fetch until opened, then previews the server HTML in a sandboxed frame', async () => {
    mocked.briefText.mockResolvedValue('<html><body>brief</body></html>')
    render(<BriefButton kind="run" id="r1" name="renewal" />)
    expect(mocked.briefText).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Brief'))
    const frame = await screen.findByTitle('Decision brief')
    expect(mocked.briefText).toHaveBeenCalledWith('run', 'r1', 'html')
    expect(frame).toHaveAttribute('sandbox', '')
    expect(frame.getAttribute('srcdoc')).toContain('brief')
  })

  it('saves the previewed HTML without refetching, and fetches Markdown separately', async () => {
    mocked.briefText.mockImplementation((_k: string, _i: string, f: string) =>
      Promise.resolve(f === 'md' ? '# brief' : '<html></html>'))
    render(<BriefButton kind="ensemble" id="e1" name="Cells A/B" />)
    fireEvent.click(screen.getByText('Brief'))
    await screen.findByTitle('Decision brief')
    fireEvent.click(screen.getByText('Download HTML'))
    await waitFor(() => expect(clicked).toEqual(['cells-a-b-ensemble-brief.html']))
    expect(mocked.briefText).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByText('Download Markdown'))
    await waitFor(() => expect(clicked).toContain('cells-a-b-ensemble-brief.md'))
    expect(mocked.briefText).toHaveBeenLastCalledWith('ensemble', 'e1', 'md')
  })

  it('says when the brief could not be loaded', async () => {
    mocked.briefText.mockRejectedValue(new Error('404 not found'))
    render(<BriefButton kind="run" id="r1" name="x" />)
    fireEvent.click(screen.getByText('Brief'))
    expect(await screen.findByText(/Could not load the brief: 404 not found/)).toBeInTheDocument()
  })

  it('closes on the close button', async () => {
    mocked.briefText.mockResolvedValue('<html></html>')
    render(<BriefButton kind="run" id="r1" name="x" />)
    fireEvent.click(screen.getByText('Brief'))
    await screen.findByTitle('Decision brief')
    fireEvent.click(screen.getByLabelText('Close brief'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})
