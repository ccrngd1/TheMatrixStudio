// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { Dossier } from './Dossier'
import type { AgentView } from '../types'
import { api } from '../api'

vi.mock('../api', () => ({
  api: {
    getDossier: vi.fn(),
    getTurnTrace: vi.fn(),
  },
}))

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

const feed = [{ turn: 1, seq: 2, speaker: 'Ada', content: 'Hello there' }]

describe('Dossier', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders real persona/goals/messages and an honest not-captured state for cognition-off runs', async () => {
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({
      run_id: 'r1', agent: 'Ada', persona: 'A cautious ethicist', goals: ['Raise risks'],
      memory_stream: [], beliefs: [], relationships: {},
      tokens_in: 10, tokens_out: 5, cost_usd: 0.001, portrait_b64: null,
    })
    render(<Dossier agent={agent} feed={feed} runId="r1" onClose={() => {}} />)

    expect(screen.getByText('A cautious ethicist')).toBeInTheDocument()
    expect(screen.getByText('Raise risks')).toBeInTheDocument()
    expect(screen.getByText('Hello there')).toBeInTheDocument()

    // Honesty gate: no fabricated cognition; explicit not-captured message.
    await waitFor(() =>
      expect(screen.getByText(/created without cognition/i)).toBeInTheDocument(),
    )
    // No "why?" trace button when there is no captured cognition.
    expect(screen.queryByText('why?')).not.toBeInTheDocument()
  })

  it('renders the memory stream and a why-trace affordance when cognition was captured', async () => {
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({
      run_id: 'r1', agent: 'Ada', persona: 'A cautious ethicist', goals: ['Raise risks'],
      memory_stream: [
        { id: 'm1', content: 'the group values consent', importance: 0.8, tags: ['fact'], timestamp: 1 },
      ],
      beliefs: [{ id: 'b1', content: 'consent is the crux', importance: 0.9, tags: ['reflection'], timestamp: 2 }],
      relationships: { Ben: 'trusted ally' },
      tokens_in: 10, tokens_out: 5, cost_usd: 0.001, portrait_b64: null,
    })
    render(<Dossier agent={agent} feed={feed} runId="r1" onClose={() => {}} />)

    await waitFor(() =>
      expect(screen.getByText('the group values consent')).toBeInTheDocument(),
    )
    expect(screen.getByText('consent is the crux')).toBeInTheDocument()
    expect(screen.getByText('trusted ally')).toBeInTheDocument()
    // The "why did it say that?" affordance is present for a captured run.
    expect(screen.getByText('why?')).toBeInTheDocument()
  })
})
