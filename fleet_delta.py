#!/usr/bin/env python3
"""fleet_delta.py -- delta-state synchronization over the file-based chat root.

Paper steal #10: Almeida, Shoker, Baquero (2017),
"Delta state replicated data types". The steal, adapted to this repo:

* Each channel's message log (NNNN-<from>-<slug>.md files, seq allocated
  under the base's atomic-mkdir lock) is a grow-only replicated state.
* Each agent keeps a *summary vector* -- <root>/.vectors/<agent>.json mapping
  channel -> last-seen seq -- the file-based analog of a version vector.
* Agents exchange only the *delta*: messages with seq > vector[channel],
  never the whole log. Motivation: the WhatsApp-side agent on the slow path
  needs a compact "what's new" without scanning every channel.

This module ADDS vector state + delta queries; it REUSES -- never
re-implements -- the base's cursor and message primitives from chat.py:
read_cursor/write_cursor, slugify, _seq_from_name, channel_dir,
parse_frontmatter, is_relevant. The base's per-channel .cursors/ files are
left in place by migration so `chat.py read` keeps working unchanged.

Public API
----------
load_vector(root, agent) -> dict[str, int]
    Read the agent's summary vector. {} when no vector file exists yet.
save_vector(root, agent, vector)
    Atomically persist the vector (tmp file + os.replace).
migrate_from_cursors(root, agent) -> dict[str, int]
    Fold the base's per-channel .cursors/<slug>.txt into the vector
    (first-use only; the .cursors files are left untouched). Returns and
    persists the vector.
delta(root, agent) -> dict[str, list[Path]]
    All unread messages across ALL channels in one call:
    {channel: [message Paths with seq > vector[channel]]}, each list sorted
    by seq, channels in sorted order. Auto-migrates on first use.
advance(root, agent, new_vector)
    Persist new_vector after a successful read of the delta.
delta_digest(root, agent, *, relevant_only=True) -> list[str]
    The slow-path payload: one line per unread message, in
    (channel, seq) order:
        "<channel> #<seq> <from>-><to> <lamport> <body, 80 chars>"

Stdlib only. Safe on I/O races (missing dirs/files read as empty).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chat  # noqa: E402  -- reuse base primitives, do not re-implement

VECTORS_DIRNAME = ".vectors"
DIGEST_BODY_LIMIT = 80


# --- vector store ------------------------------------------------------------

def _vector_path(root: Path, agent: str) -> Path:
    # chat.slugify guarantees a filesystem-safe name; chat.py itself is the
    # authority on how an agent name becomes a file name.
    return Path(root) / VECTORS_DIRNAME / f"{chat.slugify(agent)}.json"


def _validate_vector(vector: dict) -> dict[str, int]:
    if not isinstance(vector, dict):
        raise chat.AgentChatError("delta vector is not a JSON object")
    clean: dict[str, int] = {}
    for channel, seq in vector.items():
        if not isinstance(channel, str):
            raise chat.AgentChatError("delta vector has a non-string channel")
        chat._check_safe_name(channel, "channel")  # traversal guard on stored data
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise chat.AgentChatError(
                f"delta vector has a bad seq for channel '{channel}'"
            )
        clean[channel] = seq
    return clean


def load_vector(root: Path, agent: str) -> dict[str, int]:
    """Return the agent's summary vector; {} when no vector file exists yet."""
    p = _vector_path(root, agent)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise chat.AgentChatError(f"could not read delta vector: {e}") from e
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise chat.AgentChatError(f"delta vector is not valid JSON: {e}") from e
    return _validate_vector(data)


