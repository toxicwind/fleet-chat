from __future__ import annotations

import argparse
import contextlib
import heapq
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import fleet_bids
import fleet_crdt
import fleet_dag
import fleet_delta
import fleet_e2ee
import fleet_ephemeral
import fleet_gossip
import fleet_identity
import fleet_log
import fleet_presence
import fleet_relay
import fleet_roster
import fleet_stigmergy
import fleet_time
import fleet_watch

from chat_core import *

def cmd_init(root: Path, a):
    d = channel_dir(root, a.channel)
    d.mkdir(parents=True, exist_ok=True)
    (d / ".cursors").mkdir(exist_ok=True)
    meta_path = d / "_meta.json"
    if meta_path.exists():
        raise AgentChatError(f"channel '{a.channel}' already exists")
    # Fleet E2EE: priv-* channels get their symmetric key at creation.
    # Leader-side provisioning; members receive the key out of band.
    # Fail closed: no key, no private channel -- and this happens before
    # _meta.json is written, so a failed init leaves no half-made channel.
    if a.channel.startswith(fleet_e2ee.PRIV_PREFIX):
        try:
            fleet_e2ee.ensure_channel_key(a.channel)
        except Exception as e:
            die(f"cannot provision key for private channel '{a.channel}': {e}")
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
    # argparse always sets --ephemeral in production; test fixtures build
    # their own namespaces, so degrade gracefully instead of AttributeError.
    ephemeral = getattr(a, "ephemeral", None)
    if ephemeral is not None:
        fleet_ephemeral.mark_ephemeral(d, float(ephemeral))
    # Fleet discovery: index the channel AFTER _meta.json is durably written,
    # so a crashed init never indexes a half-made channel.
    fleet_watch.note_channel(root, a.channel)
    # Fleet CRDT: channel creation is a commutative op on the root log.
    _record_op(
        root, None, fleet_crdt.CHANNEL_CREATE, "system", 0,
        {"channel": a.channel},
    )
    m_str = ", ".join(members) if members else "(open)"
    eph = f" ephemeral(ttl={ephemeral}s)" if ephemeral is not None else ""
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
    require_channel(root, a.channel)
    fleet_stigmergy.react(
        root, a.channel, a.agent, target_seq=a.seq, kind=a.kind,
        strength=a.strength, ttl_s=a.ttl, note=a.note or "",
    )
    print(f"trace deposited on #{a.seq} in '{a.channel}' (kind={a.kind})")
    # Fleet CRDT: the reaction is a commutative operation.
    _record_op(
        root, a.channel, fleet_crdt.REACT, a.agent,
        fleet_time.tick(root, a.agent),
        {"target_seq": a.seq, "react_kind": a.kind, "strength": a.strength},
    )


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


