// SPDX-License-Identifier: Apache-2.0
// The login flow. Until this existed the deployed SPA had NO login at all — no Cognito
// code, no token, no Authorization header — so the CloudFront page was open to anyone.
//
// The negative cases come first, as in the KB permission suite, for the same reason: a
// suite that only proves login WORKS is the shape of a suite that would pass with the
// gate removed.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  challengeFor,
  clearTokens,
  codeFromUrl,
  completeLogin,
  createVerifier,
  errorFromUrl,
  isExpired,
  loadTokens,
  redirectUri,
  refresh,
  storeTokens,
  validToken,
  type Tokens,
} from './auth'

const CONFIG = {
  hostedUiUrl: 'https://pool.auth.us-east-1.amazoncognito.com',
  clientId: 'client123',
  redirectUri: 'https://app.example/',
}

const NOW = 1_700_000_000

function tokens(over: Partial<Tokens> = {}): Tokens {
  return {
    idToken: 'id-token',
    accessToken: 'access-token',
    refreshToken: 'refresh-token',
    expiresAt: NOW + 3600,
    ...over,
  }
}

beforeEach(() => {
  sessionStorage.clear()
  vi.restoreAllMocks()
  vi.spyOn(Date, 'now').mockReturnValue(NOW * 1000)
})

afterEach(() => sessionStorage.clear())

// --------------------------------------------------------------------------- //
// PKCE
// --------------------------------------------------------------------------- //

describe('PKCE', () => {
  it('generates a high-entropy verifier that differs every time', () => {
    const a = createVerifier()
    const b = createVerifier()
    expect(a).not.toBe(b)
    // 32 random bytes, base64url — no padding, no `+`, no `/`.
    expect(a.length).toBeGreaterThanOrEqual(43)
    expect(a).toMatch(/^[A-Za-z0-9_-]+$/)
  })

  it('derives an S256 challenge that is not the verifier', async () => {
    // `plain` is rejected by Cognito on a public client, and a challenge equal to the
    // verifier would mean the interception protection is absent while the flow works.
    const verifier = createVerifier()
    const challenge = await challengeFor(verifier)
    expect(challenge).not.toBe(verifier)
    expect(challenge).toMatch(/^[A-Za-z0-9_-]+$/)
  })

  it('derives the same challenge for the same verifier', async () => {
    const verifier = 'fixed-verifier-value'
    expect(await challengeFor(verifier)).toBe(await challengeFor(verifier))
  })
})

// --------------------------------------------------------------------------- //
// The redirect URI
// --------------------------------------------------------------------------- //

describe('redirectUri', () => {
  it('has no path, because Cognito matches callbacks exactly', () => {
    // A deep link like `/run/abc` would be rejected as an unregistered callback, so the
    // SPA always returns to the origin and restores its own view.
    const uri = redirectUri({ hostedUiUrl: 'x', clientId: 'y' })
    expect(new URL(uri).pathname).toBe('/')
  })
})

// --------------------------------------------------------------------------- //
// Token storage and expiry
// --------------------------------------------------------------------------- //

describe('token storage', () => {
  it('round-trips tokens', () => {
    storeTokens(tokens())
    expect(loadTokens()?.idToken).toBe('id-token')
  })

  it('treats corrupt storage as logged out rather than crashing', () => {
    // Otherwise the app is unrecoverable without clearing storage by hand.
    sessionStorage.setItem('matrix.tokens', 'not json')
    expect(loadTokens()).toBeNull()
  })

  it('treats a stored blob with no token as logged out', () => {
    sessionStorage.setItem('matrix.tokens', JSON.stringify({ accessToken: 'a' }))
    expect(loadTokens()).toBeNull()
  })

  it('does not use localStorage, so a session dies with the tab', () => {
    storeTokens(tokens())
    expect(localStorage.getItem('matrix.tokens')).toBeNull()
  })

  it('counts a token as expired BEFORE it actually expires', () => {
    // A token expiring mid-request produces a 401 the user reads as being logged out, so
    // the margin has to be larger than a request.
    expect(isExpired(tokens({ expiresAt: NOW + 3600 }), NOW)).toBe(false)
    expect(isExpired(tokens({ expiresAt: NOW + 30 }), NOW)).toBe(true)
    expect(isExpired(tokens({ expiresAt: NOW - 1 }), NOW)).toBe(true)
  })
})

// --------------------------------------------------------------------------- //
// The URL after Cognito redirects back
// --------------------------------------------------------------------------- //

describe('reading the callback', () => {
  it('finds the authorization code', () => {
    expect(codeFromUrl('?code=abc123&state=x')).toBe('abc123')
    expect(codeFromUrl('?state=x')).toBeNull()
  })

  it('surfaces an OAuth error, preferring the description', () => {
    // A cancelled login or a misconfigured client must say something. Without this the
    // user lands back on the login screen with no explanation and tries again forever.
    expect(errorFromUrl('?error=access_denied')).toBe('access_denied')
    expect(
      errorFromUrl('?error=invalid_request&error_description=bad+redirect'),
    ).toBe('bad redirect')
    expect(errorFromUrl('?code=abc')).toBeNull()
  })
})

