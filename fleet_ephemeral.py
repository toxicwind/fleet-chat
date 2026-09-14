"""Ephemeral channels for the agent-chat fork.

Lifecycle concept stolen from kotinder/roomcomm and adapted to pure
file-based channels (their hosted REST service is NOT used here).

What roomcomm actually implements (verified in code, /tmp/roomcomm):
- create_room (app/main.py:372-428): params = description, is_public,
  protocol_mode. A Room carries uuid, description, created_at, is_public,
  protocol_mode, plus LLM watermarks (app/models.py:10-28).
- Janitor pattern: _prune_hits (app/main.py:244-248) deletes usage rows
  older than HITS_RETENTION_DAYS = 90 (app/main.py:197). Expiry is a
  wall-clock cutoff evaluated in an explicit maintenance pass -- NOT on
  the read path.
- Bounded rooms: MAX_MESSAGES_PER_ROOM = 1000 (app/main.py:161); posting
  past the cap returns 429 "room_full" (app/main.py:541-542), permanent
  for that room.
- Deletion: admin_delete_room (app/main.py:1833-1859) cascade-deletes all
  child rows (messages, claims, revisions, discrepancies, handshakes)
  then the room itself. NOTE: roomcomm deletes WITHOUT archiving.
- Optional room TTL exists only as a roadmap bullet (README.md:307:
  "Optional room TTL (e.g. 7 / 30 days)") -- never implemented upstream.
  This module implements it, file-based.

Our adaptation:
- Channel metadata lives in the channel dir's existing ``_meta.json``
  (the fork's convention -- chat.py:421 cmd_init, chat.py:260
  require_channel, chat.py:456 cmd_channels). Ephemeral keys are merged
  in: {"ephemeral": true, "ttl_seconds": N, "created_ts": <epoch>}.
- Expiry = wall clock: now >= created_ts + ttl_seconds.
- Reaping is an explicit gc() pass that ARCHIVES FIRST (tar.gz of the
  whole channel dir into <root>/.archive/<channel>-<ts>.tar.gz) and only
  then deletes. A channel is NEVER deleted without a transcript archive.
- Name-prefix convention: channels named ``temp-*`` are treated as
  ephemeral even when never explicitly marked, using DEFAULT_TTL_SECONDS.

Stdlib only.

API:
    mark_ephemeral(channel_dir, ttl_seconds) -> Path  (the _meta.json path)
    is_expired(channel_dir) -> bool
    gc(root) -> list[str]  (names of reaped channels)
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path

META_FILE = "_meta.json"
ARCHIVE_DIR = ".archive"
TEMP_PREFIX = "temp-"

# Default TTL for channels that are ephemeral by the temp- prefix convention
# (or marked ephemeral without a valid ttl_seconds). 24h = 86400s.
#
# Why 24h, not roomcomm's roadmap suggestion of 7/30 days (README.md:307)?
# roomcomm's TTL idea targets human-facing rooms that sit idle for weeks.
# temp-* channels in this fleet are agent scratch/coordination spaces: a
# 24h default survives overnight multi-agent runs and reaps yesterday's
# clutter on the next gc, without the surprise of 7-day-old zombie rooms
# piling up. Anything that must live longer should be marked explicitly
# with its own ttl_seconds via mark_ephemeral().
DEFAULT_TTL_SECONDS = 86400


def _safe_name(name: str) -> None:
    """Mirror of the fork's _check_safe_name traversal guard (chat.py:242).

    fleet_ephemeral takes Paths (not names) so the guard is a fallback for
    direct use; the chat.py CLI must resolve names via channel_dir() first so
    the fork's own guard always applies at the boundary.
    """
    if (
        not name
        or "/" in name
        or "\\" in name
        or ":" in name
        or name in (".", "..")
    ):
        raise ValueError(f"invalid channel name (path traversal blocked): '{name}'")
    if name.startswith(".") or name.startswith("_"):
        raise ValueError(f"invalid channel name (reserved prefix blocked): '{name}'")


def _read_meta(channel_dir: Path) -> dict:
    try:
        return json.loads((channel_dir / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_meta(channel_dir: Path, meta: dict) -> Path:
    """Write _meta.json atomically (tmp file + os.replace)."""
    meta_path = channel_dir / META_FILE
    tmp_path = channel_dir / (META_FILE + ".tmp")
    tmp_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    os.replace(tmp_path, meta_path)
    return meta_path


def _created_ts(channel_dir: Path, meta: dict) -> float:
    """Best available creation timestamp for a channel dir.

    Prefers the explicit created_ts epoch (written by mark_ephemeral),
    then the fork's ISO 'created' field (chat.py cmd_init), then the
    directory mtime as a last resort.
    """
    v = meta.get("created_ts")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return float(v)
    iso = meta.get("created")
    if isinstance(iso, str) and iso:
        try:
            return datetime.datetime.fromisoformat(
                iso.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            pass
    try:
        return os.stat(channel_dir).st_mtime
    except OSError:
        return time.time()


def _effective_ttl(name: str, meta: dict) -> float:
    ttl = meta.get("ttl_seconds")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
        return float(DEFAULT_TTL_SECONDS)
    return float(ttl)


def mark_ephemeral(channel_dir: Path, ttl_seconds: float) -> Path:
    """Mark a channel ephemeral: it becomes eligible for gc() reaping after
    ttl_seconds from its creation timestamp.

    Merges {"ephemeral": True, "ttl_seconds": int, "created_ts": ...} into
    the channel's existing _meta.json, preserving all other keys. Records
    created_ts now only if the channel has no creation timestamp yet.
    """
    d = Path(channel_dir)
    if not d.is_dir():
        raise FileNotFoundError(f"channel dir not found: {d}")
    if isinstance(ttl_seconds, bool) or not isinstance(
        ttl_seconds, (int, float)
    ):
        raise ValueError("ttl_seconds must be a number")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be > 0")
    meta = _read_meta(d)
    if "created_ts" not in meta and "created" not in meta:
        meta["created_ts"] = time.time()
    meta["ephemeral"] = True
    meta["ttl_seconds"] = int(ttl_seconds)
    return _write_meta(d, meta)


def is_expired(channel_dir: Path) -> bool:
    """True if the channel is ephemeral (explicitly marked, or carrying the
    temp- prefix convention) and its TTL has elapsed. Never raises for a
    missing/unreadable channel -- returns False.
    """
    d = Path(channel_dir)
    if not d.is_dir():
        return False
    meta = _read_meta(d)
    explicit = meta.get("ephemeral") is True
    by_prefix = d.name.startswith(TEMP_PREFIX)
    if not (explicit or by_prefix):
        return False
    ttl = _effective_ttl(d.name, meta)
    return time.time() >= _created_ts(d, meta) + ttl


def gc(root: Path) -> list[str]:
    """Reap expired ephemeral channels under root.

    For each expired channel: archive the ENTIRE channel dir to
    <root>/.archive/<channel>-<UTC ts>.tar.gz FIRST, then delete the dir.
    A channel is never deleted without a successful archive. Channels that
    are not expired, not ephemeral, or fail to archive are left in place.

    The .archive dir is hidden (leading dot) so the fork's cmd_channels
    scandir filter (chat.py:453, skips names starting with ".") never
    lists it as a channel.

    Returns the list of reaped channel names (sorted by scan order).
    """
    root = Path(root)
    reaped: list[str] = []
    if not root.is_dir():
        return reaped
    archive = root / ARCHIVE_DIR
    archive.mkdir(parents=True, exist_ok=True)
    for entry in sorted(os.listdir(root)):
        if entry.startswith("."):
            continue
        d = root / entry
        if not d.is_dir():
            continue
        try:
            _safe_name(entry)
        except ValueError:
            continue
        # Only real channels (have _meta.json) or temp- convention dirs.
        if not (d / META_FILE).exists() and not entry.startswith(TEMP_PREFIX):
            continue
        try:
            if not is_expired(d):
                continue
        except Exception as exc:  # never let one bad channel kill the sweep
            print(f"gc: skipping {entry}: {exc}", file=sys.stderr)
            continue
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        tar_path = archive / f"{entry}-{ts}.tar.gz"
        n = 1
        while tar_path.exists():
            n += 1
            tar_path = archive / f"{entry}-{ts}-{n}.tar.gz"
        try:
            with tarfile.open(tar_path, "w:gz") as tf:
                tf.add(d, arcname=entry, recursive=True)
        except Exception as exc:
            print(f"gc: archive failed for {entry}: {exc}", file=sys.stderr)
            continue
        try:
            count = sum(1 for p in d.rglob("*") if p.is_file())
            shutil.rmtree(d)
        except Exception as exc:
            # Archive succeeded; keep it, leave the dir, report loudly.
            print(
                f"gc: archived {entry} -> {tar_path} but delete failed: {exc}",
                file=sys.stderr,
            )
            continue
        reaped.append(entry)
        print(f"gc: reaped {entry} -> {tar_path} ({count} files archived)")
    return reaped
