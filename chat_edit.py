#!/usr/bin/env python3
"""Apply the relay refactor to chat.py in a Squawk checkout.

Usage: python3 chat_edit.py /path/to/checkout/chat.py

Makes these edits, each asserted to match exactly once (aborts on mismatch):
  R1: add `import fleet_relay` to the fleet_* import block
  R2: update module docstring command list
  R3: refactor cmd_post -> shared _post_message + thin cmd_post wrapper
  R4: add cmd_relay_in / cmd_relay_out / cmd_squawk_feed before _record_op
  R5: register relay-in / relay-out / squawk-feed subparsers after `read`
"""

import sys
from pathlib import Path


def rep(src: str, old: str, new: str, name: str) -> str:
    n = src.count(old)
    if n != 1:
        raise SystemExit(f"EDIT {name}: expected 1 match, found {n}")
    return src.replace(old, new, 1)


def main() -> None:
    chat_py = Path(sys.argv[1])
    src = chat_py.read_text(encoding="utf-8")

    # R1: import fleet_relay (alphabetical: fleet_presence < fleet_relay < fleet_roster)
    src = rep(
        src,
        "import fleet_presence\nimport fleet_roster\n",
        "import fleet_presence\nimport fleet_relay\nimport fleet_roster\n",
        "R1-import",
    )

    # R2: module docstring command list
    src = rep(
        src,
        "Commands: init | channels | roster | post | read | wait | peek | claim | lock | check | unlock | recover | recover-pending | task | state | compact | event | keygen",
        "Commands: init | channels | roster | post | read | wait | peek | claim | lock | check | unlock | recover | recover-pending | task | state | compact | event | keygen | relay-in | relay-out | squawk-feed",
        "R2-docstring",
    )

    # R3a: cmd_post header -> _post_message (shared signed/sequenced post path)
    src = rep(
        src,
        '''def cmd_post(root: Path, a):
    d = require_channel(root, a.channel)
    body = _read_body(a)
    sender = _frontmatter_value(a.sender)
    to = _frontmatter_value(a.to or "all")
    reply = _frontmatter_value(a.reply) if a.reply else None
    channel = _frontmatter_value(a.channel)
    timestamp = _frontmatter_value(now_iso())
    status = _frontmatter_value(a.status)
    title = _frontmatter_value(a.title)
''',
        '''def _post_message(root: Path, channel: str, *, body: str, sender: str,
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
''',
        "R3a-post-header",
    )

    # R3b: filename uses the sender/title params now
    src = rep(
        src,
        '        fname = f"{seq:04d}-{slugify(a.sender)}-{slugify(a.title)}.md"',
        '        fname = f"{seq:04d}-{slugify(sender)}-{slugify(title)}.md"',
        "R3b-fname",
    )

    # R3c: extra frontmatter lines before the channel:/hmac: block
    src = rep(
        src,
        '''        fm += [
            f"channel: {channel}",
''',
        '''        if extra_frontmatter:
            for _rk, _rv in extra_frontmatter.items():
                fm.append(f"{_rk}: {_frontmatter_value(_rv)}")
        fm += [
            f"channel: {channel}",
''',
        "R3c-extra-fm",
    )

    # R3d: pass key_dir through to the signer
    src = rep(
        src,
        '''                lamport=lamport,
                parents=parents,
            ),
        )
''',
        '''                lamport=lamport,
                parents=parents,
            ),
            kd=key_dir,
        )
''',
        "R3d-sign-kd",
    )

    # R3e: tail of the old cmd_post -> return, then the cmd_post wrapper
    # and the three new relay commands, inserted before _record_op.
    relay_cmds = '''    return seq, fname


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
    key_dir = fleet_relay.resolve_key_dir(a.key_dir)
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
    key_dir = fleet_relay.resolve_key_dir(a.key_dir)
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
    """Run the squawk-feed push service (inotify hot path, one port HTTP+WS)."""
    import squawk_feed
    squawk_feed.main([
        "--root", str(root),
        "--channel", a.channel,
        "--bind", a.bind,
        "--port", str(a.port),
        "--identity", fleet_relay.resolve_identity(a.identity),
        "--key-dir", str(fleet_relay.resolve_key_dir(a.key_dir)),
    ])


def _record_op(root: Path, channel: str | None, kind: str, actor: str,
'''
    src = rep(
        src,
        '''    print(f"posted #{seq} -> {a.channel}/{fname}")


def _record_op(root: Path, channel: str | None, kind: str, actor: str,
''',
        relay_cmds,
        "R3e-relay-cmds",
    )

    # R4: subparsers, after `read`, before `wait`
    src = rep(
        src,
        '''    s.add_argument("--peek", action="store_true", help="do not advance the cursor")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser(
        "wait", help="block (sleep-poll, 0 tokens) until a reply arrives"
    )
''',
        '''    s.add_argument("--peek", action="store_true", help="do not advance the cursor")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser(
        "relay-in",
        help="relay a human message into Squawk (Muse -> Squawk, signed)",
    )
    s.add_argument("--channel", required=True, help="channel to post in")
    s.add_argument(
        "--from", dest="human", required=True,
        help="human identity the message is relayed from (e.g. chris)",
    )
    s.add_argument(
        "--text", default=None,
        help="message text; use '-' or omit to read from stdin",
    )
    s.add_argument(
        "--identity", default=None,
        help="relay signing identity "
             "(default: $SQUAWK_RELAY_IDENTITY or 'relay')",
    )
    s.add_argument(
        "--key-dir", default=None,
        help="fleet keys dir "
             "(default: $FLEET_KEYS_DIR or /home/toxic/.shingle/keys)",
    )
    s.add_argument("--to", default="all",
                   help="recipient agent, or 'all' (default all)")
    s.add_argument("--title", default="relayed message", help="message title")
    s.add_argument("--status", default="discussion", help="message status")
    s.set_defaults(func=cmd_relay_in)

    s = sub.add_parser(
        "relay-out",
        help="dump new channel messages as JSONL (Squawk -> Muse, machine contract)",
    )
    s.add_argument("channel", help="channel to read from")
    s.add_argument(
        "--since", type=int, default=0,
        help="only messages with seq greater than this (cursor)",
    )
    s.add_argument(
        "--identity", default=None,
        help="relay identity used to unseal "
             "(default: $SQUAWK_RELAY_IDENTITY or 'relay')",
    )
    s.add_argument(
        "--key-dir", default=None,
        help="fleet keys dir "
             "(default: $FLEET_KEYS_DIR or /home/toxic/.shingle/keys)",
    )
    s.add_argument(
        "--format", default="json", choices=["json"],
        help="output format (default: json)",
    )
    s.set_defaults(func=cmd_relay_out)

    s = sub.add_parser(
        "squawk-feed",
        help="run the squawk-feed push service (inotify, one port HTTP+WS)",
    )
    s.add_argument("--channel", default="fleet",
                   help="channel to watch (default: fleet)")
    s.add_argument("--bind", default="127.0.0.1", help="bind address")
    s.add_argument("--port", type=int, default=25131,
                   help="port to serve (default: 25131)")
    s.add_argument(
        "--identity", default=None,
        help="relay identity for WS auth "
             "(default: $SQUAWK_RELAY_IDENTITY or 'relay')",
    )
    s.add_argument(
        "--key-dir", default=None,
        help="fleet keys dir "
             "(default: $FLEET_KEYS_DIR or /home/toxic/.shingle/keys)",
    )
    s.set_defaults(func=cmd_squawk_feed)

    s = sub.add_parser(
        "wait", help="block (sleep-poll, 0 tokens) until a reply arrives"
    )
''',
        "R4-subparsers",
    )

    chat_py.write_text(src, encoding="utf-8")
    print(f"OK: edited {chat_py} (R1-R4 applied)")


if __name__ == "__main__":
    main()
