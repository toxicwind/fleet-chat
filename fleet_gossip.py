#!/usr/bin/env python3
"""fleet_gossip.py -- anti-entropy gossip for the agent-chat fleet.

Paper steal #1: Demers et al. 1987, "Epidemic algorithms for replicated
database maintenance".  Their setting was many replicas of a database
synchronizing over an unreliable network; ours is one shared filesystem
where the *replicas* are two views of the same channel:

  * the numbered message files  <root>/<channel>/NNNN-<from>-<slug>.md
  * the append-only index        <root>/<channel>/log.jsonl  (fleet_log.py)

A message can be present in one view and missing in the other (a crash
between the two writes, a manual delete, a torn read).  Anti-entropy is
the periodic, cheap, deterministic repair pass:

  1. scan_gaps(channel)   -- missing seqs in 1..max_seq.  Each gap is
     labeled "archived" (some <root>/.archive/<channel>-<ts>.tar.gz
     tarball -- written by fleet_ephemeral.py's gc, which archives a
     channel dir before reaping it -- still holds a message file with
     that seq) or "lost" (no archive covers it).
  2. backfill(channel)    -- recover missing messages from log.jsonl via
     fleet_log.replay().  Recovered files are byte-identical on every
     run (content derives only from the log record; no timestamps are
     invented), so two agents racing the same backfill converge instead
     of conflicting, and digests stay stable across runs.
  3. write_digest(agent)  -- sha256 over sorted message filenames+mtimes
     per channel, stored at <root>/.digests/<agent>.json.
     compare_digests(a, b) lists channels whose digests differ.  This is
     the "exchange digests with a peer" step, with the .digests dir as
     the meeting place.
  4. anti_entropy(root, agent) -- the whole pass, returning one dict
     report: {gaps_found, backfilled, unrecoverable, divergent}.

Stdlib only.  No threads, no daemons, no randomness in the core path.
No imports from chat.py: the name guard, slugify and seq parsing are
replicated here (fleet_log.py sets the precedent), so this module never
fights the coordinator's integration.  It composes with fleet_log.py
only, and degrades cleanly when log.jsonl is absent (gap scan and
digests still work; backfill just has nothing to recover from).

COMPOSITION POINT for the coordinator: backfill can only restore what
log.jsonl records -- seq, ts, agent, type, body.  The log carries no
`to`, `title`, `reply_to` or `status`, so recovered messages are marked
`status: recovered` / `title: <type>-recovered` with a
`recovered_from: log.jsonl` provenance line.  If fleet_log ever gains
optional to/title/reply_to fields (append-only schema, backward
compatible), backfill should prefer them -- look for the
`# backfill-fidelity` marker below.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import fleet_log  # sibling module; stdlib-only, never imports chat.py

DIGESTS_DIR = ".digests"
ARCHIVE_DIR = ".archive"
META_FILENAME = "_meta.json"
DIGEST_FORMAT = "fleet_gossip/digest1"

_RootType = Union[str, Path]


# --- errors -----------------------------------------------------------------


class GossipError(Exception):
    """Base error for fleet_gossip."""


class UnsafeName(GossipError):
    """A channel or agent name failed the path-traversal guard."""


class BadChannel(GossipError):
    """The named channel does not exist under the root."""


# --- replicated fork primitives ---------------------------------------------
# Mirrors of chat.py (and fleet_log.py): _check_safe_name (chat.py:244),
# slugify (chat.py:54), _seq_from_name (chat.py:269), _frontmatter_value
# (chat.py:59), and the cmd_channels channel filter (chat.py:440).  Kept in
# sync by hand so this module never imports chat.py.


def _check_safe_name(name: str, kind: str = "channel") -> None:
    if (
        not name
        or "/" in name
        or "\\" in name
        or ":" in name
        or name in (".", "..")
    ):
        raise UnsafeName(f"invalid {kind} name (path traversal blocked): {name!r}")
    if name.startswith(".") or name.startswith("_"):
        raise UnsafeName(f"invalid {kind} name (reserved prefix blocked): {name!r}")


def _slugify(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "msg"


def _one_line(value) -> str:
    """Keep a frontmatter value on exactly one physical line."""
    return re.sub(r"[\r\n]+", " ", str(value))


def _seq_from_name(name: str) -> Optional[int]:
    parts = name.split("-", 1)
    if len(parts) == 2 and parts[0].isdecimal():
        return int(parts[0])
    return None


def _channel_dir(root: _RootType, channel: str) -> Path:
    _check_safe_name(channel, "channel")
    d = Path(root) / channel
    if not d.is_dir():
        raise BadChannel(f"no such channel: {channel!r}")
    return d


def _channels(root: _RootType) -> list:
    """Channel names, sorted.  Same filter as chat.py cmd_channels: visible
    directories containing _meta.json."""
    out = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir() and os.path.exists(
                    os.path.join(entry.path, META_FILENAME)
                ):
                    out.append(entry.name)
    except OSError:
        pass
    return sorted(out)


def _message_seqs(chan: Path) -> dict:
    """seq -> Path for the channel's numbered message files."""
    seqs = {}
    try:
        with os.scandir(chan) as it:
            for entry in it:
                if not entry.name.endswith(".md"):
                    continue
                seq = _seq_from_name(entry.name)
                if seq is not None:
                    seqs[seq] = Path(entry.path)
    except OSError:
        pass
    return seqs


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- (1) gap scan --------------------------------------------------------------


