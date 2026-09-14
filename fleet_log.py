#!/usr/bin/env python3
"""fleet_log.py -- append-only per-channel JSONL index for the agent-chat fork.

WHAT THIS IS
------------
A *parallel index* to the fork's per-message ``NNNN-slug.md`` files -- not a
replacement for them.  chat.py's message files stay the source of truth;
``<channel>/log.jsonl`` is the fast catch-up lane: one small JSON object per
line, O_APPEND-atomic appends, and a ``replay()`` generator that lets a reader
jump from a stale cursor to the head of a channel without opening and parsing
thousands of individual message files.

Concept stolen from weijiafu14/agent-chatroom's ``messages.jsonl`` (one room =
one directory, one JSON object per line, appended with ``open("a")`` i.e.
O_APPEND -- see scripts/coord_write.py:373-374), with the racy parts left
behind:

* Their ``manage_lock`` (scripts/coord_write.py:45-67) is check-then-act on a
  lock file -- ``exists()`` / read owner / ``write_text()`` on acquire
  (:51-56), ``exists()`` / read / ``unlink()`` on release (:59-64) -- with no
  atomic create, so two agents can both "acquire" the same lock and a
  concurrent release can delete a freshly acquired lock (or crash on the
  loser's ``unlink()``).  We port no locks at all: O_APPEND *is* the
  concurrency control here.
* Their read-decide-append helpers (``is_duplicate_ack`` :76-90,
  ``should_skip_ack`` :170-232, ``find_decision_conflict`` :236-278) scan the
  whole log, decide, then append at :373 -- two writers can both pass the
  check and double-append.  We do no read-modify-write in the write path:
  not even for ``seq``.  The caller (chat.py) allocates ``seq`` under its
  existing atomic-mkdir channel lock and passes it in.
* Their records carry no sequence number; read cursors are file line numbers
  (scripts/coord_read.py:8-19).  Ours carry the fork's own ``seq`` so the log
  and the message files share one ordering.

CONCURRENCY MODEL
-----------------
``append()`` performs exactly one ``os.write()`` syscall on a descriptor
opened ``O_WRONLY | O_CREAT | O_APPEND``.  On POSIX each ``write()`` to an
O_APPEND descriptor is positioned atomically, so concurrent writers' records
stay contiguous and can never interleave.  (Upstream's buffered
``handle.write()`` could split a large record across several ``write()``
syscalls -- each atomically positioned, but interleavable between writers.
We bypass buffering with raw ``os.write`` and refuse to split: a short write
raises instead of silently tearing the record.)

READERS
-------
``replay()`` streams records with ``seq > since_seq`` in file order.  Only the
*tail* line can ever be torn (a writer's ``write()`` landed mid-read), so an
unparsable final line stops the stream silently -- the record shows up on the
next call once the write completes.  An unparsable line anywhere else raises
``CorruptLog``: fail loud; the index can always be rebuilt from the message
files, which remain the source of truth.

FSYNC POLICY
------------
Default ``fsync=True``: every append fsyncs the log file before returning.
Chat traffic is low-frequency; one fsync per post is cheap and makes the
index as durable as the message file it mirrors.  Cross-process *visibility*
does not need fsync (the page cache serves the record immediately); fsync
only buys crash durability.  Pass ``fsync=False`` for batch work (e.g. a
rebuild) and call ``fsync_log()`` once at the end.  We fsync the file, not
the directory: a crash in the exact instant the *first* record creates a
brand-new log.jsonl could lose the directory entry.  chat.py's channel init
should pre-create the file (``log_path(root, channel).touch()``) to close
even that window.

HMAC
----
Optional per-record authentication: pass ``key`` (str or bytes) to
``append()`` and the record gains an ``hmac`` field over the canonical JSON
of the other fields.  ``replay(..., key=...)`` verifies on the fly and raises
``BadHmac`` on mismatch; ``verify(rec, key)`` checks a single record.  The
key never touches disk here -- the caller owns key storage.

Stdlib only.  No threads, no locks, no imports from chat.py.  Add new files
only -- this module never modifies the fork.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Union

LOG_FILENAME = "log.jsonl"

_KeyType = Union[str, bytes]


# --- errors -----------------------------------------------------------------


class FleetLogError(Exception):
    """Base error for fleet_log."""


class UnsafeName(FleetLogError):
    """Channel name failed the path-traversal guard."""


class CorruptLog(FleetLogError):
    """A non-tail line of log.jsonl does not parse as a JSON object."""

    def __init__(self, path: Path, line_no: int):
        self.path = path
        self.line_no = line_no
        super().__init__(f"corrupt log line {line_no} in {path}")


class BadHmac(FleetLogError):
    """A record failed HMAC verification."""

    def __init__(self, seq):
        self.seq = seq
        super().__init__(f"record seq={seq} failed HMAC verification")


# --- paths ------------------------------------------------------------------


def _check_safe_name(name: str, kind: str = "channel") -> None:
    # Mirrors chat.py::_check_safe_name (chat.py:242). Keep the two in sync:
    # a channel name accepted by one must be accepted by the other, so the
    # log can never escape its channel directory.
    if not name or "/" in name or "\\" in name or ":" in name or name in (".", ".."):
        raise UnsafeName(f"invalid {kind} name (path traversal blocked): {name!r}")
    if name.startswith(".") or name.startswith("_"):
        raise UnsafeName(f"invalid {kind} name (reserved prefix blocked): {name!r}")


def log_path(root: Union[str, Path], channel: str) -> Path:
    """Path of the append-only log for a channel. Validates the channel name."""
    _check_safe_name(channel)
    return Path(root) / channel / LOG_FILENAME


# --- signing ----------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canon(rec: dict) -> bytes:
    # Canonical bytes for signing and for the wire: compact, sorted keys.
    # json.dumps never emits a raw newline inside a string value, so one
    # record is always exactly one line.
    return json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _key_bytes(key: _KeyType) -> bytes:
    return key.encode("utf-8") if isinstance(key, str) else bytes(key)


def _sign(rec: dict, key: _KeyType) -> str:
    return _hmac.new(_key_bytes(key), _canon(rec), hashlib.sha256).hexdigest()


def verify(rec: dict, key: _KeyType) -> bool:
    """True iff rec's ``hmac`` field authenticates its other fields under key."""
    sig = rec.get("hmac")
    if not isinstance(sig, str):
        return False
    body = {k: v for k, v in rec.items() if k != "hmac"}
    return _hmac.compare_digest(sig, _sign(body, key))


