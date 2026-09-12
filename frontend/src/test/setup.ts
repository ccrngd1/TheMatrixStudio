// SPDX-License-Identifier: Apache-2.0
import '@testing-library/jest-dom'

// jsdom does not implement scrollIntoView; components that auto-scroll (e.g. the
// conversation feed) call it in effects. Stub it so those components render in
// the test environment.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {}
}

// jsdom ships `crypto.getRandomValues` but NOT `crypto.subtle`, so PKCE's S256 challenge
// cannot be computed under test. Node's own WebCrypto is the real implementation, so this
// exercises the actual digest rather than stubbing it — a faked challenge would make the
// PKCE tests assert nothing about the derivation.
import { webcrypto } from 'node:crypto'

if (!globalThis.crypto?.subtle) {
  Object.defineProperty(globalThis, 'crypto', {
    value: webcrypto,
    configurable: true,
  })
}
