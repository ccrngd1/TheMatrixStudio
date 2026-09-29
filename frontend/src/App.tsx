// SPDX-License-Identifier: Apache-2.0
import { History } from './views/History'
import { NewRunForm } from './views/NewRunForm'
import { LiveView } from './views/LiveView'
import { AuthGate } from './views/AuthGate'
import { KnowledgeBases } from './views/KnowledgeBases'
import { EnsembleView } from './views/EnsembleView'
import { EnsembleList } from './views/EnsembleList'
import { Library } from './views/Library'
import { AppFrame, ListScreen, SettingsButton, TopBar } from './views/Shell'
import { navigate, useRoute, type Route } from './lib/route'
import { useThemeState } from './ui/theme'
import { Btn } from './ui/primitives'
import { Icon } from './ui/icons'

export default function App() {
  const themeState = useThemeState()
  return (
    <AuthGate>
      <AppFrame fx={themeState.fx}>
        <Views themeState={themeState} />
      </AppFrame>
    </AuthGate>
  )
}

const go = (r: Route) => navigate(r)

// The views, wrapped by the gate so an unauthenticated visitor never reaches them. Routed by the URL hash
// (lib/route.ts, docs/MOBILE-UI.md §3), so the back button and deep links work.
function Views({ themeState }: { themeState: ReturnType<typeof useThemeState> }) {
  const route = useRoute()
  const settings = <SettingsButton {...themeState} />

  switch (route.name) {
    case 'new':
      return (
        <NewRunForm
          key={route.fromRunId ?? 'blank'}
          fromRunId={route.fromRunId}
          step={route.step}
          onStep={(step) => navigate({ name: 'new', step, fromRunId: route.fromRunId })}
          onStarted={(runId) => go({ name: 'run', runId, tab: 'conversation' })}
          onEnsembleStarted={(ensembleId) => go({ name: 'ensemble', ensembleId })}
          onCancel={() => window.history.back()}
        />
      )
    case 'run':
    case 'scrub':
      return (
        <LiveView
          key={route.runId}
          runId={route.runId}
          tab={route.name === 'run' ? route.tab : 'conversation'}
          scrub={route.name === 'scrub'}
          onBack={() => (route.name === 'scrub' ? go({ name: 'run', runId: route.runId, tab: 'conversation' }) : go({ name: 'runs' }))}
          onOpenRun={(runId) => go({ name: 'run', runId, tab: 'conversation' })}
          onStartFresh={(runId) => go({ name: 'new', fromRunId: runId })}
        />
      )
    case 'ensemble':
      return (
        <div className="cc-legacy">
          <EnsembleView
            key={route.ensembleId}
            ensembleId={route.ensembleId}
            onBack={() => go({ name: 'ensembles' })}
            onOpenRun={(runId) => go({ name: 'run', runId, tab: 'conversation' })}
          />
        </div>
      )
    case 'ensembles':
      return (
        <ListScreen dock="ensembles" top={<TopBar title="Ensembles" sub="groups of runs, counted per group" right={settings} />}>
          <EnsembleList
            onOpen={(ensembleId) => go({ name: 'ensemble', ensembleId })}
            onNew={() => go({ name: 'new' })}
          />
        </ListScreen>
      )
    case 'knowledge':
      return (
        <ListScreen dock="knowledge" top={<TopBar title="Knowledge" sub="collections personas search" right={settings} />}>
          <KnowledgeBases onBack={() => go({ name: 'runs' })} />
        </ListScreen>
      )
    case 'library':
      return (
        <ListScreen dock="library" top={<TopBar title="Library" sub="archetypes and saved casts" right={settings} />}>
          <Library onNewRun={() => go({ name: 'new' })} />
        </ListScreen>
      )
    case 'runs':
    default:
      return (
        <ListScreen
          dock="runs"
          top={<TopBar title="Matrix Studio" brand sub="command centre" right={settings} />}
          fab={
            <Btn variant="primary" onClick={() => go({ name: 'new' })}>
              <Icon name="plus" /> New run
            </Btn>
          }
        >
          <History
            onOpen={(runId) => go({ name: 'run', runId, tab: 'conversation' })}
            onOpenEnsemble={(ensembleId) => go({ name: 'ensemble', ensembleId })}
          />
        </ListScreen>
      )
  }
}
