// SPDX-License-Identifier: Apache-2.0
// The stance surfaces (docs/MOBILE-UI.md §4.2, §4.3, §4.5, stage 5). Stance is post-run only (§6.1, decided):
// `matrix_studio/stance.py` writes it when the summary is generated, and a live run has none, so every
// component here renders nothing, or a "not yet" note, when it is absent.
//
// Never colour alone (§2 rule 2): every count carries its glyph, every bar and node its name for a screen
// reader, and the dial prints its counts beneath it.
import type { CSSProperties, KeyboardEvent } from 'react'
import type { FeedMessage } from '../../types'
import { STANCE_COLOR, STANCE_LABEL, Tag, identityOf, initials, type Stance } from '../../ui/primitives'
import { phase } from '../../ui/theme'

export type StanceMap = Record<string, Stance>
type Css = CSSProperties & Record<`--${string}`, string>
const ORDER: Stance[] = ['support', 'unstated', 'holding']
const GLYPH: Record<Stance, string> = { support: '▲', unstated: '◆', holding: '▼' }
const COUNT_CLASS: Record<Stance, string> = { support: 'cc-c-support', unstated: 'cc-c-undecided', holding: 'cc-c-holding' }
const WORD: Record<Stance, string> = { support: 'support', unstated: 'not stated', holding: 'holding out' }

export function countStances(stance: StanceMap, among?: string[]) {
  const names = among ?? Object.keys(stance)
  const c: Record<Stance, number> = { support: 0, unstated: 0, holding: 0 }
  for (const n of names) c[stance[n] ?? 'unstated']++
  return { ...c, total: names.length, pct: names.length ? Math.round((100 * c.support) / names.length) : 0 }
}

export function StanceCounts({ stance, among }: { stance: StanceMap; among?: string[] }) {
  const c = countStances(stance, among)
  return (
    <span className="cc-counts">
      {ORDER.map((k) => (
        <b key={k} className={COUNT_CLASS[k]}>
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

/** One slanted bar per persona, ordered support → not stated → holding out. */
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
      <div className="cc-rsub">{c.pct}% support</div>
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
  const ph = { '--ph': phase() } as Css
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

/** Where the room ended (§4.5): a half-dial split by stance, % support, and the three counts in words. */
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
  return (
    <>
      <svg viewBox="0 0 220 116" className="cc-gauge" role="img"
        aria-label={`Where the room ended: ${c.pct}% support; ${c.support} support, ${c.unstated} not stated, ${c.holding} holding out`}>
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
        <text x={cx} y={cy - 14} className="cc-g-pct">{c.pct}%</text>
        <text x={cx} y={cy + 2} className="cc-g-lab">SUPPORT</text>
      </svg>
      <div className="cc-gcounts" aria-hidden="true">
        {ORDER.map((k) => (
          <div key={k} className={COUNT_CLASS[k]}>
            <b>{c[k]}</b>
            <span>{WORD[k]}</span>
          </div>
        ))}
      </div>
    </>
  )
}