def _post_message(root: Path, channel: str, *, body: str, sender: str,
                  to: str = "all", reply=None, status: str = "discussion",
                  title: str = "", extra_frontmatter: dict | None = None,
                  key_dir=None) -> tuple[int, str]:
    """The shared signed/sequenced post path.

    Sequence lock, DAG parents, Lamport tick, priv-* E2EE, HMAC-SHA256 sign,
    .md write, log.jsonl append, CRDT op -- everything cmd_post does, in one
    place. relay-in calls this too; never hand-write message files.

    extra_frontmatter renders as additional frontmatter lines (after the
    standard fields, before hmac); key_dir overrides the fleet keys dir.
    Returns (seq, filename).
    """
    d = require_channel(root, channel)
    sender = _frontmatter_value(sender)
    to = _frontmatter_value(to or "all")
    reply = _frontmatter_value(reply) if reply else None
    channel = _frontmatter_value(channel)
    timestamp = _frontmatter_value(now_iso())
    status = _frontmatter_value(status)
    title = _frontmatter_value(title)
    lock = _acquire_lock(d)
    try:
        seq = _next_seq(d)
        # Fleet DAG: parent ids, computed under the seq lock (race-free).
        parents = _dag_parents(d, seq, reply)
        # Fleet Lamport: tick the sender's clock; readers sort causally.
        lamport = fleet_time.tick(root, sender)
        # Fleet E2EE: for priv-* channels, encrypt the body BEFORE signing
        # and persisting. What hits disk (.md + log.jsonl) is ciphertext;
        # the HMAC covers the ciphertext, so tampering breaks both layers.
        # Readers verify first, then decrypt for display. No key (or no
        # crypto lib) -> the post fails closed, never plaintext.
        if channel.startswith(fleet_e2ee.PRIV_PREFIX):
            try:
                body = fleet_e2ee.encrypt_message(channel, body)
            except Exception as e:
                die(f"cannot encrypt for private channel '{channel}': {e}")
        fname = f"{seq:04d}-{slugify(sender)}-{slugify(title)}.md"
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
                # v2: lamport + parents are HMAC-covered (both in scope
                # under the seq lock, computed just above).
                lamport=lamport,
                parents=parents,
                # v3: relay metadata is HMAC-covered when present, so
                # tampering with relayed_from/human invalidates the sig.
                relayed_from=(extra_frontmatter or {}).get("relayed_from"),
                human=(extra_frontmatter or {}).get("human"),
            ),
            kd=key_dir,
        )
        if extra_frontmatter:
            for _rk, _rv in extra_frontmatter.items():
                fm.append(f"{_rk}: {_frontmatter_value(_rv)}")
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
    # Fleet CRDT: the post is a commutative operation; the op log lets any
    # replica converge on the same action set regardless of order.
    _record_op(root, channel, fleet_crdt.POST, sender, lamport, {"seq": seq})
    return seq, fname


def cmd_post(root: Path, a):
    body = _read_body(a)
    seq, fname = _post_message(
        root, a.channel, body=body, sender=a.sender, to=a.to,
        reply=a.reply, status=a.status, title=a.title,
    )
    print(f"posted #{seq} -> {a.channel}/{fname}")


def _relay_read_text(a) -> str:
    """Body source for relay-in: --text, or stdin when --text is '-' or absent."""
    if a.text is not None and a.text != "-":
        data = a.text
    else:
        if sys.stdin.isatty():
            print(
                "relay-in: reading message text from stdin (Ctrl-D to finish).",
                file=sys.stderr,
            )
        try:
            data = sys.stdin.read()
            data.encode("utf-8")  # fail fast on surrogates
        except (OSError, UnicodeError) as e:
            raise AgentChatError(f"could not read relay text from stdin: {e}")
    if not data.strip():
        raise AgentChatError("empty relay text (pass --text, --text -, or pipe via stdin)")
    return data


def cmd_relay_in(root: Path, a):
    """Muse -> Squawk: relay a human message through the signed post path.

    The message is HMAC-signed by the relay identity (--identity, default
    $SQUAWK_RELAY_IDENTITY or 'relay'); the human it came from travels in
    frontmatter as relayed_from=muse-side-chat + human=<name>.
    """
    identity = fleet_relay.resolve_identity(a.identity)
    key_dir = fleet_relay.resolve_key_dir(a.key_dir, root=root)
    fleet_relay.ensure_keys_env(root=root)
    text = _relay_read_text(a)
    # Seal hook point: when the sealed envelope format lands,
    # seal_for_channel seals the human text to the channel members' keys.
    # Until then it is the identity function.
    body = fleet_relay.seal_for_channel(a.channel, text)
    seq, fname = _post_message(
        root, a.channel, body=body, sender=identity, to=a.to,
        status=a.status, title=a.title,
        extra_frontmatter={"relayed_from": fleet_relay.RELAYED_FROM,
                           "human": a.human},
        key_dir=key_dir,
    )
    print(f"relayed #{seq} -> {a.channel}/{fname} (human: {a.human})")


