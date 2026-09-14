// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from 'react'
import { colorForName, initials } from '../lib/avatar'
import { loadAvatar } from '../api'

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
  // The URL is FETCHED rather than handed to the `<img>`, because an `<img src>` cannot
  // send an `Authorization` header and the avatar route requires one — see `loadAvatar`.
  // Cached there by URL, so a cast of eight faces is eight requests per session however
  // many times this component renders.
  const [fetched, setFetched] = useState<string | null>(null)
  useEffect(() => {
    if (!portraitUrl) {
      setFetched(null)
      return
    }
    let live = true
    loadAvatar(portraitUrl).then((url) => {
      if (live) setFetched(url)
    })
    return () => {
      live = false
    }
  }, [portraitUrl])

  // The fetched blob wins; inline base64 only ever appears on runs recorded before avatars
  // moved to blob storage, and supporting it is what keeps those rendering rather than
  // silently turning into placeholders.
  const src = fetched || (portrait ? `data:image/png;base64,${portrait}` : null)
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
