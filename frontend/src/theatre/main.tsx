// SPDX-License-Identifier: Apache-2.0
// Entry point for theatre.html. A separate bundle from the main app, so the room, the sprites
// and this code are loaded only by someone who opens the theatre.
//
// The sign-in gate is the app's own. Opened from a run with `window.open`, the new tab starts
// with a copy of the opener's sessionStorage and so is already signed in; opened any other way
// (a pasted link, a reload after the opener closed) it asks for a sign-in like the app does.
import React from 'react'
import ReactDOM from 'react-dom/client'
import { AuthGate } from '../views/AuthGate'
import { Theatre } from './Theatre'
import '../index.css'
import './theatre.css'

const runRef = new URLSearchParams(location.search).get('run')

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {runRef ? (
      <AuthGate>
        <Theatre runRef={runRef} />
      </AuthGate>
    ) : (
      <p className="th-note">No run given. Open the theatre from a finished run's page.</p>
    )}
  </React.StrictMode>,
)
