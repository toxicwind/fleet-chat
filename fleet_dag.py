#!/usr/bin/env python3
"""fleet_dag.py -- hash-linked DAG message log for the fleet chat (Borth et al. 2025, "Directed Acyclic Graph CRDTs").

Every message names its parent message hashes (git-like); threads merge as
DAG joins; concurrent reply storms never corrupt structure. Stdlib only
(hashlib). No daemons, no git reimplementation.

DATA MODEL
    Channel files stay exactly the base fork's format:
    NNNN-<from>-<slug>.md with YAML frontmatter + markdown body.
    One new frontmatter line is added at post time:

        parents: [<id>, <id>]

    Each <id> is the msg_id() (64 lowercase hex sha256) of a parent message.

    Post-time rules (wired in chat.py cmd_post; exact insertion points are
    listed in INTEGRATION.md):
      * default post:        parents = [id of the previous message in the
                               channel]  -- the message with max seq < new seq.
                               (The very first message, seq 1, gets [].)
      * reply (reply_to set): parents = [id of the reply target,
                               id of the previous message]
                               -- a merge JOIN of the reply thread and the
                               channel timeline. parents[0] is always the
                               *reply target*; parents[1] the chronological
                               predecessor.

CANONICAL FORM (what msg_id hashes)
    Reuses fleet_identity.canonical_message() verbatim when available:

        <fleet_identity.canonical_message bytes>
        parents: <comma-joined ids, SORTED lexicographically>\n

    The msg_id is sha256 of that byte string, hex-encoded (64 chars).

    Reusing canonical_message gives us, for free, the same field order and
    the same normalization semantics the identity layer signs over
    (CR/LF folding in one-line fields, strip, body via CRLF->LF + rstrip
    digest). The `to:` field hashes as the raw string exactly as stored.
    The `reply_to:` field keeps the base's raw value (a "#NN" ref or name --
    fleet_identity treats it as opaque text).

    UNIFICATION POINT (future): when parents is folded into
    fleet_identity.canonical_message() itself, the canonical version tag
    should bump to "fleet-chat-v2" and fleet_dag.py's local canonicalizer
    (below) becomes a one-liner calling canonical_message(parents=...).
    Until then, fleet_dag computes msg_id = H(identity_canonical || parents).

    The parents list is SORTED for hashing (set semantics -- a join is a
    join regardless of order) while the wire/file order is preserved as
    [reply_target, previous] so thread_view() can prefer parents[0].

id: STORED OR RECOMPUTED? (design decision)
    We do NOT store `id:` in frontmatter; msg_id is always recomputed from
    the file's canonical bytes. Tradeoff:
      + No stale-id hazard: an edited message changes its hash and the
        mismatch shows up as dangling children (missing_parent), which is
        the truthful signal for content-addressed data.
      + Signing stays coherent: fleet_identity's hmac covers the same
        canonical bytes (modulo parents line) without an id self-reference
        problem.
      - O(N) sha256 on verify_chain; one hash per message is trivial.
    Consequence: "hash mismatches" as a distinct verify class do not exist
    -- the recomputed id IS the id. verify_chain() therefore checks:
      1. missing_parent  -- parent id not present in the channel (or,
         equivalently, the message's content changed post-post).
      2. self_parent     -- message lists its own id as a parent.
      3. seq_regression  -- parent seq >= child seq (the DAG must respect
         post order; a merge join's parents are strictly older).
      4. duplicate_seq   -- two files claim the same seq number.
      5. empty_parents   -- non-genesis message with no parents (genesis =
         the lowest-seq message; only it may have []).
      6. cycle           -- a parent chain loops back (structurally
         impossible under the post rules; indicates hand edits).

API
    msg_id(path) -> str                       64-hex id of a message file
    parse_message(path) -> (meta, body)       frontmatter parse mirroring
                                              base parse_frontmatter semantics
    read_channel(channel_dir) -> {id: entry}  id -> {path, seq, meta, parents}
    verify_chain(channel_dir) -> [problem]    list of dicts
    thread_view(channel_dir, msg_id) -> [Path] root..msg following parents[0]

CLI
    python3 fleet_dag.py ids <channel_dir>            # seq + short id + title
    python3 fleet_dag.py verify <channel_dir>         # print problems
    python3 fleet_dag.py thread <channel_dir> <id>   # root..msg path list
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

PARENTS_KEY = "parents"
_ID_RE = re.compile(r"[0-9a-f]{64}")


class FleetDagError(Exception):
    """Raised for malformed message files and DAG violations."""


# ---------------------------------------------------------------------------
# canonicalization (mirrors fleet_identity, with graceful fallback)
# ---------------------------------------------------------------------------

def _one_line(value) -> str:
    """Mirror fleet_identity._one_line: fold CR/LF runs to a space, strip."""
    return re.sub(r"[\r\n]+", " ", str(value)).strip()


def _norm_body(body) -> str:
    """Mirror fleet_identity._norm_body: CRLF/CR -> LF, rstrip trailing ws."""
    text = str(body).replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip()


try:  # prefer the identity module's canonical form when it exists
    from fleet_identity import canonical_message as _id_canonical
    _IDENTITY_CANON = True
except Exception:  # pragma: no cover - standalone fallback
    _IDENTITY_CANON = False


def _local_canonical(*, seq, sender, to, reply_to, channel, ts, status, title, body) -> bytes:
    """Local replica of fleet_identity.canonical_message (same bytes).

    UNIFICATION POINT: if fleet_identity later accepts a parents= argument,
    delete this and call it directly; keep the 'fleet-chat-v1' tag semantics.
    """
    digest = hashlib.sha256(_norm_body(body).encode("utf-8")).hexdigest()
    lines = [
        "fleet-chat-v1",
        f"seq: {int(seq)}",
        f"from: {_one_line(sender)}",
        f"to: {_one_line(to)}",
        f"reply_to: {_one_line(reply_to or '')}",
        f"channel: {_one_line(channel)}",
        f"ts: {_one_line(ts)}",
        f"status: {_one_line(status)}",
        f"title: {_one_line(title)}",
        f"body-sha256: {digest}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def canonical_dag(meta: dict, body: str, parents: list[str]) -> bytes:
    """Canonical bytes hashed into a message id.

    fleet_identity.canonical_message(meta...) ++ 'parents: <sorted ids>\n'.
    Sorting gives set semantics for joins: parent order on the wire does not
    affect the id.
    """
    if _IDENTITY_CANON:
        base = _id_canonical(
            seq=meta.get("seq", "0"),
            sender=meta.get("from", ""),
            to=meta.get("to", ""),
            reply_to=meta.get("reply_to", ""),
            channel=meta.get("channel", ""),
            ts=meta.get("ts", ""),
            status=meta.get("status", ""),
            title=meta.get("title", ""),
            body=body,
        )
    else:
        base = _local_canonical(
            seq=meta.get("seq", "0"),
            sender=meta.get("from", ""),
            to=meta.get("to", ""),
            reply_to=meta.get("reply_to", ""),
            channel=meta.get("channel", ""),
            ts=meta.get("ts", ""),
            status=meta.get("status", ""),
            title=meta.get("title", ""),
            body=body,
        )
    plist = sorted(p for p in parents if p)
    return base + f"{PARENTS_KEY}: {','.join(plist)}\n".encode("utf-8")


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def _seq_from_name(name: str):
    parts = name.split("-", 1)
    if len(parts) == 2 and parts[0].isdecimal():
        return int(parts[0])
    return None


def parse_message(path: Path) -> tuple[dict, str]:
    """Parse a message file, mirroring base parse_frontmatter() semantics.

    Returns (meta dict, normalized body str). `parents` is normalized to a
    list of 64-hex ids (in wire order); unknown keys are kept as strings.
    Raises FleetDagError on unreadable / malformed files.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        raise FleetDagError(f"{path.name}: unreadable ({e})")
    lines = text.split("\n")
    if not lines or not lines[0].startswith("---"):
        raise FleetDagError(f"{path.name}: missing frontmatter block")
    meta: dict = {}
    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    if end_idx is None:
        raise FleetDagError(f"{path.name}: unterminated frontmatter block")
    # Base cmd_post writes: fm lines + "---" + "" + body.rstrip() + "\n",
    # i.e. exactly one blank line between the closing --- and the body.
    rest = "\n".join(lines[end_idx + 1:])
    if rest.startswith("\n"):
        rest = rest[1:]
    body = _norm_body(rest)
    # parents: accept "[a, b]", "a,b", or a single id; wire order preserved.
    raw = meta.get(PARENTS_KEY, "")
    if not raw:
        parents: list[str] = []
    else:
        parents = [x.strip() for x in raw.strip("[]").split(",") if x.strip()]
    meta[PARENTS_KEY] = parents
    return meta, body