def _archive_tarballs(root: _RootType, channel: str) -> list:
    """Sorted tarball names in <root>/.archive belonging to this channel.

    fleet_ephemeral.py gc writes <channel>-<UTC ts>.tar.gz (plus -<n> on
    collision) holding the whole channel dir; the <channel>- prefix is
    unambiguous because channel names cannot contain '/'.
    """
    adir = Path(root) / ARCHIVE_DIR
    prefix = f"{channel}-"
    out = []
    try:
        with os.scandir(adir) as it:
            for entry in it:
                if (
                    entry.name.startswith(prefix)
                    and entry.name.endswith(".tar.gz")
                    and entry.is_file(follow_symlinks=False)
                ):
                    out.append(entry.name)
    except OSError:
        pass
    return sorted(out)


def _archived_seqs(root: _RootType, channel: str) -> set:
    """Seqs for which some channel tarball still holds a message file.

    Listing only -- members are never extracted, so hostile member paths
    (absolute, "..") are harmless.  An unreadable tarball is skipped, not
    fatal: it simply does not count as coverage.
    """
    adir = Path(root) / ARCHIVE_DIR
    covered = set()
    for tarname in _archive_tarballs(root, channel):
        try:
            with tarfile.open(adir / tarname, "r:gz") as tf:
                for member in tf.getmembers():
                    if member.isdir():
                        continue
                    base = member.name.rsplit("/", 1)[-1]
                    seq = _seq_from_name(base)
                    if seq is not None:
                        covered.add(seq)
        except (OSError, tarfile.TarError, EOFError):
            continue
    return covered


def _log_seqs(root: _RootType, channel: str) -> set:
    """Seqs present in the channel's log.jsonl (empty set when absent)."""
    seqs = set()
    for rec in fleet_log.replay(root, channel):
        try:
            s = int(rec.get("seq", 0))
        except (TypeError, ValueError):
            continue
        if s > 0:
            seqs.add(s)
    return seqs


def scan_gaps(root: _RootType, channel: str) -> dict:
    """Find missing message-file seqs in 1..max_seq.

    max_seq spans *both* views (message files and log.jsonl): a message
    present in the log but missing as a file is a gap in the file view.
    Each gap is labeled "archived" (a .archive tarball still holds it) or
    "lost".  Deterministic: everything sorted, no clock, no randomness.

    Returns {"channel", "max_seq", "missing": [{"seq","status"}]}.
    """
    chan = _channel_dir(root, channel)
    file_seqs = _message_seqs(chan)
    log_seqs = _log_seqs(root, channel)
    top = max([*file_seqs.keys(), *log_seqs, 0])
    archived = _archived_seqs(root, channel)
    missing = [
        {"seq": s, "status": "archived" if s in archived else "lost"}
        for s in range(1, top + 1)
        if s not in file_seqs
    ]
    return {"channel": channel, "max_seq": top, "missing": missing}


# --- (2) backfill --------------------------------------------------------------


