// SPDX-License-Identifier: Apache-2.0
// The inline notice on a persona or consultant whose name was switched because it was a real public figure's
// (`lib/realNames.ts`). On the row itself, not in a banner: the user is looking at the name that changed.
import { renamedMessage } from '../lib/realNames'
import type { Renamed } from '../types'

export function RenamedNotice({ renamed, kind = 'persona' }: { renamed: Renamed; kind?: 'persona' | 'consultant' }) {
  return (
    // A status, so a screen reader hears it when it appears: the field it is about changed under the user.
    <p role="status" className="mt-2 rounded border border-amber-500/40 bg-amber-950/30 px-2 py-1 text-xs text-amber-200">
      {renamedMessage(renamed, kind)}
    </p>
  )
}
