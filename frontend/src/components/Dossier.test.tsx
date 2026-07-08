// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Dossier } from './Dossier'
import type { AgentView } from '../types'

const agent: AgentView = {
  name: 'Ada',
  persona: 'A cautious ethicist',
  goals: ['Raise risks'],
  portrait: null,
  avatarResolved: true,
  messageCount: 1,
  tokensIn: 10,
  tokensOut: 5,
  costUsd: 0.001,
}

describe('Dossier honesty gate', () => {
  it('shows only real data and labels deferred fields as later version', () => {
    render(
      <Dossier
        agent={agent}
        feed={[{ turn: 1, seq: 2, speaker: 'Ada', content: 'Hello there' }]}
        onClose={() => {}}
      />,
    )
    // Real data present
    expect(screen.getByText('A cautious ethicist')).toBeInTheDocument()
    expect(screen.getByText('Raise risks')).toBeInTheDocument()
    expect(screen.getByText('Hello there')).toBeInTheDocument()

    // Deferred fields must be labeled, never fabricated.
    expect(screen.getByText('Memory stream')).toBeInTheDocument()
    expect(screen.getByText(/available in a later version/i)).toBeInTheDocument()
    expect(screen.getAllByText(/later version/i).length).toBeGreaterThan(0)
  })
})
