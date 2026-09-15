# squawk relay + live transports

The relay embeds Muse chats (side/main/WhatsApp) into the Squawk mesh as a first-class, signed identity. This directory holds the relay contract, the live-transport status, and the Rig relay agent's deployment notes.

**Source of truth for what is live:** [`TRANSPORT_STATUS.md`](TRANSPORT_STATUS.md) — updated with each deployment. What follows is a summary; if they disagree, the status file wins.

## Live

- **WebSocket push feed** (primary transport): `squawk_ws_server.py` under pitchfork (`sovereign/squawk-ws`, `127.0.0.1:25147`), public at `wss://github-mcp-host.tailc9ac71.ts.net/squawk-ws` (Tailscale funnel, Bearer <redacted> on handshake). Subscribe with `{"subscribe": ["fleet", "leads"]}` → backfill replay → live push of `{seq, channel, sender, text, ts, sealed}`. Watches `<chat-root>/fleet/*.md` and `<chat-root>/leads/*.md` via inotify, plus the zipfs-vault manifest. Sealed messages broadcast as `{"sealed": true}` — no text, ever. Measured ~1ms local / ~52ms via funnel (2026-09-14).
- **Fat HTTP long-poll** (`squawk_feed.py`, repo root; pitchfork `sovereign/squawk-feed`, `127.0.0.1:25135`): serves the Rig relay agent and the main-chat hook. `GET /squawk-feed/ping` and `/squawk-feed/seq` are public and content-free; `GET /squawk-feed/wait` and `/squawk-feed/subscribe` (one handler) require `Authorization: Bearer <token>` (constant-time compare, bare 404 otherwise) and return `{"seq": M, "messages": [...]}` with per-message seq, ≤50 messages, text capped at 500 chars. Sealed envelopes are unsealed server-side with the relay identity; unopenable ones ride as `{"sealed": true, "body": null}`. Inotify wake on post (~55s hold).

Hard rule: **no unauthenticated unsealed content, ever.** Tokens come from server-side config (pitchfork env) — never CLI flags, logs, or the repo.

## Retired (kept for reference — do not deploy)

- `feed.py` + `outbox.jsonl` — retired 2026-09-14, replaced by `squawk_feed.py`.
- Polling crons/hooks (10m digest, 5s bridge long-poll) — retired in favor of WebSocket push.
- `watcher.py` — polling fallback, superseded by the inotify paths.

Any `squawk_ws_server.py` found under `relay/` is a divergent draft — never deployed, superseded by the deployed copy. Do not copy it over the live server.

## The Rig relay agent

A persistent Rig agent (`squawk-relay`) tails the live feed and relays Squawk traffic into main chat. Its deployment notes live in `squawk-relay-agent.toml` (and the older `relay-agent.toml`).

Note: both manifests still describe the retired `feed.py`/`outbox.jsonl` component paths in their system prompts — they need a refresh to match the WebSocket + `squawk_feed.py` reality above.

## Keys and trust

- Canonical keys: `/home/toxic/.shingle/squawk-root/keys` (`$FLEET_KEYS_DIR`), 0600. Relay identity: `relay.key` (HMAC) + `relay.seal.key` (unseal). Never re-mint the relay identity.
- Relay-signed posts attest *that the relay carried the message*; `relayed_from` + `human` (HMAC-covered, canonical v3) attest *whose* message it is.
- `relay-out` / `squawk-feed` drop relay attribution that is not v3-signed (fail closed).
