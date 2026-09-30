// SPDX-License-Identifier: Apache-2.0
// The app shell (docs/MOBILE-UI.md §3, §7 stage 1): the command-centre frame, the four-item dock, the top bar,
// the settings sheet (theme, FX, sign out) and the overlay layer every sheet is portalled into.
import { useState, type ReactNode } from 'react'
import { navigate, type Route } from '../lib/route'
import { Icon, type IconName } from '../ui/icons'
import { Label, Seg, Sheet, Toggle } from '../ui/primitives'
import { THEMES, useThemeState, type Theme } from '../ui/theme'
import { useSession } from './AuthGate'
import { Rain } from '../ui/Rain'

export type DockItem = 'runs' | 'ensembles' | 'knowledge' | 'library'
const DOCK: { id: DockItem; label: string; icon: IconName }[] = [
  { id: 'runs', label: 'Runs', icon: 'runs' },
  { id: 'ensembles', label: 'Ensembles', icon: 'ensembles' },
  { id: 'knowledge', label: 'Knowledge', icon: 'knowledge' },
  { id: 'library', label: 'Library', icon: 'library' },
]

/** The frame: fills the viewport, holds the view and the overlay layer sheets render into. */
export function AppFrame({ children, fx, theme = 'holo' }: { children: ReactNode; fx: boolean; theme?: Theme }) {
  return (
    <div className={`cc-app h-screen w-full ${fx ? '' : 'cc-nofx'}`} style={{ height: '100dvh' }}>
      <Rain fx={fx} theme={theme} />
      <div id="cc-view">{children}</div>
      <div id="cc-overlay" />
    </div>
  )
}

/** The only persistent navigation (§3). Shown on the four list screens, not inside a run. */
export function Dock({ active }: { active: DockItem }) {
  return (
    <nav className="cc-tabs" aria-label="Main">
      {DOCK.map((d) => (
        <button
          key={d.id}
          type="button"
          className={d.id === active ? 'cc-on' : undefined}
          aria-current={d.id === active ? 'page' : undefined}
          onClick={() => navigate({ name: d.id } as Route)}
        >
          <Icon name={d.icon} size={20} />
          {d.label}
        </button>
      ))}
    </nav>
  )
}

/** The top bar. `brand` is the studio mark on the home screen; `code` sets a run codename in mono. */
export function TopBar({
  title, sub, back, right, brand, code,
}: {
  title: string; sub?: ReactNode; back?: () => void; right?: ReactNode; brand?: boolean; code?: boolean
}) {
  return (
    <header className="cc-top">
      {back && (
        <button type="button" className="cc-back" onClick={back} aria-label="Back">
          <Icon name="back" size={18} />
        </button>
      )}
      <div className="cc-title">
        {brand ? (
          <div className="cc-brand">
            MATRIX<em>//</em>STUDIO<small>{sub}</small>
          </div>
        ) : (
          <>
            <b className={code ? 'cc-code' : 'cc-disp'}>{title}</b>
            {sub && <div className="cc-sub">{sub}</div>}
          </>
        )}
      </div>
      {right}
    </header>
  )
}

/** Theme, effects and sign-out. Opened from the gear on the home screen. */
export function SettingsButton({ theme, setTheme, fx, setFx }: ReturnType<typeof useThemeState>) {
  const [open, setOpen] = useState(false)
  const { signOut } = useSession()
  return (
    <>
      <button type="button" className="cc-icon" aria-label="Settings" onClick={() => setOpen(true)}>
        <Icon name="gear" size={20} />
      </button>
      {open && (
        <Sheet title="Settings" onClose={() => setOpen(false)}>
          <Label>Theme</Label>
          <Seg<Theme>
            label="Theme"
            value={theme}
            onChange={setTheme}
            options={THEMES.map((t) => ({ value: t, label: t }))}
          />
          <p className="cc-muted">A theme swaps the palette and nothing else.</p>
          <div className="cc-setting">
            <span>
              Effects
              <span className="cc-muted block">The animated grid and scanlines behind the panels.</span>
            </span>
            <Toggle on={fx} onChange={setFx} label="Effects" />
          </div>
          {signOut && (
            <button type="button" className="cc-btn cc-danger cc-full mt-2" onClick={signOut}>
              Sign out
            </button>
          )}
        </Sheet>
      )}
    </>
  )
}

/** A list screen: top bar, a scrolling body, the dock. */
export function ListScreen({
  top, dock, children, fab,
}: { top: ReactNode; dock: DockItem; children: ReactNode; fab?: ReactNode }) {
  return (
    <>
      {top}
      <main className="cc-scroll">{children}</main>
      {fab && <div className="cc-fabwrap">{fab}</div>}
      <Dock active={dock} />
    </>
  )
}
