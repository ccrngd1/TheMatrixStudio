// SPDX-License-Identifier: Apache-2.0
// New messages type themselves in (docs/MOBILE-UI.md §4.2): only a message that ARRIVES while the feed is open,
// never the history the page opened on; the whole text in the DOM from the first frame, so screen readers and
// every other test read it at once; its space reserved, so the feed never jumps; and nothing with FX off or
// reduced motion. The reveal is a copy drawn over the real text, which these tests can see in jsdom; that the
// copy wraps exactly where the real text does was checked in Chromium, where there is layout.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { ConversationFeed } from './ConversationFeed'
import { FxContext } from '../ui/fx'
import { typeMs } from '../ui/TypeIn'
import type { AgentView, FeedMessage } from '../types'

vi.mock('../api', () => ({ loadAvatar: vi.fn().mockResolvedValue(null) }))

const agent = (name: string): AgentView => ({
  name, persona: 'p', goals: [], portrait: null, portraitKey: null, portraitUrl: null,
  avatarResolved: true, messageCount: 1, tokensIn: 0, tokensOut: 0, costUsd: 0,
})
const agents = { Ada: agent('Ada'), Bo: agent('Bo') }
const msg = (seq: number, content: string, extra: Partial<FeedMessage> = {}): FeedMessage =>
  ({ turn: seq, seq, speaker: seq % 2 ? 'Ada' : 'Bo', content, ...extra })

const history = [msg(1, 'already said'), msg(2, 'also already said')]
const LIVE = 'This one arrived while the page was open.'

