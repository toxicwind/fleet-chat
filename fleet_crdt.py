#!/usr/bin/env python3
"""Commutative operation log / op-based CRDT (Shapiro et al. 2011, concept steal).

The insight stolen: model every fleet action as an OPERATION, and make the
set of operations converge regardless of the order they are observed in.
Replicas that have seen the same ops -- in any order -- materialize the same
state. No total order, no leader, no coordination on the merge path.

What this is, honestly: today the fleet shares one filesystem, so there is
exactly one op log per channel and merge is trivial. The value here is
(1) a unified, append-only audit trail of ALL action kinds (posts,
reactions, channel creates, bids, claims -- not just messages), and
(2) a merge/materialize substrate with PROVEN algebraic properties, ready
for the day a fleet member works from a replica (the WhatsApp-side agent
is the obvious first candidate).

The .md message files remain the canonical human-readable data; the op
log is a derived index, like log.jsonl. It never replaces anything.

Op kinds and their merge semantics
----------------------------------
- post            {seq, lamport}            set semantics on seq (idempotent)
- react           {target_seq, kind, ...}   multiset (traces accumulate)
- channel-create  {channel}                 set semantics on channel
- bid             {task_id, score, ts}      per (task, agent): latest ts wins
- claim           {task_id, winner}          last-writer-wins by (lamport, op_id)

All five are commutative under merge_ops: merge(a, b) == merge(b, a),
associative, and idempotent. materialize() folds any op set into the same
state regardless of input order (proven in selftest).

Storage
-------
- Per channel: <channel>/.ops.jsonl   (post/react/bid/claim)
- Root:        <root>/.ops.jsonl      (channel-create)
One JSON op per line, O_APPEND single os.write. Stdlib only; chat.py
imports this module, never the reverse.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

OPS_FILENAME = ".ops.jsonl"

# Op kinds. Free-form payloads; no closed schema beyond kind + channel.
POST = "post"
REACT = "react"
CHANNEL_CREATE = "channel-create"
BID = "bid"
CLAIM = "claim"

KINDS = (POST, REACT, CHANNEL_CREATE, BID, CLAIM)


class CrdtError(Exception):
    """Stable validation error for op-log violations."""


def _check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise CrdtError(f"crdt: unknown op kind {kind!r} (expected one of {KINDS})")
    return kind


def make_op(
    kind: str,
    channel: str,
    actor: str,
    lamport: int,
    payload: dict | None = None,
    ts: float | None = None,
    op_id: str | None = None,
) -> dict:
    """Build one operation. op_id defaults to a unique
    '<actor>:<lamport>:<8 hex>' -- pass an explicit one in tests."""
    _check_kind(kind)
    if not actor or not channel:
        raise CrdtError("crdt: actor and channel must be non-empty")
    return {
        "op_id": op_id or f"{actor}:{int(lamport)}:{uuid.uuid4().hex[:8]}",
        "kind": kind,
        "channel": channel,
        "actor": str(actor),
        "lamport": int(lamport),
        "ts": float(ts) if ts is not None else time.time(),
        "payload": dict(payload or {}),
    }


def _ops_path(root, channel: str | None) -> Path:
    root = Path(root)
    if channel:
        if "/" in channel or channel.startswith(".") or ".." in channel:
            raise CrdtError(f"crdt: unsafe channel {channel!r}")
        return root / channel / OPS_FILENAME
    return root / OPS_FILENAME


def append_op(root, channel: str | None, op: dict) -> dict:
    """Append one op (O_APPEND, single write). Returns the op."""
    if not isinstance(op, dict) or "op_id" not in op:
        raise CrdtError("crdt: op must be a dict with an op_id")
    _check_kind(op.get("kind", ""))
    path = _ops_path(root, channel)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(op, ensure_ascii=False, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    return op


def read_ops(root, channel: str | None) -> list[dict]:
    """All ops in log order. Corrupt lines are skipped; missing log -> []."""
    path = _ops_path(root, channel)
    ops: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise CrdtError(f"crdt: cannot read op log: {exc}")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("op_id") and rec.get("kind") in KINDS:
            ops.append(rec)
    return ops


def merge_ops(*op_sets: list[dict]) -> list[dict]:
    """Commutative, associative, idempotent union of op sets.

    Dedupes by op_id; orders deterministically by (lamport, op_id) so every
    replica that merges the same ops iterates them in the same order.
    """
    by_id: dict[str, dict] = {}
    for ops in op_sets:
        for op in ops:
            oid = op.get("op_id")
            if oid and oid not in by_id:
                by_id[oid] = op
    return sorted(
        by_id.values(), key=lambda o: (int(o.get("lamport", 0)), str(o.get("op_id")))
    )


def diff_ops(local: list[dict], remote: list[dict]) -> list[dict]:
    """Ops in `remote` not present in `local` (by op_id). Anti-entropy's
    shopping list."""
    seen = {o.get("op_id") for o in local}
    return [o for o in remote if o.get("op_id") not in seen]


def materialize(ops: list[dict]) -> dict:
    """Fold an op set into replica state. Order-independent: any permutation
    of the same op set yields the same state (the whole point)."""
    state = {
        "messages": {},   # seq -> {actor, lamport}
        "reactions": [],   # list of {target_seq, kind, actor, ...}
        "channels": set(),  # channel-create set
        "bids": {},       # task_id -> {agent: {score, ts}} (latest ts wins)
        "claims": {},     # task_id -> {agent, lamport, op_id} (LWW)
    }
    for op in merge_ops(ops):  # canonical order; result must not depend on it
        kind = op.get("kind")
        p = op.get("payload", {}) if isinstance(op.get("payload"), dict) else {}
        if kind == POST:
            seq = p.get("seq")
            if isinstance(seq, int):
                # Set semantics: re-applying the same post op is a no-op.
                state["messages"].setdefault(
                    seq, {"actor": op.get("actor"), "lamport": op.get("lamport")}
                )
        elif kind == REACT:
            state["reactions"].append(
                {
                    "target_seq": p.get("target_seq"),
                    "kind": p.get("react_kind", "signal"),
                    "actor": op.get("actor"),
                    "strength": p.get("strength", 1.0),
                }
            )
        elif kind == CHANNEL_CREATE:
            ch = p.get("channel") or op.get("channel")
            if ch:
                state["channels"].add(ch)
        elif kind == BID:
            tid, agent = p.get("task_id"), op.get("actor")
            if tid and agent:
                cur = state["bids"].setdefault(tid, {})
                prev = cur.get(agent)
                if prev is None or float(p.get("ts", 0)) >= float(prev.get("ts", 0)):
                    cur[agent] = {"score": p.get("score"), "ts": p.get("ts", 0)}
        elif kind == CLAIM:
            tid = p.get("task_id")
            if tid:
                cur = state["claims"].get(tid)
                key = (int(op.get("lamport", 0)), str(op.get("op_id")))
                if cur is None or key >= (cur["lamport"], cur["op_id"]):
                    state["claims"][tid] = {
                        "agent": p.get("agent") or op.get("actor"),
                        "lamport": int(op.get("lamport", 0)),
                        "op_id": str(op.get("op_id")),
                    }
    state["channels"] = sorted(state["channels"])
    return state


def selftest() -> None:
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="crdt-steal-"))

    def op(kind, **kw):
        kw.setdefault("op_id", f"t:{kind}:{kw.get('lamport', 0)}:{kw.get('actor', 'a')}")
        base = dict(kind=kind, channel="c", actor="a", lamport=0,
                    ts=0.0, payload={})
        base.update(kw)
        return base

    # Two replicas observe the same actions in different orders.
    ops_a = [
        op(POST, actor="alice", lamport=1, payload={"seq": 1}),
        op(REACT, actor="bob", lamport=2,
           payload={"target_seq": 1, "react_kind": "signal"}),
        op(BID, actor="alice", lamport=3,
           payload={"task_id": "t1", "score": 0.9, "ts": 10.0}),
    ]
    ops_b = [
        op(BID, actor="bob", lamport=4,
           payload={"task_id": "t1", "score": 0.7, "ts": 11.0}),
        op(POST, actor="bob", lamport=5, payload={"seq": 2}),
        op(CLAIM, actor="alice", lamport=6,
           payload={"task_id": "t1", "agent": "alice"}),
    ]
    ops_b_shuffled = [ops_b[2], ops_b[0], ops_b[1]]

    # merge is commutative / associative / idempotent.
    m1, m2 = merge_ops(ops_a, ops_b), merge_ops(ops_b, ops_a)
    assert [o["op_id"] for o in m1] == [o["op_id"] for o in m2], "not commutative"
    m3 = merge_ops(merge_ops(ops_a), ops_b)
    assert [o["op_id"] for o in m1] == [o["op_id"] for o in m3], "not associative"
    assert [o["op_id"] for o in merge_ops(m1, m1)] == [o["op_id"] for o in m1]
    # diff drives anti-entropy.
    assert {o["op_id"] for o in diff_ops(ops_a, m1)} == \
        {o["op_id"] for o in ops_b}
    assert diff_ops(m1, ops_a) == []

    # materialize converges regardless of observation order.
    s1 = materialize(ops_a + ops_b)
    s2 = materialize(ops_b_shuffled + ops_a)
    s3 = materialize(merge_ops(ops_a, ops_b))
    assert s1 == s2 == s3, "replicas diverged"
    assert set(s1["messages"]) == {1, 2}
    assert len(s1["reactions"]) == 1
    assert s1["bids"]["t1"]["alice"]["score"] == 0.9
    assert s1["claims"]["t1"]["agent"] == "alice"

    # Duplicate delivery changes nothing (at-least-once safe).
    assert materialize(ops_a + ops_b + ops_a) == s1

    # Round-trip through the log files.
    for o in ops_a:
        append_op(root, "c", o)
    append_op(root, None, op(CHANNEL_CREATE, actor="sys", lamport=0,
                             payload={"channel": "c"}))
    assert len(read_ops(root, "c")) == 3
    assert read_ops(root, None)[0]["kind"] == CHANNEL_CREATE
    assert read_ops(root, "nope") == []
    try:
        make_op("nope", "c", "a", 0)
    except CrdtError:
        pass
    else:
        raise AssertionError("bad kind accepted")
    print("fleet_crdt selftest: OK")


if __name__ == "__main__":
    selftest()
