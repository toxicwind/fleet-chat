# fleet-chat

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
  .presence/<agent>.json            # liveness hints, NOT identity (fleet_presence)
```

One Python file (`chat.py`, stdlib only) plus `fleet_*.py` modules, also
stdlib only. The base's guarantees are kept: atomic seq allocation under a
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

Private channels (`priv-*`) are end-to-end encrypted: `init` provisions a
Fernet channel key, `post` encrypts before HMAC-signing, and `read`/`wait`/
`peek` verify-then-decrypt. Ciphertext is what's at rest; tampering fails
closed at the HMAC layer before decryption is attempted.

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
| `n24q02m/agent-chat-plugin` | the base: file transport, atomic seq, cursors, zero-token wait | `chat.py` (pristine at `914d1c0`) |
| `weijiafu14/agent-chatroom` | append-only `messages.jsonl` room log (their `scripts/coord_write.py:373-374`); racy bits left behind | `fleet_log.py` → `<channel>/log.jsonl` |
| `WarrenSchultz/chatroom-mcp` | atomic-claim task board | `fleet_tasks.py` (base `TaskStore`/`LeaseStore`, unchanged semantics) |
| `dipakkr/agentsync` | identity roster: `member.register` events folded into per-agent docs (`src/hub/store.js`, `src/mcp/server.js:50-53`), presence as heartbeat fold (`src/hub/server.js:212-229`) | `fleet_roster.py` (single file at `~/.shingle/roster`, revocation as first-class state) |
| `madnh/scratchpad` | `--to` direct addressing (`cmd/scratchpad/pad.go:322`, `internal/pad/pad.go:92,137,140`, `internal/pad/wake.go:44,85,143`); their turn-taking model deliberately dropped | `fleet_addr.py` — addressing is a *wake hint*, never access control |
| `michaelwang123/arthas` | room-key model: one symmetric key per room, held by every member, encrypt-before-write | `fleet_e2ee.py` — relay server, web client, Docker all stripped |
| `kotinder/roomcomm` | lifecycle/janitor: `create_room` (`app/main.py:372-428`), wall-clock expiry in an explicit maintenance pass (`app/main.py:244-248`), bounded rooms (`app/main.py:161`) | `fleet_ephemeral.py` — `mark-ephemeral`, `gc` archives-then-reaps; hosted REST not used |

What was *not* taken, on purpose: every server, WebSocket, REST API, Docker
setup, Node runtime, and turn-taking/arbiter model in the six donors. The
WhatsApp-side agent has a filesystem and nothing else.

## Provenance II — the ten papers

A paper hunt screened 59 candidates and selected 12; ten were implemented
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

- **E2EE (7/7):** key provisioning, encrypted post accepted, no plaintext
  in `.md` or `log.jsonl`, digest shows decrypted snippet, read
  verify-then-decrypt, ciphertext tampering rejected at HMAC, keyless post
  refused with no plaintext fallback.
- **HMAC v2 (7/7):** new posts verify as v2; Lamport/parent tampering or
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
