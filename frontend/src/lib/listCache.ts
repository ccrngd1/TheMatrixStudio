// SPDX-License-Identifier: Apache-2.0
// The last response of each list screen, kept for the life of the page.
//
// Backing out of a run remounts the Runs screen, and it used to start from nothing: a blank list and a
// multi-second request (a cold Lambda pays ~5.7 s of init on top). Now the list comes back as it was left
// and is refreshed in the background, so the only wait is the first one after loading the app.
//
// Memory only, not storage: a reload is the user asking for fresh data, and runs belong to whoever is
// signed in, so nothing here should outlive the page.
const store = new Map<string, unknown>()

export function cached<T>(key: string): T | undefined {
  return store.get(key) as T | undefined
}

export function remember<T>(key: string, value: T): T {
  store.set(key, value)
  return value
}

/** For tests, and for sign-out. */
export function clearListCache() {
  store.clear()
}
