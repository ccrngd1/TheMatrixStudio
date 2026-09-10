// SPDX-License-Identifier: Apache-2.0
import { colorForName, initials } from '../lib/avatar'

interface Props {
  name: string
  /** Legacy base64 PNG. Only runs recorded before avatars moved to blob storage have it. */
  portrait: string | null
  /** URL of the stored image — the current path. Preferred when both are present. */
  portraitUrl?: string | null
  size?: number
  ring?: boolean
}

// Renders the real portrait if present, else a deterministic initials/color
// placeholder. Avatars are optional eye-candy — a card ALWAYS renders.
export function AvatarBadge({
  name, portrait, portraitUrl, size = 56, ring = false,
}: Props) {
  const dim = { width: size, height: size }
  const ringCls = ring ? 'ring-2 ring-matrix-live' : 'ring-1 ring-matrix-border'
  // A URL wins over inline base64: the URL is the current path, and base64 only ever
  // appears on runs recorded before the change. Supporting both is what keeps those
  // runs' avatars rendering instead of silently turning into placeholders.
  const src = portraitUrl || (portrait ? `data:image/png;base64,${portrait}` : null)
  if (src) {
    return (
      <img
        src={src}
        alt={name}
        style={dim}
        className={`rounded-full object-cover ${ringCls}`}
      />
    )
  }
  return (
    <div
      style={{ ...dim, backgroundColor: colorForName(name) }}
      className={`flex items-center justify-center rounded-full font-semibold text-white ${ringCls}`}
      aria-label={`${name} placeholder avatar`}
    >
      {initials(name)}
    </div>
  )
}
