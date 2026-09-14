#!/usr/bin/env python3
"""Pass the chat root into resolve_key_dir at the three relay call sites,
so <root>/keys is preferred when present. Usage: chat_edit3.py chat.py
"""

import sys
from pathlib import Path


def rep(src: str, old: str, new: str, name: str, count: int = 1) -> str:
    n = src.count(old)
    if n != count:
        raise SystemExit(f"EDIT {name}: expected {count} match(es), found {n}")
    return src.replace(old, new)


def main() -> None:
    chat_py = Path(sys.argv[1])
    src = chat_py.read_text(encoding="utf-8")

    src = rep(
        src,
        """    identity = fleet_relay.resolve_identity(a.identity)
    key_dir = fleet_relay.resolve_key_dir(a.key_dir)
    text = _relay_read_text(a)
""",
        """    identity = fleet_relay.resolve_identity(a.identity)
    key_dir = fleet_relay.resolve_key_dir(a.key_dir, root=root)
    text = _relay_read_text(a)
""",
        "T1-relay-in",
    )

    src = rep(
        src,
        """    d = require_channel(root, a.channel)
    key_dir = fleet_relay.resolve_key_dir(a.key_dir)
    identity = fleet_relay.resolve_identity(a.identity)
""",
        """    d = require_channel(root, a.channel)
    key_dir = fleet_relay.resolve_key_dir(a.key_dir, root=root)
    identity = fleet_relay.resolve_identity(a.identity)
""",
        "T2-relay-out",
    )

    src = rep(
        src,
        '''        "--key-dir", str(fleet_relay.resolve_key_dir(a.key_dir)),
''',
        '''        "--key-dir", str(fleet_relay.resolve_key_dir(a.key_dir, root=root)),
''',
        "T3-squawk-feed",
    )

    src = rep(
        src,
        '''        help="fleet keys dir "
             "(default: $FLEET_KEYS_DIR or /home/toxic/.shingle/keys)",
''',
        '''        help="fleet keys dir (default: $FLEET_KEYS_DIR, "
             "else <root>/keys if present)",
''',
        "T4-help",
        count=3,
    )

    chat_py.write_text(src, encoding="utf-8")
    print(f"OK: edited {chat_py} (T1-T4 applied)")


if __name__ == "__main__":
    main()
