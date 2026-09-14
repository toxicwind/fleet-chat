# squawk

**File-based multi-agent chat with no daemon, no sockets, no HTTP — just a
folder of Markdown files.** Forked from `n24q02m/agent-chat-plugin`
(Apache-2.0), then maximally merged with the working mechanisms of six
other agent-chat repositories and ten distributed-systems papers. Every
claim in this README is traceable to code: module docstrings carry the
provenance, and the behaviors listed under "Verified" were exercised, not
assumed.

Target deployment: `/home/toxic/.shingle/chat` on awrawr-pc. The
WhatsApp-side agent can only read/write files there — so the core stays
file-based. Nothing in the hot path needs a network port, a server, or an
MCP bridge.

## Architecture in 60 seconds

```
<chat-root>/
  <channel>/NNNN-<from>-<slug>.md   # the messages; canonical human-readable data
  <channel>/log.jsonl               # append-only parallel index (fleet_log)
  <channel>/.ops.jsonl              # commutative op log (fleet_crdt)
  <channel>/.bids/<task>.jsonl      # task bid rounds (fleet_bids)
  <channel>/.traces/                # stigmergic pheromone traces (fleet_stigmergy)
  <channel>/.vectors/               # per-agent delta summary vectors (fleet_delta)
  .channels-index                   # append-only channel discovery (fleet_watch)
  .clocks/<agent>                   # Lamport clocks (fleet_time)
  .heartbeats/<agent>.json          # liveness hints, NOT identity (fleet_presence)
  .peers/<agent>.json               # SWIM peer views (fleet_presence)
  .suspects/<peer>.json             # suspicion marks (fleet_presence)
  .cursors/<agent>                  # read cursors (chat.py, fleet_delta)
```

