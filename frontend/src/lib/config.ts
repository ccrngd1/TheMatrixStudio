// SPDX-License-Identifier: Apache-2.0
// Runtime configuration, fetched rather than compiled in.
//
// The pool id, client id and Hosted UI domain differ per deployment and are only known
// after `cdk deploy`. Baking them in with `VITE_*` would make the JS bundle
// environment-specific: the same artefact could not be promoted between accounts, and
// every redeploy of a new stack would need a rebuild. So the deploy writes
// `config.json` next to the bundle and this reads it once at start-up.
//
// None of these are secrets. A PKCE public client's id appears in the URL of every login
// redirect, and the pool id is in the issuer of every token. What would be a secret is a
// client SECRET, which this client deliberately does not have.

export interface RuntimeConfig {
  hostedUiUrl: string
  clientId: string
  userPoolId: string
  /**
   * Whether the deployment requires a login.
   *
   * False for the local single-user tool, where uvicorn serves the SPA and the API from
   * one origin with `AUTH_MODE=single-user` and there is no pool to log in to. The flag
   * exists so the login gate is not a special case the local path has to route around —
   * and it FAILS CLOSED: a missing or unreadable `config.json` on a deployment is treated
   * as "login required", because the alternative is showing the app to anybody when the
   * config fetch fails.
   */
  authRequired: boolean
}

const LOCAL: RuntimeConfig = {
  hostedUiUrl: '',
  clientId: '',
  userPoolId: '',
  authRequired: false,
}

let cached: RuntimeConfig | null = null

/**
 * Load `config.json`. Cached, so the app pays one request.
 *
 * A 404 means the local tool, which ships no config file — that is the ONE case treated
 * as "no auth", and it is distinguishable from a failure because the server answered.
 * Anything else (a 5xx, a network error, malformed JSON) is treated as auth REQUIRED
 * with no usable settings, so the app shows an error rather than its contents.
 */
export async function loadConfig(): Promise<RuntimeConfig> {
  if (cached) return cached
  try {
    const res = await fetch('/config.json', { cache: 'no-store' })
    if (res.status === 404) {
      cached = LOCAL
      return cached
    }
    if (!res.ok) throw new Error(`config.json returned ${res.status}`)
    const body = (await res.json()) as Partial<RuntimeConfig>
    // A deployment's config.json must name a pool. Missing fields with `authRequired`
    // true would render a login button that cannot work, so this is validated rather
        // than defaulted.
    cached = {
      hostedUiUrl: String(body.hostedUiUrl ?? ''),
      clientId: String(body.clientId ?? ''),
      userPoolId: String(body.userPoolId ?? ''),
      authRequired: body.authRequired !== false,
    }
    return cached
  } catch (err) {
    // Fail CLOSED. A config fetch that failed for any reason other than a clean 404 must
    // not become "no login needed" — that is precisely how a deployment ends up open.
    cached = { ...LOCAL, authRequired: true }
    throw err
  }
}

/** Reset the cache. Tests only; there is no reason to re-read config at runtime. */
export function resetConfigCache(): void {
  cached = null
}