def _recovered_text(channel: str, seq: int, rec: dict) -> tuple:
    """Render the recovered message file.  Content is a pure function of
    the log record -- no wall-clock -- so repeated backfills are
    byte-identical and idempotent.

    # backfill-fidelity: the log record carries only seq/ts/agent/type/
    body.  `to` defaults to "all", `status` is the honest marker
    "recovered", and `title` is derived from the record's type.  If
    fleet_log ever records optional to/title/reply_to fields, prefer
    them here and drop the -recovered suffixing.
    """
    agent = _one_line(rec.get("agent", "unknown"))
    rtype = _one_line(rec.get("type", "chat"))
    title = _one_line(f"{rtype}-recovered")
    fname = f"{seq:04d}-{_slugify(agent)}-{_slugify(title)}.md"
    fm = [
        "---",
        f"seq: {seq}",
        f"from: {agent}",
        "to: all",
        f"channel: {_one_line(channel)}",
        f"ts: {_one_line(rec.get('ts', ''))}",
        "status: recovered",
        f"title: {title}",
        "recovered_from: log.jsonl",
        "---",
        "",
    ]
    body = rec.get("body", "")
    if not isinstance(body, str):
        body = str(body)
    return fname, "\n".join(fm) + body.rstrip("\n") + "\n"


def _write_recovered(chan: Path, fname: str, content: str) -> bool:
    """Atomically write the recovered file.  Never overwrites: if the
    file appeared since the gap scan (a racing backfill), leave it --
    its content is identical anyway, and we keep the original mtime so
    digests stay stable.  Returns True when this call wrote the file."""
    target = chan / fname
    if target.exists():
        return False
    tmp = chan / f".gossip-{fname}.tmp"
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)
    return True


def backfill(root: _RootType, channel: str, *, key=None) -> dict:
    """Recover missing messages from the channel's log.jsonl.

    Returns {"channel", "recovered": [seqs], "unrecoverable":
    [{"seq","status"}], "log_error": str|None}.  A missing log.jsonl is
    not an error -- there is simply nothing to recover from.  A corrupt
    or HMAC-failing log is reported in "log_error" and the recoverable
    prefix is still backfilled (fail soft, report loud).
    """
    chan = _channel_dir(root, channel)
    gaps = scan_gaps(root, channel)
    log_recs: dict = {}
    log_error = None
    try:
        for rec in fleet_log.replay(root, channel, key=key):
            try:
                s = int(rec.get("seq", 0))
            except (TypeError, ValueError):
                continue
            if s > 0:
                log_recs[s] = rec
    except (fleet_log.CorruptLog, fleet_log.BadHmac, OSError) as exc:
        log_error = f"{type(exc).__name__}: {exc}"
    recovered, unrecoverable = [], []
    for item in gaps["missing"]:
        seq = item["seq"]
        rec = log_recs.get(seq)
        if rec is None:
            unrecoverable.append(item)
            continue
        fname, content = _recovered_text(channel, seq, rec)
        if _write_recovered(chan, fname, content):
            recovered.append(seq)
    return {
        "channel": channel,
        "recovered": sorted(recovered),
        "unrecoverable": unrecoverable,
        "log_error": log_error,
    }


# --- (3) digests ----------------------------------------------------------------


def digest_channel(root: _RootType, channel: str) -> str:
    """sha256 over the channel's sorted message filenames+mtimes.

    Payload is versioned (DIGEST_FORMAT) and names the channel, so
    digests can never collide across channels or format revisions.
    Message files are write-once in this fork (posts and backfills both
    never overwrite), so mtime is a stable content proxy here -- this is
    the cheap divergence detector, not a content proof.
    """
    chan = _channel_dir(root, channel)
    entries = []
    try:
        with os.scandir(chan) as it:
            for entry in it:
                if not entry.name.endswith(".md"):
                    continue
                if _seq_from_name(entry.name) is None:
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append(f"{entry.name}\t{st.st_mtime_ns}")
    except OSError:
        pass
    entries.sort()
    h = hashlib.sha256()
    h.update(f"{DIGEST_FORMAT} {channel} {len(entries)}\n".encode("utf-8"))
    for line in entries:
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _digest_path(root: _RootType, agent: str) -> Path:
    _check_safe_name(agent, "agent")
    return Path(root) / DIGESTS_DIR / f"{agent}.json"


