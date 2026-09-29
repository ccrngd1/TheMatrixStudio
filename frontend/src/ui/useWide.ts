// SPDX-License-Identifier: Apache-2.0
// True from 768 px (docs/MOBILE-UI.md §4.11), where the run's tabs become three columns. The CSS switches the
// layout on its own; this is for the one thing CSS cannot do, which is mount the panes a phone does not show.
import { useEffect, useState } from 'react'

export const WIDE_QUERY = '(min-width: 768px)'

const query = () => (typeof window !== 'undefined' && window.matchMedia ? window.matchMedia(WIDE_QUERY) : null)

export function useWide(): boolean {
  const [wide, setWide] = useState(() => query()?.matches ?? false)
  useEffect(() => {
    const mq = query()
    if (!mq) return
    const on = () => setWide(mq.matches)
    mq.addEventListener?.('change', on)
    return () => mq.removeEventListener?.('change', on)
  }, [])
  return wide
}
