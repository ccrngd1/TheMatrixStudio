// SPDX-License-Identifier: Apache-2.0
// The stance surfaces (docs/MOBILE-UI.md §4.2, §4.3, §4.5, stage 5). Stance is post-run only (§6.1, decided):
// `matrix_studio/stance.py` writes it when the summary is generated, and a live run has none, so every
// component here renders nothing, or a "not yet" note, when it is absent.
//
// Never colour alone (§2 rule 2): every count carries its glyph, every bar and node its name for a screen
// reader, and the dial prints its counts beneath it.
//
// "% support" counts ▲ alone. Accepting with conditions (◐, 2026-10-01) is not folded into it, because the
// conditions may never be met and the headline would then overstate the room; it is printed beside it instead,
// as its own share, wherever there is any.
import type { CSSProperties, KeyboardEvent, ReactNode } from 'react'
import type { FeedMessage, StanceBasisEntry } from '../../types'
import { STANCE_COLOR, STANCE_LABEL, Tag, identityOf, initials, type Stance } from '../../ui/primitives'
import { usePhase } from '../../ui/fx'

export type StanceMap = Record<string, Stance>
type Css = CSSProperties & Record<`--${string}`, string>
const ORDER: Stance[] = ['support', 'conditional', 'unstated', 'holding']
const GLYPH: Record<Stance, string> = { support: '▲', conditional: '◐', unstated: '◆', holding: '▼' }
export const STANCE_CLASS: Record<Stance, string> = {
  support: 'cc-c-support',
  conditional: 'cc-c-conditional',
  unstated: 'cc-c-undecided',
  holding: 'cc-c-holding',
}
const WORD: Record<Stance, string> = {
  support: 'support',
  conditional: 'with conditions',
  unstated: 'not stated',
  holding: 'holding out',
}
/**
 * The states a count row shows. ◐ only where there is one: a run with no closing round cannot have it, and
 * "0 with conditions" on every older run would read as a finding rather than as a state it never had.
 */
const shown = (c: Record<Stance, number>) => ORDER.filter((k) => k !== 'conditional' || c.conditional > 0)

export function countStances(stance: StanceMap, among?: string[]) {
  const names = among ?? Object.keys(stance)
  const c: Record<Stance, number> = { support: 0, conditional: 0, unstated: 0, holding: 0 }
  for (const n of names) c[stance[n] ?? 'unstated']++
  const share = (k: Stance) => (names.length ? Math.round((100 * c[k]) / names.length) : 0)
  return { ...c, total: names.length, pct: share('support'), pctConditional: share('conditional') }
}

/** "50% support", and "· +17% with conditions" when anyone accepted with conditions. */
function supportLine(c: ReturnType<typeof countStances>) {
  return `${c.pct}% support${c.conditional ? ` · +${c.pctConditional}% with conditions` : ''}`
}

export function StanceCounts({ stance, among }: { stance: StanceMap; among?: string[] }) {
  const c = countStances(stance, among)
  return (
    <span className="cc-counts">
      {shown(c).map((k) => (
        <b key={k} className={STANCE_CLASS[k]}>
          {GLYPH[k]}
          {c[k]}
          <span className="sr-only"> {WORD[k]}</span>
        </b>
      ))}
    </span>
  )
}

export function StanceTag({ stance }: { stance: Stance }) {
  return <Tag color={STANCE_COLOR[stance]}>{STANCE_LABEL[stance]}</Tag>
}

/** One slanted bar per persona, ordered support → with conditions → not stated → holding out. */
export function StanceBar({ stance, order }: { stance: StanceMap; order: string[] }) {
  const ks = [...order].sort((a, b) => ORDER.indexOf(stance[a] ?? 'unstated') - ORDER.indexOf(stance[b] ?? 'unstated'))
  return (
    <div className="cc-sbar" role="img" aria-label={ks.map((k) => `${k}: ${WORD[stance[k] ?? 'unstated']}`).join(', ')}>
      {ks.map((k) => (
        <i key={k} style={{ '--sc': STANCE_COLOR[stance[k] ?? 'unstated'] } as Css} />
      ))}
    </div>
  )
}

/** The HUD's third cell, once the run has a stance. */
export function RoomCell({ stance, order }: { stance: StanceMap; order: string[] }) {
  const c = countStances(stance, order)
  return (
    <div>
      <div className="cc-rk">
        <span>Room</span>
        <StanceCounts stance={stance} among={order} />
      </div>
      <StanceBar stance={stance} order={order} />
      <div className="cc-rsub">{supportLine(c)}</div>
    </div>
  )
}

