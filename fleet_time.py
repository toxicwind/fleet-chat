"""fleet_time.py — Lamport logical clocks for fleet message ordering.

Paper steal #3: Leslie Lamport (1978), "Time, clocks, and the ordering of
events in a distributed system".

Why
---
Wall-clock `ts:` frontmatter cannot order fleet messages: agents run on
different machines, clocks skew, and `cmd_wait` polls on mtime. A Lamport
clock gives a causal order: every message carries a monotonically
increasing counter, and readers fold remote counters into their own clock.
Sorting by `(lamport, seq, agent_id)` yields a total order that is
*causally* consistent instead of wall-clock consistent.

Lamport rules (as implemented here)
-----------------------------------
- **Send event** (posting a message): ``tick(agent)`` — clock := clock + 1.
- **Receive event** (reading a message): ``observe(agent, remote_ts)`` —
  clock := max(clock, remote_ts). The +1 that classic Lamport does at
  receive time is intentionally deferred: it happens on the agent's *next*
  ``tick()`` (its next send). This keeps ``tick()`` the single place where
  the clock grows and keeps the receive path idempotent — re-reading the
  same message never advances the clock.

Frontmatter contract
--------------------
Field name: ``lamport:`` — a plain integer, e.g. ``lamport: 42``.
Messages written before this field existed are treated as ``lamport: 0``,
so causal sort degrades to the old seq order for legacy traffic.

Clock storage
-------------
Per-agent clock file: ``<root>/.clocks/<slugified-agent>`` holding a plain
integer (ASCII). Missing file == clock 0. Writes are atomic
(tmp file + ``os.replace``). Locks follow the base's ``_acquire_lock``
pattern (atomic ``mkdir``, stale-steal, timeout) implemented locally in
this module so it stays self-contained.

SINGLE-WRITER ASSUMPTION
------------------------
Each agent id is ticked/observed by **at most one process at a time**
(the agent's own process). The clock is *not* a shared counter across
agents — every agent owns its own file and only its own file.
Two processes of the SAME agent ticking concurrently are **unsupported**:
the per-clock lock makes the loser fail LOUD with ``ClockLockError``
instead of silently corrupting the counter. (Two *different* agents never
touch each other's files, so no cross-agent contention exists.)

Stdlib only. Add-only module: the merge coordinator wires hooks into
chat.py (see /tmp/extract-time/INTEGRATION.md).
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

__all__ = [
    "FRONTMATTER_FIELD",
    "CLOCKS_DIRNAME",
    "ClockError",
    "ClockLockError",
    "CorruptClockError",
    "tick",
    "observe",
    "read_clock",
    "message_lamport",
    "causal_sort",
    "clock_drift_report",
]

FRONTMATTER_FIELD = "lamport"
CLOCKS_DIRNAME = ".clocks"

_LOCK_TIMEOUT = 10.0   # seconds to wait for a contested clock lock
_LOCK_STALE = 30.0     # steal a clock lock older than this (crashed holder)


class ClockError(Exception):
    """Base error for fleet_time failures."""


class ClockLockError(ClockError):
    """Raised when a clock lock cannot be acquired.

    Two processes of the SAME agent ticking concurrently are unsupported;
    the loser fails loud here instead of racing the counter.
    """


class CorruptClockError(ClockError):
    """Raised when a clock file exists but does not hold a plain integer."""


# --- agent -> filename --------------------------------------------------------
# Mirrors chat.py's slugify() so clock files and .cursors/ files key the same
# agent id the same way. Duplicated here to keep this module self-contained.


def slugify(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "msg"


def _clocks_dir(root: Path) -> Path:
    return Path(root) / CLOCKS_DIRNAME


def _clock_path(root: Path, agent: str) -> Path:
    return _clocks_dir(root) / slugify(agent)


# --- atomic mkdir lock (mirrors chat.py _acquire_lock) ------------------------


def _acquire_clock_lock(clock_path: Path, timeout: float = _LOCK_TIMEOUT,
                        stale: float = _LOCK_STALE) -> Path:
    """Atomic cross-process lock for one agent's clock file, via mkdir.

    Raises ClockLockError on timeout — fail loud, never silently race.
    """
    lock = clock_path.parent / (clock_path.name + ".lock")
    clock_path.parent.mkdir(parents=True, exist_ok=True)
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
                raise ClockLockError(
                    f"could not acquire clock lock for {clock_path.name}: "
                    "another process of the same agent is ticking? "
                    "concurrent tick from two processes of one agent is unsupported"
                )
            time.sleep(0.05)


def _release_clock_lock(lock: Path) -> None:
    try:
        os.rmdir(lock)
    except OSError:
        pass


def _atomic_write_int(path: Path, value: int) -> None:
    """Write a plain int atomically: tmp file in same dir + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + f".tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(int(value)))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_int(path: Path) -> int:
    """Read the clock file. Missing file -> 0. Corrupt file -> loud error."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return 0
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        raise CorruptClockError(
            f"clock file {path} does not hold a plain integer: {raw!r}"
        )
    if value < 0:
        raise CorruptClockError(f"clock file {path} holds negative value {value}")
    return value


# --- Lamport API --------------------------------------------------------------


def read_clock(root, agent: str) -> int:
    """Current clock for agent without advancing it. Missing file -> 0."""
    return _read_int(_clock_path(Path(root), agent))


def tick(root, agent: str) -> int:
    """Send-event rule: clock := clock + 1, atomically. Returns the new value.

    Call in cmd_post AFTER acquiring the channel seq lock and BEFORE the
    frontmatter is written; stamp the returned value as ``lamport:``.
    """
    root = Path(root)
    clock_path = _clock_path(root, agent)
    lock = _acquire_clock_lock(clock_path)
    try:
        new_value = _read_int(clock_path) + 1
        _atomic_write_int(clock_path, new_value)
        return new_value
    finally:
        _release_clock_lock(lock)


def observe(root, agent: str, remote_ts) -> int:
    """Receive-event rule: clock := max(clock, remote_ts). No increment.

    The increment happens on the agent's next tick() (its next send).
    Idempotent: re-observing an old message never moves the clock.
    Returns the resulting clock value.
    """
    try:
        remote = int(remote_ts)
    except (TypeError, ValueError):
        return read_clock(root, agent)
    if remote < 0:
        remote = 0
    root = Path(root)
    clock_path = _clock_path(root, agent)
    lock = _acquire_clock_lock(clock_path)
    try:
        current = _read_int(clock_path)
        new_value = max(current, remote)
        if new_value != current:
            _atomic_write_int(clock_path, new_value)
        return new_value
    finally:
        _release_clock_lock(lock)


# --- frontmatter / sorting ----------------------------------------------------


def message_lamport(meta: dict) -> int:
    """Extract the lamport value from parsed frontmatter.

    Missing field (pre-Lamport messages) -> 0. Malformed -> 0 (fail-soft on
    the read path: sorting must never crash on one bad message; the writer
    path is what enforces the contract).
    """
    raw = (meta or {}).get(FRONTMATTER_FIELD, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def causal_sort(items):
    """Sort message tuples into causal (total) order.

    ``items``: iterable of ``(lamport, seq, agent_id, path)`` tuples.
    Sort key: ``(lamport, seq, agent_id)`` — Lamport first (causal order),
    then channel seq (unique per message on a channel; breaks cross-agent
    ties), then agent_id as the final deterministic tiebreak (only reached
    for cross-channel merges or clocks that never ticked, lamport 0).
    ``lamport=None`` is treated as 0 (legacy messages).

    Returns a new list; input order is irrelevant.
    """
    def key(item):
        lamport, seq, agent_id, _path = item
        return (
            int(lamport) if lamport is not None else 0,
            int(seq),
            str(agent_id),
        )

    return sorted(items, key=key)


def clock_drift_report(root) -> dict:
    """Debugging: {agent_slug: clock} for every clock file on disk.

    "Drift" here means divergence between agents' logical clocks — expected
    in Lamport time (clocks only advance on local sends + observed remotes);
    large gaps flag an agent that never observes (e.g. only posts).
    Corrupt files are reported as their raw string with a '!' prefix so the
    report never crashes.
    """
    root = Path(root)
    report = {}
    clocks = _clocks_dir(root)
    try:
        entries = sorted(clocks.iterdir())
    except OSError:
        return report
    for entry in entries:
        name = entry.name
        if name.endswith((".lock",)) or ".tmp-" in name:
            continue
        if not entry.is_file():
            continue
        try:
            report[name] = _read_int(entry)
        except CorruptClockError:
            try:
                report[name] = "!" + entry.read_text(encoding="utf-8").strip()
            except OSError:
                report[name] = "!unreadable"
    return report