def write_digest(root: _RootType, agent: str) -> Path:
    """(Re)write this agent's digest file.  Atomic tmp+replace, so a
    concurrent compare_digests never reads a torn file."""
    path = _digest_path(root, agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": DIGEST_FORMAT,
        "agent": agent,
        "written_at": _now_iso(),
        "channels": {ch: digest_channel(root, ch) for ch in _channels(root)},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)
    return path


def read_digest(root: _RootType, agent: str) -> dict:
    """agent's digest -> {channel: hex}.  Missing file -> {}."""
    path = _digest_path(root, agent)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise GossipError(f"unreadable digest for {agent!r}: {exc}")
    channels = payload.get("channels")
    if not isinstance(channels, dict):
        raise GossipError(f"malformed digest for {agent!r}: no channels map")
    return channels


def compare_digests(root: _RootType, a: str, b: str) -> list:
    """Sorted channels where agents a and b diverge.  A channel present
    in only one digest counts as divergent."""
    da, db = read_digest(root, a), read_digest(root, b)
    return sorted(c for c in set(da) | set(db) if da.get(c) != db.get(c))


# --- (4) anti-entropy pass --------------------------------------------------------


def anti_entropy(root: _RootType, agent: str) -> dict:
    """One full gossip round for `agent`: scan every channel, backfill
    from the log, publish a fresh digest, and compare it against every
    other agent's digest.

    Returns a JSON-serializable report:
      {"agent", "channels", "digest", "gaps_found": {ch: [seqs]},
       "backfilled": {ch: [seqs]},
       "unrecoverable": {ch: [{"seq","status"}]},
       "divergent": {ch: [other agents]},
       "errors": {ch-or-key: message}}
    Deterministic: channels and agents processed in sorted order; the
    only clock read is the digest's written_at stamp (metadata only).
    """
    _check_safe_name(agent, "agent")
    report = {
        "agent": agent,
        "channels": [],
        "digest": "",
        "gaps_found": {},
        "backfilled": {},
        "unrecoverable": {},
        "divergent": {},
        "errors": {},
    }
    chans = _channels(root)
    report["channels"] = chans
    for ch in chans:
        try:
            gaps = scan_gaps(root, ch)
            if gaps["missing"]:
                report["gaps_found"][ch] = [m["seq"] for m in gaps["missing"]]
            bf = backfill(root, ch)
            if bf["recovered"]:
                report["backfilled"][ch] = bf["recovered"]
            if bf["unrecoverable"]:
                report["unrecoverable"][ch] = bf["unrecoverable"]
            if bf["log_error"]:
                report["errors"][ch] = f"log: {bf['log_error']}"
        except (GossipError, OSError, fleet_log.FleetLogError) as exc:
            report["errors"][ch] = f"{type(exc).__name__}: {exc}"
    report["digest"] = str(write_digest(root, agent))
    ddir = Path(root) / DIGESTS_DIR
    try:
        others = sorted(
            p.stem for p in ddir.glob("*.json") if p.stem != agent
        )
    except OSError:
        others = []
    for other in others:
        try:
            div = compare_digests(root, agent, other)
        except GossipError as exc:
            report["errors"][f"digest:{other}"] = str(exc)
            continue
        for ch in div:
            report["divergent"].setdefault(ch, []).append(other)
    return report


# --- self-test ------------------------------------------------------------------