def cmd_relay_out(root: Path, a):
    """Squawk -> Muse: dump new channel messages as JSONL (machine contract).

    One JSON object per line (the fleet_relay record schema), then a final
    {"cursor": <high-water seq>} line. Signature problems are reported in
    each record ("signature": "invalid"|"revoked"|"unknown-sender"), never
    silently passed and never fatal to the stream.
    """
    d = require_channel(root, a.channel)
    key_dir = fleet_relay.resolve_key_dir(a.key_dir, root=root)
    fleet_relay.ensure_keys_env(root=root)
    identity = fleet_relay.resolve_identity(a.identity)
    top = a.since
    for p in message_files(d):
        seq = _seq_from_name(p.name)
        if seq is None or seq <= a.since:
            continue
        rec = fleet_relay.build_relay_record(
            p, channel=a.channel, identity=identity, key_dir=key_dir)
        print(json.dumps(rec, ensure_ascii=False))
        top = max(top, seq)
    print(json.dumps({"cursor": top}))


def cmd_squawk_feed(root: Path, a):
    """Run the squawk-feed fat long-poll service (bearer-authed).

    Token comes from the SQUAWK_FEED_TOKEN env var (pitchfork service
    env); the server refuses to start without it. Never a CLI flag.
    """
    import squawk_feed
    squawk_feed.main([
        "--root", str(root),
        "--channel", a.channel,
        "--bind", a.bind,
        "--port", str(a.port),
        "--identity", fleet_relay.resolve_identity(a.identity),
        "--key-dir", str(fleet_relay.resolve_key_dir(a.key_dir, root=root)),
    ])


def _record_op(root: Path, channel: str | None, kind: str, actor: str,
               lamport: int, payload: dict) -> None:
    """Append one CRDT op. Fail soft (stderr warning): the op log is a
    derived index, and it must never break the action it records."""
    try:
        fleet_crdt.append_op(
            root, channel,
            fleet_crdt.make_op(kind, channel or "root", actor, lamport,
                               payload=payload),
        )
    except Exception as e:  # noqa: BLE001 -- op log never breaks actions
        print(f"(warning: op log append failed: {e})", file=sys.stderr)


def _print_message(path: Path, meta: dict | None = None):
    """Print one message file. When the verified frontmatter `meta` is given
    and the channel is priv-*, the (already HMAC-verified) ciphertext body is
    decrypted for display. Fail closed: a missing/wrong channel key is a
    hard error, never a silent ciphertext dump or a skip."""
    print("=" * 70)
    try:
        text = path.read_text(encoding="utf-8").rstrip()
    except (OSError, UnicodeError) as e:
        print(f"(could not read message {path.name}: {e})")
        print()
        return
    if meta is not None and str(meta.get("channel", "")).startswith(
        fleet_e2ee.PRIV_PREFIX
    ):
        channel = meta["channel"]
        try:
            plaintext = fleet_e2ee.decrypt_message(channel, meta.get("body", ""))
        except Exception as e:
            die(
                f"cannot decrypt message {path.name} "
                f"in private channel '{channel}': {e}"
            )
        # Splice the plaintext in place of the ciphertext body: the body is
        # everything after the closing '---' line of the frontmatter.
        lines = text.split("\n")
        try:
            close = lines.index("---", 1)
        except ValueError:
            close = len(lines) - 1
        text = "\n".join(lines[: close + 1] + [plaintext.rstrip()])
    print(text)
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
        _print_message(p, meta)
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
            delivered = False
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
                _print_message(p, meta)
                delivered = True
            if delivered:
                write_cursor(d, a.agent, max_seq(d))
                return
            # Only irrelevant messages arrived: advance the in-memory scan
            # cursor past them and keep waiting for something relevant.
            # The on-disk cursor is untouched -- a timed-out wait must not
            # silently consume messages the agent never saw.
            cur = max_seq(d)
            continue
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
        _print_message(p, meta)
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
    # Fleet bid-then-consensus (Wang et al. 2022): while a live bid round
    # exists for the task, only the consensus winner may claim. No live
    # bids -> legacy first-come behavior. The lease itself still comes
    # from the base LeaseStore; this is a pre-check, not a new task board.
    actor = _task_actor(a)
    bid_res = None
    try:
        bid_res = fleet_bids.check_claim(root, a.channel, a.task_id, actor)
    except fleet_bids.BidError as e:
        die(str(e))
    store = _lease_store(root, a.channel)
    with contextlib.redirect_stdout(io.StringIO()):
        task = store.claim(
            a.task_id,
            actor,
            lease_seconds=a.lease_seconds,
        )
    # The round is decided: archive its bids so a later release starts fresh.
    if bid_res is not None:
        fleet_bids.archive_round(root, a.channel, a.task_id)
    # Fleet CRDT: the claim is a commutative operation (LWW by lamport).
    _record_op(
        root, a.channel, fleet_crdt.CLAIM, actor,
        fleet_time.tick(root, actor),
        {"task_id": a.task_id, "agent": actor},
    )
    # Stigmergy: every successful claim leaves a trace for suggest-role.
    # Traces must never break claims.
    try:
        note = f"claimed {a.task_id}"
        if bid_res is not None:
            wb = next(
                (b for b in bid_res["ranked"] if b["agent"] == actor), None
            )
            if wb is not None:
                note += f" as bid-winner (score={wb['score']})"
        fleet_stigmergy.record_claim(root, a.channel, actor, "task", note=note)
    except Exception:
        pass
    _print_task_result("claimed", task)