# --- write path --------------------------------------------------------------


def _single_write(fd: int, payload: bytes) -> None:
    # Exactly one write() syscall per record. O_APPEND positions it atomically,
    # so this record can neither interleave with nor be split by a concurrent
    # writer. A short write (disk full, signal) raises instead of tearing the
    # record across two syscalls and breaking the guarantee above.
    # (Python retries os.write on EINTR per PEP 475, so one call is enough.)
    n = os.write(fd, payload)
    if n != len(payload):
        raise FleetLogError(f"short write to log ({n}/{len(payload)} bytes); record not appended")


def append(
    root: Union[str, Path],
    channel: str,
    *,
    seq: int,
    agent: str,
    type: str,
    body: str,
    ts: Optional[str] = None,
    key: Optional[_KeyType] = None,
    fsync: bool = True,
    # Fidelity fields (optional): carried so anti-entropy backfill can
    # reconstruct a byte-identical, HMAC-verifiable message file.
    msg_hmac: Optional[str] = None,
    to: Optional[str] = None,
    title: Optional[str] = None,
    reply_to: Optional[str] = None,
    status: Optional[str] = None,
    lamport: Optional[int] = None,
    parents: Optional[list] = None,
) -> int:
    """Append one record to the channel log. Returns seq.

    ``seq`` is caller-assigned (chat.py's ``_next_seq`` under its atomic-mkdir
    channel lock) -- this function never reads the log to pick one.  The
    record is ``{"seq","ts","agent","type","body"[,"hmac"]}``, one JSON object
    per line, written with a single O_APPEND ``write()``.
    """
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise FleetLogError(f"seq must be a positive int, got {seq!r}")
    for label, val in (("agent", agent), ("type", type), ("body", body)):
        if not isinstance(val, str):
            raise FleetLogError(f"{label} must be str, got {val.__class__.__name__}")
    if ts is not None and not isinstance(ts, str):
        raise FleetLogError(f"ts must be str, got {ts.__class__.__name__}")

    rec = {
        "seq": seq,
        "ts": ts if ts is not None else _now_iso(),
        "agent": agent,
        "type": type,
        "body": body,
    }
    # Optional fidelity fields: stored verbatim when provided.
    if msg_hmac is not None:
        if not isinstance(msg_hmac, str):
            raise FleetLogError("msg_hmac must be str")
        rec["msg_hmac"] = msg_hmac
    for label, val in (("to", to), ("title", title), ("reply_to", reply_to),
                       ("status", status)):
        if val is not None:
            if not isinstance(val, str):
                raise FleetLogError(f"{label} must be str")
            rec[label] = val
    if lamport is not None:
        if not isinstance(lamport, int) or isinstance(lamport, bool):
            raise FleetLogError("lamport must be int")
        rec["lamport"] = lamport
    if parents is not None:
        if not isinstance(parents, list):
            raise FleetLogError("parents must be a list")
        rec["parents"] = [str(x) for x in parents]
    if key is not None:
        rec["hmac"] = _sign(rec, key)
    payload = _canon(rec) + b"\n"

    path = log_path(root, channel)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        _single_write(fd, payload)
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)
    return seq