def save_vector(root: Path, agent: str, vector: dict[str, int]):
    """Atomically persist the vector (tmp file + os.replace)."""
    vector = _validate_vector(dict(vector))
    d = Path(root) / VECTORS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    target = _vector_path(root, agent)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(vector, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def migrate_from_cursors(root: Path, agent: str) -> dict[str, int]:
    """Fold the base's per-channel .cursors/<slug>.txt into the vector.

    First-use only. Reads via chat.read_cursor (the base's own cursor
    reader) so whatever the base wrote is exactly what the vector starts
    with. The .cursors files are left untouched -- `chat.py read` keeps
    working against them.
    """
    vector: dict[str, int] = {}
    for channel in _channels(root):
        chan = chat.channel_dir(root, channel)
        seq = chat.read_cursor(chan, agent)
        if seq > 0:
            vector[channel] = seq
    save_vector(root, agent, vector)
    return vector


def _ensure_vector(root: Path, agent: str) -> dict[str, int]:
    if _vector_path(root, agent).exists():
        return load_vector(root, agent)
    return migrate_from_cursors(root, agent)


# --- delta -------------------------------------------------------------------

def _channels(root: Path) -> list[str]:
    """Channel names: non-hidden dirs holding a _meta.json (same convention
    as the base's `channels` command, whose scan lives inline there)."""
    names = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                if (
                    not entry.name.startswith(".")
                    and entry.is_dir()
                    and os.path.exists(os.path.join(entry.path, "_meta.json"))
                ):
                    names.append(entry.name)
    except OSError:
        pass
    return sorted(names)


def _new_message_paths(chan: Path, after_seq: int) -> list[tuple[int, Path]]:
    """One-pass scandir: (seq, path) for messages with seq > after_seq,
    sorted by seq. Mirrors the base's cmd_read scan; the seq-extraction
    primitive (chat._seq_from_name) is the base's own."""
    found: list[tuple[int, Path]] = []
    try:
        with os.scandir(chan) as it:
            for entry in it:
                if not entry.name.endswith(".md"):
                    continue
                seq = chat._seq_from_name(entry.name)
                if seq is not None and seq > after_seq:
                    found.append((seq, Path(entry.path)))
    except OSError:
        pass
    found.sort(key=lambda x: x[0])
    return found


def delta(root: Path, agent: str) -> dict[str, list[Path]]:
    """{channel: [message Paths with seq > vector[channel]]} across ALL
    channels in a single call. Channels in sorted order, each list sorted
    by seq. Auto-migrates from .cursors on first use."""
    vector = _ensure_vector(root, agent)
    out: dict[str, list[Path]] = {}
    for channel in _channels(root):
        cur = vector.get(channel, 0)
        paths = [p for _, p in _new_message_paths(chat.channel_dir(root, channel), cur)]
        if paths:
            out[channel] = paths
    return out


def advance(root: Path, agent: str, new_vector: dict[str, int]):
    """Persist new_vector after a successful read of the delta."""
    save_vector(root, agent, new_vector)


# --- digest: the slow-path payload -------------------------------------------

def _body_snippet(path: Path, limit: int = DIGEST_BODY_LIMIT) -> str:
    """First `limit` chars of the message body (text after the frontmatter),
    whitespace-collapsed. The base has no body extractor, so this one lives
    here. Bodies on priv-* channels are ciphertext (fleet_e2ee); the snippet
    stays opaque, never a plaintext leak."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return "(unreadable)"
    lines = text.splitlines()
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                body_start = i + 1
                break
    body = " ".join(" ".join(lines[body_start:]).split())
    if len(body) > limit:
        return body[:limit] + "..."
    return body


def delta_digest(root: Path, agent: str, *, relevant_only: bool = True) -> list[str]:
    """Slow-path payload: one line per unread message in (channel, seq)
    order: "<channel> #<seq> <from>-><to> <lamport> <body, 80 chars>".

    Lamport note: the base frontmatter carries no lamport field. The
    channel-local seq counter IS that channel's Lamport clock (monotonic,
    allocated under the base's atomic-mkdir lock), so the message's seq
    doubles as its Lamport stamp here. If a true cross-channel lamport field
    is ever added to the base, swap this one line.

    Relevance uses the base's chat.is_relevant (delegates to fleet_addr):
    `to` is a wake-worthiness hint, broadcast the default. Pass
    relevant_only=False for the full firehose.
    """
    lines: list[str] = []
    for channel, paths in delta(root, agent).items():
        for path in paths:
            meta = chat.parse_frontmatter(path)  # the base's reader
            if relevant_only and not chat.is_relevant(meta, agent):
                continue
            frm = meta.get("from", "?").strip() or "?"
            to = meta.get("to", "all").strip() or "all"
            seq = chat._seq_from_name(path.name)
            if seq is None:
                continue
            snippet = _body_snippet(path)
            lines.append(f"{channel} #{seq:04d} {frm}->{to} {seq} {snippet}")
    return lines


# --- selftest ----------------------------------------------------------------

def _selftest() -> int:
    """Build a throwaway root, exercise migrate/delta/digest/advance."""
    root = Path(tempfile.mkdtemp(prefix="delta-steal-"))
    agent = "whatsapp-relay"

    def mkmsg(channel: str, seq: int, frm: str, to: str, body: str):
        d = root / channel
        d.mkdir(parents=True, exist_ok=True)
        (d / "_meta.json").write_text(json.dumps({"channel": channel}), encoding="utf-8")
        (d / f"{seq:04d}-{frm}-note.md").write_text(
            "\n".join([
                "---", f"seq: {seq}", f"from: {frm}", f"to: {to}",
                f"channel: {channel}", "ts: 2026-09-14T00:00:00+00:00",
                "status: open", "title: note", "---", "", body, "",
            ]),
            encoding="utf-8",
        )

    mkmsg("ops", 1, "yote", "all", "bridge is up")
    mkmsg("ops", 2, "yote", "main", "main-only note about the router")
    mkmsg("general", 1, "shingle", "all", "hello fleet " * 20)
    # simulate a legacy .cursors read: agent already saw ops#1
    (root / "ops" / ".cursors").mkdir(parents=True, exist_ok=True)
    (root / "ops" / ".cursors" / f"{chat.slugify(agent)}.txt").write_text("1")

    vec = migrate_from_cursors(root, agent)
    assert vec == {"ops": 1}, f"migrate: {vec}"
    assert (root / ".vectors" / f"{chat.slugify(agent)}.json").exists()

    d = delta(root, agent)
    assert set(d) == {"ops", "general"}, f"delta channels: {set(d)}"
    assert [p.name for p in d["ops"]] == ["0002-yote-note.md"]
    assert [p.name for p in d["general"]] == ["0001-shingle-note.md"]

    lines = delta_digest(root, agent)
    # ops#2 is addressed to 'main' only, so relevance filtering drops it.
    assert len(lines) == 1, f"digest (relevant): {lines}"
    assert lines[0].startswith("general #0001 shingle->all 1 "), lines[0]
    assert "..." in lines[0]  # long body truncated at 80 chars

    lines_all = delta_digest(root, agent, relevant_only=False)
    assert len(lines_all) == 2
    assert lines_all[1].startswith("ops #0002 yote->main 2 "), lines_all[1]

    # advance past everything, delta goes quiet
    new_vec = dict(vec)
    for ch, paths in d.items():
        new_vec[ch] = max(new_vec.get(ch, 0), max(chat._seq_from_name(p.name) for p in paths))
    advance(root, agent, new_vec)
    assert delta(root, agent) == {}
    assert delta_digest(root, agent) == []
    assert load_vector(root, agent) == {"ops": 2, "general": 1}
    print("fleet_delta selftest: PASS (migrate/delta/digest/advance)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "selftest":
        sys.exit(_selftest())
    print("usage: python3 fleet_delta.py selftest", file=sys.stderr)
    sys.exit(2)