def cmd_task_bid(root: Path, a):
    """Record a suitability bid for bid-then-consensus allocation."""
    try:
        bid = fleet_bids.record_bid(
            root, a.channel, a.task_id, _task_actor(a), a.score, note=a.note or ""
        )
    except fleet_bids.BidError as e:
        die(str(e))
    res = fleet_bids.resolve(root, a.channel, a.task_id)
    print(
        f"bid recorded for task '{a.task_id}' by '{bid['agent']}' "
        f"(score={bid['score']})"
    )
    if res["winner"]:
        ranked = ", ".join(f"{b['agent']}={b['score']}" for b in res["ranked"])
        print(f"current consensus winner: '{res['winner']}' (ranked: {ranked})")
    # Fleet CRDT: the bid is a commutative operation (latest-wins per agent).
    _record_op(
        root, a.channel, fleet_crdt.BID, _task_actor(a),
        fleet_time.tick(root, _task_actor(a)),
        {"task_id": a.task_id, "score": bid["score"], "ts": bid["ts"]},
    )


def cmd_task_bids(root: Path, a):
    """Show (or clear) a task's current bid round."""
    if a.clear:
        n = fleet_bids.clear_bids(root, a.channel, a.task_id)
        print(f"cleared {n} bid(s) for task '{a.task_id}'")
        return
    res = fleet_bids.resolve(root, a.channel, a.task_id)
    if a.json:
        print(json.dumps(res, indent=2))
        return
    if not res["ranked"]:
        print(f"(no live bids for task '{a.task_id}')")
        return
    for i, b in enumerate(res["ranked"], 1):
        mark = " <-- consensus winner" if b["agent"] == res["winner"] else ""
        note = f" -- {b['note']}" if b.get("note") else ""
        print(f"{i}. {b['agent']}: score={b['score']}{mark}{note}")


def cmd_dag(root: Path, a):
    """Verify the hash-linked message DAG of a channel.

    Checks parent links, seq integrity, duplicate seqs, cycles, and
    unparseable files. Empty problem list == clean chain.
    """
    d = require_channel(root, a.channel)
    problems = fleet_dag.verify_chain(d)
    if a.json:
        print(json.dumps(problems, indent=2))
        return
    if not problems:
        n = len(fleet_dag.read_channel(d))
        print(f"DAG for '{a.channel}': clean ({n} message(s) verified)")
        return
    print(f"DAG for '{a.channel}': {len(problems)} problem(s)")
    for prob in problems:
        print(f"  [{prob['type']}] {prob['file']}: {prob['detail']}")


