// SPDX-License-Identifier: Apache-2.0
import '@testing-library/jest-dom'

// jsdom does not implement scrollIntoView; components that auto-scroll (e.g. the
// conversation feed) call it in effects. Stub it so those components render in
// the test environment.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {}
}
