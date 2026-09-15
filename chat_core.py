from __future__ import annotations

import datetime as _dt
import os
import re
import sys
import time
from pathlib import Path

from fleet_addr import addressed_wait_filter

def root_dir(explicit: str | None) -> Path:
    # Precedence: --root flag > AGENT_CHAT_ROOT env > ~/agent-chat default.
    base = (
        explicit or os.environ.get("AGENT_CHAT_ROOT") or str(Path.home() / "agent-chat")
    )
    return Path(base)


def now_iso() -> str:
    # Local time WITH offset so a git-committed thread is unambiguous across machines.
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "msg"


def _frontmatter_value(value) -> str:
    """Keep a dynamic frontmatter value on exactly one physical line."""
    return re.sub(r"[\r\n]+", " ", str(value))


class AgentChatError(Exception):
    pass


EVENT_SCHEMA_VERSION = 1
EVENT_TYPES = ("capability", "status")
CAPABILITY_PRIMITIVES = (
    "messages",
    "cursors",
    "wait",
    "tasks",
    "dependencies",
    "leases",
    "path_locks",
    "state_summary",
)
STATUS_VALUES = ("ready", "busy", "idle", "blocked", "stopped")


class AdapterEventError(AgentChatError):
    """Stable validation error for adapter-neutral events."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _event_text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(
            ord(char) < 32 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF
            for char in value
        )
    ):
        raise AdapterEventError("EVENT_INVALID_TEXT", f"{field} is invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise AdapterEventError("EVENT_INVALID_TEXT", f"{field} is invalid") from error
    try:
        _check_safe_name(value, field)
    except AgentChatError as error:
        raise AdapterEventError("EVENT_INVALID_TEXT", str(error)) from error
    return value


def _event_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise AdapterEventError("EVENT_INVALID_TIMESTAMP", "ts must be a string")
    try:
        parsed = _dt.datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        )
    except (TypeError, ValueError) as error:
        raise AdapterEventError(
            "EVENT_INVALID_TIMESTAMP", "ts must be ISO-8601"
        ) from error
    if parsed.tzinfo is None:
        raise AdapterEventError("EVENT_INVALID_TIMESTAMP", "ts must include an offset")
    return value


def validate_adapter_event(value: object) -> dict:
    if not isinstance(value, dict):
        raise AdapterEventError("EVENT_INVALID_RECORD", "event must be an object")
    event_type = value.get("event")
    if value.get("schema_version") != EVENT_SCHEMA_VERSION or isinstance(
        value.get("schema_version"), bool
    ):
        raise AdapterEventError("EVENT_UNSUPPORTED_VERSION", "schema_version must be 1")
    if event_type not in EVENT_TYPES:
        raise AdapterEventError(
            "EVENT_INVALID_TYPE", "event must be capability or status"
        )
    allowed = {"schema_version", "event", "agent", "harness", "ts"}
    if event_type == "capability":
        allowed.add("primitives")
    else:
        allowed.update({"status", "detail"})
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise AdapterEventError("EVENT_UNKNOWN_FIELD", ", ".join(unknown))
    for required in ("agent", "harness"):
        if required not in value:
            raise AdapterEventError("EVENT_REQUIRED_FIELD_MISSING", required)
        _event_text(value[required], required)
    if "ts" not in value:
        raise AdapterEventError("EVENT_REQUIRED_FIELD_MISSING", "ts")
    _event_timestamp(value["ts"])
    normalized = dict(value)
    if event_type == "capability":
        primitives = value.get("primitives")
        if (
            not isinstance(primitives, list)
            or not primitives
            or any(not isinstance(primitive, str) for primitive in primitives)
        ):
            raise AdapterEventError(
                "EVENT_INVALID_PRIMITIVES", "primitives must be strings"
            )
        if len(set(primitives)) != len(primitives):
            raise AdapterEventError(
                "EVENT_DUPLICATE_PRIMITIVE", "primitives must be unique"
            )
        for primitive in primitives:
            if primitive not in CAPABILITY_PRIMITIVES:
                raise AdapterEventError("EVENT_UNKNOWN_PRIMITIVE", str(primitive))
        normalized["primitives"] = list(primitives)
    else:
        status = value.get("status")
        if status not in STATUS_VALUES:
            raise AdapterEventError("EVENT_INVALID_STATUS", str(status))
        if "detail" in value:
            detail = value["detail"]
            if not isinstance(detail, str) or any(
                ord(char) < 32
                or 0x7F <= ord(char) <= 0x9F
                or 0xD800 <= ord(char) <= 0xDFFF
                for char in detail
            ):
                raise AdapterEventError("EVENT_INVALID_TEXT", "detail is invalid")
            try:
                detail.encode("utf-8")
            except UnicodeEncodeError as error:
                raise AdapterEventError(
                    "EVENT_INVALID_TEXT", "detail is invalid"
                ) from error
    return normalized


def make_capability_event(
    agent: str,
    harness: str,
    *,
    primitives: list[str] | None = None,
    timestamp: str | None = None,
) -> dict:
    return validate_adapter_event(
        {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event": "capability",
            "agent": agent,
            "harness": harness,
            "ts": timestamp or now_iso(),
            "primitives": list(primitives or CAPABILITY_PRIMITIVES),
        }
    )


def make_status_event(
    agent: str,
    harness: str,
    status: str,
    *,
    detail: str | None = None,
    timestamp: str | None = None,
) -> dict:
    event = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event": "status",
        "agent": agent,
        "harness": harness,
        "ts": timestamp or now_iso(),
        "status": status,
    }
    if detail is not None:
        event["detail"] = detail
    return validate_adapter_event(event)


def die(msg: str, code: int = 1):
    print(f"agent-chat: {msg}", file=sys.stderr)
    raise SystemExit(code)


# --- channel + message primitives -------------------------------------------


def _check_safe_name(name: str, kind: str):
    """Prevent path traversal vulnerabilities."""
    if not name or "/" in name or "\\" in name or ":" in name or name in (".", ".."):
        raise AgentChatError(f"invalid {kind} name (path traversal blocked): '{name}'")
    if name.startswith(".") or name.startswith("_"):
        raise AgentChatError(f"invalid {kind} name (reserved prefix blocked): '{name}'")


_TASK_MARKER_RE = re.compile(r"task-[A-Za-z0-9][A-Za-z0-9_-]*\.md")


def channel_dir(root: Path, channel: str) -> Path:
    _check_safe_name(channel, "channel")
    return root / channel


def require_channel(root: Path, channel: str) -> Path:
    d = channel_dir(root, channel)
    if not (d / "_meta.json").exists():
        raise AgentChatError(
            f"channel '{channel}' not found under {root} (run: init {channel})"
        )
    return d


def _seq_from_name(name: str) -> int | None:
    # Optimization: Native string parsing (.split, .isdecimal) is ~40% faster
    # than re.match in tight polling loops. isdecimal is used to prevent
    # ValueError on Unicode superscripts (e.g. ², which isdigit accepts).
    parts = name.split("-", 1)
    if len(parts) == 2 and parts[0].isdecimal():
        return int(parts[0])
    return None


def message_files(chan: Path):
    files = []
    try:
        with os.scandir(chan) as it:
            files = [
                Path(e.path)
                for e in it
                if e.name.endswith(".md") and _seq_from_name(e.name) is not None
            ]
    except OSError:
        pass
    return sorted(files, key=lambda p: _seq_from_name(p.name))


def parse_frontmatter(path: Path) -> dict:
    """Minimal front-matter reader: the block between the first two '---' lines.

    Values are strings except `to`, normalized to a list ([] == broadcast/all).
    """
    meta: dict = {}
    try:
        with path.open(encoding="utf-8") as f:
            first_line = f.readline()
            if not first_line.startswith("---"):
                return meta
            temp_meta = {}
            found_end = False
            for line in f:
                stripped = line.strip()
                if stripped == "---":
                    found_end = True
                    break
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                temp_meta[k.strip()] = v.strip()
            if not found_end:
                return meta
            meta = temp_meta
    except (OSError, UnicodeError):
        return meta
    # Normalize `to` -> list of recipients (empty == everyone).
    raw = meta.get("to", "").strip()
    if raw in ("", "all", "[]", "*"):
        meta["to_list"] = []
    else:
        meta["to_list"] = [x.strip() for x in raw.strip("[]").split(",") if x.strip()]
    return meta


def is_relevant(meta: dict, agent: str) -> bool:
    # Delegated to fleet_addr (ported from madnh/scratchpad): `to` is a hint
    # for wake-worthiness, never a visibility lock. Broadcast is the default.
    return addressed_wait_filter(meta, agent)


# --- atomic sequence lock ----------------------------------------------------


def _acquire_lock(chan: Path, timeout: float = 10.0, stale: float = 30.0) -> Path:
    """Atomic cross-platform lock via mkdir (fails if the dir already exists).

    Steals a lock older than `stale` seconds so a crashed poster can't wedge the
    channel forever.
    """
    lock = chan / "_seq.lock"
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
                raise AgentChatError(
                    "could not acquire channel seq lock (another poster is stuck?)"
                )
            time.sleep(0.05)


def _release_lock(lock: Path):
    try:
        os.rmdir(lock)
    except OSError:
        pass


def _next_seq(chan: Path) -> int:
    return max_seq(chan) + 1


# --- cursors -----------------------------------------------------------------


def cursor_path(chan: Path, agent: str) -> Path:
    return chan / ".cursors" / f"{slugify(agent)}.txt"


def read_cursor(chan: Path, agent: str) -> int:
    p = cursor_path(chan, agent)
    try:
        return int(p.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def write_cursor(chan: Path, agent: str, seq: int):
    p = cursor_path(chan, agent)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(seq), encoding="utf-8")


def max_seq(chan: Path) -> int:
    maximum = 0
    try:
        with os.scandir(chan) as it:
            for entry in it:
                if entry.name.endswith(".md"):
                    seq = _seq_from_name(entry.name)
                    if seq is not None and seq > maximum:
                        maximum = seq
    except OSError:
        pass
    return maximum


# --- commands ----------------------------------------------------------------




__all__ = [
    "root_dir",
    "now_iso",
    "slugify",
    "_frontmatter_value",
    "AgentChatError",
    "EVENT_SCHEMA_VERSION",
    "EVENT_TYPES",
    "CAPABILITY_PRIMITIVES",
    "STATUS_VALUES",
    "AdapterEventError",
    "_event_text",
    "_event_timestamp",
    "validate_adapter_event",
    "make_capability_event",
    "make_status_event",
    "die",
    "_check_safe_name",
    "_TASK_MARKER_RE",
    "channel_dir",
    "require_channel",
    "_seq_from_name",
    "message_files",
    "parse_frontmatter",
    "is_relevant",
    "_acquire_lock",
    "_release_lock",
    "_next_seq",
    "cursor_path",
    "read_cursor",
    "write_cursor",
    "max_seq",
]