const hexPts = (x: number, y: number, r: number) =>
  [0, 1, 2, 3, 4, 5]
    .map((i) => {
      const a = -Math.PI / 2 + (i * Math.PI) / 3
      return `${(x + r * Math.cos(a)).toFixed(1)},${(y + r * Math.sin(a)).toFixed(1)}`
    })
    .join(' ')

const GRAD: Record<string, [string, string]> = {
  a1: ['#f9a8d4', '#c084fc'], a2: ['#a5f3fc', '#38bdf8'], a3: ['#fde68a', '#f59e0b'],
  a4: ['#a7f3d0', '#34d399'], a5: ['#fecdd3', '#fb7185'], a6: ['#c7d2fe', '#818cf8'], a0: ['#cbd5e1', '#94a3b8'],
}

/**
 * The room map (§4.3): one node per persona on a ring, sized by turns taken, its halo the end stance.
 *
 * The lines are **sequence, not replies** (§6.2): one per pair who spoke back to back, thicker the more
 * often. Nothing records who a message answers, so a line says only that one spoke after the other — and
 * the legend says so, because "who is arguing with whom" is the reading a viewer will reach for.
 */
export function RoomMap({
  order, feed, stance, next, onOpen,
}: { order: string[]; feed: FeedMessage[]; stance: StanceMap | null; next: string | null; onOpen: (name: string) => void }) {
  // Taken once, when the map mounts (`ui/fx.ts`): read per render, it moved the running loops on every new
  // message, so the live edge and the active halo jumped ahead each turn.
  const ph = { '--ph': usePhase() } as Css
  const cx = 160, cy = 124, R = 90
  const pos: Record<string, [number, number]> = {}
  order.forEach((k, i) => {
    const a = -Math.PI / 2 + (i / Math.max(1, order.length)) * Math.PI * 2
    pos[k] = [cx + R * Math.cos(a), cy + R * Math.sin(a)]
  })
  const seq = feed.filter((m) => !m.consultant && !m.injected && pos[m.speaker]).map((m) => m.speaker)
  const edges: Record<string, number> = {}
  for (let i = 1; i < seq.length; i++) {
    if (seq[i - 1] === seq[i]) continue
    const key = [seq[i - 1], seq[i]].sort().join('|')
    edges[key] = (edges[key] ?? 0) + 1
  }
  const maxE = Math.max(1, ...Object.values(edges))
  const curve = (a: string, b: string) => {
    const [x1, y1] = pos[a], [x2, y2] = pos[b]
    const mx = ((x1 + x2) / 2) * 0.7 + cx * 0.3, my = ((y1 + y2) / 2) * 0.7 + cy * 0.3
    return `M${x1.toFixed(1)},${y1.toFixed(1)} Q${mx.toFixed(1)},${my.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}`
  }
  const turns: Record<string, number> = {}
  seq.forEach((k) => (turns[k] = (turns[k] ?? 0) + 1))
  const spoke = order.filter((k) => turns[k])
  const c = stance ? countStances(stance, spoke) : null
  const last = seq.length > 1 && seq[seq.length - 2] !== seq[seq.length - 1] ? curve(seq[seq.length - 2], seq[seq.length - 1]) : null
  const key = (k: string) => (e: KeyboardEvent) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), onOpen(k))

  return (
    <svg viewBox="0 0 320 262" className="cc-radar" role="group" aria-label="Room map">
      <defs>
        {Object.entries(GRAD).map(([k, [c1, c2]]) => (
          <linearGradient key={k} id={`cc-g-${k}`} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor={c1} />
            <stop offset="1" stopColor={c2} />
          </linearGradient>
        ))}
      </defs>
      <g className="cc-rings" aria-hidden="true">
        {[34, 62, R, 120].map((r) => <circle key={r} cx={cx} cy={cy} r={r} />)}
        <line x1={cx - 124} y1={cy} x2={cx + 124} y2={cy} />
        <line x1={cx} y1={cy - 124} x2={cx} y2={cy + 124} />
      </g>
      <g aria-hidden="true">
        {Object.entries(edges).map(([k, w]) => {
          const [a, b] = k.split('|')
          return (
            <path key={k} d={curve(a, b)} className="cc-edge"
              style={{ strokeOpacity: 0.15 + (0.55 * w) / maxE, strokeWidth: 1 + (2.5 * w) / maxE }} />
          )
        })}
        {last && <path d={last} className="cc-edge-live" style={ph} />}
      </g>
      <circle cx={cx} cy={cy} r={27} className="cc-core" aria-hidden="true" />
      {c ? (
        <>
          <text x={cx} y={cy + 4} className="cc-cpct">{c.pct}%</text>
          <text x={cx} y={cy + 14} className="cc-clabel">SUPPORT</text>
        </>
      ) : (
        <text x={cx} y={cy + 3} className="cc-clabel">NO STANCE YET</text>
      )}
      {order.map((k) => {
        const [x, y] = pos[k]
        const r = 13 + Math.min(7, (turns[k] ?? 0) * 1.4)
        const st = stance?.[k]
        const slot = identityOf(k, order)
        return (
          <g key={k} className={`cc-node ${k === next ? 'cc-active' : ''}`} role="button" tabIndex={0}
            aria-label={`${k}: ${turns[k] ?? 0} turns${st ? `, ${WORD[st]}` : ''}. Open dossier`}
            onClick={() => onOpen(k)} onKeyDown={key(k)}
            style={{ '--ring': st ? STANCE_COLOR[st] : 'var(--line-hi)', ...(k === next ? ph : {}) } as Css}>
            <polygon points={hexPts(x, y, r + 4)} className="cc-halo" />
            <polygon points={hexPts(x, y, r)} fill={`url(#cc-g-${slot})`} />
            <text x={x} y={y + 3.5} className="cc-ntext">{initials(k)}</text>
            <text x={x} y={y + r + 15} className="cc-nlabel">{k.split(' ')[0].toUpperCase()}</text>
          </g>
        )
      })}
    </svg>
  )
}