def _selftest() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Channel "general": files 1,2,4; log has 1..4 -> gap 3, recoverable.
        g = root / "general"
        g.mkdir()
        (g / "_meta.json").write_text('{"channel": "general"}', encoding="utf-8")
        for name in ("0001-alice-hello.md", "0002-bob-hi.md", "0004-alice-again.md"):
            (g / name).write_text("---\nseq: 0\n---\nbody\n", encoding="utf-8")
        # Channel "old" is created BEFORE any digest is written, so every
        # agent's digest covers the same channel set. Files 1,3; tarball
        # covers 2; no log -> archived+lost mix.
        o = root / "old"
        o.mkdir()
        (o / "_meta.json").write_text("{}", encoding="utf-8")
        (o / "0001-x-a.md").write_text("x", encoding="utf-8")
        (o / "0003-x-c.md").write_text("x", encoding="utf-8")
        adir = root / ARCHIVE_DIR
        adir.mkdir()
        data = b"archived body"
        ti = tarfile.TarInfo("old/0002-x-b.md")
        ti.size = len(data)
        with tarfile.open(adir / "old-20260914T000000Z.tar.gz", "w:gz") as tf:
            tf.addfile(ti, io.BytesIO(data))
        # A tarball for a *different* channel must not count as coverage.
        ti2 = tarfile.TarInfo("older/0002-x-b.md")
        ti2.size = len(data)
        with tarfile.open(adir / "older-20260914T000000Z.tar.gz", "w:gz") as tf:
            tf.addfile(ti2, io.BytesIO(data))

        write_digest(root, "alice")  # pre-repair digest, for divergence later
        for s, who in ((1, "alice"), (2, "bob"), (3, "carol"), (4, "alice")):
            fleet_log.append(
                root, "general", seq=s, agent=who, type="chat",
                body=f"msg{s}", fsync=False,
            )
        fleet_log.fsync_log(root, "general")

        gaps = scan_gaps(root, "general")
        assert gaps["max_seq"] == 4, gaps
        assert [m["seq"] for m in gaps["missing"]] == [3], gaps
        assert gaps["missing"][0]["status"] == "lost", gaps

        bf = backfill(root, "general")
        assert bf["recovered"] == [3] and bf["unrecoverable"] == [], bf
        assert bf["log_error"] is None, bf
        rec_path = g / "0003-carol-chat-recovered.md"
        assert rec_path.exists()
        text = rec_path.read_text(encoding="utf-8")
        assert text.startswith("---\n"), text
        for line in (
            "seq: 3", "from: carol", "to: all", "status: recovered",
            "recovered_from: log.jsonl",
        ):
            assert line in text, line
        mtime = rec_path.stat().st_mtime_ns
        bf2 = backfill(root, "general")  # idempotent: no rewrite, no dup
        assert bf2["recovered"] == [], bf2
        assert rec_path.stat().st_mtime_ns == mtime
        assert scan_gaps(root, "general")["missing"] == []

        gaps2 = scan_gaps(root, "old")
        assert [m["seq"] for m in gaps2["missing"]] == [2], gaps2
        assert gaps2["missing"][0]["status"] == "archived", gaps2
        bf3 = backfill(root, "old")
        assert bf3["recovered"] == [], bf3
        assert bf3["unrecoverable"] == [{"seq": 2, "status": "archived"}], bf3

        # Digests: alice's pre-repair digest diverges from bob's post-repair one.
        write_digest(root, "bob")
        assert digest_channel(root, "general") == digest_channel(root, "general")
        assert compare_digests(root, "alice", "bob") == ["general"], \
            compare_digests(root, "alice", "bob")
        assert compare_digests(root, "alice", "nobody") == ["general", "old"]

        # Full pass for dave: converges to bob's state, reports old's gap.
        rep = anti_entropy(root, "dave")
        assert rep["backfilled"] == {}, rep
        assert rep["unrecoverable"] == {
            "old": [{"seq": 2, "status": "archived"}]
        }, rep
        assert rep["divergent"] == {"general": ["alice"]}, rep
        assert rep["errors"] == {}, rep
        assert compare_digests(root, "bob", "dave") == []

        # Name guards.
        for bad in ("", "../x", "a/b", ".hidden", "_priv"):
            try:
                scan_gaps(root, bad)
            except (UnsafeName, BadChannel):
                pass
            else:
                raise AssertionError(f"guard missed channel {bad!r}")
        try:
            write_digest(root, "a/b")
        except UnsafeName:
            pass
        else:
            raise AssertionError("guard missed agent a/b")
        try:
            scan_gaps(root, "nope")
        except BadChannel:
            pass
        else:
            raise AssertionError("BadChannel not raised")

    print("fleet_gossip selftest OK")


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 3:
        print(json.dumps(anti_entropy(sys.argv[1], sys.argv[2]), indent=2))
    else:
        _selftest()
