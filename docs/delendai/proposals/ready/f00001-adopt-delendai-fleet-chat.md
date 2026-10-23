---
id: f00001
status: ready
type: proposal
track: adoption-migration
date: 2026-10-21
kind: feat
title: Adopt delendai workflow (fleet-chat)
shipped-in: []
recan: []
related:
    - f00084 # init command that scaffolded this proposal
    - f00089 # adoption-plan umbrella
ownership:
    - { agent: technical_investigator, task: 'A1: inventory the foreign proposal/skill/tool surface (do not modify it)' }
    - { agent: proposal_guardian, task: 'A2: map the foreign convention onto the canonical delendai layout' }
globalGate: validate
acceptance:
    - { command: bun run typecheck, expect: exit0 }
    - { command: bun run test, expect: exit0 }
    - { command: bun run validate, expect: exit0 }
---

# f00001 — Adopt delendai (fleet-chat)

## goal

Adopt the delendai workflow in this project: a single canonical
proposals layout, namespace-prefixed tools, the `{ ok, error }` envelope,
and a proposals-driven swarm. Where the project already has its own
proposal/plan convention, **migrate** it onto ours rather than starting
a parallel system.

## why

This proposal was scaffolded by `delendai init` (f00089 U1). The id `f00001`
was allocated as the next free id in this project's canonical proposals
space — it is **not** a hardcoded `f00001`, so it cannot collide with a
proposal that already exists here.

**Foreign proposal system.** No existing proposal/plan convention was
detected in this project. This plan adopts the canonical delendai
layout from scratch under `docs/delendai/proposals/`.

## non-goals

- **No in-place conversion of foreign files.** The mapping and skill
  migration below are advisory: `init` never writes, deletes, or moves
  a foreign proposal, skill, or tool. The target's own agents execute
  the migration.
- **No runtime tool renaming.** The namespace-unification slice is
  plan output; the host enforces prefixing when the server boots.
- **No hardcoded ids.** Ids are allocated as the next free id in the
  target's canonical proposals space, never a fixed `f00001`.

## slices

### S1 — inventory the foreign surface (read-only)

- **Status**: pending
- **Files**: `docs/delendai/proposals/ready/f00001-a1-inventory.md`
- **Gate**: bun run validate

Capture every existing proposal/record, skill, and tool the project
declares. Save the structured output under
`docs/delendai/proposals/ready/f00001-a1-inventory.md`. Touch nothing.

### S2 — map foreign → canonical

- **Status**: pending
- **Files**: `docs/delendai/proposals/`
- **Gate**: bun run validate

Produce the mapping from the foreign convention to the canonical
delendai layout (file naming, id space, status folders). The mapping
is advisory; converting the foreign files is a later, explicit step the
target's agents perform — `init` never converts them in place.

### S3 — skill migration

- **Status**: pending
- **Files**: `docs/delendai/skills/`
- **Gate**: bun run validate

Bring the project's skill surface onto the canonical layout. `init` writes the bundled core skills listed below. The project's own skills are only inventoried: `init` never moves, deletes or rewrites them, and the target's own agents carry out that part.

**Migrate OUR canonical skills into the target** (`docs/delendai/skills/`):

- `delendai-conventional-commits-and-release` → applies to `@delendai/core`
- `delendai-error-collection` → applies to `@delendai/*`
- `delendai-failure-modes` → applies to `@delendai/*`
- `delendai-operator` → applies to `@delendai/*`
- `delendai-plugin-authoring` → applies to `@delendai/*`
- `delendai-token-budget-discipline` → applies to `@delendai/core`
- `delendai-token-budget-playbook` → applies to `@delendai/*`

**Also available, with the plugin each belongs to** (`init` does not copy these):

- `delendai-audit-playbook` → applies to `@delendai/audit`
- `delendai-audit-runner` → applies to `@delendai/audit`
- `delendai-concurrency-patterns` → applies to `@delendai/proposals, @delendai/notification`
- `delendai-debugging-playbook` → applies to `@delendai/skills-pack, @delendai/logs, @delendai/proposals`
- `delendai-external-mcps` → applies to `@delendai/external-mcps`
- `delendai-incident-response` → applies to `@delendai/skills-pack, @delendai/observability, @delendai/logs, @delendai/notification, @delendai/proposals`
- `delendai-legacy-proposal-migration` → applies to `@delendai/proposals`
- `delendai-migrate-from-x` → applies to `@delendai/skills-pack, @delendai/refactor, @delendai/git, @delendai/quality, @delendai/proposals`
- `delendai-multi-agent-coordination` → applies to `@delendai/proposals, @delendai/notification`
- `delendai-performance-optimization` → applies to `@delendai/skills-pack, @delendai/perf, @delendai/quality`
- `delendai-pr-review-checklist` → applies to `@delendai/skills-pack, @delendai/git, @delendai/forge, @delendai/quality, @delendai/security`
- `delendai-proposal-swarm-runner` → applies to `@delendai/proposals`
- `delendai-proposals-workflow-playbook` → applies to `@delendai/proposals`
- `delendai-quality-and-rules-gates` → applies to `@delendai/quality, @delendai/rules`
- `delendai-rules-dogma-priority` → applies to `@delendai/rules`
- `delendai-rules-solid-architecture` → applies to `@delendai/rules`
- `delendai-security-hardening-checklist` → applies to `@delendai/skills-pack, @delendai/security, @delendai/env`
- `delendai-state-repair-playbook` → applies to `@delendai/proposals`
- `delendai-status-marker-and-closure` → applies to `@delendai/status-marker`
- `delendai-tabs-component` → applies to `@delendai/web`