function arcPath(cx: number, cy: number, r: number, a0: number, a1: number) {
  const p = (a: number) => `${(cx + r * Math.cos(a)).toFixed(1)},${(cy + r * Math.sin(a)).toFixed(1)}`
  return `M${p(a0)} A${r},${r} 0 ${a1 - a0 > Math.PI ? 1 : 0},1 ${p(a1)}`
}

/** Where the room ended (§4.5): a half-dial split by stance, % support, and the counts in words. */
export function StanceDial({ stance, among }: { stance: StanceMap; among?: string[] }) {
  const c = countStances(stance, among)
  const cx = 110, cy = 104, r = 80
  let a0 = Math.PI
  const segs = ORDER.map((k) => {
    const span = c.total ? (Math.PI * c[k]) / c.total : 0
    const d = c[k] ? arcPath(cx, cy, r, a0 + 0.025, a0 + span - 0.025) : ''
    a0 += span
    return d ? <path key={k} d={d} className="cc-g-seg" style={{ stroke: STANCE_COLOR[k], color: STANCE_COLOR[k] }} /> : null
  })
  const said = shown(c).map((k) => `${c[k]} ${WORD[k]}`).join(', ')
  return (
    <>
      <svg viewBox="0 0 220 116" className="cc-gauge" role="img"
        aria-label={`Where the room ended: ${supportLine(c).replace(' · ', ', ')}; ${said}`}>
        <path d={arcPath(cx, cy, r, Math.PI, 2 * Math.PI - 0.0001)} className="cc-g-track" />
        {segs}
        {Array.from({ length: 11 }, (_, i) => {
          const a = Math.PI + (i * Math.PI) / 10, o = i % 5 ? 14 : 18
          return (
            <line key={i} className="cc-g-tick"
              x1={cx + (r + 11) * Math.cos(a)} y1={cy + (r + 11) * Math.sin(a)}
              x2={cx + (r + o) * Math.cos(a)} y2={cy + (r + o) * Math.sin(a)} />
          )
        })}
        {/* The figure is ▲ alone; ◐ gets a line of its own under it rather than a share of the number. */}
        <text x={cx} y={cy - (c.conditional ? 24 : 14)} className="cc-g-pct">{c.pct}%</text>
        <text x={cx} y={cy - (c.conditional ? 8 : -2)} className="cc-g-lab">SUPPORT</text>
        {c.conditional > 0 && (
          <text x={cx} y={cy + 4} className="cc-g-lab" style={{ fill: STANCE_COLOR.conditional }}>
            +{c.pctConditional}% WITH CONDITIONS
          </text>
        )}
      </svg>
      <div className="cc-gcounts" aria-hidden="true">
        {shown(c).map((k) => (
          <div key={k} className={STANCE_CLASS[k]}>
            <b>{c[k]}</b>
            <span>{WORD[k]}</span>
          </div>
        ))}
      </div>
    </>
  )
}

