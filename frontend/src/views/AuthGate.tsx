// SPDX-License-Identifier: Apache-2.0
// Nothing renders until the user is authenticated.
//
// A gate rather than per-view checks. The app has three views and a dozen panels, and any
// of them added later would need remembering — whereas a gate that wraps the whole app
// cannot be forgotten by a new view. It is the client-side analogue of the server's
// single authorisation chokepoint, and it is worth being clear that it is NOT the
// boundary: every API route is 401 without a token regardless. The gate stops an
// unauthenticated visitor seeing an application shell that cannot work.

import { useCallback, useEffect, useState } from 'react'
import { setTokenProvider, setUnauthorizedHandler } from '../api'
import {
  beginLogin,
  clearTokens,
  codeFromUrl,
  completeLogin,
  errorFromUrl,
  loadTokens,
  logout,
  validToken,
} from '../lib/auth'
import { loadConfig, type RuntimeConfig } from '../lib/config'

type State =
  | { name: 'loading' }
  | { name: 'login'; config: RuntimeConfig; error?: string }
  | { name: 'ready'; config: RuntimeConfig | null }
  | { name: 'broken'; error: string }

export function AuthGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<State>({ name: 'loading' })

  useEffect(() => {
    let alive = true

    const start = async () => {
      let config: RuntimeConfig
      try {
        config = await loadConfig()
      } catch (err) {
        // Fails CLOSED: a config that cannot be read must not become "no login needed",
        // which is exactly how a deployment ends up open to the world.
        if (alive) {
          setState({
            name: 'broken',
            error: `Could not load configuration: ${(err as Error).message}`,
          })
        }
        return
      }

      if (!config.authRequired) {
        // The local single-user tool. No pool, no token, and the API resolves the
        // identity itself.
        if (alive) setState({ name: 'ready', config: null })
        return
      }

      // Cognito rejected the attempt, or the user cancelled at the Hosted UI.
      const oauthError = errorFromUrl()
      if (oauthError) {
        history.replaceState({}, '', window.location.pathname)
        if (alive) setState({ name: 'login', config, error: oauthError })
        return
      }

      const code = codeFromUrl()
      if (code) {
        try {
          await completeLogin(config, code)
        } catch (err) {
          history.replaceState({}, '', window.location.pathname)
          if (alive) {
            setState({ name: 'login', config, error: (err as Error).message })
          }
          return
        }
        // Strip `?code=` so a reload does not retry a consumed code — which Cognito
        // rejects, and which would look like a broken login rather than a stale URL.
        history.replaceState({}, '', window.location.pathname)
      }

      if (!loadTokens()) {
        if (alive) setState({ name: 'login', config })
        return
      }
      const token = await validToken(config)
      if (!token) {
        if (alive) setState({ name: 'login', config })
        return
      }

      setTokenProvider(() => validToken(config))
      setUnauthorizedHandler(() => {
        // A token the API refuses is a dead session. Clearing and re-rendering sends the
        // user to the login screen instead of leaving 401s in every panel.
        clearTokens()
        setTokenProvider(null)
        setState({ name: 'login', config, error: 'Your session expired.' })
      })
      if (alive) setState({ name: 'ready', config })
    }

    void start()
    return () => {
      alive = false
    }
  }, [])

  const signIn = useCallback((config: RuntimeConfig) => {
    void beginLogin({ hostedUiUrl: config.hostedUiUrl, clientId: config.clientId })
  }, [])

  if (state.name === 'loading') {
    return (
      <div className="flex min-h-screen items-center justify-center text-slate-400">
        Loading…
      </div>
    )
  }

  if (state.name === 'broken') {
    return (
      <div className="mx-auto max-w-lg p-8 text-center">
        <h1 className="mb-3 text-xl font-bold text-red-300">Configuration error</h1>
        <p className="text-sm text-slate-400">{state.error}</p>
      </div>
    )
  }

  if (state.name === 'login') {
    const usable = state.config.hostedUiUrl && state.config.clientId
    return (
      <div className="flex min-h-screen items-center justify-center p-6">
        <div className="w-full max-w-sm rounded-lg border border-matrix-border bg-matrix-panel p-6 text-center">
          <h1 className="mb-2 text-xl font-bold text-slate-100">
            TheMatrix Simulation Studio
          </h1>
          <p className="mb-5 text-sm text-slate-400">Sign in to continue.</p>
          {state.error && (
            <p className="mb-4 rounded border border-red-900/60 bg-red-900/20 p-2 text-xs text-red-300">
              {state.error}
            </p>
          )}
          {usable ? (
            <button
              onClick={() => signIn(state.config)}
              className="w-full rounded-lg bg-matrix-accent px-4 py-2 font-semibold text-matrix-bg hover:bg-sky-400"
            >
              Sign in
            </button>
          ) : (
            // A login button that cannot work is worse than none: it sends the user to a
            // broken redirect and tells them nothing.
            <p className="text-xs text-amber-300">
              This deployment requires a login but its configuration names no user
              pool. Check <code>config.json</code>.
            </p>
          )}
        </div>
      </div>
    )
  }

  return (
    <>
      {state.config && (
        <button
          onClick={() => logout(state.config as RuntimeConfig)}
          title="Sign out"
          className="fixed right-3 top-3 z-50 rounded border border-matrix-border bg-matrix-panel px-2 py-1 text-xs text-slate-400 hover:text-slate-100"
        >
          Sign out
        </button>
      )}
      {children}
    </>
  )
}