**Absorb the target's EXISTING skills** (inventory, do not clobber):

No existing skills were detected in this project. The canonical skills above are migrated into `docs/delendai/skills/` from scratch.

### S4 — tool-namespace unification

- **Status**: pending
- **Files**: `.vscode/mcp.json`
- **Gate**: bun run validate

Unify the tool surface under the **prefix-per-plugin** contract: every delendai tool is exposed as `<prefix>_<plugin>_<tool>`, so plugins never collide with each other or with the target's own tools. This is **plan output**, not a runtime change — the host enforces the prefixing when the server boots.

**Our tool namespaces** (resolved plugin set):

- `delendai_adaptive-optimizer`_* — delendai `adaptive-optimizer` plugin
- `delendai_agent-orchestrator`_* — delendai `agent-orchestrator` plugin
- `delendai_audit`_* — delendai `audit` plugin
- `delendai_auto-agent-selector`_* — delendai `auto-agent-selector` plugin
- `delendai_auto-plugin-selector`_* — delendai `auto-plugin-selector` plugin
- `delendai_commit-policy`_* — delendai `commit-policy` plugin
- `delendai_completion`_* — delendai `completion` plugin
- `delendai_container`_* — delendai `container` plugin
- `delendai_context-for-change`_* — delendai `context-for-change` plugin
- `delendai_conventions`_* — delendai `conventions` plugin
- `delendai_deps`_* — delendai `deps` plugin
- `delendai_diagram`_* — delendai `diagram` plugin
- `delendai_docs`_* — delendai `docs` plugin
- `delendai_env`_* — delendai `env` plugin
- `delendai_error-reporting`_* — delendai `error-reporting` plugin
- `delendai_forge`_* — delendai `forge` plugin
- `delendai_git`_* — delendai `git` plugin
- `delendai_i18n`_* — delendai `i18n` plugin
- `delendai_impact-analysis`_* — delendai `impact-analysis` plugin
- `delendai_link-check`_* — delendai `link-check` plugin
- `delendai_logs`_* — delendai `logs` plugin
- `delendai_memory`_* — delendai `memory` plugin
- `delendai_notification`_* — delendai `notification` plugin
- `delendai_orchestrator-runner`_* — delendai `orchestrator-runner` plugin
- `delendai_perf`_* — delendai `perf` plugin
- `delendai_project-health`_* — delendai `project-health` plugin
- `delendai_project-kpis`_* — delendai `project-kpis` plugin
- `delendai_proposals`_* — delendai `proposals` plugin
- `delendai_quality`_* — delendai `quality` plugin
- `delendai_quality-policy`_* — delendai `quality-policy` plugin
- `delendai_rules`_* — delendai `rules` plugin
- `delendai_search`_* — delendai `search` plugin
- `delendai_security`_* — delendai `security` plugin
- `delendai_status-marker`_* — delendai `status-marker` plugin
- `delendai_tech-debt`_* — delendai `tech-debt` plugin
- `delendai_test-convention`_* — delendai `test-convention` plugin
- `delendai_test-policy`_* — delendai `test-policy` plugin
- `delendai_usage-tracking`_* — delendai `usage-tracking` plugin

No foreign MCP tool surface was detected in this project; only delendai tools are registered.

**No collisions.** Every namespace above is distinct, so ours and the target's tools coexist without renaming either side.

### S5 — one agent source of truth

- **Status**: pending
- **Files**: `AGENTS.md`, `docs/delendai/AGENT-BOOTSTRAP.md`
- **Gate**: bun run validate

Consolidate this project's agent guidance into ONE canonical source:
`docs/delendai/AGENT-BOOTSTRAP.md` holds the rules; `AGENTS.md` and
`CLAUDE.md` stay thin pointers to it. Fold any pre-existing agent
instructions found in this repo (a `CONTRIBUTING` agent section, a
custom `.cursorrules`, a hand-written `CLAUDE.md`) into the bootstrap so
there is no second, drifting copy for agents to disagree over.

## acceptance

- `bun run typecheck` → exit 0.
- `bun run test` → exit 0.
- `bun run validate` → exit 0.
- The adoption plan is advisory only: no foreign proposal, skill, or
  tool is written, deleted, or moved by `init`.
