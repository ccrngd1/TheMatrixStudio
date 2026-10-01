// SPDX-License-Identifier: Apache-2.0
// The command-centre primitives (docs/MOBILE-UI.md §5, §7 stage 0): thin React wrappers over the classes in
// styles/command.css, so a screen composes the design system rather than restating it. Each takes `className`
// for layout and passes other props through.
import type { ButtonHTMLAttributes, CSSProperties, HTMLAttributes, ReactNode } from 'react'
import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from './icons'
import { phase } from './theme'

const cx = (...parts: (string | false | null | undefined)[]) => parts.filter(Boolean).join(' ')
type Css = CSSProperties & Record<`--${string}`, string | number>

// ─── identity ─────────────────────────────────────────────────────────────────────────────────────────────

/** Persona identity colours: fixed pastels that do not change with the theme (§5.1). */
export const IDENTITY = ['a1', 'a2', 'a3', 'a4', 'a5', 'a6'] as const
const IDENTITY_TEXT: Record<string, string> = {
  a1: '#f0abfc', a2: '#7dd3fc', a3: '#fcd34d', a4: '#86efac', a5: '#fda4af', a6: '#c7d2fe', a0: '#94a3b8',
}

/** A stable identity slot for a persona: the order the cast declares, cycling after six. */
export function identityOf(name: string, cast: string[]): string {
  const i = cast.indexOf(name)
  return i < 0 ? 'a0' : IDENTITY[i % IDENTITY.length]
}

export const identityColor = (slot: string) => IDENTITY_TEXT[slot] ?? IDENTITY_TEXT.a0

export const initials = (name: string) =>
  name.split(/\s+/).filter(Boolean).map((w) => w[0]).join('').slice(0, 2).toUpperCase()

// ─── stance (§6.1, decided: post-run only) ────────────────────────────────────────────────────────────────

/**
 * Where a persona ended up. `unstated` is not "undecided": nothing recorded says which way they went.
 * `conditional` (2026-10-01) is a closing statement that accepts with conditions: signed, but on something not
 * yet done, or with an objection of their own left standing. A state of its own because folding it into support
 * hides the conditions, and into holding hides the signature.
 */
export type Stance = 'support' | 'conditional' | 'unstated' | 'holding'
export const STANCE_COLOR: Record<Stance, string> = {
  support: 'var(--support)',
  conditional: 'var(--conditional)',
  unstated: 'var(--undecided)',
  holding: 'var(--hold)',
}
export const STANCE_LABEL: Record<Stance, string> = {
  support: '▲ support',
  conditional: '◐ with conditions',
  unstated: '◆ not stated',
  holding: '▼ holding out',
}

// ─── panels and controls ──────────────────────────────────────────────────────────────────────────────────

export type RunState = 'running' | 'stopping' | 'complete' | 'stopped' | 'capped' | 'ensemble'

/** The chamfered panel. `edge` paints the left status edge; `live` adds the sweeping border. */
export function Panel({
  children, className, edge, hero, live, center, as = 'div', style, ...rest
}: HTMLAttributes<HTMLElement> & {
  edge?: RunState; hero?: boolean; live?: boolean; center?: boolean; as?: 'div' | 'section' | 'article'
}) {
  const Tag = as
  return (
    <Tag
      className={cx('cc-card', hero && 'cc-hero', live && 'cc-livecard', center && 'cc-center', className)}
      data-s={edge}
      style={live ? ({ '--ph': phase(), ...style } as Css) : style}
      {...rest}
    >
      {children}
    </Tag>
  )
}

