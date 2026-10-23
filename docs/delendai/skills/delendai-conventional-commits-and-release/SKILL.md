---
name: delendai-conventional-commits-and-release
appliesTo: ['@delendai/core']
description: How a commit message maps to the next semver bump, why `derive-version.ts` (not a human) decides the version, and the exact dry-run -> --write -> tag -> publish flow `release.script.ts` drives. Use before writing a commit message or running anything under `bun run release`.
---

# delendai conventional commits + release

## Decision tree

1. About to commit? -> pick the Conventional Commit type from the table below.
   Never hand-edit a `package.json` `version` field — that's `derive-version`'s job.
2. About to release? -> `bun run release` (dry-run, no flags) first. Read the
   printed plan.
3. Plan looks right? -> add `--write` to apply the bump to every `package.json`.
4. Ready to ship? -> `--publish` (builds `dist/`, runs `bun run validate` unless
   `--no-validate`, then publishes `PUBLISH_ORDER` in sequence).
5. CI path: a push to `main` runs `derive-version.ts` (no flags) to decide
   `release`/`version`/`bump` from commits since the last `vX.Y.Z` tag — it
   never touches `package.json`; the git tag is the only source of truth for
   "what was the last released version".

## Commit type -> semver bump

| Commit prefix | Bump | Notes |
|---|---|---|
| `fix:` / `perf:` | patch | |
| `feat:` | minor | |
| `feat!:` / any `!:` / body has `BREAKING CHANGE:` | major | breaking always wins, regardless of type |
| `docs:` `chore:` `ci:` `test:` `style:` `build:` `refactor:` `revert:` | none | recognised but non-releasable — contributes nothing to the bump |
| anything else with real content (non-Conventional-Commit message) | patch | safe default in `classifyBump` (`scripts/derive-version.ts`), because automated commits sometimes don't follow the convention |

Source of truth: `classifyBump` in `scripts/derive-version.ts` — the strongest
bump across all commits since the last tag wins (major > minor > patch > none).
Merge commits (`^Merge `) are skipped; they carry no release intent of their own.

## `derive-version` is the single source of truth

- Never bump a `package.json` version by hand. `derive-version.ts` computes
  `{ release, version, bump, lastTag }` from git history; `release.script.ts`
  (`bun run release`) is the only writer, and only behind `--write`.
- First release ever (no `vX.Y.Z` tag yet): publishes whatever version is
  already declared in `packages/core/package.json`, unchanged.
- The lockstep lives in `tools/scripts/release/release-plan.ts`
  (`computeReleasePlan`): every package in `PUBLISH_ORDER` moves to the SAME
  target version, and any package with a `peerDependencies['@delendai/core']`
  gets that range rewritten to `^<target>`.

## Flow: dry-run -> --write -> tag -> publish

```
bun run release                       # dry-run: print current versions, no writes
bun run release --bump=patch          # dry-run: plan a patch bump
bun run release --bump=minor --write  # APPLY: write version+peer to every package.json
git tag vX.Y.Z && git push --tags     # tag the bumped commit (manual step today)
bun run release --publish             # validate -> build -> bun publish, in PUBLISH_ORDER
```

`--bump` and `--set=X.Y.Z` are mutually exclusive. `--publish` alone (no
`--bump`/`--set`) publishes the CURRENT versions as-is — useful for a re-run
after a failed publish without re-bumping.

## `publishOrder` (10 packages, fixed order)

From `tools/scripts/release/release-plan.ts` `PUBLISH_ORDER` — core first
(every plugin declares it as a `peerDependency`), then plugins:

```
packages/core
plugins/proposals
plugins/rules
plugins/memory
plugins/git
plugins/quality
plugins/search
plugins/notification
plugins/docs
plugins/deps
```

## Risks

- **`fix:` misclassified as `feat:`** — ships an undeserved minor bump.
  Re-read the change: did it add new capability (feat) or just correct
  behaviour (fix)? When in doubt, `fix:` is the safer default.
- **`chore:` breaking Conventional Commits** — `chore:` itself contributes
  `none` to the bump (correct), but a malformed header (e.g. missing the
  trailing `:`, or `Chore:` capitalised) falls through `parseHeader`'s regex
  and gets treated as "non-conventional with content" -> an unwanted patch
  bump, and CI's commit-lint may fail it outright. Keep the exact lowercase
  `type:` / `type(scope):` / `type!:` shape.

## Never do

- Never hand-edit a `version` field in any `package.json` — `derive-version`
  / `release.script.ts` own that field exclusively.
- Never run `--publish` without first reading the dry-run plan it prints.
- Never reorder `PUBLISH_ORDER` locally to "fix" a perceived dependency
  issue — it is derived from the real `peerDependencies` graph; if it looks
  wrong, that's a finding for a proposal, not a one-off local edit.
- Never assume a non-conventional commit message is harmless — it silently
  becomes a patch release.

## Smoke

```
bun tools/scripts/release/derive-version.script.ts   # or: bunx vitest run tools/scripts/release/*.spec.ts
```
Confirms `classifyBump(['feat: x']) === 'minor'`,
`classifyBump(['fix: x']) === 'patch'`,
`classifyBump(['feat!: x']) === 'major'`, and that `bun run release` (no
flags) prints the 10-package `PUBLISH_ORDER` table without writing anything.
