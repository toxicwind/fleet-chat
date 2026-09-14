#!/usr/bin/env python3
"""agent-chat: peer-to-peer coordination for multiple agent sessions via markdown files.

Zero-dependency (Python stdlib only), with the structured coordination stores in
the sibling `agent_chat/` package. Run from a complete checkout or use the
installed `agent-chat` entry point. `wait` sleeps in-process between filesystem
checks; no command calls a model/provider, runs MCP, or starts a peer agent.

Model
-----
A ROOT dir holds CHANNELS (one folder each = one "group chat"). Each channel holds
numbered message files `NNNN-<from>-<slug>.md` with YAML frontmatter, a `_meta.json`
(members/topic) and per-agent read cursors under `.cursors/`. Sequence numbers are
allocated under a filesystem lock (atomic `mkdir`) so two sessions can never claim
the same number -- the exact race that produced duplicate "seq 11" files in the
hand-rolled prototype.

Commands: init | channels | roster | post | read | wait | peek | claim | lock | check | unlock | recover | recover-pending | task | state | compact | event | keygen
Run `python chat.py <command> --help` for flags.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import heapq
import io
import json
import os
import re
import sys
import time
from pathlib import Path

from fleet_addr import addressed_wait_filter

import fleet_dag
import fleet_delta
import fleet_ephemeral
import fleet_gossip
import fleet_identity
import fleet_log
import fleet_presence
import fleet_roster
import fleet_stigmergy
import fleet_time
import fleet_wait
import fleet_watch

# --- root + small helpers ----------------------------------------------------


def root_dir(explicit: str | None) -> Path:
    # Precedence: --root flag > AGENT_CHAT_ROOT env > ~/agent-chat default.
    base = (
        explicit or os.environ.get("AGENT_CHAT_ROOT") or str(Path.home() / "agent-chat")
    )
    return Path(base)


def now_iso() -> str:
    # Local time WITH offset so a git-committed thread is unambiguous across machines.
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "msg"


def _frontmatter_value(value) -> str:
    """Keep a dynamic frontmatter value on exactly one physical line."""
    return re.sub(r"[\r\n]+", " ", str(value))


class AgentChatError(Exception):
    pass


EVENT_SCHEMA_VERSION = 1
EVENT_TYPES = ("capability", "status")
CAPABILITY_PRIMITIVES = (
    "messages",
    "cursors",
    "wait",
    "tasks",
    "dependencies",
    "leases",
    "path_locks",
    "state_summary",
)
STATUS_VALUES = ("ready", "busy", "idle", "blocked", "stopped")


class AdapterEventError(AgentChatError):
    """Stable validation error for adapter-neutral events."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _event_text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(
            ord(char) < 32 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF
            for char in value
        )
    ):
        raise AdapterEventError("EVENT_INVALID_TEXT", f"{field} is invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise AdapterEventError("EVENT_INVALID_TEXT", f"{field} is invalid") from error
    try:
        _check_safe_name(value, field)
    except AgentChatError as error:
        raise AdapterEventError("EVENT_INVALID_TEXT", str(error)) from error
    return value


def _event_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise AdapterEventError("EVENT_INVALID_TIMESTAMP", "ts must be a string")
    try:
        parsed = _dt.datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        )
    except (TypeError, ValueError) as error:
        raise AdapterEventError(
            "EVENT_INVALID_TIMESTAMP", "ts must be ISO-8601"
        ) from error
    if parsed.tzinfo is None:
        raise AdapterEventError("EVENT_INVALID_TIMESTAMP", "ts must include an offset")
    return value


def validate_adapter_event(value: object) -> dict:
    if not isinstance(value, dict):
        raise AdapterEventError("EVENT_INVALID_RECORD", "event must be an object")
    event_type = value.get("event")
    if value.get("schema_version") != EVENT_SCHEMA_VERSION or isinstance(
        value.get("schema_version"), bool
    ):
        raise AdapterEventError("EVENT_UNSUPPORTED_VERSION", "schema_version must be 1")
    if event_type not in EVENT_TYPES:
        raise AdapterEventError(
            "EVENT_INVALID_TYPE", "event must be capability or status"
        )
    allowed = {"schema_version", "event", "agent", "harness", "ts"}
    if event_type == "capability":
        allowed.add("primitives")
    else:
        allowed.update({"status", "detail"})
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise AdapterEventError("EVENT_UNKNOWN_FIELD", ", ".join(unknown))
    for required in ("agent", "harness"):
        if required not in value:
            raise AdapterEventError("EVENT_REQUIRED_FIELD_MISSING", required)
        _event_text(value[required], required)
    if "ts" not in value:
        raise AdapterEventError("EVENT_REQUIRED_FIELD_MISSING", "ts")
    _event_timestamp(value["ts"])
    normalized = dict(value)
    if event_type == "capability":
        primitives = value.get("primitives")
        if (
            not isinstance(primitives, list)
            or not primitives
            or any(not isinstance(primitive, str) for primitive in primitives)
        ):
            raise AdapterEventError(
                "EVENT_INVALID_PRIMITIVES", "primitives must be strings"
            )
        if len(set(primitives)) != len(primitives):
            raise AdapterEventError(
                "EVENT_DUPLICATE_PRIMITIVE", "primitives must be unique"
            )
        for primitive in primitives:
            if primitive not in CAPABILITY_PRIMITIVES:
                raise AdapterEventError("EVENT_UNKNOWN_PRIMITIVE", str(primitive))
        normalized["primitives"] = list(primitives)
    else:
        status = value.get("status")
        if status not in STATUS_VALUES:
            raise AdapterEventError("EVENT_INVALID_STATUS", str(status))
        if "detail" in value:
            detail = value["detail"]
            if not isinstance(detail, str) or any(
                ord(char) < 32
                or 0x7F <= ord(char) <= 0x9F
                or 0xD800 <= ord(char) <= 0xDFFF
                for char in detail
            ):
                raise AdapterEventError("EVENT_INVALID_TEXT", "detail is invalid")
            try:
                detail.encode("utf-8")
            except UnicodeEncodeError as error:
                raise AdapterEventError(
                    "EVENT_INVALID_TEXT", "detail is invalid"
                ) from error
    return normalized


def make_capability_event(
    agent: str,
    harness: str,
    *,
    primitives: list[str] | None = None,
    timestamp: str | None = None,
) -> dict:
    return validate_adapter_event(
        {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event": "capability",
            "agent": agent,
            "harness": harness,
            "ts": timestamp or now_iso(),
            "primitives": list(primitives or CAPABILITY_PRIMITIVES),
        }
    )


def make_status_event(
    agent: str,
    harness: str,
    status: str,
    *,
    detail: str | None = None,
    timestamp: str | None = None,
) -> dict:
    event = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event": "status",
        "agent": agent,
        "harness": harness,
        "ts": timestamp or now_iso(),
        "status": status,
    }
    if detail is not None:
        event["detail"] = detail
    return validate_adapter_event(event)


def die(msg: str, code: int = 1):
    print(f"agent-chat: {msg}", file=sys.stderr)
    raise SystemExit(code)


# --- channel + message primitives -------------------------------------------


def _check_safe_name(name: str, kind: str):
    """Prevent path traversal vulnerabilities."""
    if not name or "/" in name or "\\" in name or ":" in name or name in (".", ".."):
        raise AgentChatError(f"invalid {kind} name (path traversal blocked): '{name}'")
    if name.startswith(".") or name.startswith("_"):
        raise AgentChatError(f"invalid {kind} name (reserved prefix blocked): '{name}'")


_TASK_MARKER_RE = re.compile(r"task-[A-Za-z0-9][A-Za-z0-9_-]*\.md")


def channel_dir(root: Path, channel: str) -> Path:
    _check_safe_name(channel, "channel")
    return root / channel


def require_channel(root: Path, channel: str) -> Path:
    d = channel_dir(root, channel)
    if not (d / "_meta.json").exists():
        raise AgentChatError(
            f"channel '{channel}' not found under {root} (run: init {channel})"
        )
    return d