/** A panel that is a button: the whole card is the tap target. */
export function PanelButton({
  children, className, edge, live, style, ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { edge?: RunState; live?: boolean }) {
  return (
    <button
      type="button"
      className={cx('cc-card', live && 'cc-livecard', className)}
      data-s={edge}
      style={live ? ({ '--ph': phase(), ...style } as Css) : style}
      {...rest}
    >
      {children}
    </button>
  )
}

export function Btn({
  children, className, variant, size, full, type = 'button', ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'danger' | 'warn'; size?: 'sm'; full?: boolean
}) {
  return (
    <button
      type={type}
      className={cx('cc-btn', variant && `cc-${variant}`, size && `cc-${size}`, full && 'cc-full', className)}
      style={variant === 'primary' ? ({ '--ph': phase() } as Css) : undefined}
      {...rest}
    >
      {children}
    </button>
  )
}

export type TagTone = 'live' | 'ok' | 'warn' | 'danger' | 'ens' | 'shiftc'
export function Tag({
  children, tone, color, pulse, className,
}: { children: ReactNode; tone?: TagTone; color?: string; pulse?: boolean; className?: string }) {
  return (
    <span className={cx('cc-tag', tone && `cc-${tone}`, className)} style={color ? ({ '--c': color } as Css) : undefined}>
      {pulse && <i className="cc-pulse" style={{ '--ph': phase() } as Css} />}
      {children}
    </span>
  )
}

export function Chip({
  children, on, className, onClick, ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { on?: boolean }) {
  if (!onClick) return <span className={cx('cc-chip', on && 'cc-on', className)}>{children}</span>
  return (
    <button type="button" className={cx('cc-chip', on && 'cc-on', className)} onClick={onClick} {...rest}>
      {children}
    </button>
  )
}

/** The `// LABEL` heading used for every section. Mono, tracked, upper-case (§5.2). */
export function Label({ children, className, as = 'p' }: { children: ReactNode; className?: string; as?: 'p' | 'h2' | 'h3' }) {
  const Tag = as
  return <Tag className={cx('cc-label', className)}>{children}</Tag>
}

// ─── data ─────────────────────────────────────────────────────────────────────────────────────────────────

/** One tick per turn while that is legible; a plain meter once there are too many to count. */
export function Ticks({ n, max, live }: { n: number; max: number; live?: boolean }) {
  if (max > 30) return <Meter value={max ? n / max : 0} />
  return (
    <div className={cx('cc-ticks', live && 'cc-livet')} role="img" aria-label={`${n} of ${max} turns`}>
      {Array.from({ length: max }, (_, i) => (
        <i
          key={i}
          className={cx(i < n && 'cc-on', live && i === n - 1 && 'cc-head')}
          style={live && i === n - 1 ? ({ '--ph': phase() } as Css) : undefined}
        />
      ))}
    </div>
  )
}

export function Meter({ value, warn }: { value: number; warn?: boolean }) {
  const pct = Math.max(0, Math.min(1, value)) * 100
  return (
    <div className={cx('cc-meter', warn && 'cc-warn')} role="meter" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
      <i style={{ width: `${pct}%` }} />
    </div>
  )
}

/** A persona's hex token. `ring` is a stance or identity colour; `active` glows green (the next speaker). */
export function Hex({
  name, slot, size, ring, active,
}: { name?: string; slot: string; size?: 'xs' | 'sm' | 'lg'; ring?: string; active?: boolean }) {
  return (
    <span
      className={cx('cc-hx', size && `cc-${size}`, active && 'cc-active')}
      style={{ '--ring': ring ?? 'var(--line-hi)', ...(active ? { '--ph': phase() } : {}) } as Css}
      aria-label={name}
    >
      <i className={`cc-${name ? slot : 'a0'}`}>{name ? initials(name) : '◆'}</i>
    </span>
  )
}

export function Toggle({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      className={cx('cc-toggle', on && 'cc-on')}
      onClick={() => onChange(!on)}
    />
  )
}

/** A segmented control: a few mutually exclusive options. */
export function Seg<T extends string>({
  options, value, onChange, label,
}: { options: { value: T; label: ReactNode }[]; value: T; onChange: (v: T) => void; label: string }) {
  return (
    <div className="cc-seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          className={cx(o.value === value && 'cc-on')}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

/** The three-cell summary strip every run surface leads with (§4.2). */
export function HudStrip({ children, className }: { children: ReactNode; className?: string }) {
  return <Panel className={cx('cc-hudstrip', className)}>{children}</Panel>
}

export function HudCell({
  label, value, sub, right, children,
}: { label: ReactNode; value?: ReactNode; sub?: ReactNode; right?: ReactNode; children?: ReactNode }) {
  return (
    <div>
      <div className="cc-rk">
        <span>{label}</span>
        {right}
      </div>
      {value !== undefined && <div className="cc-readout">{value}</div>}
      {children}
      {sub !== undefined && <div className="cc-rsub">{sub}</div>}
    </div>
  )
}

// ─── sheets ───────────────────────────────────────────────────────────────────────────────────────────────

/**
 * A bottom sheet on a phone, a 450 px side panel from 768 px (§4.11) — one component, rearranged by CSS.
 * Escape and the scrim close it. `title` is the sheet's accessible name.
 */
export function Sheet({
  title, onClose, children, tall, header,
}: { title: string; onClose: () => void; children: ReactNode; tall?: boolean; header?: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  // Portalled to the app's overlay layer: a sheet opened from inside a scrolled or clipped container (a Hint in
  // a form, say) must still cover the screen, not the container. Falls back to <body> outside the shell.
  const host = document.getElementById('cc-overlay') ?? document.body
  return createPortal(
    <>
      <div className="cc-scrim" onClick={onClose} aria-hidden="true" />
      <div className={cx('cc-sheet', tall && 'cc-tall')} role="dialog" aria-modal="true" aria-label={title}>
        <div className="cc-sh-h">
          <div className="cc-grow">{header ?? <b>{title}</b>}</div>
          <button type="button" className="cc-icon" onClick={onClose} aria-label="Close">
            <Icon name="close" size={18} />
          </button>
        </div>
        <div className="cc-sbody">{children}</div>
      </div>
    </>,
    host,
  )
}