// `liveFrom` defaults to the backlog above; passing it as undefined means unset, as the scrubber does.
function feedOf(feed: FeedMessage[], opts: { fx?: boolean; liveFrom?: number | null } = {}) {
  const fx = opts.fx ?? true
  const liveFrom = 'liveFrom' in opts ? opts.liveFrom : 2
  return (
    <FxContext.Provider value={fx}>
      <ConversationFeed feed={feed} agents={agents} activeSpeaker={null} thinking={false} liveFrom={liveFrom} />
    </FxContext.Provider>
  )
}
const typing = () => document.querySelectorAll('.cc-typing')
const msgOf = (text: string) => screen.getByText(text).closest('.cc-msg')!

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame', 'performance'] })
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('type-in of new messages', () => {
  it('renders what was there when the feed opened at once', () => {
    render(feedOf(history))
    expect(typing()).toHaveLength(0)
    expect(document.querySelector('.cc-new')).toBeNull()
  })

  it('types a message that arrives, with the full text in the DOM and its space taken from the first frame', () => {
    const { rerender } = render(feedOf(history))
    rerender(feedOf([...history, msg(3, LIVE)]))
    // The real paragraph, whole, is what a query (and a screen reader) finds: it is only made transparent.
    const real = screen.getByText(LIVE).closest('p')!
    expect(real.textContent).toBe(LIVE)
    const host = real.parentElement!
    expect(host).toHaveClass('cc-typein', 'cc-typing')
    expect(msgOf(LIVE)).toHaveClass('cc-new')
    // The copy drawn over it is hidden from assistive tech and cannot take focus. It holds every character:
    // the typed part as text, the rest as hidden generated content (so the text is found once, not twice),
    // laid out so it wraps where the final text will.
    const layer = host.querySelector('.cc-typein-fx')!
    expect(layer).toHaveAttribute('aria-hidden', 'true')
    expect(layer).toHaveAttribute('inert')
    const typed = () => layer.textContent ?? ''
    const rest = () => layer.querySelector<HTMLElement>('.cc-ghost')?.dataset.rest ?? ''
    expect(typed() + rest()).toBe(LIVE)
    expect(typed()).toBe('')
    // Halfway through, about half is typed.
    act(() => vi.advanceTimersByTime(typeMs(LIVE.length) / 2))
    expect(typed().length).toBeGreaterThan(LIVE.length / 3)
    expect(typed().length).toBeLessThan((LIVE.length * 2) / 3)
    expect(typed() + rest()).toBe(LIVE)
    expect(screen.getAllByText(LIVE)).toHaveLength(1)
    // Done: the copy goes and the real text shows. Nothing is left scheduled.
    act(() => vi.advanceTimersByTime(typeMs(LIVE.length)))
    expect(typing()).toHaveLength(0)
    expect(document.querySelector('.cc-typein-fx')).toBeNull()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('a citation in the copy cannot take focus', () => {
    const { rerender } = render(
      <FxContext.Provider value>
        <ConversationFeed feed={history} agents={agents} activeSpeaker={null} thinking={false} liveFrom={2} runId="r1"
          sourceIndex={{ 'Pilot playbook #2': { chunk_id: 3, document_id: 'd1', title: 'Pilot playbook', ordinal: 2 } as never }} />
      </FxContext.Provider>,
    )
    rerender(
      <FxContext.Provider value>
        <ConversationFeed feed={[...history, msg(3, 'As [Pilot playbook #2] says, measure it.')]} agents={agents}
          activeSpeaker={null} thinking={false} liveFrom={2} runId="r1"
          sourceIndex={{ 'Pilot playbook #2': { chunk_id: 3, document_id: 'd1', title: 'Pilot playbook', ordinal: 2 } as never }} />
      </FxContext.Provider>,
    )
    const layer = document.querySelector('.cc-typein-fx')!
    const copies = layer.querySelectorAll('button')
    expect(copies.length).toBeGreaterThan(0)
    for (const el of copies) expect(el).toHaveAttribute('tabindex', '-1')
    // The real citation, under the copy, is the one that works.
    expect(screen.getByRole('button', { name: /Pilot playbook #2/ })).not.toHaveAttribute('tabindex')
  })

  it('does not type the backlog revealed after mount (the load), only what comes after it', () => {
    // The feed mounts empty while the backlog loads, then gets it all at once: history, not arrivals.
    const { rerender } = render(feedOf([], { liveFrom: null }))
    rerender(feedOf(history, { liveFrom: 2 }))
    expect(typing()).toHaveLength(0)
    rerender(feedOf([...history, msg(3, LIVE)], { liveFrom: 2 }))
    expect(typing()).toHaveLength(1)
  })

  it('types nothing without a liveFrom: the scrubber, and a stream that could not tell history from new', () => {
    const { rerender } = render(feedOf(history, { liveFrom: undefined }))
    rerender(feedOf([...history, msg(3, LIVE)], { liveFrom: undefined }))
    expect(typing()).toHaveLength(0)
  })

  it('shows a jump (Catch up) at once rather than typing many messages together', () => {
    const { rerender } = render(feedOf(history))
    rerender(feedOf([...history, msg(3, 'one'), msg(4, 'two'), msg(5, 'three')]))
    expect(typing()).toHaveLength(0)
  })

  it('does not type an operator injection, which is a banner, not speech', () => {
    const { rerender } = render(feedOf(history))
    rerender(feedOf([...history, msg(3, 'From the customer', { injected: true, speaker: 'Customer email' })]))
    expect(typing()).toHaveLength(0)
  })

  it('nothing animates with FX off, and turning FX on later does not replay what arrived', () => {
    const { rerender } = render(feedOf(history, { fx: false }))
    rerender(feedOf([...history, msg(3, LIVE)], { fx: false }))
    expect(typing()).toHaveLength(0)
    expect(document.querySelector('.cc-new')).toBeNull()
    rerender(feedOf([...history, msg(3, LIVE)], { fx: true }))
    expect(typing()).toHaveLength(0)
  })

  it('nothing animates when reduced motion is asked for', () => {
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: q.includes('reduce') }))
    const { rerender } = render(feedOf(history))
    rerender(feedOf([...history, msg(3, LIVE)]))
    expect(typing()).toHaveLength(0)
    expect(document.querySelector('.cc-new')).toBeNull()
  })

  it('a feed that remounts (a tab switch) does not type its messages again', () => {
    const first = render(feedOf(history))
    first.rerender(feedOf([...history, msg(3, LIVE)]))
    expect(typing()).toHaveLength(1)
    first.unmount()
    render(feedOf([...history, msg(3, LIVE)]))
    expect(typing()).toHaveLength(0)
  })

  it('leaves no frame loop behind when the feed goes mid-type', () => {
    const { rerender, unmount } = render(feedOf(history))
    rerender(feedOf([...history, msg(3, LIVE)]))
    expect(vi.getTimerCount()).toBeGreaterThan(0)
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('a long run keeps nothing scheduled between turns', () => {
    // Forty turns arriving one by one, each typed and finished before the next: no loop survives its message.
    let feed = history
    const { rerender } = render(feedOf(feed))
    for (let s = 3; s < 43; s++) {
      feed = [...feed, msg(s, `turn ${s} says something`)]
      rerender(feedOf(feed))
      act(() => vi.advanceTimersByTime(1500))
      expect(vi.getTimerCount()).toBe(0)
    }
    expect(typing()).toHaveLength(0)
  })
})

describe('loop phase in the feed (§5.4)', () => {
  it('keeps each loop’s phase across re-renders, so a new turn does not move it', () => {
    const banner = msg(3, 'From the customer', { injected: true, speaker: 'Customer email' })
    const live = (thinking: boolean, feed: FeedMessage[]) => (
      <ConversationFeed feed={feed} agents={agents} activeSpeaker="Ada" thinking={thinking} />
    )
    const { rerender } = render(live(true, [...history, banner]))
    const signal = () => document.querySelector<HTMLElement>('.cc-signal')!.style.getPropertyValue('--ph')
    const eq = () => document.querySelector<HTMLElement>('.cc-eq')!.style.getPropertyValue('--ph')
    const [s0, e0] = [signal(), eq()]
    expect(s0).toMatch(/^-\d+ms$/)
    vi.setSystemTime(Date.now() + 700)
    rerender(live(true, [...history, banner, msg(4, 'next')]))
    expect(signal()).toBe(s0)
    expect(eq()).toBe(e0)
  })
})