def _seq_from_name(name: str) -> int | None:
    # Optimization: Native string parsing (.split, .isdecimal) is ~40% faster
    # than re.match in tight polling loops. isdecimal is used to prevent
    # ValueError on Unicode superscripts (e.g. ², which isdigit accepts).
    parts = name.split("-", 1)
    if len(parts) == 2 and parts[0].isdecimal():
        return int(parts[0])
    return None


def message_files(chan: Path):
    files = []
    try:
        with os.scandir(chan) as it:
            files = [
                Path(e.path)
                for e in it
                if e.name.endswith(".md") and _seq_from_name(e.name) is not None
            ]
    except OSError:
        pass
    return sorted(files, key=lambda p: _seq_from_name(p.name))


def parse_frontmatter(path: Path) -> dict:
    """Minimal front-matter reader: the block between the first two '---' lines.

    Values are strings except `to`, normalized to a list ([] == broadcast/all).
    """
    meta: dict = {}
    try:
        with path.open(encoding="utf-8") as f:
            first_line = f.readline()
            if not first_line.startswith("---"):
                return meta
            temp_meta = {}
            found_end = False
            for line in f:
                stripped = line.strip()
                if stripped == "---":
                    found_end = True
                    break
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                temp_meta[k.strip()] = v.strip()
            if not found_end:
                return meta
            meta = temp_meta
    except (OSError, UnicodeError):
        return meta
    # Normalize `to` -> list of recipients (empty == everyone).
    raw = meta.get("to", "").strip()
    if raw in ("", "all", "[]", "*"):
        meta["to_list"] = []
    else:
        meta["to_list"] = [x.strip() for x in raw.strip("[]").split(",") if x.strip()]
    return meta


def is_relevant(meta: dict, agent: str) -> bool:
    # Delegated to fleet_addr (ported from madnh/scratchpad): `to` is a hint
    # for wake-worthiness, never a visibility lock. Broadcast is the default.
    return addressed_wait_filter(meta, agent)


# --- atomic sequence lock ----------------------------------------------------


def _acquire_lock(chan: Path, timeout: float = 10.0, stale: float = 30.0) -> Path:
    """Atomic cross-platform lock via mkdir (fails if the dir already exists).

    Steals a lock older than `stale` seconds so a crashed poster can't wedge the
    channel forever.
    """
    lock = chan / "_seq.lock"
    start = time.time()
    while True:
        try:
            os.mkdir(lock)
            return lock
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale:
                    try:
                        os.rmdir(lock)
                    except OSError:
                        pass
                    continue
            except FileNotFoundError:
                continue
            if time.time() - start > timeout:
                raise AgentChatError(
                    "could not acquire channel seq lock (another poster is stuck?)"
                )
            time.sleep(0.05)


def _release_lock(lock: Path):
    try:
        os.rmdir(lock)
    except OSError:
        pass


def _next_seq(chan: Path) -> int:
    return max_seq(chan) + 1


# --- cursors -----------------------------------------------------------------


def cursor_path(chan: Path, agent: str) -> Path:
    return chan / ".cursors" / f"{slugify(agent)}.txt"


def read_cursor(chan: Path, agent: str) -> int:
    p = cursor_path(chan, agent)
    try:
        return int(p.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def write_cursor(chan: Path, agent: str, seq: int):
    p = cursor_path(chan, agent)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(seq), encoding="utf-8")


def max_seq(chan: Path) -> int:
    maximum = 0
    try:
        with os.scandir(chan) as it:
            for entry in it:
                if entry.name.endswith(".md"):
                    seq = _seq_from_name(entry.name)
                    if seq is not None and seq > maximum:
                        maximum = seq
    except OSError:
        pass
    return maximum


# --- commands ----------------------------------------------------------------


