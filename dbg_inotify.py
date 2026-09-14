#!/usr/bin/env python3
"""Debug: does the ctypes inotify wrapper fire on file create+close?"""
import select
import tempfile
from pathlib import Path

import squawk_feed

d = Path(tempfile.mkdtemp())
ino = squawk_feed._Inotify(d, squawk_feed.WATCH_MASK)
print("watching", d, "fd", ino.fd, flush=True)
(d / "0001-test-x.md").write_text("hello")
r, _, _ = select.select([ino.fd], [], [], 3.0)
print("select readable:", bool(r), flush=True)
if r:
    print("events drained:", ino.read_events(), flush=True)
    print("high now:", squawk_feed._channel_high(d), flush=True)
ino.close()
print("done", flush=True)