def cmd_thread(root: Path, a):
    """Show the reply thread from the root message to a target.

    Target may be a full message id, an unambiguous id prefix, or a seq
    number. Follows parents[0] (the reply thread) up to genesis.
    """
    d = require_channel(root, a.channel)
    target = a.target
    if target.isdigit():
        chan = fleet_dag.read_channel(d)
        seq = int(target)
        mids = [m for m, e in chan.items() if e["seq"] == seq]
        if not mids:
            die(f"no message with seq {seq} in '{a.channel}'")
        target = mids[0]
    try:
        chain = fleet_dag.thread_view(d, target)
    except fleet_dag.FleetDagError as e:
        die(str(e))
    for path in chain:
        meta, body = fleet_dag.parse_message(path)
        first = body.strip().splitlines()[0] if body.strip() else "(empty)"
        print(f"#{meta.get('seq')} {meta.get('from', '?')}: {first[:80]}")


def cmd_clocks(root: Path, a):
    """Show per-agent Lamport clocks (causal-time diagnostics).

    Clocks advance on local sends and on observed remote timestamps.
    Large gaps flag an agent that posts but never reads.
    """
    report = fleet_time.clock_drift_report(root)
    if a.json:
        print(json.dumps(report, indent=2))
        return
    if not report:
        print("(no agent clocks recorded)")
        return
    for agent in sorted(report):
        print(f"{agent}: {report[agent]}")


def cmd_ops(root: Path, a):
    """Show the commutative op log (Shapiro et al. 2011).

    The op log records every fleet action kind -- posts, reactions,
    channel creates, bids, claims -- as commutative operations. Any two
    replicas that have seen the same ops materialize the same state,
    regardless of the order they observed them in. The .md files remain
    the canonical human-readable data; this is the convergence substrate.
    """
    ops = fleet_crdt.read_ops(root, a.channel)
    if a.json:
        print(json.dumps(ops, indent=2))
        return
    where = f"channel '{a.channel}'" if a.channel else "root"
    if not ops:
        print(f"(no ops for {where})")
        return
    if a.materialize:
        state = fleet_crdt.materialize(ops)
        print(f"{where}: {len(ops)} op(s) materialized")
        print(f"  messages: {sorted(state['messages'])}")
        print(f"  reactions: {len(state['reactions'])}")
        print(f"  channels: {state['channels']}")
        bids = {
            t: {ag: b["score"] for ag, b in agents.items()}
            for t, agents in state["bids"].items()
        }
        print(f"  bids: {bids}")
        claims = {t: c["agent"] for t, c in state["claims"].items()}
        print(f"  claims: {claims}")
        return
    for o in ops:
        print(f"{o['lamport']:>4} {o['kind']:<14} {o['actor']:<12} {o['op_id']}")


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




__all__ = [
    "cmd_init",
    "cmd_keygen",
    "cmd_mark_ephemeral",
    "cmd_gc",
    "cmd_heartbeat",
    "cmd_presence",
    "cmd_react",
    "cmd_gossip",
    "cmd_suggest_role",
    "cmd_suspect",
    "cmd_channels",
    "cmd_roster",
    "_read_body",
    "_resolve_reply_target",
    "_dag_parents",
    "_post_message",
    "cmd_post",
    "_relay_read_text",
    "cmd_relay_in",
    "cmd_relay_out",
    "cmd_squawk_feed",
    "_record_op",
    "_print_message",
    "_sender_cleared",
    "cmd_digest",
    "cmd_read",
    "cmd_wait",
    "cmd_peek",
    "cmd_claim",
    "_task_store",
    "_lease_store",
    "_path_lock_store",
    "_state_store",
    "cmd_state",
    "cmd_compact",
    "_event_body",
    "cmd_event_post",
    "cmd_event_read",
    "cmd_lock",
    "cmd_check",
    "cmd_unlock",
    "cmd_path_recover",
    "cmd_path_recover_pending",
    "_task_values",
    "_task_actor",
    "_task_owner",
    "_print_task_result",
    "cmd_task_create",
    "cmd_task_list",
    "cmd_task_show",
    "cmd_task_update",
    "_task_transition",
    "cmd_task_done",
    "cmd_task_block",
    "cmd_task_release",
    "cmd_task_claim",
    "cmd_task_bid",
    "cmd_task_bids",
    "cmd_dag",
    "cmd_thread",
    "cmd_clocks",
    "cmd_ops",
    "cmd_task_renew",
    "cmd_task_recover",
    "cmd_task_recover_pending",
]
