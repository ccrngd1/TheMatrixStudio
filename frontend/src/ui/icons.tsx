// SPDX-License-Identifier: Apache-2.0
// Icon paths ported verbatim from docs/mockups/prototype.html (24-unit grid, 1.8 stroke).
import type { CSSProperties } from 'react'

export const ICONS = {
  runs: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  ensembles: '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
  knowledge: '<path d="M4 5a2 2 0 0 1 2-2h12v16H6a2 2 0 0 0-2 2z"/><path d="M4 19V5M8 7h6"/>',
  library: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c.6-3.4 3-5 6-5s5.4 1.6 6 5"/><circle cx="17" cy="9" r="2.5"/><path d="M16 14.2c2.6-.2 4.4 1.3 5 4.8"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  play: '<path d="M7 4.5v15l12-7.5z" fill="currentColor"/>',
  step: '<path d="M6 5v14l9-7z" fill="currentColor"/><path d="M18 5v14"/>',
  branch: '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 7v10M18 10c0 4-6 3-11 7"/>',
  doc: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 12h6M9 16h6"/>',
  chat: '<path d="M4 5h16v11H9l-5 4z"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3 2"/>',
  expert: '<path d="M2 9l10-5 10 5-10 5z"/><path d="M6 11v5c3 2.5 9 2.5 12 0v-5"/>',
  back: '<path d="M15 5l-7 7 7 7"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  more: '<circle cx="5" cy="12" r="1.3" fill="currentColor"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/><circle cx="19" cy="12" r="1.3" fill="currentColor"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>',
  download: '<path d="M12 4v12M7 11l5 5 5-5M4 20h16"/>',
  stop: '<rect x="6" y="6" width="12" height="12" fill="currentColor"/>',
  resume: '<path d="M4 12a8 8 0 1 0 3-6.2"/><path d="M4 4v4h4"/>',
  share: '<path d="M12 15V4M8 8l4-4 4 4M5 12v8h14v-8"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 8v.01"/>',
  spark: '<path d="M12 3l2 6 6 2-6 2-2 6-2-6-6-2 6-2z"/>',
  save: '<path d="M5 4h11l3 3v13H5z"/><path d="M8 4v5h7M8 20v-6h8v6"/>',
  model: '<rect x="5" y="5" width="14" height="14"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
  import: '<path d="M12 4v10M8 10l4 4 4-4"/><path d="M5 14v6h14v-6"/>',
  // Not in the prototype: drawn for hiding a run from the Runs list, on the same grid and stroke.
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/><path d="M4 4l16 16"/>',
} as const

export type IconName = keyof typeof ICONS

export function Icon({ name, size = 16, style }: { name: IconName; size?: number; style?: CSSProperties }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ flex: 'none', verticalAlign: -3, ...style }}
      aria-hidden="true"
      // The paths are static strings from this file, never user input.
      dangerouslySetInnerHTML={{ __html: ICONS[name] }}
    />
  )
}
