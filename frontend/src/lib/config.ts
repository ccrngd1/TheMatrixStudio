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

const CLOSED: RuntimeConfig = {
  hostedUiUrl: '',
  clientId: '',
  userPoolId: '',
  authRequired: true,
}

let cached: RuntimeConfig | null = null

/**
 * Load `config.json`. Cached, so the app pays one request.
 *
 * EVERY failure — 404, 5xx, network error, malformed JSON — is treated as auth
 * REQUIRED with no usable settings, so the app shows an error rather than its contents.
 *
 * A 404 used to be the one exception, read as "the local tool, which ships no config
 * file". That inference was wrong twice over:
 *
 *   - The local tool does not 404. uvicorn serves the SPA with a catch-all that
 *     SPA-falls-back to `index.html`, so `/config.json` came back as 200 text/html and
 *     `res.json()` threw. The local tool is now served an explicit `/config.json` by
 *     the API (see `_runtime_config` in api/app.py), so it says `authRequired: false`
 *     rather than relying on absence.
 *   - Absence is reachable on a DEPLOYMENT. The documented SPA sync was
 *     `aws s3 sync … --delete`, which deletes the `config.json` that `cdk deploy`
 *     writes separately — and the old semantics turned that into a page with no login
 *     at all. The sync now excludes it, and this no longer trusts absence either.
 */
export async function loadConfig(): Promise<RuntimeConfig> {
  if (cached) return cached
  try {
    const res = await fetch('/config.json', { cache: 'no-store' })
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
    // Fail CLOSED. A config fetch that failed for ANY reason must not become "no login
    // needed" — that is precisely how a deployment ends up open to the world.
    cached = CLOSED
    throw err
  }
}

/** Reset the cache. Tests only; there is no reason to re-read config at runtime. */
export function resetConfigCache(): void {
  cached = null
}
