// SPDX-License-Identifier: Apache-2.0
//
// The export rules live server-side (matrix_studio/export.py), so these test only what this
// component owns: the right request, a real download, and a PDF path that survives popup blocking.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ExportMenu } from './ExportMenu'
import { api } from '../api'

vi.mock('../api', () => ({ api: { exportText: vi.fn() } }))
const mocked = api as unknown as { exportText: ReturnType<typeof vi.fn> }

describe('ExportMenu', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    URL.createObjectURL = vi.fn(() => 'blob:x')
    URL.revokeObjectURL = vi.fn()
  })

  it('downloads Markdown for this run under a file name that says what it is', async () => {
    mocked.exportText.mockResolvedValue('# run')
    const clicked: string[] = []
    const orig = HTMLAnchorElement.prototype.click
    HTMLAnchorElement.prototype.click = function () {
      clicked.push((this as HTMLAnchorElement).download)
    }
    render(<ExportMenu kind="run" id="r1" name="renewal, take 2" />)
    fireEvent.click(screen.getByText('Markdown'))
    await waitFor(() => expect(clicked).toEqual(['renewal-take-2-run.md']))
    expect(mocked.exportText).toHaveBeenCalledWith('run', 'r1', 'md')
    HTMLAnchorElement.prototype.click = orig
  })

  it('asks for the ensemble route when exporting an ensemble', async () => {
    mocked.exportText.mockResolvedValue('<html></html>')
    HTMLAnchorElement.prototype.click = vi.fn()
    render(<ExportMenu kind="ensemble" id="e1" name="cells" />)
    fireEvent.click(screen.getByText('HTML'))
    await waitFor(() => expect(mocked.exportText).toHaveBeenCalledWith('ensemble', 'e1', 'html'))
  })

  it('opens the print window BEFORE fetching, so it is not blocked as a popup', async () => {
    // A window opened after an `await` is treated as a popup. The order is the whole fix.
    const order: string[] = []
    const win = {
      document: { open: vi.fn(), write: vi.fn(), close: vi.fn() },
      focus: vi.fn(), print: vi.fn(() => order.push('print')), close: vi.fn(),
    }
    window.open = vi.fn(() => {
      order.push('open')
      return win as unknown as Window
    })
    mocked.exportText.mockImplementation(async () => {
      order.push('fetch')
      return '<html>export</html>'
    })
    render(<ExportMenu kind="run" id="r1" name="x" />)
    fireEvent.click(screen.getByText('PDF'))
    await waitFor(() => expect(win.print).toHaveBeenCalled())
    expect(order).toEqual(['open', 'fetch', 'print'])
    expect(win.document.write).toHaveBeenCalledWith('<html>export</html>')
    expect(mocked.exportText).toHaveBeenCalledWith('run', 'r1', 'html')
  })

  it('says what to do when the print window is blocked anyway', async () => {
    window.open = vi.fn(() => null)
    render(<ExportMenu kind="run" id="r1" name="x" />)
    fireEvent.click(screen.getByText('PDF'))
    expect(await screen.findByText(/Allow popups for this site/)).toBeInTheDocument()
    expect(mocked.exportText).not.toHaveBeenCalled()
  })

  it('closes the empty print window and reports a failed export', async () => {
    const win = { document: { open: vi.fn(), write: vi.fn(), close: vi.fn() },
                  focus: vi.fn(), print: vi.fn(), close: vi.fn() }
    window.open = vi.fn(() => win as unknown as Window)
    mocked.exportText.mockRejectedValue(new Error('404: Run not found'))
    render(<ExportMenu kind="run" id="r1" name="x" />)
    fireEvent.click(screen.getByText('PDF'))
    expect(await screen.findByText(/Export failed: 404/)).toBeInTheDocument()
    expect(win.close).toHaveBeenCalled()
    expect(win.print).not.toHaveBeenCalled()
  })
})
