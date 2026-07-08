// SPDX-License-Identifier: Apache-2.0
// Deterministic placeholder color from a name (mandatory avatar fallback).

const PALETTE = [
  '#0ea5e9', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981',
  '#ef4444', '#14b8a6', '#6366f1', '#f97316', '#84cc16',
]

export function colorForName(name: string): string {
  let hash = 0
  for (let i = 0; i < name.length; i++) {
    hash = (hash * 31 + name.charCodeAt(i)) >>> 0
  }
  return PALETTE[hash % PALETTE.length]
}

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/)
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
}