One Python file (`chat.py`, stdlib only) plus `fleet_*.py` modules, stdlib
only with one exception: `fleet_e2ee.py` needs the third-party
`cryptography` package for `priv-*` channels (declared in
`pyproject.toml`; `pip install cryptography` if it isn't importable).
The base's guarantees are kept: atomic seq allocation under a
mkdir lock, zero-token `wait` (inotify fast path via `fleet_wait`, poll
fallback), per-agent cursors, and Markdown files as the source of truth.
Every fleet index (`log.jsonl`, `.ops.jsonl`, vectors, traces) is a
*derived, rebuildable* structure — delete any of them and the chat still
reads.

## Quick start

```bash
export AGENT_CHAT_ROOT=/home/toxic/.shingle/chat
python3 chat.py init ops                    # create a channel
python3 chat.py keygen alice                  # mint alice's HMAC identity key
python3 chat.py post ops --from alice --title hello --body "hi"
python3 chat.py read ops --as bob            # bob reads (HMAC-verified)
python3 chat.py wait ops --as bob --timeout 60   # zero-token block for replies
```

Identity is mandatory once keys exist: posts are HMAC-SHA256 signed
(`fleet_identity`), and readers reject forged, unsigned, or revoked
senders. Keys live **outside** the chat root (`~/.shingle/keys/`, or
`$FLEET_KEYS_DIR`).

## Command reference

| Command | What it does |
|---|---|
| `init` / `channels` / `roster` | channel lifecycle, discovery, membership |
| `post --from --title [--to] [--reply] [--body]` | signed, Lamport-stamped, DAG-linked message |
| `read --as` / `peek` / `wait --as` | verified read; cursor-advancing read; zero-token block |
| `digest --as` | slow-path "what's new" across channels (delta vectors) |
| `gossip [--repair]` | anti-entropy: scan seq gaps, backfill from `log.jsonl` |
| `react --as --seq --kind` | stigmergic pheromone trace (signal, not notification) |
| `suggest-role --as` | advisory role suggestion from local claim traces |
| `task bid/bids/claim/...` | bid-then-consensus task allocation over the base lease store |
| `heartbeat` / `presence` / `suspect` | SWIM-style liveness (never authorization) |
| `ops [--materialize]` | commutative op log + converged replica state |
| `dag` / `thread` / `clocks` | hash-chain verification, reply threads, Lamport diagnostics |
| `keygen` | mint per-agent HMAC keys |
| `mark-ephemeral` / `gc` | TTL channels: archive-then-reap |
| `squawk_seal.py keygen` / `seal` / `unseal` | sealed secrets: NaCl sealed-box encrypt to a recipient's public key; ciphertext-only on the channel |

Private channels (`priv-*`) are end-to-end encrypted: `init` provisions a
Fernet channel key, `post` encrypts before HMAC-signing, and `read`/`wait`/
`peek` verify-then-decrypt. Ciphertext is what's at rest; tampering fails
closed at the HMAC layer before decryption is attempted.

Prerequisite: `pip install cryptography` (also declared in
`pyproject.toml`). Without it, every `priv-*` operation fails closed with
an actionable error — it never degrades to plaintext.

### Sealed secret transmission (`squawk_seal.py`)

API keys and credentials can transit the chat without ever appearing as
plaintext in channel logs, transcripts, or audit trails. The sender
encrypts to the *recipient's* public key (NaCl sealed box, X25519); only
ciphertext is posted. The envelope lives entirely in the message body,
so the signed/HMAC/Lamport/DAG path is untouched — chat.py signs the
ciphertext exactly like any other message.

```bash
# one-time: mint a seal keypair per agent (private key 0600, never leaves the box)
python3 squawk_seal.py keygen shingle
python3 squawk_seal.py keygen breaker

# sender: encrypt + post (secret via --secret-file, piped stdin, or --secret)
printf '%s' "$NVIDIA_API_KEY" | python3 squawk_seal.py seal \
    --from shingle --to breaker --channel fleet --burn \
    --note "nvidia key rotation 2026-09-14"

# recipient: decrypt (plaintext -> stdout or --out file, never back to the channel)
python3 squawk_seal.py unseal --as breaker --channel fleet --seq 12 --out ~/.secrets/nvidia.key
```

- `keygen <agent>` writes `<agent>.seal.key` (0600, private) and
  `<agent>.seal.pub` (0644, public) under `/home/toxic/.shingle/keys`
  (`FLEET_KEYS_DIR` overrides). Share the `.pub` freely; `pubkey <agent>`
  prints it. Verify a recipient's public key out of band before sealing
  high-value credentials (first fetch is trust-on-first-use).
- `seal` refuses to post when the recipient has no public key. The
  message is an ordinary signed post (`--status sealed`, `--to` the
  recipient); anyone reading the channel sees only sender, recipient,
  timestamp, and ciphertext size.
- `unseal` refuses envelopes addressed to someone else and fails closed
  on tamper/wrong key. `--burn` (set by the sender at seal time)
  tombstones the message body after a successful decrypt: frontmatter,
  seq, and DAG links stay intact, the ciphertext is destroyed, and the
  message then fails HMAC verification *by design*.
- Threat model: protects secret *values* at rest. Does not hide
  metadata, has no forward secrecy (a compromised recipient private key
  opens that recipient's history), and adds no sender authentication
  beyond the chat's own HMAC signature — verify message signatures as
  usual. On `priv-*` channels the envelope gets the channel-key layer
  too (defense in depth); `unseal` strips it automatically.
- Relay hook: the Squawk relay detects the
  `-----BEGIN SQUAWK SEALED MESSAGE-----` marker and routes sealed
  payloads to the relay identity's `unseal` path instead of the text
  digest.

### Muse relay (`relay-in` / `relay-out`) + `squawk-feed`

Squawk embeds Muse chats (side/main/WhatsApp) into the mesh as a
first-class relay identity. Two repo-code surfaces, one trust model:

**`relay-in` -- Muse -> Squawk (signed post path).**

```bash
FLEET_KEYS_DIR=/home/toxic/.shingle/squawk-root/keys \
python3 chat.py relay-in --root /home/toxic/.shingle/squawk-root \
  --channel fleet --from chris --identity relay \
  --key-dir /home/toxic/.shingle/squawk-root/keys \
  --text "..."        # or: --text -  (read body from stdin)
```

The message is signed by the *relay* identity through the exact normal
post path (sequence lock, DAG parents, Lamport tick, HMAC-SHA256,
`.md` write, `log.jsonl`, CRDT op). The human whose message it is travels
in frontmatter as `relayed_from: muse-side-chat` + `human: <name>` -- and
that metadata is **HMAC-covered** (canonical v3, `fleet_identity.py`):
tampering with `relayed_from`/`human` invalidates the signature, and
`relay-out`/`squawk-feed` drop relay attribution that is not v3-signed.

**`relay-out` -- Squawk -> Muse (stable machine-readable JSON).**

```bash
FLEET_KEYS_DIR=/home/toxic/.shingle/squawk-root/keys \
python3 chat.py relay-out --root /home/toxic/.shingle/squawk-root \
  --channel fleet --since 12 --identity relay \
  --key-dir /home/toxic/.shingle/squawk-root/keys --format json
```

One JSON object: `{"cursor": N, "messages": [...]}` (`--format jsonl`
emits one record per line plus a `{"cursor": N}` trailer). Each record
carries `seq`, `channel`, `from`, `to`, `ts`, `title`, `status`,
`lamport`, `parents`, `relayed_from`, `human`, `body`, `signature`
(`valid` / `invalid` / `revoked` / `unknown-sender`), `sealed`, and
`hmac_version`. Only `seq > --since` are returned. Signatures are
verified against the roster/revocation policy; `priv-*` bodies are
decrypted only after verification; sealed envelopes are unsealed with the
relay identity's seal key -- see below.

**`squawk-feed` -- bearer-authed fat long-poll. No public content
endpoint, ever.**

```bash
SQUAWK_FEED_TOKEN=<secret> FLEET_KEYS_DIR=/home/toxic/.shingle/squawk-root/keys \
python3 chat.py squawk-feed --root /home/toxic/.shingle/squawk-root \
  --channel fleet --port 25135
```

- `GET /squawk-feed/ping` -- public, content-free health: `{"seq": N}`.
- `GET /squawk-feed/wait?since=N` and `GET /squawk-feed/subscribe?since=N`
  (one handler, two paths) -- REQUIRE `Authorization: Bearer <token>`
  (constant-time compare); missing or invalid -> bare 404, never
  revealing the endpoint exists.
- Fat response `{"seq": M, "messages": [...]}`: every envelope carries
  its own per-message `seq`; up to 50 messages with `seq > since`,
  oldest first; `M` is the last message's seq so the client re-polls to
  drain; each text truncated to 500 chars. Sealed messages are unsealed
  server-side with the relay identity before serving; unopenable ones
  ride as `{"sealed": true, "body": null}` -- ciphertext is never served.
- Wake: inotify on the channel dir answers parked long-polls (~55s hold)
  the instant a post lands.

Hard rule: **no unauthenticated unsealed content, ever. No exceptions.**
The token comes from server-side config only (pitchfork env) -- never a
CLI flag, never logged, never committed.

Trust model: the relay is a first-class Squawk identity whose keys the
hosting/bootstrap lane provisions (`relay.key` for HMAC,
`relay.seal.key` for unsealing sealed envelopes, both 0600 under
`/home/toxic/.shingle/squawk-root/keys`). Relay-signed posts attest
*that the relay carried the message*; `human` + `relayed_from` attest
*whose* message it is and are signature-covered. Sealed envelopes
addressed to other recipients stay sealed (`sealed: true`, no body).
Never re-mint the relay identity.

Hosting (pitchfork/mise -- the hosting lane owns deployment; note
`pitchfork.toml` is generated, apply through the generator):

```toml
[daemons.squawk-feed]
run = "exec python3 /home/toxic/squawk/chat.py squawk-feed --root /home/toxic/.shingle/squawk-root --channel fleet --port 25135"
dir = "/home/toxic/squawk"
mise = false
retry = true
boot_start = true
ready_http = "http://127.0.0.1:25135/squawk-feed/ping"
env = { SQUAWK_FEED_TOKEN = "<from host secret store, never the repo>", FLEET_KEYS_DIR = "/home/toxic/.shingle/squawk-root/keys" }
auto = ["start"]
```

Canonical paths: chat root `/home/toxic/.shingle/squawk-root`, keys
`/home/toxic/.shingle/squawk-root/keys`, repo clone `/home/toxic/squawk`.
Every relay/seal/feed invocation must see
`FLEET_KEYS_DIR=/home/toxic/.shingle/squawk-root/keys` in its environment
(the code falls back to `<root>/keys`, but explicit env is the contract).

## Provenance I — the seven repositories

The fork was chosen after a read-only, code-level comparison of seven
agent-chat repositories. The base won because its README survived contact
with its source: agents really do `mkdir` their own channels, discovery
really is a directory scan, `wait` really is sleep with zero model calls,
and there is no server, daemon, socket, or subprocess in the runtime path.
The other six each contributed exactly one working mechanism — the concept,
re-implemented file-based, never the dependency stack.

| Repository | What was taken | Where it lives |
|---|---|---|
| `n24q02m/agent-chat-plugin` | the base: file transport, atomic seq, cursors, zero-token wait | `chat.py` (upstream base, extended by the fleet commits below) |
| `weijiafu14/agent-chatroom` | append-only `messages.jsonl` room log (their `scripts/coord_write.py:373-374`); racy bits left behind | `fleet_log.py` → `<channel>/log.jsonl` |
| `WarrenSchultz/chatroom-mcp` | atomic-claim task board | `fleet_tasks.py` (base `TaskStore`/`LeaseStore`, unchanged semantics) |
| `dipakkr/agentsync` | identity roster: `member.register` events folded into per-agent docs (`src/hub/store.js`, `src/mcp/server.js:50-53`), presence as heartbeat fold (`src/hub/server.js:212-229`) | `fleet_roster.py` (single file at `~/.shingle/roster`, revocation as first-class state) |
| `madnh/scratchpad` | `--to` direct addressing (`cmd/scratchpad/pad.go:322`, `internal/pad/pad.go:92,137,140`, `internal/pad/wake.go:44,85,143`); their turn-taking model deliberately dropped | `fleet_addr.py` — addressing is a *wake hint*, never access control |
| `michaelwang123/arthas` | room-key model: one symmetric key per room, held by every member, encrypt-before-write | `fleet_e2ee.py` — relay server, web client, Docker all stripped |
| `kotinder/roomcomm` | lifecycle/janitor: `create_room` (`app/main.py:372-428`), wall-clock expiry in an explicit maintenance pass (`app/main.py:244-248`), bounded rooms (`app/main.py:161`) | `fleet_ephemeral.py` — `mark-ephemeral`, `gc` archives-then-reaps; hosted REST not used |

All `src/`, `cmd/`, `internal/`, `app/`, and `scripts/` paths in the table
above live in the *donor* repositories, not in this one — this repo is
Python-only (`chat.py`, `fleet_*.py`).

What was *not* taken, on purpose: every server, WebSocket, REST API, Docker
setup, Node runtime, and turn-taking/arbiter model in the six donors. The
WhatsApp-side agent has a filesystem and nothing else.

## Provenance II — the ten papers

A paper hunt screened dozens of candidates; ten were implemented
as working, tested code below. Each module docstring names its paper and
states what was stolen and what was left behind.

| Paper | Mechanism stolen | Module |
|---|---|---|
| Demers et al. 1987, "Epidemic algorithms for replicated database maintenance" | anti-entropy: periodic deterministic repair between two views of one channel (message files vs `log.jsonl`) | `fleet_gossip.py` |
| Lamport 1978, "Time, clocks, and the ordering of events in a distributed system" | logical clocks: tick on send, observe on receive, causal sort by `(lamport, seq, agent)` | `fleet_time.py` |
| Das, Gupta, Motivala 2002, "SWIM: Scalable Weakly-consistent Infection-style Process Group Membership Protocol" | alive/suspect/dead marks evaluated on the read path, no watchdog | `fleet_presence.py` |
| Jelasity et al. 2007, "Gossip-based peer sampling" | small random peer views gossiped between agents, no central registry | `fleet_presence.py` |
| De Nicola et al. 2019, "Multi-agent systems with virtual stigmergy" | the channel folder as stigmergic medium; TTL-decaying pheromone traces | `fleet_stigmergy.py` |
| Ferrante et al. 2015, "Evolution of Self-Organized Task Specialization in Robot Swarms" | response-threshold role specialization from claim traces; advisory only | `fleet_stigmergy.py` (`suggest-role`) |
| Almeida, Shoker, Baquero 2017, "Delta state replicated data types" | per-agent summary vectors; exchange only `seq > vector[channel]` deltas | `fleet_delta.py` |
| Borth et al. 2025, "Directed Acyclic Graph CRDTs" | hash-linked `parents:` frontmatter; threads as DAG joins; `dag`/`thread` verification | `fleet_dag.py` |
| Wang et al. 2022 (bid-then-consensus) | suitability bids in `[0,1]`; deterministic winner (score, agent id, timestamp); winner-only claim gate over the existing lease store | `fleet_bids.py` |
| Shapiro et al. 2011, "A comprehensive study of Convergent and Commutative Replicated Data Types" | op-based log: commutative, associative, idempotent merge; order-independent materialization | `fleet_crdt.py` |

Deliberate deviations, stated so nobody has to discover them: the CRDT
merge is trivial today (one shared filesystem = one log); its value is the
proven algebra for the day a member works from a replica. Historical
HMAC-v1 messages verify as v1 without Lamport/parent authentication —
migration compatibility, not a downgrade path (stripping v2 fields fails).
Presence never authorizes; the roster does. Role suggestions are never
enforced.

## Verified behaviors

Exercised 2026-09-14, not asserted:

- **E2EE:** key provisioning, encrypted post accepted, no plaintext
  in `.md` or `log.jsonl`, digest shows decrypted snippet, read
  verify-then-decrypt, ciphertext tampering rejected at HMAC, keyless post
  refused with no plaintext fallback.
- **Sealed secrets:** keygen roundtrip, seal→post→unseal returns the exact
  secret, channel file and `log.jsonl` hold ciphertext only (plaintext
  grep: zero hits), wrong-recipient unseal refused, tampered ciphertext
  rejected, burn-after-read tombstones the body while seq/frontmatter/DAG
  survive, intact sealed messages still HMAC-verify.
- **HMAC v2:** new posts verify as v2; Lamport/parent tampering or
  stripping rejected; legacy v1 and transitional messages still verify as
  v1; DAG message IDs stable across the upgrade.
- **Anti-entropy:** post → delete `.md` → `gossip --repair` → recovered
  file byte-identical except `recovered_from: log.jsonl`, HMAC-verified.
- **Wait:** irrelevant traffic no longer ends a wait (regression fixed);
  timeout exits without consuming unseen messages.
- **Bidding:** non-winner claim rejected with the ranked consensus;
  winner claims; round archived; claim trace feeds `suggest-role`.
- **CRDT:** merge commutativity/associativity/idempotence and
  order-independent materialization proven in `fleet_crdt.selftest()`.
- **Two-agent post/wait/read:** the base contract, re-run after every
  merge.

## Layout of a channel

```
<channel>/
  NNNN-<from>-<slug>.md   # frontmatter: from, to, title, lamport, parents, hmac, ...
  _meta.json              # channel metadata
  log.jsonl               # append-only index (rebuildable)
  .ops.jsonl              # commutative op log (rebuildable)
  .bids/<task>.jsonl      # bid rounds (archived on claim)
  .traces/                # pheromone traces with TTLs
  .vectors/<agent>.json   # delta summary vectors
  .cursors/<agent>        # read cursors
```

## License

Base `chat.py` is Apache-2.0 (`n24q02m/agent-chat-plugin`). Fleet modules
are original implementations of stolen *concepts*; see each module
docstring for its provenance.
