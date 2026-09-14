#!/usr/bin/env python3
"""fleet_tasks.py -- file-based atomic-claim task board for the agent-chat fleet.

A task board per channel, stored as plain files under ``<channel>/tasks/``.
Each task is one JSON file ``<task-id>.json``::

    {"id": ..., "title": ..., "body": ..., "status": "open|claimed|done",
     "owner": ..., "created_by": ..., "ts": ...}

This is a port of the atomic-claim board from WarrenSchultz/chatroom-mcp
(upstream: chatroom/server.py ``claim_task`` + chatroom/db.py ``tasks`` table),
adapted to pure file-based operation: no server, no sqlite, no MCP.

Upstream mechanism (verified in code, not from the README):
  * Atomicity comes from a single conditional SQLite UPDATE --
    ``UPDATE tasks SET assignee=?, status='in_progress', version=version+1 ...
    WHERE id=? AND room=? AND status IN ('pending','blocked')
    AND (assignee IS NULL OR assignee=?)`` (server.py:344-348). SQLite
    serializes writers, so exactly one concurrent claimant sees
    ``cur.rowcount == 1``; the losers see 0 and are told who holds the task.
  * Lifecycle: pending -> in_progress -> done, plus blocked/cancelled
    (db.py:42-44). CLAIMABLE_STATUSES = ("pending", "blocked").
  * Task fields: id, room, title, body, status, assignee, depends_on (JSON),
    version (optimistic concurrency), created_by, created_ts, updated_ts
    (db.py:66-79). Re-claim by the current holder is idempotent
    (the ``OR assignee=?`` clause). release_task returns a task to pending.

File-based port -- how the atomic claim works here:
  * Claim = write a unique ticket file, then ``os.link(ticket, <id>.claim)``.
    Hard-link creation is atomic *and create-exclusive*: the kernel refuses
    with FileExistsError if ``<id>.claim`` already exists. Exactly one of N
    concurrent claimers succeeds; every loser gets FileExistsError. This is
    the file-based equivalent of upstream's ``rowcount == 1``.
  * Why not ``os.rename`` (the task's suggested mechanism)? rename() is
    atomic but it *replaces* the destination: two concurrent renames both
    "succeed" and both claimants walk away believing they won (last writer
    silently clobbers the first). rename() gives atomic *visibility*, which
    we still use for rewriting the task JSON (readers never see a torn
    file), but only link() gives atomic *exclusivity*, which is what a claim
    needs. Requirement: ticket and claim file live in the same directory so
    the link never crosses filesystems -- guaranteed, both are in
    ``<channel>/tasks/``.
  * Only the link winner rewrites ``<id>.json`` to status="claimed".
    Crash between link and rewrite is healed by idempotent re-claim: the
    holder re-runs claim_task, which completes the JSON flip.
  * Task IDs are ``task-<12 hex>`` (secrets.token_hex), unique without any
    lock. Name validation mirrors chat.py::_check_safe_name (chat.py:242).

Task lifecycle here (simplified from upstream per spec): open -> claimed -> done.
release_task() sends claimed -> open (owner only), like upstream's release.

Task events are appended to ``<channel>/tasks/_events.jsonl`` (one JSON per
line: ts, kind, task_id, actor, detail) so a watcher / chat.py integration
can mirror them into the message stream without this module importing chat.py.

Stdlib only. Safe for concurrent use by any number of fleet members, each of
which can only touch files.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import secrets
import sys
from pathlib import Path

STATUSES = ("open", "claimed", "done")

_EVENTS_LOG = "_events.jsonl"


class FleetTaskError(Exception):
    """Raised for invalid names, missing tasks/channels, and lock timeouts."""


class TaskNotFound(FleetTaskError):
    """Raised when the task id does not exist in the channel."""


def _now_iso() -> str:
    # Local time WITH offset, same convention as chat.py::now_iso (chat.py:47).
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _check_safe_name(name: str, kind: str):
    """Prevent path traversal. Semantics mirror chat.py::_check_safe_name
    (chat.py:242) so task/channel names are safe under the same rules."""
    if not name or "/" in name or "\\" in name or ":" in name or name in (".", ".."):
        raise FleetTaskError(f"invalid {kind} name (path traversal blocked): '{name}'")
    if name.startswith(".") or name.startswith("_"):
        raise FleetTaskError(f"invalid {kind} name (reserved prefix blocked): '{name}'")


def _tasks_dir(root: str | Path, channel: str) -> Path:
    _check_safe_name(channel, "channel")
    chan = Path(root) / channel
    if not chan.is_dir():
        raise FleetTaskError(f"channel '{channel}' does not exist under {root}")
    d = chan / "tasks"
    d.mkdir(exist_ok=True)
    return d


def _task_path(tdir: Path, task_id: str) -> Path:
    _check_safe_name(task_id, "task")
    return tdir / f"{task_id}.json"


def _claim_path(tdir: Path, task_id: str) -> Path:
    _check_safe_name(task_id, "task")
    return tdir / f"{task_id}.claim"


def _read_task_json(path: Path) -> dict:
    try:
        task = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TaskNotFound(f"task file not found: {path.name}")
    except (OSError, ValueError) as e:
        raise FleetTaskError(f"could not read task {path.name}: {e}")
    if not isinstance(task, dict) or task.get("status") not in STATUSES:
        raise FleetTaskError(f"task file {path.name} is corrupt")
    return task


def _write_task_json(path: Path, task: dict):
    """Atomically replace the task file: write unique temp, then os.replace.

    rename/replace is atomic on POSIX -- readers see the old or the new
    version, never a torn file. (Atomic visibility; exclusivity for claims
    comes from os.link, see claim_task.)"""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(task, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX


def _log_event(tdir: Path, kind: str, task_id: str, actor: str, detail: str = ""):
    """Append-only outbox other processes can tail to mirror events into the
    message stream. Appends are atomic for small writes on POSIX."""
    line = json.dumps(
        {
            "ts": _now_iso(),
            "kind": kind,
            "task_id": task_id,
            "actor": actor,
            "detail": detail[:200],
        }
    )
    with open(tdir / _EVENTS_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _claim_holder(tdir: Path, task_id: str) -> str | None:
    """Who holds the claim ticket, or None. The ticket file written by the
    link() winner records the owner, so losers can report who beat them."""
    try:
        data = json.loads(_claim_path(tdir, task_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    owner = data.get("owner") if isinstance(data, dict) else None
    return owner or None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def create_task(
    root: str | Path, channel: str, title: str, body: str, created_by: str
) -> dict:
    """Create an open task. Returns the task dict. Id is ``task-<12 hex>``,
    unique without locking. The file appears atomically via os.replace."""
    title = (title or "").strip()
    if not title:
        raise FleetTaskError("title must not be empty")
    if not (created_by or "").strip():
        raise FleetTaskError("created_by must not be empty")
    tdir = _tasks_dir(root, channel)
    task_id = f"task-{secrets.token_hex(6)}"
    task = {
        "id": task_id,
        "title": title,
        "body": body or "",
        "status": "open",
        "owner": None,
        "created_by": created_by.strip(),
        "ts": _now_iso(),
    }
    _write_task_json(_task_path(tdir, task_id), task)
    _log_event(tdir, "task_created", task_id, created_by.strip(), title[:120])
    return task


def claim_task(root: str | Path, channel: str, task_id: str, owner: str) -> bool:
    """Atomically claim an open task. Returns True iff this caller won.

    Protocol:
      1. Read the task; bail unless it exists and is open.
      2. If the caller already holds the claim ticket, return True
         (idempotent re-claim, like upstream's ``OR assignee=?``), repairing
         the task JSON if a previous winner crashed before flipping it.
      3. Write a uniquely-named ticket, then os.link(ticket, <id>.claim).
         The link is atomic and fails with FileExistsError if a ticket
         already exists -- exactly one of N concurrent claimers wins.
      4. Only the winner rewrites the task JSON to claimed/owner.
    """
    owner = (owner or "").strip()
    if not owner:
        raise FleetTaskError("owner must not be empty")
    tdir = _tasks_dir(root, channel)
    tpath = _task_path(tdir, task_id)
    cpath = _claim_path(tdir, task_id)

    task = _read_task_json(tpath)
    holder = _claim_holder(tdir, task_id)

    if holder == owner:
        # Idempotent re-claim; also heals a crash between link() and the
        # JSON rewrite below (ticket exists, JSON still "open").
        if task["status"] != "claimed" or task["owner"] != owner:
            task["status"] = "claimed"
            task["owner"] = owner
            _write_task_json(tpath, task)
        return True
    if holder is not None or task["status"] != "open":
        return False

    ticket = tdir / f".claim-{os.getpid()}-{secrets.token_hex(8)}.tmp"
    ticket.write_text(
        json.dumps({"owner": owner, "ts": _now_iso(), "pid": os.getpid()}),
        encoding="utf-8",
    )
    try:
        try:
            os.link(ticket, cpath)
        except FileExistsError:
            return False  # lost the race; someone else linked first
        # Winner: flip the task JSON (atomic replace; losers never touch it).
        # No one could have released/completed between our check and the link
        # win: both require an existing claim ticket, and none existed.
        task = _read_task_json(tpath)
        task["status"] = "claimed"
        task["owner"] = owner
        _write_task_json(tpath, task)
        _log_event(tdir, "task_claimed", task_id, owner, task["title"][:120])
        return True
    finally:
        try:
            ticket.unlink()
        except OSError:
            pass


def complete_task(root: str | Path, channel: str, task_id: str, owner: str) -> bool:
    """Mark a claimed task done. Only the owner may complete. Returns True
    iff the transition happened (or was already done by this owner)."""
    owner = (owner or "").strip()
    if not owner:
        raise FleetTaskError("owner must not be empty")
    tdir = _tasks_dir(root, channel)
    tpath = _task_path(tdir, task_id)
    task = _read_task_json(tpath)
    holder = _claim_holder(tdir, task_id)
    effective_owner = task.get("owner") or holder
    if task["status"] == "done":
        return effective_owner == owner
    if task["status"] != "claimed" or effective_owner != owner:
        return False
    task["status"] = "done"
    task["owner"] = owner
    _write_task_json(tpath, task)
    try:
        _claim_path(tdir, task_id).unlink()
    except FileNotFoundError:
        pass
    _log_event(tdir, "task_completed", task_id, owner, task["title"][:120])
    return True


def release_task(root: str | Path, channel: str, task_id: str, owner: str) -> bool:
    """Give up a claimed task so another agent can take it (upstream's
    release_task). claimed -> open, owner cleared, ticket removed."""
    owner = (owner or "").strip()
    if not owner:
        raise FleetTaskError("owner must not be empty")
    tdir = _tasks_dir(root, channel)
    tpath = _task_path(tdir, task_id)
    task = _read_task_json(tpath)
    holder = _claim_holder(tdir, task_id)
    effective_owner = task.get("owner") or holder
    if task["status"] != "claimed" or effective_owner != owner:
        return False
    task["status"] = "open"
    task["owner"] = None
    _write_task_json(tpath, task)
    try:
        _claim_path(tdir, task_id).unlink()
    except FileNotFoundError:
        pass
    _log_event(tdir, "task_released", task_id, owner, task["title"][:120])
    return True


def get_task(root: str | Path, channel: str, task_id: str) -> dict:
    """Read one task. Raises TaskNotFound if missing."""
    tdir = _tasks_dir(root, channel)
    return _read_task_json(_task_path(tdir, task_id))


def list_tasks(
    root: str | Path, channel: str, status: str | None = None
) -> list[dict]:
    """List tasks in a channel, optionally filtered by status. Sorted by ts."""
    if status is not None and status not in STATUSES:
        raise FleetTaskError(f"status must be one of {STATUSES}")
    tdir = _tasks_dir(root, channel)
    out = []
    try:
        names = os.listdir(tdir)
    except OSError:
        return []
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            task = _read_task_json(tdir / name)
        except FleetTaskError:
            continue
        if status is None or task["status"] == status:
            out.append(task)
    out.sort(key=lambda t: (t["ts"], t["id"]))
    return out


def task_holder(root: str | Path, channel: str, task_id: str) -> str | None:
    """Who currently holds the task (claim ticket owner), or None."""
    return _claim_holder(_tasks_dir(root, channel), task_id)


# ---------------------------------------------------------------------------
# Minimal CLI (for manual use / testing; the real CLI lives in chat.py)
# ---------------------------------------------------------------------------


def _default_root(explicit: str | None) -> Path:
    return Path(
        explicit or os.environ.get("AGENT_CHAT_ROOT") or str(Path.home() / ".shingle" / "chat")
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="file-based atomic-claim task board")
    ap.add_argument("--root", default=None, help="chat root (default: $AGENT_CHAT_ROOT or ~/.shingle/chat)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create")
    p.add_argument("channel")
    p.add_argument("title")
    p.add_argument("--body", default="")
    p.add_argument("--by", required=True, help="creator identity")

    p = sub.add_parser("claim")
    p.add_argument("channel")
    p.add_argument("task_id")
    p.add_argument("--owner", required=True)

    p = sub.add_parser("complete")
    p.add_argument("channel")
    p.add_argument("task_id")
    p.add_argument("--owner", required=True)

    p = sub.add_parser("release")
    p.add_argument("channel")
    p.add_argument("task_id")
    p.add_argument("--owner", required=True)

    p = sub.add_parser("get")
    p.add_argument("channel")
    p.add_argument("task_id")

    p = sub.add_parser("list")
    p.add_argument("channel")
    p.add_argument("--status", choices=STATUSES, default=None)

    a = ap.parse_args(argv)
    root = _default_root(a.root)
    try:
        if a.cmd == "create":
            print(json.dumps(create_task(root, a.channel, a.title, a.body, a.by), indent=2))
        elif a.cmd == "claim":
            ok = claim_task(root, a.channel, a.task_id, a.owner)
            print(json.dumps({"claimed": ok, "holder": task_holder(root, a.channel, a.task_id)}))
        elif a.cmd == "complete":
            print(json.dumps({"completed": complete_task(root, a.channel, a.task_id, a.owner)}))
        elif a.cmd == "release":
            print(json.dumps({"released": release_task(root, a.channel, a.task_id, a.owner)}))
        elif a.cmd == "get":
            print(json.dumps(get_task(root, a.channel, a.task_id), indent=2))
        elif a.cmd == "list":
            tasks = list_tasks(root, a.channel, a.status)
            print(json.dumps(tasks, indent=2))
    except FleetTaskError as e:
        print(f"fleet_tasks: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