def read_channel(channel_dir: str | Path) -> dict:
    """Read all NNNN-*.md messages -> {msg_id: entry}.

    entry = {"path": Path, "seq": int, "meta": dict, "body": str,
             "parents": [ids], "id": id}.
    Skips (silently) files that fail to parse; verify_chain reports them.
    """
    d = Path(channel_dir)
    out: dict = {}
    try:
        entries = sorted(d.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    for entry in entries:
        if not entry.name.endswith(".md"):
            continue
        seq = _seq_from_name(entry.name)
        if seq is None:
            continue
        try:
            meta, body = parse_message(entry)
        except FleetDagError:
            continue
        mid = msg_id_of(meta, body)
        out[mid] = {
            "path": entry,
            "seq": seq,
            "meta": meta,
            "body": body,
            "parents": meta.get(PARENTS_KEY, []),
            "id": mid,
        }
    return out


def msg_id_of(meta: dict, body: str) -> str:
    """sha256 hex of canonical_dag(meta, body, meta['parents'])."""
    parents = meta.get(PARENTS_KEY, [])
    if not isinstance(parents, list):
        parents = []
    return hashlib.sha256(canonical_dag(meta, body, parents)).hexdigest()


def msg_id(path: str | Path) -> str:
    """sha256 hex id of a message file: H(canonical frontmatter+body)."""
    meta, body = parse_message(Path(path))
    return msg_id_of(meta, body)


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------

def verify_chain(channel_dir: str | Path) -> list[dict]:
    """Verify the hash-linked DAG of a channel.

    Returns a list of problem dicts:
      {"type": ..., "file": name, "detail": ...}
    Types: unparseable, missing_parent, self_parent, seq_regression,
           duplicate_seq, empty_parents, cycle. Empty list == clean chain.
    """
    d = Path(channel_dir)
    problems: list[dict] = []
    chan = read_channel(d)
    by_seq: dict[int, list[str]] = {}
    for mid, e in chan.items():
        by_seq.setdefault(e["seq"], []).append(mid)

    # duplicate seq numbers
    for seq in sorted(by_seq):
        if len(by_seq[seq]) > 1:
            problems.append({
                "type": "duplicate_seq",
                "file": ", ".join(sorted(chan[m]["path"].name for m in by_seq[seq])),
                "detail": f"{len(by_seq[seq])} messages claim seq {seq}",
            })

    # report files that would not parse at all
    try:
        names = [p.name for p in d.iterdir()
                 if p.name.endswith(".md") and _seq_from_name(p.name) is not None]
    except OSError:
        names = []
    parsed_names = {e["path"].name for e in chan.values()}
    for name in sorted(set(names) - parsed_names):
        problems.append({
            "type": "unparseable",
            "file": name,
            "detail": "frontmatter parse failed (skipped from DAG)",
        })

    if not chan:
        return problems
    genesis_seq = min(e["seq"] for e in chan.values())

    for mid in sorted(chan, key=lambda m: chan[m]["seq"]):
        e = chan[mid]
        name = e["path"].name
        parents = e["parents"]
        if not parents:
            if e["seq"] != genesis_seq:
                problems.append({
                    "type": "empty_parents",
                    "file": name,
                    "detail": "non-genesis message has empty parents list",
                })
            continue
        if mid in parents:
            problems.append({
                "type": "self_parent",
                "file": name,
                "detail": "message lists its own id as a parent",
            })
        seen = set()
        for p in parents:
            if p in seen:
                continue
            seen.add(p)
            pe = chan.get(p)
            if pe is None:
                problems.append({
                    "type": "missing_parent",
                    "file": name,
                    "detail": f"parent id {p[:12]}... not found in channel "
                              "(absent file, or parent edited after post)",
                })
            elif pe["seq"] >= e["seq"]:
                problems.append({
                    "type": "seq_regression",
                    "file": name,
                    "detail": f"parent {pe['path'].name} (seq {pe['seq']}) is not "
                              f"older than child (seq {e['seq']})",
                })

    # cycle detection over the parent graph (defensive; post rules forbid it)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {mid: WHITE for mid in chan}
    stack: list[str] = []

    def visit(m: str) -> None:
        color[m] = GRAY
        stack.append(m)
        for p in chan[m]["parents"]:
            if p not in chan:
                continue
            if color[p] == GRAY:
                cyc = stack[stack.index(p):] + [p]
                problems.append({
                    "type": "cycle",
                    "file": chan[m]["path"].name,
                    "detail": "parent cycle: " + " -> ".join(
                        chan[x]["path"].name for x in cyc),
                })
            elif color[p] == WHITE:
                visit(p)
        stack.pop()
        color[m] = BLACK

    sys.setrecursionlimit(max(10000, len(chan) * 4 + 100))
    for mid in chan:
        if color[mid] == WHITE:
            visit(mid)

    return problems


# ---------------------------------------------------------------------------
# thread rendering
# ---------------------------------------------------------------------------

def thread_view(channel_dir: str | Path, target_id: str) -> list[Path]:
    """Return message paths from root to target_id, following parents.

    At a merge join the wire order is [reply_target, previous]; the view
    follows parents[0] (the reply thread), which is the rendering a chat
    client wants. Terminates at genesis (empty parents) or at a missing
    parent; cycles are cut defensively.
    Raises FleetDagError if target_id is unknown.
    """
    chan = read_channel(channel_dir)
    full = target_id.strip().lower()
    # allow unambiguous short-id prefixes
    matches = [m for m in chan if m.startswith(full)]
    if not matches:
        raise FleetDagError(f"unknown message id {target_id!r}")
    if len(matches) > 1:
        raise FleetDagError(
            f"ambiguous id prefix {target_id!r}: {len(matches)} matches")
    mid = matches[0]
    chain = [chan[mid]["path"]]
    visited = {mid}
    while True:
        parents = chan[mid]["parents"]
        if not parents:
            break  # genesis
        nxt = parents[0]
        if nxt not in chan or nxt in visited:
            break
        mid = nxt
        visited.add(mid)
        chain.append(chan[mid]["path"])
    chain.reverse()
    return chain


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> int:
    ap = argparse.ArgumentParser(description="fleet chat DAG: ids / verify / thread")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ids", help="list seq + short id + title for a channel")
    p.add_argument("channel_dir")
    p = sub.add_parser("verify", help="verify the hash-linked DAG of a channel")
    p.add_argument("channel_dir")
    p = sub.add_parser("thread", help="print root..msg thread path for an id")
    p.add_argument("channel_dir")
    p.add_argument("msg_id")
    a = ap.parse_args()
    try:
        if a.cmd == "ids":
            chan = read_channel(a.channel_dir)
            for mid in sorted(chan, key=lambda m: chan[m]["seq"]):
                e = chan[mid]
                print(f"#{e['seq']:04d} {mid[:12]} {e['path'].name} "
                      f"{e['meta'].get('title', '')}")
        elif a.cmd == "verify":
            problems = verify_chain(a.channel_dir)
            if not problems:
                print("DAG OK: chain clean")
                return 0
            for pr in problems:
                print(f"{pr['type']}: {pr['file']}: {pr['detail']}")
            return 1
        elif a.cmd == "thread":
            for pth in thread_view(a.channel_dir, a.msg_id):
                print(pth)
    except FleetDagError as e:
        print(f"fleet_dag: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