// --------------------------------------------------------------------------- //
// The code exchange
// --------------------------------------------------------------------------- //

describe('completeLogin', () => {
  it('refuses without a stored verifier', async () => {
    // No verifier means this is not the browser that started the login — a pasted URL,
    // or a replay. Proceeding would exchange a code the origin cannot prove it requested.
    await expect(completeLogin(CONFIG, 'code')).rejects.toThrow(/verifier/i)
  })

  it('exchanges the code and stores the tokens', async () => {
    sessionStorage.setItem('matrix.pkce_verifier', 'v')
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        id_token: 'new-id',
        access_token: 'new-access',
        refresh_token: 'new-refresh',
        expires_in: 3600,
      }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const got = await completeLogin(CONFIG, 'the-code')
    expect(got.idToken).toBe('new-id')
    expect(loadTokens()?.idToken).toBe('new-id')

    const body = new URLSearchParams(fetchMock.mock.calls[0][1].body as string)
    expect(body.get('grant_type')).toBe('authorization_code')
    expect(body.get('code')).toBe('the-code')
    expect(body.get('code_verifier')).toBe('v')
    // No client secret: this is a public client and sending one would mean it had been
    // configured with one, which disables mandatory PKCE.
    expect(body.get('client_secret')).toBeNull()
  })

  it('consumes the verifier even when the exchange fails', async () => {
    // A verifier that outlived its code is a replay waiting to happen.
    sessionStorage.setItem('matrix.pkce_verifier', 'v')
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 400, statusText: 'Bad', json: async () => ({}) }),
    )
    await expect(completeLogin(CONFIG, 'code')).rejects.toThrow(/400/)
    expect(sessionStorage.getItem('matrix.pkce_verifier')).toBeNull()
  })

  it('rejects a token response with no id_token', async () => {
    // The `openid` scope missing from the client's allowed scopes. Otherwise the symptom
    // is a successful login followed by 401 on everything, which sends you to Cognito.
    sessionStorage.setItem('matrix.pkce_verifier', 'v')
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ access_token: 'a', expires_in: 3600 }),
      }),
    )
    await expect(completeLogin(CONFIG, 'code')).rejects.toThrow(/id_token/)
  })
})

// --------------------------------------------------------------------------- //
// Refresh
// --------------------------------------------------------------------------- //

describe('refresh', () => {
  it('carries the old refresh token forward when the response omits one', async () => {
    // A refresh grant does NOT return a new refresh token. Dropping it logs the user out
    // at the first refresh — a bug that only appears an hour in.
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ id_token: 'i2', access_token: 'a2', expires_in: 3600 }),
      }),
    )
    const next = await refresh(CONFIG, tokens({ refreshToken: 'keep-me' }))
    expect(next?.refreshToken).toBe('keep-me')
  })

  it('returns null with no refresh token rather than calling Cognito', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    expect(await refresh(CONFIG, tokens({ refreshToken: undefined }))).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('returns null on a revoked refresh token', async () => {
    // A normal end of session, not an error to surface.
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 400, statusText: 'Bad', json: async () => ({}) }),
    )
    expect(await refresh(CONFIG, tokens())).toBeNull()
  })
})

// --------------------------------------------------------------------------- //
// validToken — the single place a token is obtained
// --------------------------------------------------------------------------- //

describe('validToken', () => {
  it('returns null when logged out', async () => {
    expect(await validToken(CONFIG)).toBeNull()
  })

  it('returns a live token without calling Cognito', async () => {
    storeTokens(tokens({ expiresAt: NOW + 3600 }))
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    expect(await validToken(CONFIG)).toBe('id-token')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('refreshes a near-expired token and returns the new one', async () => {
    storeTokens(tokens({ expiresAt: NOW + 10 }))
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ id_token: 'fresh', access_token: 'a', expires_in: 3600 }),
      }),
    )
    expect(await validToken(CONFIG)).toBe('fresh')
  })

  it('clears the session when a refresh fails, rather than returning a dead token', async () => {
    // Returning the expired token would send a request the API rejects, and the user
    // would see a 401 rather than a login screen.
    storeTokens(tokens({ expiresAt: NOW - 1 }))
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 400, statusText: 'x', json: async () => ({}) }),
    )
    expect(await validToken(CONFIG)).toBeNull()
    expect(loadTokens()).toBeNull()
  })
})

describe('clearTokens', () => {
  it('removes the verifier as well as the tokens', () => {
    storeTokens(tokens())
    sessionStorage.setItem('matrix.pkce_verifier', 'v')
    clearTokens()
    expect(loadTokens()).toBeNull()
    expect(sessionStorage.getItem('matrix.pkce_verifier')).toBeNull()
  })
})
