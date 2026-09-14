# CHANGELOG

<!-- version list -->

## v0.7.3 (2026-09-14)

### Bug Fixes

- **ci**: Ruff E741/E402 in smoke_relay.py + tests/test_squawk_feed.py (fat-feed merge fallout)
  ([`3d5c295`](https://github.com/toxicwind/squawk/commit/3d5c2957727ea7bd7f34284324c598eb01dfa1dc))


## v0.7.2 (2026-09-14)

### Bug Fixes

- **deps**: Update n24q02m/better-semantic-release action to v1.6.2
  ([#199](https://github.com/n24q02m/agent-chat-plugin/pull/199),
  [`0b68ca4`](https://github.com/n24q02m/agent-chat-plugin/commit/0b68ca49e4b70716a28e9cfe03d99ad6e9e94a33))

### Chores

- **release**: Fix stale two-branch comment (single-main lane)
  ([`4622c41`](https://github.com/n24q02m/agent-chat-plugin/commit/4622c41b209f86e119c4eec97427cebfc17becc6))

- **release**: Single-main release lane (staging branch retired)
  ([`92b5304`](https://github.com/n24q02m/agent-chat-plugin/commit/92b5304687396375725f91e71ee18ac35a047198))


## v0.7.1 (2026-09-12)

### Bug Fixes

- **deps**: Update astral-sh/setup-uv action to v10.1.0
  ([#192](https://github.com/n24q02m/agent-chat-plugin/pull/192),
  [`af685f4`](https://github.com/n24q02m/agent-chat-plugin/commit/af685f4d235f900aa2decda2237210e3fdc26774))

- **deps**: Update ruff to v0.16.7 ([#193](https://github.com/n24q02m/agent-chat-plugin/pull/193),
  [`19bc27b`](https://github.com/n24q02m/agent-chat-plugin/commit/19bc27bff1476c29d169222ff8b4276b877216bf))

### Chores

- Pin BSR action to v1.6.1 stable (6e688489)
  ([#194](https://github.com/n24q02m/agent-chat-plugin/pull/194),
  [`a035ea7`](https://github.com/n24q02m/agent-chat-plugin/commit/a035ea795e7014de57c160958f64ad42376c868f))

- **rulesets**: Align IaC with repo-bootstrap template
  ([`6de2ba3`](https://github.com/n24q02m/agent-chat-plugin/commit/6de2ba3c7f3dae0636c85a9bb6ae75a863f8011f))


## v0.7.0 (2026-09-11)

### Bug Fixes

- Remove duplicate metavar kwarg from event subparser (breaks py_compile)
  ([`70eb853`](https://github.com/n24q02m/agent-chat-plugin/commit/70eb85325b5e250a72c681c870d7fffe4fdb2a45))

- **deps**: Update github/codeql-action action to v4.38.0
  ([#188](https://github.com/n24q02m/agent-chat-plugin/pull/188),
  [`235ea2e`](https://github.com/n24q02m/agent-chat-plugin/commit/235ea2e5f1ad1a7e87529c64f8b81e5a8909a1b4))

### Continuous Integration

- Fix job-level if: env context invalid in jobs.<id>.if (startup failure)
  ([`e0fbd15`](https://github.com/n24q02m/agent-chat-plugin/commit/e0fbd154efab9185279cd3baf6af6e2e1c3815d5))

- Wire unified merge=release ladder (push staging=beta, main=stable)
  ([`7cf97e5`](https://github.com/n24q02m/agent-chat-plugin/commit/7cf97e5465ea9174e890e0066d795c94111f3d03))


## v0.6.1-beta.4 (2026-09-08)

### Bug Fixes

- Keep skill references portable
  ([`b9e0136`](https://github.com/n24q02m/agent-chat-plugin/commit/b9e0136d83081b80322be67a947545effee6b0b1))


## v0.6.1-beta.3 (2026-09-08)

### Bug Fixes

- Bound Agent Chat context surfaces
  ([`d9dc92a`](https://github.com/n24q02m/agent-chat-plugin/commit/d9dc92aa54c1a97b30ad505783902498ef93dc87))


## v0.6.1-beta.2 (2026-09-05)

### Bug Fixes

- Restore semantic release changelog updates
  ([#175](https://github.com/n24q02m/agent-chat-plugin/pull/175),
  [`ba98d95`](https://github.com/n24q02m/agent-chat-plugin/commit/ba98d9519f7dca21ff72b2a7c9ed7a748cc656f3))
