---
name: delendai-plugin-authoring
appliesTo: ['@delendai/*']
description: How to author or modify an delendai plugin — the contract, the namespace, outputSchema discipline, durable state, path containment, and the test/build gates. Use when adding a tool, creating a plugin, or changing a plugin's surface.
---

# Authoring an delendai plugin

A plugin is an opt-in capability the core loads by specifier. The core stays
agnostic; the plugin receives everything resolved through `IMcpPluginContext`.

## The contract

```ts
import { definePlugin } from '@delendai/core/public';

export default definePlugin({
  name: 'example',                 // becomes the tool namespace prefix
  // optionsSchema: z.object({...}) // validated BEFORE register() runs
  register(ctx) {                  // ctx: IMcpPluginContext
    // ctx.workspace, ctx.corePaths, ctx.pluginCacheDir, ctx.pluginDocsDir,
    // ctx.namespacePrefix, ctx.options, ctx.args
    return { tools: [/* IToolRegistration[] */], prompts, resources, knowledge };
  },
});
```

- **Namespace everything.** Tools are exposed as `<namespace>_<tool>`; never collide.
- **Declare an `outputSchema` for every tool.** Open `catchall` schemas are a
  documented exception, not the default. Return compact JSON via `toolJson`/`toolOk`,
  errors via `toolError` (`{ ok:false, error:{ reason, nextAction } }`).
- **Validate inputs with Zod** (`.strict()`, `.min(1)`, no unknown keys).

## Invariants (non-negotiable)

- No `process.cwd()` — resolve paths from `ctx.workspace` / injected options.
- Runtime cache and scratch data MUST use `ctx.pluginCacheDir` or
   `ctx.cachePath(relativePath)`. Never write `.cache/delendai`, a dot-folder,
   or a temporary file in the workspace root directly. Legacy locations must be
   declared through `legacyCachePaths` on the plugin contract so boot-time
   reconciliation can move them without overwriting canonical data.
- The core exposes `cache_reconcile` for dry-run inspection and explicit repair;
   `.continue` remains outside this migration because Continue.dev discovers it
   as editor configuration rather than runtime cache.
- Async I/O only in handlers/engines (`fs/promises`); no `*Sync` in hot paths.
- Workspace-scoped path inputs (`roots`, `manifest`, …) → `resolveWorkspaceContained`.
- Durable state → `withFileMutex` + `writeFileAtomic`; treat corrupt ≠ empty with
  `quarantineCorruptFile`.
- Persisting user text → run it through `redactSecrets` first.
- Ephemeral artefacts the agent will unlink must live under
  `ctx.pluginCacheDir/exec/`. Use `resolveExecPath(ctx, name)` (or
  `withEphemeralExec(ctx, name, fn)` for the write → run → unlink
  shape). The `check-ephemeral-paths` lint refuses `os.tmpdir()`,
  `mkdtempSync(join(tmpdir(), …))`, and `writeFile('/tmp/…')` inside
  runtime code; test fixtures (`*.spec.ts`) and CLI tooling under
  `tools/scripts/` are exempt.
- Keep engines pure over injected readers so tests don't touch the real FS.

## Adding a new language + dogma

To support a new language in the rules plugin under the SOLID architecture, you must add its adapter, dogma, preset, and registry entries:

1. **Dogma Interface & Adapter** (`plugins/rules/src/lib/contracts/dogma-adapter.interface.ts` & `plugins/rules/src/lib/frameworks/dogmas/<lang>.dogma.ts`):
   Define the language's core guidelines (ownership, naming, error model, null-safety, async, immutability, testing, packageManager, bullets).
2. **Language Adapter** (`plugins/rules/src/lib/frameworks/languages/<lang>.adapter.ts`):
   Implement `ILanguageAdapter` to detect the language's manifest files in the directory.
3. **Base Linter Provider** (`plugins/rules/src/lib/frameworks/languages/base/<family>-base.provider.ts`):
   Provide check/fix/typecheck command builders for the linter tool.
4. **Preset Data** (`plugins/rules/src/lib/frameworks/presets/data/<family>.ts`):
   Define the default preset carrying linter configs, conventions, and required packages.
5. **Registry Entry** (`plugins/rules/src/lib/frameworks/registry/factory.ts`):
   Import and append the new adapter to the default adapters array, and include the preset data in the presets list.
6. **Online Preset Mapping** (`plugins/rules/src/lib/frameworks/online-preset.ts`):
   Register the new language preset and packages for freshness checks.

## Checklist before you commit

1. Tests next to the code (`*.spec.ts`); protocol behaviour → e2e with a real
   in-memory MCP server (mirror `packages/core/tests/src/lib/e2e`).
2. `bun run types:generate` if the tool surface changed (drift-guard test enforces it).
3. Update the plugin `README.md`; if user-visible on the site, add ALL i18n keys.
4. `bun run validate` green.
5. Conventional Commit (`feat(<plugin>): …`).