/** Why the closing statement did not decide, in words, for a stance the summary decided. */
const FALLBACK_WHY: Record<NonNullable<StanceBasisEntry['fallback']>, string | null> = {
  no_closing_round: null,
  no_statement: 'they made no closing statement',
  classifier_failed: 'their closing statement could not be read (the classifier failed)',
  no_verdict: 'the classifier gave no usable verdict on their closing statement',
  unclear: 'their closing statement did not say whether they accept',
  unverified_quote: 'the sentence the classifier quoted is not in their statement, so its verdict was discarded',
}

/**
 * Why a persona has their stance (`matrix_studio/stance.py`): which source decided it, and the words it rests
 * on. A closing-statement quote is the persona's own words, checked to be in what they said; a summary
 * "holding out" quotes the summary's account of their objection, and says so.
 */
export function StanceWhy({ entry }: { entry: StanceBasisEntry }) {
  let source: string
  if (entry.source === 'closing') source = 'From their closing statement'
  else if (entry.stance === 'holding') source = "From the summary: named among its dissenters. In the summary's words"
  else if (entry.stance === 'support') source = 'From the summary: not a dissenter, and they said their position moved'
  else source = 'From the summary and the shift flags: nothing recorded says which way they went'
  const why = entry.source === 'summary' && entry.fallback ? FALLBACK_WHY[entry.fallback] : null
  return (
    <span className="cc-swhy">
      <span className="cc-sm cc-t2">
        {source}
        {entry.quote ? ': ' : '.'}
      </span>
      {entry.quote && <span className="cc-swhy-q">“{entry.quote}”</span>}
      {why && <span className="cc-muted cc-sm"> Not from the closing statement: {why}.</span>}
    </span>
  )
}

/** Each persona's stance with its basis, for "Where the room ended". */
export function StanceBasisList({
  basis, order,
}: { basis: Record<string, StanceBasisEntry>; order: string[] }) {
  const names = order.filter((n) => basis[n])
  if (!names.length) return null
  return (
    <ul className="cc-plain cc-swhy-list">
      {names.map((n) => (
        <li key={n}>
          <b>{n}</b> <span className={STANCE_CLASS[basis[n].stance]}>{STANCE_LABEL[basis[n].stance]}</span>
          <br />
          <StanceWhy entry={basis[n]} />
        </li>
      ))}
    </ul>
  )
}

/**
 * What the room map's marks mean, drawn with the marks themselves. The map is easy to over-read: a line
 * looks like "these two argued", and what it records is only that one spoke straight after the other.
 */
export function RoomMapKey({ hasStance }: { hasStance: boolean }) {
  const Row = ({ mark, children }: { mark: ReactNode; children: ReactNode }) => (
    <div className="cc-mapkey-row">
      <svg viewBox="0 0 40 16" width="40" height="16" aria-hidden="true" className="cc-radar">
        {mark}
      </svg>
      <span>{children}</span>
    </div>
  )
  return (
    <div className="cc-mapkey">
      <Row mark={<path d="M2,12 Q20,2 38,12" className="cc-edge" style={{ strokeOpacity: 0.7, strokeWidth: 3 }} />}>
        <b>A line</b> joins two people who spoke <b>one straight after the other</b>. Thicker and brighter = that
        hand-off happened more often. It shows who tends to follow whom, <i>not</i> who answered whom: the
        app does not record who a message replies to.
      </Row>
      <Row mark={<path d="M2,12 Q20,2 38,12" className="cc-edge-live" />}>
        <b>The moving dashed line</b> is the latest hand-off: the last two people to speak.
      </Row>
      <Row mark={<polygon points={hexPts(20, 8, 6)} fill="url(#cc-g-a2)" stroke="var(--t2)" />}>
        <b>A hexagon</b> is a persona. Bigger = more turns taken. A small one with few lines is someone the room
        left out. Tap one for their dossier.
      </Row>
      <Row mark={<polygon points={hexPts(20, 8, 7)} fill="none" stroke="var(--hold)" strokeWidth="2.2" />}>
        <b>The ring colour</b> is where they ended:{' '}
        {hasStance ? (
          <>
            ▲ support, ◐ with conditions, ◆ not stated, ▼ holding out. The centre is the share who support
            outright; those who accept with conditions are not counted in it.
          </>
        ) : (
          <>shown once the run is finished and summarised.</>
        )}
      </Row>
    </div>
  )
}
