// SPDX-License-Identifier: Apache-2.0
import { colorForName, initials } from '../lib/avatar'

interface Props {
  name: string
  portrait: string | null
  size?: number
  ring?: boolean
}

// Renders the real portrait if present, else a deterministic initials/color
// placeholder. Avatars are optional eye-candy — a card ALWAYS renders.
export function AvatarBadge({ name, portrait, size = 56, ring = false }: Props) {
  const dim = { width: size, height: size }
  const ringCls = ring ? 'ring-2 ring-matrix-live' : 'ring-1 ring-matrix-border'
  if (portrait) {
    return (
      <img
        src={`data:image/png;base64,${portrait}`}
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
