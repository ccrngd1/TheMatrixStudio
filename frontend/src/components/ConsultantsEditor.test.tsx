// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { ConsultantsEditor, consultantsConfig, blankConsultant } from './ConsultantsEditor'

vi.mock('../api', () => ({ api: { listKnowledgeBases: vi.fn().mockResolvedValue({ knowledge_bases: [], count: 0 }) } }))

describe('consultantsConfig', () => {
  it('keeps named consultants only, with their sources', () => {
    const out = consultantsConfig([
      { ...blankConsultant(), name: ' Ada ', expertise: 'codes', knowledgeBases: ['kb1'], docTitle: '', docText: 'text' },
      blankConsultant(),
    ])
    expect(out).toEqual([{ name: 'Ada', expertise: 'codes', knowledge_bases: ['kb1'],
      document_texts: [{ title: 'Ada notes', text: 'text' }] }])
  })
})

describe('ConsultantsEditor', () => {
  it('adds a consultant and caps the list at five', () => {
    const onChange = vi.fn()
    render(<ConsultantsEditor consultants={[]} onChange={onChange} limit={6} onLimit={vi.fn()} />)
    fireEvent.click(screen.getByText('+ Add consultant'))
    expect(onChange).toHaveBeenCalledWith([blankConsultant()])
    const five = Array.from({ length: 5 }, blankConsultant)
    render(<ConsultantsEditor consultants={five} onChange={vi.fn()} limit={6} onLimit={vi.fn()} />)
    expect(screen.getAllByText('+ Add consultant')[1]).toBeDisabled()
  })
})
