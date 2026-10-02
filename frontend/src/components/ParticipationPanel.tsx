// SPDX-License-Identifier: Apache-2.0
import { colorForName } from '../lib/avatar'
import type { FeedMessage } from '../types'
import { PersonaName } from '../ui/PersonaName'

interface Props {
  /** Every revealed turn, in order. The panel is a view of this and nothing else. */
  feed: FeedMessage[]
  /** Cast order, so a persona who never spoke still gets a row. */
  order: string[]
  /** Reveal and scroll to a turn. `seq` because it identifies a message uniquely. */
  onJump: (seq: number) => void
}

/** Gini coefficient of turn share: 0 = everyone spoke equally, 1 = one persona took all. */
export function giniOfShares(shares: number[]): number {
  const ordered = [...shares].sort((a, b) => a - b)
  const n = ordered.length
  const total = ordered.reduce((s, x) => s + x, 0)
  if (!n || !total) return 0
  const weighted = ordered.reduce((s, x, i) => s + (2 * (i + 1) - n - 1) * x, 0)
  return weighted / (n * total)
}

/**
 * Who spoke, how often, and when — with every turn clickable.
 *
 * Read straight off the revealed feed rather than from a new endpoint: the feed already
 * holds every turn (a completed run's events are all fetched on open), so a stats route
 * would be a second source of truth for a number the client can count. It also means the
 * panel matches what the viewer can currently see during playback instead of jumping ahead
 * of the scrubber.
 *
 * A row per cast member INCLUDING anyone with zero turns, because that is the finding worth
 * surfacing: a persona with an authored knowledge base and convictions who never spoke is
 * the failure mode `docs/studies/SPEAKER-SELECTION-EVALUATION.md` exists to remove, and a panel that
 * only lists speakers would hide exactly that.
 */
export function ParticipationPanel({ feed: all, order, onJump }: Props) {
  // Only participants' own turns. A consultant answers when asked and an injected message is the
  // operator's; counting either made a scheduled "Residents' survey" read as a speaker with 8% of turns.
  const feed = all.filter((m) => !m.consultant && !m.injected)
  const names = [...new Set([...order, ...feed.map((m) => m.speaker)])]
  const turnsBy = new Map<string, FeedMessage[]>(names.map((n) => [n, []]))
  for (const m of feed) turnsBy.get(m.speaker)?.push(m)

  const total = feed.length
  const rows = names
    .map((name) => ({ name, turns: turnsBy.get(name) ?? [] }))
    .sort((a, b) => b.turns.length - a.turns.length || a.name.localeCompare(b.name))
  const most = rows.length ? rows[0].turns.length : 0
  const gini = giniOfShares(rows.map((r) => r.turns.length))

  if (total === 0) {
    return (
      <section className="rounded-lg border border-matrix-border bg-matrix-panel p-3">
        <h2 className="text-sm font-semibold text-slate-300">Participation</h2>
        <p className="mt-1 text-xs text-slate-500">No turns yet.</p>
      </section>
    )
  }

  return (
    <section className="rounded-lg border border-matrix-border bg-matrix-panel p-3">
      <div className="flex items-baseline justify-between">
        <h2 className="text-sm font-semibold text-slate-300">Participation</h2>
        <span className="text-[11px] text-slate-500">
          {total} turn{total === 1 ? '' : 's'} · {rows.length} speaker
          {rows.length === 1 ? '' : 's'}
        </span>
      </div>
      <p className="mt-0.5 text-[11px] text-slate-500">
        Turn share is {gini <= 0.15 ? 'even' : gini <= 0.3 ? 'uneven' : 'very uneven'} (Gini{' '}
        {gini.toFixed(2)}). Click any turn to jump to it.
      </p>

      <ul className="mt-2 space-y-2">
        {rows.map(({ name, turns }) => (
          <li key={name}>
            <div className="flex items-baseline gap-2">
              <span
                aria-hidden="true"
                className="h-2 w-2 shrink-0 rounded-full"
                style={{ backgroundColor: colorForName(name) }}
              />
              <span className="min-w-0 flex-1 truncate text-xs text-slate-300">
                <PersonaName name={name} />
              </span>
              {/* "3 turns · 75%" as two separate elements. A bare number beside a
                  percentage reads ambiguously, and it also means a test can ask for the
                  count without matching the share. */}
              <span className="shrink-0 text-xs tabular-nums text-slate-400">
                <span>
                  {turns.length} turn{turns.length === 1 ? '' : 's'}
                </span>
                <span className="text-slate-600"> · </span>
                <span>{Math.round((turns.length / total) * 100)}%</span>
              </span>
            </div>
            {/* Proportional bar, scaled to the LOUDEST speaker rather than to the run
                length: at 6 speakers every bar would otherwise sit under a fifth of the
                width and the comparison would be unreadable. */}
            <div className="mt-1 h-1 w-full rounded bg-matrix-border/60">
              <div
                className="h-1 rounded"
                style={{
                  width: most ? `${(turns.length / most) * 100}%` : '0%',
                  backgroundColor: colorForName(name),
                }}
              />
            </div>
            {/* A cell for EVERY turn in the run, not just this persona's — the position
                along the row is the position in the conversation. A packed strip of three
                markers says "three turns" and hides the thing actually worth seeing: that
                they were turns 1, 2 and 4, or that nobody heard from this persona between
                turn 5 and turn 30. Filled = they spoke and it is clickable; the rest are
                inert spacers, so the columns line up down the whole panel. */}
            <div className="mt-1 flex gap-[1px]">
              {feed.map((m) =>
                m.speaker === name ? (
                  <button
                    key={m.seq}
                    type="button"
                    onClick={() => onJump(m.seq)}
                    aria-description={`${m.content.slice(0, 80)}`}
                    aria-label={`Jump to turn ${m.turn}, ${name}`}
                    className="h-3 min-w-[3px] flex-1 rounded-sm opacity-80 transition hover:opacity-100 hover:ring-1 hover:ring-matrix-accent"
                    style={{ backgroundColor: colorForName(name) }}
                  />
                ) : (
                  <span
                    key={m.seq}
                    aria-hidden="true"
                    data-testid="gap"
                    className="h-3 min-w-[3px] flex-1 rounded-sm bg-matrix-border/40"
                  />
                ),
              )}
            </div>
            {turns.length === 0 && (
              <p className="mt-0.5 text-[11px] text-amber-400/80">never spoke</p>
            )}
          </li>
        ))}
      </ul>
      {/* The axis, once at the bottom. Without it the rows are a pattern with no scale —
          a reader cannot tell whether a gap is three turns or thirty. */}
      <div className="mt-1 flex justify-between text-[10px] text-slate-600">
        {/* The FIRST and LAST turn numbers in the feed, not 1 and the count. A branch's
            feed starts at the fork — turns 13..40 — and labelling that "turn 1 … turn 28"
            would misdescribe every position on the axis. */}
        <span>turn {feed[0].turn}</span>
        <span>turn {feed[feed.length - 1].turn}</span>
      </div>
    </section>
  )
}
