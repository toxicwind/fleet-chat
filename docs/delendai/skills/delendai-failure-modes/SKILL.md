---
name: delendai-failure-modes
appliesTo: ['@delendai/*']
description: What to do when an delendai tool returns a failure envelope — lock conflict, corrupt file, state inconsistency, command-policy block, or a timeout. Use when a tool reports ok:false or a swarm gets stuck.
---

# delendai failure modes

Tools return a structured error envelope: `{ ok:false, error:{ reason, nextAction } }`.
Read `nextAction` first — it usually names the recovery tool. Common reasons:

## `lock-conflict` (a slice is claimed by another agent)

The slice's files are owned by a peer. **Do not poll** `agent_lock status` in a loop.
Wait for the `lock-released` notification (notification plugin) and retry the claim,
or pick a different file-disjoint slice from `proposal_board`. Locks are stale-aware:
a dead holder's lock is reclaimable after the heartbeat window.

## `corrupt-file` / `CorruptFileError`

A store file failed to parse. The runtime already quarantined it
(`<file>.corrupt-<id>`) and continued with empty state — corrupt is treated
differently from empty on purpose. Inspect the quarantined copy if you need the data;
otherwise the store self-heals. If it recurs, run `state_health`.

## `state-inconsistency` (proposals)

Run `state_health` to get a diagnosis, then `state_repair` to reconcile (zombie
claims, orphaned queue entries, drifted index). Re-run `state_health` to confirm green.

## command-policy block (`code 126`, quality plugin)

The command isn't on the allow-list (or is on the deny-list). This is a trust
boundary, not a bug — adjust the project's `commandPolicy`/validation matrix rather
than trying to bypass it.

## timeouts (`code 124`)

A spawned command (quality) or a mutex acquisition exceeded its bound. For quality,
the process group was killed (no zombies). For locks, the waiter is bounded and will
steal a stale lock; if contention is high, back off and wait on a notification rather
than hammering the claim.

## General

- Never loop calling tools after `stop: true` — that's the anti-idle brake firing.
- When unsure of current state, `delendai_overview` + `compact_status` re-orient cheaply.
