// SPDX-License-Identifier: Apache-2.0
// Cognito Hosted UI login, authorization-code flow with PKCE.
//
// Until this existed the deployed SPA had **no login at all** — no Cognito code, no
// token, no `Authorization` header — so the CloudFront page was open to anyone. Nothing
// was exposed, because every API route answers 401 and the SPA's relative `/api/...`
// calls were not even reaching the API. But the moment CloudFront routes `/api/*` to the
// API Gateway (which it now does, and must, for the app to work at all) an
// unauthenticated page IS the hole. The two changes have to ship together.
//
// ## Why PKCE and not an implicit flow or a client secret
//
// The app client is configured `GenerateSecret: false` and `AllowedOAuthFlows: ['code']`,
// with template tests asserting both. A browser cannot keep a secret, and generating one
// would make this a *confidential* client — which makes PKCE optional in Cognito and
// leaves the flow open to code interception while still working perfectly in a demo.
// Implicit is worse again: it returns tokens in the URL fragment, which lands in browser
// history, `Referer` headers and any logging in between.
//
// So: `code` + PKCE, and the verifier never leaves this origin.
//
// ## Where tokens are kept, and the trade
//
// `sessionStorage`, not `localStorage`. Both are readable by any script on the origin, so
// neither defends against XSS — the difference is lifetime: `sessionStorage` dies with the
// tab, so a shared or forgotten machine does not keep a live session. The genuinely
// XSS-proof option is an httpOnly cookie set by a backend that holds the tokens, which
// means a session backend this project does not have and which §5.1 does not contemplate.
// Recorded as the known limit rather than implied to be solved.

export interface AuthConfig {
  /** Cognito Hosted UI domain, e.g. `https://x.auth.us-east-1.amazoncognito.com`. */
  hostedUiUrl: string
  /** App client id. Public by design in a PKCE client — it is in every redirect URL. */
  clientId: string
  /** Optional: where Cognito sends the user back. Defaults to this page's origin. */
  redirectUri?: string
}

export interface Tokens {
  idToken: string
  accessToken: string
  refreshToken?: string
  /** Epoch seconds when `idToken` expires. */
  expiresAt: number
}

const STORAGE_KEY = 'matrix.tokens'
const VERIFIER_KEY = 'matrix.pkce_verifier'

// Refresh this far before expiry. A token that expires mid-request produces a 401 the
// user reads as being logged out, so the margin is generous relative to a request.
const REFRESH_MARGIN_SECONDS = 120

function base64Url(bytes: Uint8Array): string {
  let binary = ''
  for (const b of bytes) binary += String.fromCharCode(b)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** A high-entropy PKCE verifier. `crypto.getRandomValues`, never `Math.random`. */
export function createVerifier(): string {
  const bytes = new Uint8Array(32)
  crypto.getRandomValues(bytes)
  return base64Url(bytes)
}

/** S256 challenge for a verifier. Cognito rejects `plain` on a public client. */
export async function challengeFor(verifier: string): Promise<string> {
  // `crypto.subtle` exists only in a SECURE CONTEXT — HTTPS, or localhost. CloudFront is
  // HTTPS so this is satisfied in the deployment, but serving the SPA over plain HTTP on
  // any other host makes it `undefined` and login fails with a bare TypeError about
  // reading 'digest'. Saying so is the difference between a five-minute fix and an hour
  // spent on Cognito.
  if (!globalThis.crypto?.subtle) {
    throw new Error(
      'Web Crypto is unavailable, so PKCE cannot be used. This page must be served ' +
        'over HTTPS (or from localhost) — crypto.subtle only exists in a secure context.',
    )
  }
  const digest = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(verifier),
  )
  return base64Url(new Uint8Array(digest))
}

export function redirectUri(config: AuthConfig): string {
  // The origin, with no path: Cognito matches callback URLs EXACTLY, so a deep link
  // like `/run/abc` would be rejected as an unregistered callback. The SPA restores
  // its own view after login instead.
  return config.redirectUri ?? `${window.location.origin}/`
}

export function loadTokens(): Tokens | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Tokens
    if (!parsed.idToken || !parsed.expiresAt) return null
    return parsed
  } catch {
    // Corrupt storage is treated as "not logged in" rather than crashing the app on
    // load, which would be unrecoverable without clearing storage by hand.
    return null
  }
}

export function storeTokens(tokens: Tokens): void {
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(tokens))
}

export function clearTokens(): void {
  sessionStorage.removeItem(STORAGE_KEY)
  sessionStorage.removeItem(VERIFIER_KEY)
}

export function isExpired(tokens: Tokens, nowSec = Date.now() / 1000): boolean {
  return tokens.expiresAt - REFRESH_MARGIN_SECONDS <= nowSec
}

