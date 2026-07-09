// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from 'react'
import { api } from '../api'
import type { BranchTreeNode, BranchTreeResponse } from '../types'

interface Props {
  runId: string
  // Called when the user clicks a node to navigate to that run.
  onOpenRun?: (runId: string) => void
}

const MUTATION_LABELS: Record<string, string> = {
  inject_message: '💬 inject',
  continue: '▶ continue',
  edit_goal: '🎯 edit goal',
  add_persona: '➕ add persona',
  remove_persona: '➖ remove persona',
  promote_aside: '⤴ promote aside',
}

const STATUS_DOT: Record<string, string> = {
  complete: 'bg-green-500',
  running: 'bg-sky-400 animate-pulse',
  failed: 'bg-red-500',
  interrupted: 'bg-yellow-400',
}

function statusDot(status: string) {
  return STATUS_DOT[status] ?? 'bg-slate-500'
}

function buildChildren(
  nodes: Record<string, BranchTreeNode>,
  parentId: string | null,
): BranchTreeNode[] {
  return Object.values(nodes)
    .filter((n) => n.parent_run_id === parentId)
    .sort((a, b) => a.created_at - b.created_at)
}

function TreeNode({
  node,
  nodes,
  depth,
  currentRunId,
  onOpenRun,
}: {
  node: BranchTreeNode
  nodes: Record<string, BranchTreeNode>
  depth: number
  currentRunId: string
  onOpenRun?: (id: string) => void
}) {
  const children = buildChildren(nodes, node.id)
  const isCurrent = node.id === currentRunId
  const mutLabel = node.mutation_kind ? MUTATION_LABELS[node.mutation_kind] ?? node.mutation_kind : null

  return (
    <li>
      <div
        className={`flex cursor-pointer items-start gap-2 rounded px-2 py-1 hover:bg-matrix-panel/80 ${isCurrent ? 'border border-matrix-accent/50 bg-matrix-panel' : ''}`}
        style={{ paddingLeft: `${8 + depth * 16}px` }}
        onClick={() => onOpenRun?.(node.id)}
        title={node.id}
      >
        {/* connector line indicator */}
        {depth > 0 && (
          <span className="mt-1.5 shrink-0 text-slate-600">└</span>
        )}
        <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${statusDot(node.status)}`} />
        <div className="min-w-0">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className={`truncate text-sm font-medium ${isCurrent ? 'text-matrix-accent' : 'text-slate-200'}`}>
              {node.name ?? node.slug ?? node.id.slice(0, 8)}
            </span>
            {mutLabel && (
              <span className="rounded border border-matrix-border px-1.5 py-0.5 text-[10px] text-slate-500">
                {mutLabel} @ turn {node.branch_turn}
              </span>
            )}
          </div>
          <div className="text-[11px] text-slate-500">
            {node.turn_count} turns · ${node.total_cost_usd.toFixed(4)} · {node.status}
          </div>
        </div>
      </div>
      {children.length > 0 && (
        <ul>
          {children.map((c) => (
            <TreeNode
              key={c.id}
              node={c}
              nodes={nodes}
              depth={depth + 1}
              currentRunId={currentRunId}
              onOpenRun={onOpenRun}
            />
          ))}
        </ul>
      )}
    </li>
  )
}

// Phase 2b branch-tree. Renders the full lineage rooted at the ancestor of
// this run; clicking any node navigates to it. Only shown when the tree has
// >1 node (i.e. when there are actual branches).
export function BranchTree({ runId, onOpenRun }: Props) {
  const [tree, setTree] = useState<BranchTreeResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .getRunTree(runId)
      .then(setTree)
      .catch((e) => setError((e as Error).message))
  }, [runId])

  if (error || !tree) return null
  if (Object.keys(tree.nodes).length <= 1) return null

  const root = tree.nodes[tree.root_id]
  if (!root) return null

  return (
    <section className="rounded-lg border border-matrix-border bg-matrix-panel p-3">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-matrix-accent">
        Timeline branches
      </h2>
      <ul className="space-y-0.5">
        <TreeNode
          node={root}
          nodes={tree.nodes}
          depth={0}
          currentRunId={runId}
          onOpenRun={onOpenRun}
        />
      </ul>
    </section>
  )
}