def fsync_log(root: Union[str, Path], channel: str) -> None:
    """Durability barrier for a batch of ``fsync=False`` appends."""
    fd = os.open(log_path(root, channel), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --- read path ---------------------------------------------------------------


def replay(
    root: Union[str, Path],
    channel: str,
    since_seq: int = 0,
    *,
    key: Optional[_KeyType] = None,
) -> Iterator[dict]:
    """Yield log records with ``seq > since_seq``, in file order.

    File order equals seq order when writers append under chat.py's channel
    seq lock (the recommended integration).  A torn *tail* line -- a writer's
    ``write()`` racing our read -- stops the stream silently; the record
    appears on the next call.  A bad line anywhere else raises ``CorruptLog``.
    With ``key`` set, every record is HMAC-verified (``BadHmac`` on failure).
    Missing log file yields nothing (channel with no posts yet).
    """
    path = log_path(root, channel)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return
    total = len(lines)
    for i, raw in enumerate(lines, start=1):
        last = i == total
        if raw.endswith("\n"):
            text, complete = raw[:-1], True
        else:
            text, complete = raw, False
        if not text.strip():
            continue  # tolerate blank lines; we never write them
        try:
            rec = json.loads(text)
        except json.JSONDecodeError:
            if last and not complete:
                # Torn tail: a concurrent write() was in flight. Stop here;
                # the record will be whole on the next replay().
                return
            raise CorruptLog(path, i)
        if not isinstance(rec, dict):
            raise CorruptLog(path, i)
        if key is not None and not verify(rec, key):
            raise BadHmac(rec.get("seq"))
        try:
            rec_seq = int(rec.get("seq", 0))
        except (TypeError, ValueError):
            raise CorruptLog(path, i)
        if rec_seq > since_seq:
            yield rec


def last_seq(root: Union[str, Path], channel: str) -> int:
    """Highest seq observed in the log. Advisory only -- racy under concurrent
    writers; for diagnostics and rebuilds, never for seq allocation."""
    top = 0
    for rec in replay(root, channel):
        try:
            s = int(rec.get("seq", 0))
        except (TypeError, ValueError):
            continue
        top = max(top, s)
    return top


# --- self-test ----------------------------------------------------------------


def _selftest() -> None:
    import tempfile

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        chan_dir = Path(tmp) / "general"
        chan_dir.mkdir()

        # traversal guard mirrors chat.py
        for bad in ("", ".", "..", "a/b", "a\\b", "a:b", ".hidden", "_priv"):
            try:
                log_path(tmp, bad)
            except UnsafeName:
                pass
            else:
                raise AssertionError(f"guard missed {bad!r}")

        # basic append + replay
        append(tmp, "general", seq=1, agent="alice", type="chat", body="hello")
        append(tmp, "general", seq=2, agent="bob", type="chat", body="hi", key="s3cret", ts="2026-09-14T00:00:00+00:00")
        recs = list(replay(tmp, "general"))
        assert [r["seq"] for r in recs] == [1, 2], recs
        assert recs[1]["hmac"] and verify(recs[1], "s3cret")
        assert not verify(recs[1], "wrong")
        assert list(replay(tmp, "general", since_seq=1))[-1]["seq"] == 2
        try:
            list(replay(tmp, "general", key="wrong"))
        except BadHmac:
            pass
        else:
            raise AssertionError("BadHmac not raised")

        # torn tail tolerance: truncate mid-record
        p = log_path(tmp, "general")
        with open(p, "ab") as fh:
            fh.write(b'{"seq": 3, "ts": "2026')
        assert [r["seq"] for r in replay(tmp, "general", since_seq=2)] == []
        # ...then complete it and it appears
        with open(p, "ab") as fh:
            fh.write(b'-09-14T00:00:00+00:00", "agent": "x", "type": "chat", "body": "y"}\n')
        got = list(replay(tmp, "general", since_seq=2))
        assert [r["seq"] for r in got] == [3], got

        # corrupt mid-file line raises
        with open(p, "ab") as fh:
            fh.write(b"NOT JSON\n")
        append(tmp, "general", seq=4, agent="z", type="chat", body="after")
        try:
            list(replay(tmp, "general", since_seq=3))
        except CorruptLog as e:
            assert e.line_no == 4, e.line_no
        else:
            raise AssertionError("CorruptLog not raised")

        # missing log -> empty replay, last_seq 0
        assert list(replay(tmp, "nope")) == []
        assert last_seq(tmp, "nope") == 0

    print(f"fleet_log selftest OK ({time.time() - t0:.2f}s)")


if __name__ == "__main__":
    _selftest()