/** Send the browser to the Hosted UI. Stores the verifier for the return leg. */
export async function beginLogin(config: AuthConfig): Promise<void> {
  const verifier = createVerifier()
  sessionStorage.setItem(VERIFIER_KEY, verifier)
  const challenge = await challengeFor(verifier)
  const params = new URLSearchParams({
    response_type: 'code',
    client_id: config.clientId,
    redirect_uri: redirectUri(config),
    // `openid` is what produces an ID token, which is what the API Gateway JWT
    // authorizer validates. Without it the flow succeeds and every API call is still 401.
    scope: 'openid email profile',
    code_challenge_method: 'S256',
    code_challenge: challenge,
  })
  window.location.assign(`${config.hostedUiUrl}/oauth2/authorize?${params}`)
}

/** The `?code=` on the URL after Cognito redirects back, if any. */
export function codeFromUrl(search = window.location.search): string | null {
  return new URLSearchParams(search).get('code')
}

/** The `?error=` Cognito returns when the user cancels or the client is misconfigured. */
export function errorFromUrl(search = window.location.search): string | null {
  const params = new URLSearchParams(search)
  const error = params.get('error')
  if (!error) return null
  return params.get('error_description') || error
}

function toTokens(payload: Record<string, unknown>, previous?: Tokens): Tokens {
  const expiresIn = Number(payload.expires_in ?? 3600)
  return {
    idToken: String(payload.id_token ?? ''),
    accessToken: String(payload.access_token ?? ''),
    // A refresh grant does NOT return a new refresh token, so the old one is carried
    // forward. Dropping it would log the user out at the first refresh — a bug that only
    // appears an hour in, which is the worst time to find it.
    refreshToken: (payload.refresh_token as string) || previous?.refreshToken,
    expiresAt: Math.floor(Date.now() / 1000) + expiresIn,
  }
}

async function tokenRequest(
  config: AuthConfig,
  body: Record<string, string>,
  previous?: Tokens,
): Promise<Tokens> {
  const res = await fetch(`${config.hostedUiUrl}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(body).toString(),
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = JSON.stringify(await res.json())
    } catch {
      /* keep the status text */
    }
    throw new Error(`Token request failed (${res.status}): ${detail}`)
  }
  const tokens = toTokens(await res.json(), previous)
  if (!tokens.idToken) {
    // A token response with no ID token means the `openid` scope is missing from the
    // client's allowed scopes. Loud, because the symptom otherwise is a successful login
    // followed by 401 on everything.
    throw new Error('Cognito returned no id_token — is the openid scope enabled?')
  }
  return tokens
}

/** Exchange the authorization code for tokens. Consumes the stored verifier. */
export async function completeLogin(
  config: AuthConfig,
  code: string,
): Promise<Tokens> {
  const verifier = sessionStorage.getItem(VERIFIER_KEY)
  if (!verifier) {
    throw new Error(
      'No PKCE verifier for this login. Start again from the login screen.',
    )
  }
  try {
    const tokens = await tokenRequest(config, {
      grant_type: 'authorization_code',
      client_id: config.clientId,
      code,
      redirect_uri: redirectUri(config),
      code_verifier: verifier,
    })
    storeTokens(tokens)
    return tokens
  } finally {
    // Single-use, whether or not the exchange worked: a verifier that outlived its code
    // is a replay waiting to happen.
    sessionStorage.removeItem(VERIFIER_KEY)
  }
}

/** Refresh with the stored refresh token. Returns null when it cannot be done. */
export async function refresh(
  config: AuthConfig,
  tokens: Tokens,
): Promise<Tokens | null> {
  if (!tokens.refreshToken) return null
  try {
    const next = await tokenRequest(
      config,
      {
        grant_type: 'refresh_token',
        client_id: config.clientId,
        refresh_token: tokens.refreshToken,
      },
      tokens,
    )
    storeTokens(next)
    return next
  } catch {
    // An expired or revoked refresh token is a normal end of session, not an error to
    // surface — the caller sends the user back to the Hosted UI.
    return null
  }
}

/**
 * A valid ID token, refreshing first if it is close to expiry. Null when logged out.
 *
 * The single place a token is obtained, so `Authorization` cannot be attached from a
 * stale copy held somewhere else.
 */
export async function validToken(config: AuthConfig): Promise<string | null> {
  const tokens = loadTokens()
  if (!tokens) return null
  if (!isExpired(tokens)) return tokens.idToken
  const refreshed = await refresh(config, tokens)
  if (refreshed) return refreshed.idToken
  clearTokens()
  return null
}

/** End the session locally and at Cognito, so the next login is a real one. */
export function logout(config: AuthConfig): void {
  clearTokens()
  const params = new URLSearchParams({
    client_id: config.clientId,
    logout_uri: redirectUri(config),
  })
  window.location.assign(`${config.hostedUiUrl}/logout?${params}`)
}
