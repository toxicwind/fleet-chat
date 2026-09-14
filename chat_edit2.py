#!/usr/bin/env python3
"""Retarget chat.py's squawk-feed subcommand from the HTTP service to the
zipfs-vault writer daemon. Usage: python3 chat_edit2.py /path/to/chat.py
Every replacement asserts exactly one match.
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

    src = rep(
        src,
        '''def cmd_squawk_feed(root: Path, a):
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
''',
        '''def cmd_squawk_feed(root: Path, a):
    """Run the squawk-feed writer daemon (fleet channel -> zipfs-vault)."""
    import squawk_feed
    argv = [
        "--root", str(root),
        "--channel", a.channel,
        "--identity", fleet_relay.resolve_identity(a.identity),
        "--key-dir", str(fleet_relay.resolve_key_dir(a.key_dir)),
        "--via", a.via or os.environ.get("ZIPFS_VIA") or "rclone",
    ]
    if a.zipfs_dir:
        argv += ["--zipfs-dir", a.zipfs_dir]
    if a.since is not None:
        argv += ["--since", str(a.since)]
    if a.cursor_file:
        argv += ["--cursor-file", a.cursor_file]
    if a.pull_first:
        argv += ["--pull-first"]
    squawk_feed.main(argv)
''',
        "S1-cmd",
    )

    src = rep(
        src,
        '''    s = sub.add_parser(
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
''',
        '''    s = sub.add_parser(
        "squawk-feed",
        help="run the squawk-feed writer daemon (fleet channel -> zipfs-vault)",
    )
    s.add_argument("--channel", default="fleet",
                   help="channel to watch (default: fleet)")
    s.add_argument(
        "--identity", default=None,
        help="relay identity for unseal "
             "(default: $SQUAWK_RELAY_IDENTITY or 'relay')",
    )
    s.add_argument(
        "--key-dir", default=None,
        help="fleet keys dir "
             "(default: $FLEET_KEYS_DIR or /home/toxic/.shingle/keys)",
    )
    s.add_argument(
        "--zipfs-dir", default=None,
        help="zipfs-vault skill dir "
             "(default: ~/workspace/skills/zipfs-vault)",
    )
    s.add_argument(
        "--via", default=None,
        help="vault transport: rclone | gws (default: $ZIPFS_VIA or rclone)",
    )
    s.add_argument(
        "--since", type=int, default=None,
        help="start cursor (default: from vault + saved cursor)",
    )
    s.add_argument(
        "--cursor-file", default=None,
        help="persist writer cursor here "
             "(default: ~/.local/state/squawk-feed/<channel>.cursor)",
    )
    s.add_argument(
        "--pull-first", action="store_true",
        help="pull before every put batch (another writer may sync)",
    )
    s.set_defaults(func=cmd_squawk_feed)
''',
        "S2-subparser",
    )

    chat_py.write_text(src, encoding="utf-8")
    print(f"OK: edited {chat_py} (S1-S2 applied)")


if __name__ == "__main__":
    main()
