---
name: delendai-operator
appliesTo: ['@delendai/*']
description: How an agent (or human operator) should orient itself in a fresh delendai session — which preset to load, which tool to call first, and how to read the overview payload before doing anything else. Use at the start of every session, before touching any plugin-specific tool.
---

# delendai operator

This is the **first** skill any agent should read in a fresh session. It
answers "what do I call before I do anything else" — the other skills
(`delendai-proposal-swarm-runner`, `delendai-state-repair-playbook`, `delendai-token-budget-playbook`,
`delendai-concurrency-patterns`) assume you have already done this.

## Decision tree

```
session starts
  → delendai_overview { compact: true }
      → read `recommendedNextAction` (it IS the orchestrator — do not
        restate the workflow yourself, follow what it says)
      → if it points at a proposals tool (auto_work, continue_proposal):
          read `delendai-proposal-swarm-runner` SKILL.md next
      → if it reports a failure envelope ({ ok:false, error:{ reason } }):
          read `delendai-failure-modes` SKILL.md
      → otherwise: proceed with the task using the tools `overview`
        already told you are loaded
```

## Picking a preset

The host CLI resolves `--preset=<name>` to a curated plugin list
(`packages/core/src/lib/plugins/parse-cli-args.ts`, `PLUGIN_PRESETS`):

| Preset     | Plugins                                                                     | When                                      |
| ---------- | ---------------------------------------------------------------------------- | ------------------------------------------ |
| `minimal`  | `git`, `search`                                                              | Read-only orientation, lightweight session |
| `standard` | `git`, `search`, `memory`, `docs`, `rules`, `quality`, `deps`               | Full single-agent toolkit                  |
| `swarm`    | `standard` + `proposals`, `notification`, `status-marker`, `test-convention` | Multi-agent coordination (this repo's own dogfooding setup) |

Opt-in plugins (`audit`, `web` once `f00029` ships) are **never** added to a
preset automatically — they are loaded explicitly with `--plugins=<name>`
because they carry capabilities (running an LLM audit, hitting the network)
that should not be silently on by default.

## Reading `overview`

`delendai_overview { compact: true }` (`packages/core/src/lib/tools/overview-tool.ts`)
returns a small payload whose most important field is
`recommendedNextAction: string` — a plain-English instruction, not a tool
name to memorise. Trust it over any cached mental model of "what delendai
usually wants"; the field is computed from the live state (loaded plugins,
proposal queue, lock contention) at call time.

Call it **once** per turn, not in a loop. If the situation changes (a lock
releases, a slice closes), the next `overview` call will reflect that — you
do not need to poll it speculatively.

## Memory vs docs vs reread

Use this order when the answer might already exist:

1. **Durable fact already worth keeping?** Use `memory_recall` / `memory_list`
   first.
2. **Canonical repo guidance or longer explanation?** Use `docs_list` /
   `docs_read`.
3. **Local implementation detail for the current slice only?** Reread the
   specific file you are touching.

Durable memory is for distilled reusable facts, not logs or raw tool output. If
 the note would not help after the current slice closes, do not persist it.

## Never do

1. Hardcode a plugin list instead of a `--preset` name — presets exist so
   the curated list lives in one place (`parse-cli-args.ts`) and changes
   propagate to every caller.
2. Skip `overview` and jump straight to a plugin tool "because you already
   know what to do" — the `recommendedNextAction` may differ from your
   assumption (e.g. a slice you planned to claim got claimed by a peer
   between sessions).
3. Treat `overview`'s compact payload as exhaustive — it is intentionally
   small (~318 tokens measured in `docs/delendai/TOKEN-BUDGETS.md`); drill into a
   specific tool (`proposal_board`, `state_health`) only when you actually
   need the verbose detail it omits.
4. Use memory as a transcript sink — durable memory is for short reusable
   facts; transient exploration stays in session context and should be
   compacted away when the task changes.

## Smoke

Calling `delendai_knowledge` with no `id` lists every entry as
`{id, title}` and never 404s — if it does, the host failed to assemble
plugin knowledge and the session should stop and report the failure rather
than continue with a degraded surface.
