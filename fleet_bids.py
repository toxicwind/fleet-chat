#!/usr/bin/env python3
"""Bid-then-consensus task allocation (Wang et al. 2022, concept steal).

First-come-first-served claiming rewards the fastest agent, not the most
suitable one, and stampedes on contended tasks. Instead, agents publish
BIDS -- a self-assessed suitability score in [0, 1] -- for an open task,
and the winner is chosen by a deterministic rule that every agent can
verify independently from the same bid log. No auctioneer, no extra
round trips, no new task board: the lease itself still comes from the
base LeaseStore; the bid gate is a pre-check in chat.py cmd_task_claim.

Protocol
--------
1. ``task create``                       (base machinery, unchanged)
2. ``task bid <id> --as AGENT --score S`` (any agent, re-bidding updates)
3. ``task claim <id> --as WINNER``        (only the consensus winner's
                                          claim succeeds while bids live)

Consensus rule (total, deterministic order):
    highest score wins; tie -> lexicographically smallest agent id;
    then earliest bid timestamp. Every agent reading the same bid log
    computes the same ranking.

Liveness: bids expire after BID_TTL_SECONDS (default 3600s). A winner that
never claims loses priority once its bid goes stale; with no live bids the
task falls back to first-come claiming. ``task bids --clear`` resets a
round early (leader intervention). On a successful claim the round's bids
are archived (``<task_id>.jsonl`` -> ``<task_id>.claimed-<ts>.jsonl``) so a
later release starts a fresh round.

Storage: ``<channel>/.bids/<task_id>.jsonl``, one JSON bid per line,
O_APPEND single os.write per bid. Re-bids append; resolution takes each
agent's LATEST bid. Stdlib only; no imports from chat.py (it imports us).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

BIDS_DIRNAME = ".bids"
BID_TTL_SECONDS = 3600.0


class BidError(Exception):
    """Stable validation error for bid-then-consensus violations."""


def _check_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BidError(f"bid: {label} must be a non-empty string")
    if (
        "/" in value
        or "\\" in value
        or "\x00" in value
        or value.startswith(".")
        or value in ("..",)
        or ".." in value.split("-")
    ):
        raise BidError(f"bid: unsafe {label} {value!r}")
    return value


def _check_score(score) -> float:
    try:
        s = float(score)
    except (TypeError, ValueError):
        raise BidError(f"bid: score must be a number in [0, 1], got {score!r}")
    if not (0.0 <= s <= 1.0):
        raise BidError(f"bid: score must be in [0, 1], got {s}")
    return s


def _bids_dir(root, channel: str) -> Path:
    root = Path(root)
    channel = _check_name(channel, "channel")
    d = root / channel / BIDS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _bid_path(root, channel: str, task_id: str) -> Path:
    _check_name(task_id, "task_id")
    return _bids_dir(root, channel) / f"{task_id}.jsonl"


def record_bid(
    root,
    channel: str,
    task_id: str,
    agent: str,
    score: float,
    note: str = "",
    ts: float | None = None,
) -> dict:
    """Append one bid. Re-bidding updates (resolution takes the latest).

    Returns the recorded bid dict. Raises BidError on bad input.
    """
    agent = _check_name(agent, "agent")
    s = _check_score(score)
    if ts is None:
        ts = time.time()
    bid = {
        "agent": agent,
        "score": s,
        "ts": float(ts),
        "note": str(note or ""),
    }
    path = _bid_path(root, channel, task_id)
    payload = (json.dumps(bid, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    return bid


def read_bids(root, channel: str, task_id: str) -> list[dict]:
    """All recorded bids for a task, in append order. Corrupt lines are
    skipped (fail soft, read path); a missing file means no bids."""
    path = _bid_path(root, channel, task_id)
    bids: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise BidError(f"bid: cannot read bid log: {exc}")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(rec, dict)
            and isinstance(rec.get("agent"), str)
            and isinstance(rec.get("score"), (int, float))
        ):
            bids.append(rec)
    return bids


def _latest_per_agent(bids: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for b in bids:
        a = b["agent"]
        if a not in latest or float(b.get("ts", 0)) >= float(latest[a].get("ts", 0)):
            latest[a] = b
    return latest


def resolve(
    root,
    channel: str,
    task_id: str,
    *,
    now: float | None = None,
    ttl: float = BID_TTL_SECONDS,
) -> dict:
    """Deterministic consensus over the bid log.

    Returns {"winner": agent|None, "ranked": [bids...], "live": [...],
    "stale": [...]}. Ranking: highest score, then smallest agent id, then
    earliest ts. Each agent contributes its latest live bid only.
    """
    if now is None:
        now = time.time()
    bids = read_bids(root, channel, task_id)
    live = [
        b for b in _latest_per_agent(bids).values()
        if now - float(b.get("ts", 0)) <= ttl
    ]
    stale = [b for b in bids if b not in live]
    ranked = sorted(
        live, key=lambda b: (-float(b["score"]), str(b["agent"]), float(b.get("ts", 0)))
    )
    return {
        "winner": ranked[0]["agent"] if ranked else None,
        "ranked": ranked,
        "live": live,
        "stale": stale,
    }


def check_claim(
    root,
    channel: str,
    task_id: str,
    agent: str,
    *,
    ttl: float = BID_TTL_SECONDS,
    now: float | None = None,
) -> dict | None:
    """Bid gate for cmd_task_claim. Returns the resolution when bids exist.

    Raises BidError if live bids exist and `agent` is not the consensus
    winner. Returns None when no live bids exist (legacy first-come claim
    proceeds). Never raises for missing bid files.
    """
    res = resolve(root, channel, task_id, ttl=ttl, now=now)
    if res["winner"] is None:
        return None
    if res["winner"] != agent:
        ranked = ", ".join(
            f"{b['agent']}={b['score']}" for b in res["ranked"]
        )
        raise BidError(
            f"bid consensus: task '{task_id}' is awarded to '{res['winner']}' "
            f"(ranked: {ranked}); '{agent}' may not claim while bids are live"
        )
    return res


def archive_round(root, channel: str, task_id: str) -> Path | None:
    """Archive a finished bid round so a later release starts fresh.

    Renames <task_id>.jsonl -> <task_id>.claimed-<unixts>.jsonl. Returns the
    archive path, or None when there was no live bid file.
    """
    path = _bid_path(root, channel, task_id)
    if not path.exists():
        return None
    dest = path.with_name(f"{task_id}.claimed-{int(time.time())}.jsonl")
    os.replace(path, dest)
    return dest


def clear_bids(root, channel: str, task_id: str) -> int:
    """Leader intervention: drop the current round's bids. Returns the
    number of bid records discarded."""
    path = _bid_path(root, channel, task_id)
    bids = read_bids(root, channel, task_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return len(bids)


def selftest() -> None:
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="bids-steal-"))
    # Deterministic conflict resolution.
    record_bid(root, "c", "t1", "bob", 0.7, ts=1000.0)
    record_bid(root, "c", "t1", "alice", 0.9, ts=1001.0)
    record_bid(root, "c", "t1", "carol", 0.9, ts=1002.0)
    res = resolve(root, "c", "t1", now=1003.0, ttl=3600)
    # alice and carol tie at 0.9 -> lexicographically smallest agent wins.
    assert res["winner"] == "alice", res
    assert [b["agent"] for b in res["ranked"]] == ["alice", "carol", "bob"]
    # Re-bid updates: bob outbids everyone.
    record_bid(root, "c", "t1", "bob", 0.95, ts=1004.0)
    res = resolve(root, "c", "t1", now=1005.0, ttl=3600)
    assert res["winner"] == "bob", res
    # Gate: non-winner is rejected, winner passes.
    try:
        check_claim(root, "c", "t1", "alice", ttl=3600, now=1005.0)
    except BidError as e:
        assert "awarded to 'bob'" in str(e), e
    else:
        raise AssertionError("non-winner claim was not rejected")
    assert check_claim(root, "c", "t1", "bob", ttl=3600, now=1005.0)["winner"] == "bob"
    # Stale bids -> no gate (bids are from ts~1000, real now is far later).
    res = resolve(root, "c", "t1", now=1005.0 + 7200, ttl=3600)
    assert res["winner"] is None and not res["live"], res
    assert check_claim(root, "c", "t1", "alice") is None
    # Archive + clear.
    assert archive_round(root, "c", "t1") is not None
    assert read_bids(root, "c", "t1") == []
    record_bid(root, "c", "t2", "zed", 0.1, ts=1.0)
    assert clear_bids(root, "c", "t2") == 1
    assert read_bids(root, "c", "t2") == []
    # Bad input rejected.
    for bad in (-0.1, 1.5, "x"):
        try:
            record_bid(root, "c", "t3", "a", bad)
        except BidError:
            pass
        else:
            raise AssertionError(f"score {bad!r} accepted")
    print("fleet_bids selftest: OK")


if __name__ == "__main__":
    selftest()
