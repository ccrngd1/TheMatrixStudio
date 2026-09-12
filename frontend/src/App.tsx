// SPDX-License-Identifier: Apache-2.0
import { useState } from 'react'
import { History } from './views/History'
import { NewRunForm } from './views/NewRunForm'
import { LiveView } from './views/LiveView'
import { AuthGate } from './views/AuthGate'

type View =
  | { name: 'history' }
  // `fromRunId` prefills the form from an existing run's setup. Held in the view
  // rather than inside the form so remounting on a different source re-loads it.
  | { name: 'new'; fromRunId?: string }
  | { name: 'run'; runId: string }

// Minimal client-side view switching — no router dependency needed for Phase 1.
export default function App() {
  return (
    <AuthGate>
      <Views />
    </AuthGate>
  )
}

// The views, wrapped by the gate so an unauthenticated visitor never reaches them. Split
// out rather than gated inside each case: three views today and a dozen panels, and the
// next one added would have to remember, whereas a wrapper cannot be forgotten.
function Views() {
  const [view, setView] = useState<View>({ name: 'history' })

  switch (view.name) {
    case 'new':
      return (
        <NewRunForm
          key={view.fromRunId ?? 'blank'}
          fromRunId={view.fromRunId}
          onStarted={(runId) => setView({ name: 'run', runId })}
          onCancel={() => setView({ name: 'history' })}
        />
      )
    case 'run':
      return (
        <LiveView
          runId={view.runId}
          onBack={() => setView({ name: 'history' })}
          onOpenRun={(runId) => setView({ name: 'run', runId })}
          onStartFresh={(runId) => setView({ name: 'new', fromRunId: runId })}
        />
      )
    case 'history':
    default:
      return (
        <History
          onOpen={(runId) => setView({ name: 'run', runId })}
          onNew={() => setView({ name: 'new' })}
        />
      )
  }
}