def cmd_init(root: Path, a):
    d = channel_dir(root, a.channel)
    d.mkdir(parents=True, exist_ok=True)
    (d / ".cursors").mkdir(exist_ok=True)
    meta_path = d / "_meta.json"
    if meta_path.exists():
        raise AgentChatError(f"channel '{a.channel}' already exists")
    members = [m.strip() for m in (a.members or "").split(",") if m.strip()]
    meta_path.write_text(
        json.dumps(
            {
                "channel": a.channel,
                "members": members,
                "topic": a.topic or "",
                "created": now_iso(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if a.ephemeral is not None:
        fleet_ephemeral.mark_ephemeral(d, float(a.ephemeral))
    # Fleet discovery: index the channel AFTER _meta.json is durably written,
    # so a crashed init never indexes a half-made channel.
    fleet_watch.note_channel(root, a.channel)
    m_str = ", ".join(members) if members else "(open)"
    eph = f" ephemeral(ttl={a.ephemeral}s)" if a.ephemeral is not None else ""
    print(f"created channel '{a.channel}' at {d}  members={m_str}{eph}")


def cmd_keygen(root: Path, a):
    try:
        key_path = fleet_identity.keygen(a.agent_id, force=a.force)
    except fleet_identity.FleetIdentityError as e:
        die(str(e))
    print(f"key written for '{a.agent_id}' at {key_path}  (keep it secret; 0600)")


def cmd_mark_ephemeral(root: Path, a):
    d = require_channel(root, a.channel)
    fleet_ephemeral.mark_ephemeral(d, float(a.ttl))
    print(f"channel '{a.channel}' marked ephemeral (ttl={a.ttl}s)")


def cmd_gc(root: Path, a):
    if a.dry_run:
        expired = []
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if not entry.is_dir() or entry.name.startswith("."):
                        continue
                    try:
                        if fleet_ephemeral.is_expired(Path(entry.path)):
                            expired.append(entry.name)
                    except OSError:
                        pass
        except OSError as e:
            die(f"cannot scan chat root: {e}")
        if expired:
            print("would reap (expired ephemeral channels):")
            for name in sorted(expired):
                print(f"  {name}")
        else:
            print("(no expired ephemeral channels)")
        return
    reaped = fleet_ephemeral.gc(root)
    if reaped:
        print("reaped (archived to .archive/ first):")
        for name in reaped:
            print(f"  {name}")
    else:
        print("(no expired ephemeral channels)")


def cmd_heartbeat(root: Path, a):
    """Explicit per-turn liveness ping. Call at the top of every agent turn.

    Incarnation bumps on every call, so this is NOT auto-wired into other
    commands -- one heartbeat per agent turn is the contract.
    """
    hb = fleet_presence.heartbeat(root, a.agent)
    print(f"heartbeat for '{a.agent}': incarnation={hb['incarnation']}")


def cmd_presence(root: Path, a):
    """SWIM-style liveness hints. NEVER authorization: fleet_roster.py is the
    trust registry; this is gossip-targeting only."""
    states = fleet_presence.alive_agents(root)
    names = sorted([a.agent] if a.agent else states)
    if not names:
        print("(no heartbeats recorded yet)")
        return
    print(f"{'AGENT':<28}{'STATE':<10}LAST-SEEN")
    for name in names:
        state = states.get(name, "dead")  # never heartbeated
        age = fleet_presence.heartbeat_age(root, name)
        age_s = "never" if age == float("inf") else f"{age:.0f}s ago"
        print(f"{name:<28}{state:<10}{age_s}")
    print()
    print("liveness hints, NOT credentials: never use presence for authorization.")
    print("suspect/dead = no heartbeat within 60s/300s; idle and crashed are")
    print("indistinguishable. A fresh heartbeat always refutes suspicion marks.")


def cmd_react(root: Path, a):
    """Deposit a pheromone trace pointing at a message (stigmergic signal).

    Reactions are signals, NOT notifications: nobody is paged; agents that
    read the field notice. Traces decay with their TTL and are invisible
    past it.
    """
    d = require_channel(root, a.channel)
    fleet_stigmergy.react(
        root, a.channel, a.agent, target_seq=a.seq, kind=a.kind,
        strength=a.strength, ttl_s=a.ttl, note=a.note or "",
    )
    print(f"trace deposited on #{a.seq} in '{a.channel}' (kind={a.kind})")


def cmd_gossip(root: Path, a):
    """Anti-entropy pass (Demers et al. 1987): scan for seq gaps, backfill
    recoverable ones from log.jsonl, publish a divergence digest.

    Backfilled files are byte-faithful reconstructions (original frontmatter
    + original hmac, plus a non-HMAC-covered recovered_from marker), so they
    pass verification on the read path. Gossip never allocates seqs.
    """
    if a.channel:
        gaps = fleet_gossip.scan_gaps(root, a.channel)
        bf = fleet_gossip.backfill(root, a.channel) if a.repair else None
        if a.json:
            print(json.dumps({"gaps": gaps, "backfill": bf}, indent=2))
        else:
            missing = [m["seq"] for m in gaps["missing"]]
            print(f"channel '{a.channel}': max_seq={gaps['max_seq']}, "
                  f"missing={missing or 'none'}")
            if bf:
                print(f"  backfilled={bf['recovered'] or 'none'}, "
                      f"unrecoverable={[m['seq'] for m in bf['unrecoverable']] or 'none'}")
                if bf["log_error"]:
                    print(f"  log_error={bf['log_error']}")
        return
    report = (fleet_gossip.anti_entropy(root, a.agent) if a.repair
              else fleet_gossip.scan_only(root, a.agent))
    if a.json:
        print(json.dumps(report, indent=2))
        return
    print(f"gossip pass for '{a.agent}': {len(report['channels'])} channel(s)")
    for ch, seqs in sorted(report["gaps_found"].items()):
        print(f"  {ch}: gaps at seq {seqs}")
    for ch, seqs in sorted(report["backfilled"].items()):
        print(f"  {ch}: backfilled seq {seqs}")
    for ch, items in sorted(report["unrecoverable"].items()):
        seqs = [m["seq"] if isinstance(m, dict) else m for m in items]
        print(f"  {ch}: UNRECOVERABLE seq {seqs}")
    for ch, others in sorted(report["divergent"].items()):
        print(f"  {ch}: divergent vs {others}")
    for k, e in sorted(report["errors"].items()):
        print(f"  error [{k}]: {e}")
    if report["unrecoverable"] or report["errors"]:
        raise SystemExit(3)


def cmd_suggest_role(root: Path, a):
    """ADVISORY ONLY role suggestion from local claim traces.

    Emergent specialization (Ferrante et al. 2015): the fleet self-balances
    because agents follow such local readings, not because anyone assigns.
    This command never enforces, never writes, never orders -- there is no
    --enforce flag and there never will be.
    """
    require_channel(root, a.channel)
    s = fleet_stigmergy.suggest_role(root, a.channel, a.agent)
    if s is None:
        print("(no traces yet -- nothing to suggest; the field is empty)")
        return
    print(json.dumps(s, indent=2))


def cmd_suspect(root: Path, a):
    """Record a SWIM suspicion mark (gossip hint with attribution, not a verdict)."""
    ok = fleet_presence.suspect(root, a.by, a.peer, a.reason or "")
    if ok:
        print(f"suspicion mark recorded: {a.by} suspects {a.peer}")
    else:
        print(f"(no mark: {a.peer} heartbeat is fresh or never seen)")


def cmd_channels(root: Path, a):
    if not root.exists():
        print(f"(no channels yet under {root})")
        return
    rows = []
    found_channels = []
    # Optimization: Use os.scandir instead of Path.glob("*/_meta.json") to discover channels.
    # This avoids instantiating thousands of Path objects for discarded subdirectories.
    # Filters out hidden directories (starting with '.') to maintain parity with glob("*").
    try:
        with os.scandir(root) as it:
            for entry in it:
                if (
                    not entry.name.startswith(".")
                    and entry.is_dir()
                    and os.path.exists(os.path.join(entry.path, "_meta.json"))
                ):
                    found_channels.append(entry.name)
    except OSError:
        pass
    for chan_name in sorted(found_channels):
        chan = root / chan_name
        meta_path = chan / "_meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        count = 0
        last_path = None
        last_seq = 0
        try:
            with os.scandir(chan) as it:
                for entry in it:
                    if not entry.name.endswith(".md"):
                        continue
                    seq = _seq_from_name(entry.name)
                    if seq is None:
                        continue
                    count += 1
                    if last_path is None or seq > last_seq:
                        last_path = Path(entry.path)
                        last_seq = seq
        except OSError:
            pass
        last = "-"
        if last_path is not None:
            lm = parse_frontmatter(last_path)
            title = lm.get("title", "")
            if len(title) > 40:
                title = title[:37] + "..."
            last = f"#{last_seq} {lm.get('from', '?')}: {title}"
        members_str = ", ".join(meta.get("members", [])) or "(open)"
        if len(members_str) > 40:
            members_str = members_str[:37] + "..."
        rows.append((chan.name, members_str, count, last))
    if not rows:
        print(f"(no channels yet under {root})")
        return
    w = max(len("CHANNEL"), max(len(r[0]) for r in rows))
    print(f"{'CHANNEL'.ljust(w)}  MSGS  MEMBERS / LAST")
    for name, members, n, last in rows:
        print(f"{name.ljust(w)}  {str(n).rjust(4)}  {members}")
        print(f"{' '.ljust(w)}        last: {last}")


def cmd_roster(root: Path, a):
    d = require_channel(root, a.channel)
    try:
        meta = json.loads((d / "_meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise AgentChatError(
            f"could not read or parse _meta.json for channel '{a.channel}'"
        )
    print(f"channel : {meta.get('channel')}")
    print(f"topic   : {meta.get('topic') or '(none)'}")
    print(f"members : {', '.join(meta.get('members', [])) or '(open)'}")
    count = 0
    try:
        with os.scandir(d) as it:
            count = sum(
                1
                for entry in it
                if entry.name.endswith(".md") and _seq_from_name(entry.name) is not None
            )
    except OSError:
        pass
    print(f"messages: {count}")


def _read_body(a) -> str:
    if a.body is not None:
        return a.body
    if a.body_file:
        try:
            return Path(a.body_file).read_text(encoding="utf-8")
        except OSError as e:
            raise AgentChatError(f"could not read body file: {e}")
        except UnicodeError as e:
            raise AgentChatError(f"could not read body file: {e}")
    # Default: read from stdin so agents can pipe long markdown bodies.
    if sys.stdin.isatty():
        print(
            "agent-chat: Enter message body; press Ctrl-D (or Ctrl-Z and Enter on Windows) to finish.",
            file=sys.stderr,
        )
    try:
        data = sys.stdin.read()
        # Force encoding to catch surrogates immediately
        data.encode("utf-8")
    except (OSError, UnicodeError) as e:
        raise AgentChatError(f"could not read body from stdin: {e}")

    if not data.strip():
        raise AgentChatError("empty body (pass --body, --body-file, or pipe via stdin)")
    return data


def _resolve_reply_target(d: Path, reply: str):
    """Resolve a --reply value to a parent message id (or None)."""
    m = re.fullmatch(r"#?(\d+)", reply.strip())
    if not m:
        return None  # name-style reply; no id join
    want = int(m.group(1))
    for p in message_files(d):
        if _seq_from_name(p.name) == want:
            return fleet_dag.msg_id(p)
    return None


def _dag_parents(d: Path, seq: int, reply):
    """Fleet DAG parent ids for a new message: [reply_target?, previous].

    Must run INSIDE the seq lock, after _next_seq: the "previous message" id
    is only stable while we hold it. Genesis (seq 1) gets [].
    """
    prev_id = None
    if seq > 1:
        prev_path = None
        prev_seq = 0
        for p in message_files(d):
            ps = _seq_from_name(p.name)
            if ps is not None and ps < seq and ps > prev_seq:
                prev_seq, prev_path = ps, p
        if prev_path is not None:
            prev_id = fleet_dag.msg_id(prev_path)
    target_id = _resolve_reply_target(d, reply) if reply else None
    # Dedupe preserving wire order: a reply to the immediately-previous
    # message would otherwise list the same parent twice.
    parents = []
    for x in (target_id, prev_id):
        if x and x not in parents:
            parents.append(x)
    return parents


def cmd_post(root: Path, a):
    d = require_channel(root, a.channel)
    body = _read_body(a)
    sender = _frontmatter_value(a.sender)
    to = _frontmatter_value(a.to or "all")
    reply = _frontmatter_value(a.reply) if a.reply else None
    channel = _frontmatter_value(a.channel)
    timestamp = _frontmatter_value(now_iso())
    status = _frontmatter_value(a.status)
    title = _frontmatter_value(a.title)
    lock = _acquire_lock(d)
    try:
        seq = _next_seq(d)
        # Fleet DAG: parent ids, computed under the seq lock (race-free).
        parents = _dag_parents(d, seq, reply)
        # Fleet Lamport: tick the sender's clock; readers sort causally.
        lamport = fleet_time.tick(root, sender)
        fname = f"{seq:04d}-{slugify(a.sender)}-{slugify(a.title)}.md"
        fm = [
            "---",
            f"seq: {seq}",
            f"from: {sender}",
            f"to: {to}",
        ]
        if reply is not None:
            fm.append(f"reply_to: {reply}")
        # Fleet identity: HMAC-sign the canonical message bytes. sign() raises
        # FleetIdentityError when the sender has no key -> the post fails closed.
        sig = fleet_identity.sign(
            sender,
            fleet_identity.canonical_message(
                seq=seq,
                sender=sender,
                to=to,
                reply_to=reply,
                channel=channel,
                ts=timestamp,
                status=status,
                title=title,
                body=body,
            ),
        )
        fm += [
            f"channel: {channel}",
            f"ts: {timestamp}",
            f"status: {status}",
            f"title: {title}",
            f"lamport: {lamport}",
            f"parents: [{', '.join(parents)}]",
            f"hmac: {sig}",
            "---",
            "",
        ]
        (d / fname).write_text("\n".join(fm) + body.rstrip() + "\n", encoding="utf-8")
        # Fleet log: parallel append-only JSONL index (one os.write per record,
        # seq assigned by the caller under the existing seq lock). Never fails
        # the post: the .md file is the source of truth; a lost/corrupt
        # log.jsonl is always rebuildable from message files.
        try:
            fleet_log.append(
                root,
                channel,
                seq=seq,
                agent=sender,
                type="message",
                body=body,
                ts=timestamp,
                # Fidelity fields: let anti-entropy backfill reconstruct a
                # byte-identical, HMAC-verifiable message file.
                msg_hmac=sig,
                to=to,
                title=title,
                reply_to=reply,
                status=status,
                lamport=lamport,
                parents=parents,
            )
        except Exception as e:  # noqa: BLE001 -- the index must not break posts
            print(f"(warning: log.jsonl append failed: {e})", file=sys.stderr)
    finally:
        _release_lock(lock)
    print(f"posted #{seq} -> {a.channel}/{fname}")


def _print_message(path: Path):
    print("=" * 70)
    try:
        print(path.read_text(encoding="utf-8").rstrip())
    except (OSError, UnicodeError) as e:
        print(f"(could not read message {path.name}: {e})")
    print()


def _sender_cleared(meta: dict) -> None:
    """Roster revocation gate: call after HMAC verification on a read path.

    Rejects revoked senders outright. Unknown senders are rejected once the
    roster is enrolled (non-empty); an empty roster means bootstrap mode where
    HMAC alone is the gate.
    """
    sender = meta.get("from", "")
    rec = fleet_roster.lookup(sender)
    if rec is not None and rec.get("revoked"):
        die(f"identity check failed: sender '{sender}' is revoked")
    if rec is None and fleet_roster.list_all():
        die(f"identity check failed: sender '{sender}' is not enrolled in the fleet roster")


def cmd_digest(root: Path, a):
    """Slow-path what's-new digest across ALL channels (delta-state sync).

    One line per unread message: "<channel> #<seq> <from>-><to> <lamport>
    <body, 80 chars>". The per-agent vector (.vectors/<agent>.json) advances
    unless --peek. First use migrates the base's .cursors into the vector so
    the digest starts from "what I've read".

    NOTE: digest lines are addressing-filtered hints for slow-path agents;
    the HMAC/roster trust boundary is enforced on the full read path.
    """
    vec = fleet_delta.load_vector(root, a.agent)
    if not vec:
        vec = fleet_delta.migrate_from_cursors(root, a.agent)
    deltas = fleet_delta.delta(root, a.agent)
    for line in fleet_delta.delta_digest(root, a.agent, relevant_only=not a.all):
        print(line)
    if not deltas:
        print(f"(no new messages for {a.agent}; vector unchanged)")
        return
    if not a.peek:
        adv = dict(vec)
        for ch, paths in deltas.items():
            top = max((_seq_from_name(p.name) or 0) for p in paths)
            adv[ch] = max(adv.get(ch, 0), top)
        fleet_delta.advance(root, a.agent, adv)


def cmd_read(root: Path, a):
    d = require_channel(root, a.channel)
    cur = 0 if a.all else read_cursor(d, a.agent)
    shown = 0

    # Optimization: One O(N) glob scan to find both top seq and unread messages,
    # avoiding O(N log N) message_files sort and redundant max_seq glob.
    found = []
    top = 0
    try:
        with os.scandir(d) as it:
            for entry in it:
                if not entry.name.endswith(".md"):
                    continue
                seq = _seq_from_name(entry.name)
                if seq is None:
                    continue
                top = max(top, seq)
                if seq > cur:
                    found.append((seq, Path(entry.path)))
    except OSError:
        pass

    found.sort(key=lambda x: x[0])

    for seq, p in found:
        meta = parse_frontmatter(p)
        if not a.all and not is_relevant(meta, a.agent):
            continue
        try:
            meta = fleet_identity.verify_on_read(p)
        except fleet_identity.FleetIdentityError as e:
            die(f"identity check failed: {e}")
        _sender_cleared(meta)
        # Fleet Lamport: fold the sender's clock into ours (max, no tick).
        fleet_time.observe(root, a.agent, fleet_time.message_lamport(meta))
        _print_message(p)
        shown += 1

    if not a.peek:
        write_cursor(d, a.agent, top)
    if shown == 0:
        print(f"(no new messages for {a.agent} in '{a.channel}'; cursor at #{cur})")


def cmd_wait(root: Path, a):
    # Fleet fast path: fleet_wait arms inotify BEFORE the initial scan (race-free),
    # falling back to scandir polling on non-Linux or inotify failure. Zero-token:
    # blocks in-process, no subprocess, no network. --interval survives as the
    # poll-fallback quantum.
    import fleet_wait as _fw

    _fw._POLL_TICK = max(0.05, a.interval)  # --interval survives as the poll-fallback quantum

    d = require_channel(root, a.channel)
    cur = read_cursor(d, a.agent)
    deadline = time.time() + a.timeout
    while True:
        remaining = max(0.0, deadline - time.time())
        found = _fw.wait_for_new_messages(d, cur, remaining)
        if found:
            for p in found:
                meta = parse_frontmatter(p)
                if not a.all and not is_relevant(meta, a.agent):
                    continue
                try:
                    meta = fleet_identity.verify_on_read(p)
                except fleet_identity.FleetIdentityError as e:
                    die(f"identity check failed: {e}")
                _sender_cleared(meta)
                # Fleet Lamport: fold the sender's clock into ours.
                fleet_time.observe(root, a.agent, fleet_time.message_lamport(meta))
                _print_message(p)
            write_cursor(d, a.agent, max_seq(d))
            return
        # found == [] means the timeout expired with no new relevant messages.
        print(
            f"(timeout after {a.timeout}s: no new messages for {a.agent} in '{a.channel}')",
            file=sys.stderr,
        )
        raise SystemExit(2)


def cmd_peek(root: Path, a):
    d = require_channel(root, a.channel)

    if a.n <= 0:
        return

    # Optimization: Use a min-heap to find top N messages in O(N log K) time
    # rather than sorting all messages O(N log N) via message_files()
    top_n = []
    try:
        with os.scandir(d) as it:
            for entry in it:
                if not entry.name.endswith(".md"):
                    continue
                seq = _seq_from_name(entry.name)
                if seq is not None:
                    if len(top_n) < a.n:
                        heapq.heappush(top_n, (seq, Path(entry.path)))
                    elif seq > top_n[0][0]:
                        heapq.heapreplace(top_n, (seq, Path(entry.path)))
    except OSError:
        pass

    # Extract in ascending order (heappop gets the smallest first)
    files = [heapq.heappop(top_n)[1] for _ in range(len(top_n))]

    for p in files:
        try:
            meta = fleet_identity.verify_on_read(p)
        except fleet_identity.FleetIdentityError as e:
            die(f"identity check failed: {e}")
        _sender_cleared(meta)
        _print_message(p)
    if not files:
        print(f"(channel '{a.channel}' is empty)")


def cmd_claim(root: Path, a):
    """Atomically claim a task marker file by renaming it (os.replace is atomic).

    Convention: a claimable task is a file `task-<id>.md`. Claiming renames it to
    `task-<id>.CLAIMED-<agent>.md`. If the source is already gone, another agent
    won the race -- exit non-zero so the caller moves on.
    """
    _check_safe_name(a.task, "task")
    if not _TASK_MARKER_RE.fullmatch(a.task):
        raise AgentChatError(
            f"invalid task name (expected task-<id>.md marker): '{a.task}'"
        )
    d = require_channel(root, a.channel)
    src = d / a.task
    dst = d / (Path(a.task).stem + f".CLAIMED-{slugify(a.agent)}.md")
    lock = _acquire_lock(d)
    try:
        if dst.exists():
            die(f"task '{a.task}' already claimed or missing (lost the race)", code=3)
        if not src.is_file():
            die(f"task '{a.task}' already claimed or missing (lost the race)", code=3)
        try:
            os.replace(src, dst)  # atomic on Windows + POSIX within the claim lock
        except FileNotFoundError:
            die(f"task '{a.task}' already claimed or missing (lost the race)", code=3)
    finally:
        _release_lock(lock)
    print(f"claimed {a.task} -> {dst.name}")


def _task_store(root: Path, channel: str):
    from agent_chat.task_model import TaskValidationError
    from agent_chat.task_store import TaskStore

    try:
        chan = channel_dir(root, channel)
    except AgentChatError as error:
        raise TaskValidationError(
            "TASK_INVALID_CHANNEL",
            f"invalid channel name: '{channel}' ({error})",
        ) from error
    return TaskStore(chan, root=root)


def _lease_store(root: Path, channel: str):
    from agent_chat.lease_store import LeaseStore
    from agent_chat.task_model import TaskValidationError

    try:
        chan = channel_dir(root, channel)
    except AgentChatError as error:
        raise TaskValidationError(
            "TASK_INVALID_CHANNEL",
            f"invalid channel name: '{channel}' ({error})",
        ) from error
    return LeaseStore(chan, root=root)


def _path_lock_store(root: Path, channel: str):
    from agent_chat.path_locks import PathLockStore

    try:
        chan = channel_dir(root, channel)
    except AgentChatError as error:
        from agent_chat.path_locks import PathLockError

        raise PathLockError(
            "PATH_LOCK_INVALID_CHANNEL",
            f"invalid channel name: '{channel}' ({error})",
        ) from error
    return PathLockStore(chan, root=root)


def _state_store(root: Path, channel: str):
    from agent_chat.state_store import StateStore, StateValidationError

    try:
        chan = channel_dir(root, channel)
    except AgentChatError as error:
        raise StateValidationError(
            "STATE_INVALID_CHANNEL",
            f"invalid channel name: '{channel}' ({error})",
        ) from error
    return StateStore(chan, root=root)


def cmd_state(root: Path, a):
    store = _state_store(root, a.channel)
    if getattr(a, "write", False):
        # The durable audit message is internal state; JSON mode must emit only
        # the requested document.
        with contextlib.redirect_stdout(io.StringIO()):
            summary = store.compact(
                actor=getattr(a, "actor", None),
                audit=not getattr(a, "no_audit", False),
                strict=getattr(a, "strict", False),
            )
        if getattr(a, "json", False):
            print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
        else:
            print(f"compacted state for {a.channel} -> {a.channel}/state.md")
    else:
        if getattr(a, "json", False):
            summary = store.summarize(strict=getattr(a, "strict", False))
            print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
        else:
            md = store.render(strict=getattr(a, "strict", False))
            print(md, end="")


def cmd_compact(root: Path, a):
    store = _state_store(root, a.channel)
    # Keep the audit message while reserving stdout for this command's result.
    with contextlib.redirect_stdout(io.StringIO()):
        summary = store.compact(
            actor=getattr(a, "actor", None),
            audit=not getattr(a, "no_audit", False),
            strict=getattr(a, "strict", False),
        )
    if getattr(a, "json", False):
        print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
    else:
        print(
            f"compacted state for {a.channel} -> {a.channel}/state.md (open_tasks={len(summary.open_tasks)}, locks={len(summary.path_locks)}, decisions={len(summary.decisions)})"
        )


def _event_body(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
        parts = raw.split("---", 2)
        body = parts[2].strip() if len(parts) >= 3 else ""
        return validate_adapter_event(json.loads(body))
    except (json.JSONDecodeError, UnicodeError, OSError) as error:
        raise AdapterEventError("EVENT_MALFORMED_BODY", path.name) from error


def cmd_event_post(root: Path, a):
    event_type = a.event_type
    if event_type == "capability":
        primitives = None
        if a.primitives:
            primitives = [
                value.strip()
                for item in a.primitives
                for value in item.split(",")
                if value.strip()
            ]
        event = make_capability_event(
            a.sender,
            a.harness,
            primitives=primitives,
        )
    else:
        event = make_status_event(
            a.sender,
            a.harness,
            a.status,
            detail=a.detail,
        )
    args = argparse.Namespace(
        channel=a.channel,
        sender=a.sender,
        to="all",
        reply=None,
        status=f"event.{event['event']}",
        title=f"event:{event['event']}",
        body=json.dumps(
            event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        body_file=None,
    )
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_post(root, args)
    print(f"posted event {event['event']} -> {a.channel}")


def cmd_event_read(root: Path, a):
    channel = require_channel(root, a.channel)
    expected = getattr(a, "event_type", None)
    for path in message_files(channel):
        meta = parse_frontmatter(path)
        status = meta.get("status", "")
        if not status.startswith("event."):
            continue
        event = _event_body(path)
        if expected and event["event"] != expected:
            continue
        print(json.dumps(event, ensure_ascii=False, sort_keys=True))


def cmd_lock(root: Path, a):
    store = _path_lock_store(root, a.channel)
    with contextlib.redirect_stdout(io.StringIO()):
        record = store.lock(
            a.owner,
            a.paths,
            lease_seconds=a.lease_seconds,
            actor=a.owner,
        )
    normalized = ", ".join(path.normalized_path for path in record.paths)
    print(f"locked {record.lock_id} -> {a.channel}/{normalized}")


def cmd_check(root: Path, a):
    store = _path_lock_store(root, a.channel)
    conflicts = store.check(a.paths, owner=a.owner)
    if not conflicts:
        print("available")
        return
    for record in conflicts:
        expiry = f" expires={record.expires_at}"
        print(f"locked {record.lock_id} owner={record.owner}{expiry}")


def cmd_unlock(root: Path, a):
    store = _path_lock_store(root, a.channel)
    with contextlib.redirect_stdout(io.StringIO()):
        record = store.unlock(a.target, a.owner, actor=a.owner)
    print(f"unlocked {record.lock_id} from {a.channel}")


def cmd_path_recover(root: Path, a):
    store = _path_lock_store(root, a.channel)
    with contextlib.redirect_stdout(io.StringIO()):
        record = store.recover(
            a.target,
            a.owner,
            a.reason,
            lease_seconds=a.lease_seconds,
            actor=a.owner,
        )
    print(
        f"recovered {record.lock_id} for {record.owner} "
        f"previous_owner={record.previous_owner} reason={record.recovery_reason}"
    )


def cmd_path_recover_pending(root: Path, a):
    store = _path_lock_store(root, a.channel)
    with contextlib.redirect_stdout(io.StringIO()):
        store.recover_pending(
            actor=a.actor,
            publication_resolution=a.publication_resolution,
        )
    print(f"recovered pending path-lock transaction in {a.channel}")


def _task_values(values) -> list[str]:
    items: list[str] = []
    for value in values or []:
        items.extend(item.strip() for item in value.split(",") if item.strip())
    return items


def _task_actor(args) -> str:
    return args.actor


def _task_owner(value: str | None) -> str | None:
    return value if value else None


def _print_task_result(action: str, task) -> None:
    print(f"{action} task {task.id} [{task.status}]")


def cmd_task_create(root: Path, a):
    store = _task_store(root, a.channel)
    from agent_chat.task_model import TaskRecord

    task = TaskRecord.from_dict(
        {
            "id": a.task_id,
            "channel": a.channel,
            "title": a.title,
            "status": "open",
            "owner": _task_owner(a.owner),
            "created_by": a.creator,
            "depends_on": _task_values(a.depends_on),
            "files_hint": _task_values(a.files_hint),
            "acceptance": _task_values(a.acceptance),
            "lease_expires_at": None,
            "branch": a.branch,
            "updated_at": now_iso(),
        },
    )
    with contextlib.redirect_stdout(io.StringIO()):
        created = store.create(task, actor=a.creator)
    _print_task_result("created", created)


def cmd_task_list(root: Path, a):
    store = _task_store(root, a.channel)
    tasks = store.list()
    if not tasks:
        print("ID  STATUS  OWNER  DEPENDS_ON  TITLE")
        print("(no tasks)")
        return
    rows = []
    for task in tasks:
        owner = task.owner or "-"
        dependencies = ", ".join(task.depends_on) or "-"
        rows.append((task.id, task.status, owner, dependencies, task.title))
    w_id = max(len("ID"), max(len(r[0]) for r in rows))
    w_status = max(len("STATUS"), max(len(r[1]) for r in rows))
    w_owner = max(len("OWNER"), max(len(r[2]) for r in rows))
    w_deps = max(len("DEPENDS_ON"), max(len(r[3]) for r in rows))
    print(
        f"{'ID'.ljust(w_id)}  {'STATUS'.ljust(w_status)}  {'OWNER'.ljust(w_owner)}  {'DEPENDS_ON'.ljust(w_deps)}  TITLE"
    )
    for r_id, r_status, r_owner, r_deps, r_title in rows:
        print(
            f"{r_id.ljust(w_id)}  {r_status.ljust(w_status)}  {r_owner.ljust(w_owner)}  {r_deps.ljust(w_deps)}  {r_title}"
        )


def cmd_task_show(root: Path, a):
    store = _task_store(root, a.channel)
    task, statuses, ready = store.show_with_dependencies(a.task_id)
    if not statuses or ready:
        dependency_summary = "ready"
    else:
        blocked = [
            f"{dependency}={status}"
            for dependency, status in statuses.items()
            if status != "done"
        ]
        dependency_summary = "blocked (" + ", ".join(blocked) + ")"
    print(f"id: {task.id}")
    print(f"channel: {task.channel}")
    print(f"title: {task.title}")
    print(f"status: {task.status}")
    print(f"owner: {task.owner or '-'}")
    print(f"created_by: {task.created_by}")
    print(f"depends_on: {', '.join(task.depends_on) or '-'}")
    print(f"dependencies: {dependency_summary}")
    print(f"files_hint: {', '.join(task.files_hint) or '-'}")
    print(f"acceptance: {'; '.join(task.acceptance) or '-'}")
    print(f"lease_expires_at: {task.lease_expires_at or '-'}")
    print(f"branch: {task.branch or '-'}")
    print(f"updated_at: {task.updated_at}")


def cmd_task_update(root: Path, a):
    store = _task_store(root, a.channel)
    raw = vars(a)
    changes = {}
    for field in ("title", "owner", "branch", "status"):
        if field in raw:
            changes[field] = raw[field]
    for field in ("depends_on", "files_hint", "acceptance"):
        if field in raw:
            changes[field] = _task_values(raw[field])
    if raw.get("clear_owner"):
        changes["owner"] = None
    if raw.get("clear_branch"):
        changes["branch"] = None
    if not changes:
        from agent_chat.task_model import TaskValidationError

        raise TaskValidationError(
            "TASK_INVALID_UPDATE", "task update requires at least one field"
        )
    with contextlib.redirect_stdout(io.StringIO()):
        task = store.update(a.task_id, changes, actor=_task_actor(a))
    _print_task_result("updated", task)


def _task_transition(root: Path, a, status: str, action: str):
    store = _task_store(root, a.channel)
    with contextlib.redirect_stdout(io.StringIO()):
        task = store.update(a.task_id, actor=_task_actor(a), status=status)
    _print_task_result(action, task)


def cmd_task_done(root: Path, a):
    store = _lease_store(root, a.channel)
    with contextlib.redirect_stdout(io.StringIO()):
        task = store.complete_or_done(a.task_id, _task_actor(a))
    _print_task_result("done", task)


def cmd_task_block(root: Path, a):
    _task_transition(root, a, "blocked", "blocked")


def cmd_task_release(root: Path, a):
    store = _lease_store(root, a.channel)
    with contextlib.redirect_stdout(io.StringIO()):
        task = store.release_or_open(a.task_id, _task_actor(a))
    _print_task_result("released", task)


def cmd_task_claim(root: Path, a):
    store = _lease_store(root, a.channel)
    with contextlib.redirect_stdout(io.StringIO()):
        task = store.claim(
            a.task_id,
            _task_actor(a),
            lease_seconds=a.lease_seconds,
        )
    _print_task_result("claimed", task)


def cmd_task_renew(root: Path, a):
    store = _lease_store(root, a.channel)
    with contextlib.redirect_stdout(io.StringIO()):
        task = store.renew(
            a.task_id,
            _task_actor(a),
            lease_seconds=a.lease_seconds,
        )
    _print_task_result("renewed", task)


def cmd_task_recover(root: Path, a):
    store = _lease_store(root, a.channel)
    with contextlib.redirect_stdout(io.StringIO()):
        task = store.recover(
            a.task_id,
            _task_actor(a),
            reason=a.reason,
            lease_seconds=a.lease_seconds,
        )
    _print_task_result("recovered", task)


def cmd_task_recover_pending(root: Path, a):
    store = _lease_store(root, a.channel)
    with contextlib.redirect_stdout(io.StringIO()):
        store.recover_pending(
            actor=_task_actor(a),
            publication_resolution=a.publication_resolution,
        )
    print(f"recovered pending lease transaction in {a.channel}")


# --- argparse ----------------------------------------------------------------


class _TaskArgumentParser(argparse.ArgumentParser):
    def error(self, message: str):
        from agent_chat.task_model import TaskValidationError

        lower = message.lower()
        if (
            "invalid choice" in lower
            or "unknown subcommand" in lower
            or "unrecognized arguments" in lower
        ):
            code = "TASK_INVALID_COMMAND"
        elif (
            "required" in lower
            or "missing" in lower
            or "invalid" in lower
            or "expected" in lower
        ):
            code = "TASK_INVALID_ARGUMENT"
        else:
            code = "TASK_INVALID_ARGUMENT"
        raise TaskValidationError(code, f"cli error: {message}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chat.py", description="peer agent chat over markdown files"
    )
    p.add_argument(
        "--root", help="chat root dir (default: $AGENT_CHAT_ROOT or ~/agent-chat)"
    )
    sub = p.add_subparsers(
        title="commands",
        dest="cmd",
        required=True,
        help="available commands",
        metavar="COMMAND",
    )

    s = sub.add_parser("init", help="create a channel")
    s.add_argument("channel", help="name of the channel to create")
    s.add_argument("--members", help="comma-separated agent names")
    s.add_argument("--topic", help="initial topic of the channel")
    s.add_argument(
        "--ephemeral",
        type=float,
        default=None,
        help="create as an ephemeral channel with this TTL in seconds",
    )
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("mark-ephemeral", help="mark a channel ephemeral with a TTL")
    s.add_argument("channel", help="channel to mark")
    s.add_argument("ttl", type=float, help="time-to-live in seconds")
    s.set_defaults(func=cmd_mark_ephemeral)

    s = sub.add_parser("gc", help="archive then reap expired ephemeral channels")
    s.add_argument("--dry-run", action="store_true", help="list what would be reaped")
    s.set_defaults(func=cmd_gc)

    s = sub.add_parser("heartbeat", help="per-turn liveness ping (call at the top of every agent turn)")
    s.add_argument("--as", dest="agent", required=True, help="agent sending the heartbeat")
    s.set_defaults(func=cmd_heartbeat)

    s = sub.add_parser(
        "presence",
        help="SWIM-style liveness hints (NOT credentials -- never use for authorization)",
    )
    s.add_argument("agent", nargs="?", default=None, help="single agent to inspect (default: all)")
    s.set_defaults(func=cmd_presence)

    s = sub.add_parser("suspect", help="record a SWIM suspicion mark (gossip hint, not a verdict)")
    s.add_argument("peer", help="agent suspected of being down")
    s.add_argument("--by", required=True, help="agent recording the suspicion")
    s.add_argument("--reason", default="", help="why the peer is suspected")
    s.set_defaults(func=cmd_suspect)

    s = sub.add_parser(
        "react",
        help="deposit a pheromone trace on a message (stigmergic signal, not a notification)",
    )
    s.add_argument("channel", help="channel holding the message")
    s.add_argument("--as", dest="agent", required=True, help="reacting agent")
    s.add_argument("--seq", type=int, required=True, help="target message seq")
    s.add_argument("--kind", default="signal", help="trace kind (default: signal)")
    s.add_argument("--strength", type=float, default=1.0, help="pheromone strength")
    s.add_argument("--ttl", type=float, default=None, help="trace TTL in seconds")
    s.add_argument("--note", default="", help="free-form label (task types are emergent, not an enum)")
    s.set_defaults(func=cmd_react)

    s = sub.add_parser(
        "suggest-role",
        help="ADVISORY ONLY: suggest a specialization from local claim traces (never enforced)",
    )
    s.add_argument("channel", help="channel to read traces from")
    s.add_argument("--as", dest="agent", required=True, help="agent asking for a suggestion")
    s.set_defaults(func=cmd_suggest_role)

    s = sub.add_parser(
        "gossip",
        help="anti-entropy pass: scan seq gaps, backfill from log, compare digests",
    )
    s.add_argument("--as", dest="agent", required=True, help="agent running the pass")
    s.add_argument("--channel", default=None, help="scope to one channel (default: all)")
    s.add_argument("--repair", dest="repair", action="store_true", default=True,
                   help="backfill recoverable gaps (default)")
    s.add_argument("--no-repair", dest="repair", action="store_false",
                   help="scan + digests only, no backfill")
    s.add_argument("--json", action="store_true", help="print the raw report as JSON")
    s.set_defaults(func=cmd_gossip)

    s = sub.add_parser("keygen", help="mint an HMAC identity key for an agent")
    s.add_argument("agent_id", help="agent id (must match fleet identity rules)")
    s.add_argument("--force", action="store_true", help="rotate: replace existing key")
    s.set_defaults(func=cmd_keygen)

    s = sub.add_parser("channels", help="list channels")
    s.set_defaults(func=cmd_channels)

    s = sub.add_parser("roster", help="show a channel's members")
    s.add_argument("channel", help="channel to inspect")
    s.set_defaults(func=cmd_roster)

    s = sub.add_parser(
        "post", help="post a message (body via --body/--body-file/stdin)"
    )
    s.add_argument("channel", help="channel to post in")
    s.add_argument("--from", dest="sender", required=True, help="sender agent name")
    s.add_argument("--to", help="recipient agent, or 'all' (default all)")
    s.add_argument("--title", required=True, help="message title")
    s.add_argument("--reply", type=int, help="seq this replies to")
    s.add_argument(
        "--status", default="discussion", help="message status (default: discussion)"
    )
    s.add_argument("--body", help="literal message body content")
    s.add_argument("--body-file", help="read message body from file")
    s.set_defaults(func=cmd_post)

    event = sub.add_parser("event", help="post/read adapter-neutral events")
    event_sub = event.add_subparsers(
        title="event commands",
        dest="event_cmd",
        required=True,
        help="available event commands",
        metavar="COMMAND",
    )
    s = event_sub.add_parser("post", help="post a capability or status event")
    s.add_argument("channel", help="channel to post the event in")
    s.add_argument("--from", dest="sender", required=True, help="sender agent name")
    s.add_argument(
        "--type",
        dest="event_type",
        choices=EVENT_TYPES,
        required=True,
        help="type of the event",
    )
    s.add_argument("--harness", required=True, help="harness name")
    s.add_argument("--status", choices=STATUS_VALUES, help="status of the agent")
    s.add_argument("--detail", help="optional details about the status")
    s.add_argument(
        "--primitives", action="append", help="primitives supported by the agent"
    )
    s.set_defaults(func=cmd_event_post)
    s = event_sub.add_parser("read", help="read validated adapter-neutral events")
    s.add_argument("channel", help="channel to read events from")
    s.add_argument(
        "--type", dest="event_type", choices=EVENT_TYPES, help="filter by event type"
    )
    s.set_defaults(func=cmd_event_read)

    s = sub.add_parser("read", help="print new messages for an agent (advances cursor)")
    s.add_argument("channel", help="channel to read from")
    s.add_argument(
        "--as", dest="agent", required=True, help="agent reading the messages"
    )
    s.add_argument(
        "--all", action="store_true", help="show entire thread, ignore relevance"
    )
    s.add_argument("--peek", action="store_true", help="do not advance the cursor")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser(
        "wait", help="block (sleep-poll, 0 tokens) until a reply arrives"
    )
    s.add_argument("channel", help="channel to wait on")
    s.add_argument(
        "--as", dest="agent", required=True, help="agent waiting for messages"
    )
    s.add_argument(
        "--timeout", type=float, default=900.0, help="maximum wait time in seconds"
    )
    s.add_argument(
        "--interval", type=float, default=5.0, help="polling interval in seconds"
    )
    s.add_argument(
        "--all",
        action="store_true",
        help="wake on any new message, not just ones relevant to --as",
    )
    s.set_defaults(func=cmd_wait)

    s = sub.add_parser("digest", help="slow-path what's-new digest across all channels")
    s.add_argument("--as", dest="agent", required=True, help="agent reading the digest")
    s.add_argument("--peek", action="store_true", help="print digest but do not advance the vector")
    s.add_argument("--all", action="store_true", help="include messages not addressed to the agent")
    s.set_defaults(func=cmd_digest)

    s = sub.add_parser("peek", help="show last N messages without touching the cursor")
    s.add_argument("channel", help="channel to peek into")
    s.add_argument("-n", type=int, default=3, help="number of messages to show")
    s.set_defaults(func=cmd_peek)

    s = sub.add_parser("claim", help="atomically claim a task-<id>.md marker")
    s.add_argument("channel", help="channel containing the task")
    s.add_argument("task", help="task marker filename, e.g. task-12.md")
    s.add_argument("--as", dest="agent", required=True, help="agent claiming the task")
    s.set_defaults(func=cmd_claim)

    s = sub.add_parser("lock", help="lock workspace-relative paths")
    s.add_argument("channel", help="channel to lock paths in")
    s.add_argument("paths", nargs="+", help="paths to lock")
    s.add_argument(
        "--as",
        "--from",
        "--owner",
        dest="owner",
        required=True,
        help="agent acquiring the lock",
    )
    s.add_argument(
        "--lease-seconds",
        "--lease",
        "--ttl",
        type=float,
        default=300.0,
        help="duration of the lease in seconds",
    )
    s.set_defaults(func=cmd_lock)

    s = sub.add_parser("check", help="check workspace-relative paths for conflicts")
    s.add_argument("channel", help="channel to check paths in")
    s.add_argument("paths", nargs="+", help="paths to check")
    s.add_argument(
        "--as", "--from", "--owner", dest="owner", help="agent checking the paths"
    )
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("unlock", help="release an owned path lock")
    s.add_argument("channel", help="channel containing the lock")
    s.add_argument("target", help="lock id or exact normalized path")
    s.add_argument(
        "--as",
        "--from",
        "--owner",
        dest="owner",
        required=True,
        help="agent releasing the lock",
    )
    s.set_defaults(func=cmd_unlock)

    s = sub.add_parser("recover", help="recover an expired path lock explicitly")
    s.add_argument("channel", help="channel containing the lock")
    s.add_argument("target", help="lock id or exact normalized path")
    s.add_argument(
        "--as",
        "--from",
        "--owner",
        dest="owner",
        required=True,
        help="agent recovering the lock",
    )
    s.add_argument("--reason", required=True, help="reason for recovery")
    s.add_argument(
        "--lease-seconds",
        "--lease",
        "--ttl",
        type=float,
        default=300.0,
        help="duration of the new lease in seconds",
    )
    s.set_defaults(func=cmd_path_recover)
    s = sub.add_parser(
        "recover-pending",
        help="recover a pending crashed path-lock transaction",
    )
    s.add_argument("channel", help="channel containing the transaction")
    s.add_argument(
        "--as",
        "--from",
        "--owner",
        dest="actor",
        required=True,
        help="agent recovering the transaction",
    )
    s.add_argument(
        "--resolve-publication",
        dest="publication_resolution",
        choices=("rollback", "published"),
        help="how to resolve the pending publication",
    )
    s.set_defaults(func=cmd_path_recover_pending)

    s = sub.add_parser("state", help="render or show channel state summary")
    s.add_argument("channel", help="channel to get state for")
    s.add_argument("--as", "--from", "--actor", dest="actor", help="agent identity")
    s.add_argument(
        "--write", "--save", action="store_true", help="write state.md to channel"
    )
    s.add_argument(
        "--no-audit", action="store_true", help="skip posting audit message on write"
    )
    s.add_argument("--json", action="store_true", help="output structured JSON summary")
    s.add_argument(
        "--strict", action="store_true", help="strictly validate all source files"
    )
    s.set_defaults(func=cmd_state)

    s = sub.add_parser("compact", help="compact channel state into state.md")
    s.add_argument("channel", help="channel to compact")
    s.add_argument("--as", "--from", "--actor", dest="actor", help="agent identity")
    s.add_argument(
        "--no-audit", action="store_true", help="do not post audit event to channel"
    )
    s.add_argument("--json", action="store_true", help="output structured JSON summary")
    s.add_argument(
        "--strict", action="store_true", help="strictly validate all source files"
    )
    s.set_defaults(func=cmd_compact)

    task = sub.add_parser(
        "task",
        help="manage structured task records",
    )
    task_sub = task.add_subparsers(
        title="task commands",
        dest="task_cmd",
        required=True,
        parser_class=_TaskArgumentParser,
        help="available task commands",
        metavar="COMMAND",
    )
    task.error = _TaskArgumentParser.error.__get__(task, _TaskArgumentParser)

    s = task_sub.add_parser("create", help="create a task record")
    s.add_argument("channel", help="channel to create the task in")
    s.add_argument("task_id", help="unique identifier for the task")
    s.add_argument(
        "--from",
        "--created-by",
        dest="creator",
        required=True,
        help="agent creating the task",
    )
    s.add_argument("--title", required=True, help="title of the task")
    s.add_argument("--owner", help="agent owning the task")
    s.add_argument(
        "--depends-on", action="append", default=[], help="task dependencies"
    )
    s.add_argument(
        "--files-hint", action="append", default=[], help="files related to this task"
    )
    s.add_argument(
        "--acceptance", action="append", default=[], help="acceptance criteria"
    )
    s.add_argument("--branch", help="git branch for the task")
    s.set_defaults(func=cmd_task_create)

    s = task_sub.add_parser("list", help="list task records")
    s.add_argument("channel", help="channel to list tasks from")
    s.set_defaults(func=cmd_task_list)

    s = task_sub.add_parser("show", help="show one task record")
    s.add_argument("channel", help="channel containing the task")
    s.add_argument("task_id", help="task to show")
    s.set_defaults(func=cmd_task_show)

    s = task_sub.add_parser("update", help="update task fields")
    s.add_argument("channel", help="channel containing the task")
    s.add_argument("task_id", help="task to update")
    s.add_argument(
        "--as", "--from", dest="actor", required=True, help="agent updating the task"
    )
    s.add_argument("--title", default=argparse.SUPPRESS, help="new title")
    s.add_argument("--owner", default=argparse.SUPPRESS, help="new owner")
    s.add_argument(
        "--clear-owner", action="store_true", help="remove the current owner"
    )
    s.add_argument(
        "--depends-on",
        action="append",
        default=argparse.SUPPRESS,
        help="new dependencies",
    )
    s.add_argument(
        "--files-hint",
        action="append",
        default=argparse.SUPPRESS,
        help="new files hint",
    )
    s.add_argument(
        "--acceptance",
        action="append",
        default=argparse.SUPPRESS,
        help="new acceptance criteria",
    )
    s.add_argument("--branch", default=argparse.SUPPRESS, help="new git branch")
    s.add_argument(
        "--clear-branch", action="store_true", help="remove the current branch"
    )
    s.add_argument("--status", default=argparse.SUPPRESS, help="new status")

    s.set_defaults(func=cmd_task_update)

    s = task_sub.add_parser("claim", help="claim a ready task with a lease")
    s.add_argument("channel", help="channel containing the task")
    s.add_argument("task_id", help="task to claim")
    s.add_argument(
        "--as", "--from", dest="actor", required=True, help="agent claiming the task"
    )
    s.add_argument(
        "--lease-seconds",
        "--lease",
        "--ttl",
        type=float,
        default=300.0,
        help="duration of the lease in seconds",
    )
    s.set_defaults(func=cmd_task_claim)

    s = task_sub.add_parser("renew", help="renew an owned task lease")
    s.add_argument("channel", help="channel containing the task")
    s.add_argument("task_id", help="task to renew")
    s.add_argument(
        "--as", "--from", dest="actor", required=True, help="agent renewing the task"
    )
    s.add_argument(
        "--lease-seconds",
        "--lease",
        "--ttl",
        type=float,
        default=300.0,
        help="duration of the new lease in seconds",
    )
    s.set_defaults(func=cmd_task_renew)

    s = task_sub.add_parser("recover", help="recover an expired task lease")
    s.add_argument("channel", help="channel containing the task")
    s.add_argument("task_id", help="task to recover")
    s.add_argument(
        "--as", "--from", dest="actor", required=True, help="agent recovering the task"
    )
    s.add_argument("--reason", required=True, help="reason for recovery")
    s.add_argument(
        "--lease-seconds",
        "--lease",
        "--ttl",
        type=float,
        default=300.0,
        help="duration of the new lease in seconds",
    )
    s.set_defaults(func=cmd_task_recover)

    s = task_sub.add_parser(
        "recover-pending",
        help="recover a pending crashed lease transaction",
    )
    s.add_argument("channel", help="channel containing the transaction")
    s.add_argument(
        "--as",
        "--from",
        dest="actor",
        required=True,
        help="agent recovering the transaction",
    )
    s.add_argument(
        "--resolve-publication",
        dest="publication_resolution",
        choices=("rollback", "published"),
        help="how to resolve the pending publication",
    )
    s.set_defaults(func=cmd_task_recover_pending)

    for command, handler, help_text, action in (
        ("done", cmd_task_done, "mark a task done", "done"),
        ("block", cmd_task_block, "mark a task blocked", "blocked"),
        ("release", cmd_task_release, "release a task back to open", "released"),
    ):
        s = task_sub.add_parser(command, help=help_text)
        s.add_argument("channel", help="channel containing the task")
        s.add_argument("task_id", help="task to operate on")
        s.add_argument(
            "--as",
            "--from",
            dest="actor",
            required=True,
            help="agent performing the action",
        )
        s.set_defaults(func=handler)

    return p


def _is_task_error(error: Exception) -> bool:
    try:
        from agent_chat.task_model import TaskError
    except (ImportError, ModuleNotFoundError):
        return False
    return isinstance(error, TaskError)


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        root = root_dir(args.root)
        args.func(root, args)
    except AgentChatError as e:
        die(str(e), code=2 if isinstance(e, AdapterEventError) else 1)
    except KeyboardInterrupt:
        print(file=sys.stderr)  # print a newline to cleanly break from input prompts
        die("cancelled by user", code=130)
    except OSError as error:
        if "args" in locals() and getattr(args, "cmd", None) == "task":
            die(f"TASK_IO_ERROR: {error}", code=2)
        die(f"I/O error: {error}", code=1)
    except Exception as error:
        if _is_task_error(error):
            die(str(error), code=2)
        raise


if __name__ == "__main__":
    main()
