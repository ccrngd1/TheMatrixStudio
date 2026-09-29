// SPDX-License-Identifier: Apache-2.0
// What sits around a run's panes (docs/MOBILE-UI.md §4.2): the status tag, the HUD, the tabs and the face
// strip. The HUD's third cell, the room, waits for stage 5 (§6.1): stance is only known once a run ends.
import type { ReactNode } from 'react'
import { hrefOf, type RunTab } from '../../lib/route'
import { Hex, HudCell, HudStrip, Meter, Tag, Ticks, identityOf } from '../../ui/primitives'

const pad = (n: number) => String(n).padStart(2, '0')

/** The header's status. Every state is a word, so none depends on its colour. */
export function RunStatusTag({
  status, stalled, turn, max,
}: { status: string; stalled?: boolean; turn: number; max?: number }) {
  if (status === 'running' && stalled) return <Tag tone="warn">⚠ Stalled</Tag>
  switch (status) {
    case 'running':
      return (
        <Tag tone="live" pulse>
          Live {pad(turn)}
          {max ? `/${pad(max)}` : ''}
        </Tag>
      )
    case 'complete':
      return <Tag tone="ok">✓ Complete</Tag>
    case 'stopped':
      return <Tag tone="warn">■ Stopped</Tag>
    case 'capped':
      return <Tag tone="danger">$ Capped</Tag>
    case 'failed':
      return <Tag tone="danger">✕ Failed</Tag>
    case 'interrupted':
      return <Tag tone="warn">Interrupted</Tag>
    case 'idle':
      return <Tag>Connecting</Tag>
    default:
      return <Tag>{status ? status[0].toUpperCase() + status.slice(1) : 'Unknown'}</Tag>
  }
}

/** Turn and spend, the first two things a watcher asks (§2 rule 3). */
export function RunHud({
  turn, max, live, cost, tokensIn, tokensOut,
}: { turn: number; max?: number; live: boolean; cost: number; tokensIn: number; tokensOut: number }) {
  const k = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : String(n))
  return (
    <HudStrip className="cc-runhud">
      <HudCell
        label="Turn"
        value={
          <>
            {pad(turn)}
            {max ? <small>/{pad(max)}</small> : null}
          </>
        }
      >
        {max ? <Ticks n={Math.min(turn, max)} max={max} live={live} /> : <Meter value={0} />}
      </HudCell>
      <HudCell label="Spend" value={`$${cost.toFixed(cost < 1 ? 4 : 2)}`} sub={`${k(tokensIn)} in / ${k(tokensOut)} out`} />
    </HudStrip>
  )
}

/** Conversation · Cast · Analysis. Links, so each tab has a URL and the back button steps through them. */
export function RunTabs({ runId, tab, locked }: { runId: string; tab: RunTab; locked: boolean }) {
  const t = (id: RunTab, label: ReactNode) => (
    <a
      href={hrefOf({ name: 'run', runId, tab: id })}
      className={tab === id ? 'cc-on' : undefined}
      aria-current={tab === id ? 'page' : undefined}
      role="tab"
      aria-selected={tab === id}
    >
      {label}
    </a>
  )
  return (
    <nav className="cc-htabs cc-runtabs" role="tablist" aria-label="Run views">
      {t('conversation', 'Conversation')}
      {t('cast', 'Cast')}
      {t(
        'analysis',
        locked ? (
          <>
            Analysis <span className="cc-lock" aria-label="locked until the run ends">◇</span>
          </>
        ) : (
          'Analysis'
        ),
      )}
    </nav>
  )
}

/** The cast as tokens. The next speaker glows; a tap opens the dossier. */
export function FaceStrip({
  order, next, onOpen,
}: { order: string[]; next: string | null; onOpen: (name: string) => void }) {
  if (order.length === 0) return null
  return (
    <div className="cc-faces">
      {order.map((name) => (
        <button key={name} type="button" onClick={() => onOpen(name)} aria-label={`${name}: open dossier`}>
          <Hex name={name} slot={identityOf(name, order)} size="sm" active={name === next} />
        </button>
      ))}
      <span className="cc-hint" aria-hidden="true">
        Tap → dossier
      </span>
    </div>
  )
}
