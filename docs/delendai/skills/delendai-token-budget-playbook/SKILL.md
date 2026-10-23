---
name: delendai-token-budget-playbook
appliesTo: ['@delendai/*']
description: How to keep an agent session's token cost low — which tools are compact-native, which to delegate to a subagent instead of calling from the main thread, and how the repo's own metrics/regression gate measures cost. Use before calling a verbose tool, or when a session feels like it is burning context too fast.
---

# token budget playbook

delendai treats token cost as a first-class invariant, not an
afterthought: the `metrics` tool measures it per-call
(`packages/core/src/lib/metrics/metrics-tool.ts`), and `f00027`'s CI gate
(`tools/scripts/metrics/diff-snapshots.script.ts`) fails the build if any
tool's bytes-per-call regresses more than the configured threshold
(default +20%) between releases. This skill is the day-to-day discipline
that keeps that gate green.

## Compact-first, then drill

Most plugin tools that can be "small or big" expose a `compact: true`
input — read it that way first, and only call the verbose form when you
actually need the extra detail:

| Verbose                  | Compact equivalent                          |
| ------------------------- | -------------------------------------------- |
| `proposals_proposal_board`          | `proposals_auto_work { compact: true }` / `proposals_compact_status` |
| `proposals_state_health` (full)     | `proposals_compact_status` for routine orientation; reserve `proposals_state_health` for suspected drift |
| `delendai_overview` (default) | `delendai_overview { compact: true }` |
| `audit_audit_consolidate` with no scope | `audit_audit_consolidate { scope: <narrow> }` |
| `get_rules` (dogmas) | Cache dogmas in memory; avoid repeatedly fetching all dogmas from the workspace |

## Tools to delegate, not call from the main thread

Per `CLAUDE.md`'s "keep the main thread cheap" rule: any of the following,
called from the orchestrating/root context, should instead be delegated to
a subagent (e.g. `delendai-orchestrator`) so the verbose result is
absorbed there and only a compact summary comes back:

- `proposals_proposal_board` in its verbose form.
- `proposals_state_health` full dumps (vs the compact `healthy: boolean` check).
- `audit_audit_consolidate` without a `scope` filter.
- `search_search` with `maxResults > 50`.

## How the regression gate measures cost

The persisted `metrics` snapshot
(`<cacheDir>/metrics/<ISO>.json`, written by `metrics { persist: true }`)
records `totalBytes` and `totalMs` *cumulative across the whole process
run* per tool. The CI gate normalises that to **bytes-per-call** and
**ms-per-call** before diffing against the previous release's snapshot —
raw totals would conflate "called more often" with "got more expensive
per call". If you add a new tool or change an existing tool's output
shape, expect the next `metrics-gate` CI run to pick up the delta; a
genuine increase in payload size should be a deliberate trade-off, not an
accident from e.g. forgetting a `compact` branch.

## `/compact` between unrelated tasks

Once a slice or proposal is closed and you are about to start unrelated
work, run `/compact` (or your host's equivalent) before continuing — do
not carry a closed slice's verbose tool output forward for the rest of the
session. The cost of re-reading a digest you already have is strictly
worse than the cost of letting it drop and re-fetching only if actually
needed later.

If a fact from that closed slice is truly reusable after the slice ends,
distil it into durable memory first. If it would only help while the current
investigation is still warm, do not persist it — compact it away with the
rest of the transient context.

## Smoke

The in-process token-budget e2e
(`packages/core/tests/src/lib/e2e/token-budget.e2e.spec.ts`) fails the
build if `delendai_overview`'s compact payload exceeds its measured
budget — if you see that test fail after a change to `overview-tool.ts`,
that is the signal this skill exists to prevent: a tool you thought was
"compact" silently grew.
