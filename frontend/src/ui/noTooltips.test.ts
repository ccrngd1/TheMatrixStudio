// SPDX-License-Identifier: Apache-2.0
// docs/MOBILE-UI.md §2 rule 6 and §7 stage 1: tap, never hover. A `title=` tooltip appears only on hover,
// which a phone does not have, so every explanation in the app was unreachable on a touch screen. They
// were converted to an ⓘ sheet (`Hint`), `aria-description` or visually hidden text; this keeps new ones
// out.
//
// A source scan with the TypeScript parser rather than a regex: JSX attributes span lines, and a component
// prop that happens to be called `title` (a Sheet's, a Section's) is a heading, not a tooltip. Only
// lowercase, intrinsic elements count. `<iframe title>` is exempt: there it is the frame's accessible name.
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const ROOT = join(__dirname, '..')
// The 8-bit theatre is its own page with its own design, outside the command-centre app.
const SKIP = [join(ROOT, 'theatre')]
const ALLOWED_TAGS = new Set(['iframe'])

function files(dir: string): string[] {
  if (SKIP.includes(dir)) return []
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name)
    if (e.isDirectory()) return files(p)
    return p.endsWith('.tsx') && !p.includes('.test.') ? [p] : []
  })
}

function tooltips(path: string): string[] {
  const sf = ts.createSourceFile(path, readFileSync(path, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const out: string[] = []
  const visit = (n: ts.Node) => {
    if (ts.isJsxOpeningElement(n) || ts.isJsxSelfClosingElement(n)) {
      const tag = n.tagName.getText(sf)
      if (/^[a-z]/.test(tag) && !ALLOWED_TAGS.has(tag)) {
        for (const a of n.attributes.properties) {
          if (ts.isJsxAttribute(a) && a.name.getText(sf) === 'title') {
            out.push(`${path.slice(ROOT.length + 1)}:${sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1} <${tag} title>`)
          }
        }
      }
    }
    ts.forEachChild(n, visit)
  }
  visit(sf)
  return out
}

describe('no hover-only tooltips', () => {
  it('no intrinsic element carries a title= tooltip', () => {
    expect(files(ROOT).flatMap(tooltips)).toEqual([])
  })
})
