#!/usr/bin/env python3
"""fleet_stigmergy.py -- virtual stigmergy + emergent task specialization.

Paper #8: De Nicola et al. 2019, "Multi-agent systems with virtual
stigmergy". The channel folder IS the stigmergic medium: agents deposit
pheromone traces (signals, task claims, help-wanted cries) into
<channel>/.traces/ and coordinate by *reading the field*, not by
messaging each other. Each trace carries a TTL; its influence decays
linearly to zero, so stale information evaporates on its own and the
medium never needs a leader to clean it up.

Paper #9: Ferrante et al. 2015, "Evolution of Self-Organized Task
Specialization in Robot Swarms". Agents watch the claim traces, estimate
which task-types are under-served, and specialize into the thinnest roles
via local response thresholds. suggest_role() is the response-
threshold readout: it tells an agent which role the field *currently
needs*, and the fleet self-balances because agents follow the suggestion --
never because anyone assigns it. That is the whole point of the paper:
specialization must EMERGE from local readings, not be enforced top-down.

EMERGENCE vs ENFORCEMENT (design contract):
  * suggest_role() is ADVISORY ONLY. It reads state, returns a suggestion,
    writes nothing, runs nothing, orders nothing.
  * No function in this module ever assigns a task, changes an agent's
    behavior, or writes an "order" trace. If you want enforcement, build a
    different module -- enforcement in this one would be a lie about the
    papers it cites.
  * Task-type taxonomy is EMERGENT, not an enum (paper #7's warning against
    over-specified schemas): types are free-form strings agents choose
    when they call record_claim(). Convergent vocabularies arise because
    agents read each other's traces, the same way they arise in the
    papers' swarms.

Trace file layout (one JSON file per trace):
    <channel>/.traces/<kind>-<ts>-<agent-slug>-<rand4>.json
    {"kind": ..., "agent": ..., "strength": ..., "ttl_s": ...,
     "ts": <epoch float>, "note": ...,
     "task_type": ... (task-claim/help-wanted only),
     "task_id": ...   (task-claim only),
     "target_seq": ... (react only)}

The .traces dir starts with a dot, like .cursors/: the fork's cmd_channels
scandir filter (chat.py:453) and fleet_ephemeral's gc() both skip dot
entries, so traces never show up as channels and channel reaping never
touches them separately (the whole channel dir, traces included, is
archived and deleted as one unit).

Composition points (for the merge coordinator -- this module changes
nothing else):
  * fleet_tasks.py exists and has NO stigmergy hook: it logs claims to
    <channel>/tasks/_events.jsonl. Wire it by calling
    fleet_stigmergy.record_claim(root, channel, agent=owner,
    task_type=<free-form>, task_id=task_id) right after claim_task()
    returns True. The task_type string is up to the caller (agent's own
    vocabulary, or derived from the task title) -- do NOT close it into
    an enum.
  * fleet_ephemeral.py exists and reaps whole channels, not traces.
    reapable() lists past-TTL traces and sweep() deletes them; wire sweep
    into a periodic per-channel gc pass or a chat gc-traces command.

Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

TRACES_DIR = ".traces"

KINDS = ("signal", "task-claim", "help-wanted")

DEFAULT_TTLS = {
    "signal": 3600,           # 1h: a bare pheromone puff evaporates fast
    "task-claim": 7 * 86400,  # 7d: specialization memory must span sessions
    "help-wanted": 86400,     # 24h: a cry for help is answered or it dies
}

# An agent "dominates" a task-type when it holds more than half of the
# live claim strength for it. Dominated types are deprioritized in
# suggestions: the point is to fill thin roles, not to pile an agent
# onto a role it already saturates.
DOMINANCE_CUTOFF = 0.5


class StigmergyError(Exception):
    """Raised for invalid names, kinds, strengths, and missing channels."""


def _check_safe_name(name: str, kind: str):
    """Prevent path traversal. Semantics mirror chat.py::_check_safe_name
    (chat.py:244) so channel/agent names are safe under the same rules."""
    if not name or "/" in name or "\\" in name or ":" in name or name in (".", ".."):
        raise StigmergyError(f"invalid {kind} name (path traversal blocked): '{name}'")
    if name.startswith(".") or name.startswith("_"):
        raise StigmergyError(f"invalid {kind} name (reserved prefix blocked): '{name}'")


def _slugify(text: str, maxlen: int = 40) -> str:
    """Mirror of chat.py::slugify (chat.py:53): safe for filenames."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "x"


def _now() -> float:
    return time.time()


def _now_iso() -> str:
    # Local time WITH offset, same convention as chat.py::now_iso (chat.py:47).
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _traces_dir(root: str | Path, channel: str, create: bool = False) -> Path:
    _check_safe_name(channel, "channel")
    chan = Path(root) / channel
    if not chan.is_dir():
        raise StigmergyError(f"channel '{channel}' does not exist under {root}")
    d = chan / TRACES_DIR
    if create:
        d.mkdir(exist_ok=True)
    return d


def _check_strength(strength: float) -> float:
    if isinstance(strength, bool) or not isinstance(strength, (int, float)):
        raise StigmergyError("strength must be a number")
    if strength < 0:
        raise StigmergyError("strength must be >= 0")
    return float(strength)


def _check_ttl(ttl_s: float | None, kind: str) -> float:
    if ttl_s is None:
        return float(DEFAULT_TTLS[kind])
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float)):
        raise StigmergyError("ttl_s must be a number")
    if ttl_s <= 0:
        raise StigmergyError("ttl_s must be > 0")
    return float(ttl_s)


# ---------------------------------------------------------------------------
# Writing traces
# ---------------------------------------------------------------------------


def deposit(
    root: str | Path,
    channel: str,
    kind: str,
    agent: str,
    strength: float = 1.0,
    ttl_s: float | None = None,
    note: str = "",
    extra: dict | None = None,
) -> Path:
    """Deposit one pheromone trace. Returns the trace file path.

    kind must be one of KINDS. ttl_s defaults per DEFAULT_TTLS. extra is
    merged into the trace payload (used for task_type / target_seq /
    task_id by the helpers below).
    """
    if kind not in KINDS:
        raise StigmergyError(f"kind must be one of {KINDS}, got '{kind}'")
    agent = (agent or "").strip()
    _check_safe_name(agent, "agent")
    strength = _check_strength(strength)
    ttl_s = _check_ttl(ttl_s, kind)
    tdir = _traces_dir(root, channel, create=True)
    ts = _now()
    trace = {
        "kind": kind,
        "agent": agent,
        "strength": strength,
        "ttl_s": ttl_s,
        "ts": ts,
        "ts_iso": _now_iso(),
        "note": (note or "")[:500],
    }
    if extra:
        trace.update(extra)
    name = f"{kind}-{int(ts)}-{_slugify(agent)}-{secrets.token_hex(2)}.json"
    # token_hex(2) gives 4 hex chars; unique names need no lock.
    path = tdir / name
    tmp = path.with_name(f".{name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(trace, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX: readers never see a torn trace
    return path


def react(
    root: str | Path,
    channel: str,
    agent: str,
    target_seq: int,
    kind: str = "signal",
    strength: float = 1.0,
    ttl_s: float | None = None,
    note: str = "",
) -> Path:
    """Lightweight signal primitive: a pheromone trace pointing at a message.

    target_seq is the message's sequence number (the NNNN in
    NNNN-<from>-<slug>.md). The reaction label ("+1", "ack", "eyes", ...)
    goes in note; kind stays one of KINDS (default 'signal'). TTL defaults
    to 24h so reactions to messages outlive bare signal puffs.
    """
    if isinstance(target_seq, bool) or not isinstance(target_seq, int):
        raise StigmergyError("target_seq must be an integer message sequence number")
    if target_seq < 0:
        raise StigmergyError("target_seq must be >= 0")
    if ttl_s is None and kind == "signal":
        ttl_s = 86400  # reactions point at messages; give them a day
    return deposit(
        root, channel, kind, agent, strength, ttl_s, note,
        extra={"target_seq": target_seq},
    )


def record_claim(
    root: str | Path,
    channel: str,
    agent: str,
    task_type: str,
    task_id: str | None = None,
    note: str = "",
    strength: float = 1.0,
    ttl_s: float | None = None,
) -> Path:
    """Write a 'task-claim' trace for a claimed task.

    THE composition hook for fleet_tasks.py: call this right after
    fleet_tasks.claim_task() returns True, e.g.::

        if fleet_tasks.claim_task(root, channel, task_id, owner):
            fleet_stigmergy.record_claim(root, channel, owner,
                                         task_type=<free-form type>,
                                         task_id=task_id)

    task_type is a FREE-FORM string (the agent's own vocabulary, or derived
    from the task title) -- never a closed enum. Convergent vocabularies
    emerge because agents read each other's traces.
    """
    task_type = (task_type or "").strip()
    if not task_type:
        raise StigmergyError("task_type must be a non-empty free-form string")
    extra = {"task_type": task_type[:200]}
    if task_id:
        extra["task_id"] = task_id[:200]
    return deposit(
        root, channel, "task-claim", agent, strength, ttl_s,
        note or f"claimed {task_type}",
        extra=extra,
    )


def help_wanted(
    root: str | Path,
    channel: str,
    agent: str,
    task_type: str,
    note: str = "",
    strength: float = 1.0,
    ttl_s: float | None = None,
) -> Path:
    """Deposit a 'help-wanted' trace: an inverse pheromone (a request for
    work of a task_type, rather than a claim of it). Agents scanning for
    under-served roles treat these as need stimuli alongside the claim
    histogram. Same free-form task_type rule as record_claim()."""
    task_type = (task_type or "").strip()
    if not task_type:
        raise StigmergyError("task_type must be a non-empty free-form string")
    return deposit(
        root, channel, "help-wanted", agent, strength, ttl_s,
        note or f"help wanted: {task_type}",
        extra={"task_type": task_type[:200]},
    )


# ---------------------------------------------------------------------------
# Reading traces: decay and the visible field
# ---------------------------------------------------------------------------


def effective_strength(trace: dict, now: float | None = None) -> float:
    """Linear pheromone decay: strength * max(0, 1 - age/ttl).

    A trace at birth has full strength; it fades linearly and is exactly
    zero (invisible) once its TTL has elapsed. Linear, not exponential, so
    a trace's remaining influence is directly proportional to its remaining
    lifetime -- agents can read "how much longer this matters" straight
    off the strength.
    """
    now = _now() if now is None else now
    strength = trace.get("strength", 0.0)
    ttl = trace.get("ttl_s", 0.0)
    ts = trace.get("ts", 0.0)
    try:
        strength, ttl, ts = float(strength), float(ttl), float(ts)
    except (TypeError, ValueError):
        return 0.0
    if ttl <= 0 or strength <= 0:
        return 0.0
    age = now - ts
    if age >= ttl:
        return 0.0
    if age <= 0:
        return strength
    return strength * (1.0 - age / ttl)


def _read_trace_file(path: Path) -> dict | None:
    """Parse one trace file; None if corrupt (never let one bad file kill
    the read of the whole field)."""
    try:
        trace = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(trace, dict):
        return None
    if trace.get("kind") not in KINDS:
        return None
    return trace


def read_traces(
    root: str | Path,
    channel: str,
    kind: str | None = None,
    agent: str | None = None,
    visible_only: bool = True,
    now: float | None = None,
) -> list[dict]:
    """Read the pheromone field of a channel.

    Each returned dict is the trace payload plus:
      _eff: effective (decayed) strength right now
      _path: the trace file name
      _expired: True if past TTL (only present when visible_only=False)

    Traces past their TTL are invisible by default -- they have evaporated
    from the medium and must not influence readings. Pass
    visible_only=False to see the corpses (e.g. before a sweep).
    Sorted by descending effective strength, then oldest first.
    """
    if kind is not None and kind not in KINDS:
        raise StigmergyError(f"kind must be one of {KINDS}, got '{kind}'")
    now = _now() if now is None else now
    tdir = _traces_dir(root, channel)
    out: list[dict] = []
    if not tdir.is_dir():
        return out
    try:
        names = sorted(os.listdir(tdir))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        trace = _read_trace_file(tdir / name)
        if trace is None:
            continue
        if kind is not None and trace.get("kind") != kind:
            continue
        if agent is not None and trace.get("agent") != agent:
            continue
        eff = effective_strength(trace, now)
        expired = eff <= 0.0
        if visible_only and expired:
            continue
        trace = dict(trace)
        trace["_eff"] = eff
        trace["_path"] = name
        if not visible_only:
            trace["_expired"] = expired
        out.append(trace)
    out.sort(key=lambda t: (-t["_eff"], t.get("ts", 0.0), t["_path"]))
    return out


def reapable(root: str | Path, channel: str, now: float | None = None) -> list[Path]:
    """List trace files past their TTL -- the corpses the gc reaper should
    collect. Composition point for fleet_ephemeral.py-style gc: expired
    traces are already invisible to read_traces(); this just enumerates
    what sweep() would delete. Never raises for a missing .traces dir."""
    now = _now() if now is None else now
    tdir = _traces_dir(root, channel)
    dead: list[Path] = []
    if not tdir.is_dir():
        return dead
    for trace in read_traces(root, channel, visible_only=False, now=now):
        if trace.get("_expired"):
            dead.append(tdir / trace["_path"])
    return sorted(dead)


def sweep(root: str | Path, channel: str, now: float | None = None) -> list[str]:
    """Delete expired traces. Returns the deleted file names. Best-effort:
    a file that vanishes or refuses deletion is skipped, never fatal."""
    deleted: list[str] = []
    for path in reapable(root, channel, now=now):
        try:
            path.unlink()
            deleted.append(path.name)
        except OSError:
            pass
    return deleted


# ---------------------------------------------------------------------------
# Paper #9: emergent task specialization via local response thresholds
# ---------------------------------------------------------------------------


def claim_histogram(
    root: str | Path, channel: str, now: float | None = None
) -> dict[str, dict[str, float]]:
    """{task_type: {agent: total_effective_claim_strength}} over the visible
    (not-yet-evaporated) task-claim traces.

    This is the field an agent reads to decide where it is needed. Strength
    is decay-weighted, so recent claims count more than ancient ones: the
    histogram reflects the CURRENT division of labor, not history. Values
    are rounded to 3 decimals; traces without a task_type payload are
    ignored (they are not claims about a type).
    """
    now = _now() if now is None else now
    hist: dict[str, dict[str, float]] = {}
    for trace in read_traces(root, channel, kind="task-claim", now=now):
        ttype = trace.get("task_type")
        agent = trace.get("agent")
        if not isinstance(ttype, str) or not ttype or not agent:
            continue
        agents = hist.setdefault(ttype, {})
        agents[agent] = agents.get(agent, 0.0) + trace["_eff"]
    return {
        ttype: {a: round(s, 3) for a, s in sorted(agents.items())}
        for ttype, agents in sorted(hist.items())
    }


def suggest_role(
    root: str | Path, channel: str, agent: str, now: float | None = None
) -> dict | None:
    """Suggest the task-type this agent should specialize into next.

    ADVISORY ONLY -- this function reads the pheromone field and returns a
    suggestion. It writes nothing, runs nothing, and orders nothing. The
    agent is free to ignore it. The fleet balances BECAUSE agents tend to
    follow such local readings, not because any reading is enforced; that
    is the entire content of Ferrante et al. 2015's result.

    Algorithm (local response thresholds, no leader):
      1. Read claim_histogram(): the current division of labor.
      2. Rank task-types by total live claim strength, ascending: the
         thinnest type is the most under-served (highest "need").
      3. Bias AWAY from types the agent already dominates (>50% of the
         live claim strength): piling onto a saturated role helps nobody.
         If every type is dominated by this agent, fall back to all types
         (a lone agent should still see the thinnest role).
      4. Suggest the thinnest remaining type; tie-break toward the type the
         agent has touched least.

    Returns None when there are no live claim traces (no information to
    specialize on). The returned dict includes the numbers behind the
    suggestion so the agent (or a human reading the log) can audit it.
    """
    _check_safe_name((agent or "").strip(), "agent")
    agent = agent.strip()
    hist = claim_histogram(root, channel, now=now)
    if not hist:
        return None
    totals = {t: round(sum(agents.values()), 3) for t, agents in hist.items()}
    mine = {t: agents.get(agent, 0.0) for t, agents in hist.items()}
    share = {
        t: (mine[t] / totals[t] if totals[t] > 0 else 0.0) for t in hist
    }
    dominated = {t: share[t] > DOMINANCE_CUTOFF for t in hist}
    candidates = [t for t in hist if not dominated[t]] or list(hist)
    # Thinnest first; tie-break toward types the agent has barely touched.
    pick = min(candidates, key=lambda t: (totals[t], mine[t]))
    others = sorted(
        (t for t in hist if t != pick), key=lambda t: (totals[t], mine[t])
    )
    return {
        "suggested_type": pick,
        "advisory": True,
        "reason": (
            f"task-type '{pick}' has the thinnest live claim field "
            f"({totals[pick]} total strength) and you hold only "
            f"{share[pick] * 100:.0f}% of it; taking it on would fill the "
            f"fleet's most under-served role. Advisory only -- no one "
            f"assigns you this; the fleet balances because agents follow "
            f"such local readings, not because they are ordered to."
        ),
        "type_total_strength": totals[pick],
        "agent_strength_in_type": mine[pick],
        "agent_share_of_type": round(share[pick], 3),
        "agent_dominates_type": dominated[pick],
        "other_types_by_need": [
            {
                "task_type": t,
                "total_strength": totals[t],
                "agent_strength": mine[t],
                "agent_share": round(share[t], 3),
                "dominated_by_agent": dominated[t],
            }
            for t in others
        ],
        "emergence_note": (
            "Ferrante et al. 2015: specialization emerges when each agent "
            "applies its own response threshold to the locally-observed "
            "stimulus (here, the claim field). Enforcing this suggestion "
            "would replace emergence with assignment and invalidate the "
            "result this module implements."
        ),
    }


# ---------------------------------------------------------------------------
# Minimal CLI (for manual use / testing; the real CLI lives in chat.py)
# ---------------------------------------------------------------------------


def _default_root(explicit: str | None) -> Path:
    return Path(
        explicit or os.environ.get("AGENT_CHAT_ROOT") or str(Path.home() / ".shingle" / "chat")
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="virtual stigmergy + emergent specialization")
    ap.add_argument("--root", default=None, help="chat root (default: $AGENT_CHAT_ROOT or ~/.shingle/chat)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("deposit")
    p.add_argument("channel")
    p.add_argument("kind", choices=KINDS)
    p.add_argument("--agent", required=True)
    p.add_argument("--strength", type=float, default=1.0)
    p.add_argument("--ttl", type=float, default=None, dest="ttl_s")
    p.add_argument("--note", default="")

    p = sub.add_parser("react")
    p.add_argument("channel")
    p.add_argument("--agent", required=True)
    p.add_argument("--seq", type=int, required=True, help="target message sequence number")
    p.add_argument("--kind", choices=KINDS, default="signal")
    p.add_argument("--note", default="", help="reaction label, e.g. +1 / ack / eyes")
    p.add_argument("--strength", type=float, default=1.0)
    p.add_argument("--ttl", type=float, default=None, dest="ttl_s")

    p = sub.add_parser("claim")
    p.add_argument("channel")
    p.add_argument("--agent", required=True)
    p.add_argument("--type", required=True, dest="task_type", help="free-form task type")
    p.add_argument("--task-id", default=None)
    p.add_argument("--note", default="")

    p = sub.add_parser("help")
    p.add_argument("channel")
    p.add_argument("--agent", required=True)
    p.add_argument("--type", required=True, dest="task_type", help="free-form task type")
    p.add_argument("--note", default="")

    p = sub.add_parser("read")
    p.add_argument("channel")
    p.add_argument("--kind", choices=KINDS, default=None)
    p.add_argument("--agent", default=None)
    p.add_argument("--all", action="store_true", help="include expired traces")

    p = sub.add_parser("hist")
    p.add_argument("channel")

    p = sub.add_parser("suggest")
    p.add_argument("channel")
    p.add_argument("--agent", required=True)

    p = sub.add_parser("reap")
    p.add_argument("channel")

    p = sub.add_parser("sweep")
    p.add_argument("channel")

    a = ap.parse_args(argv)
    root = _default_root(a.root)
    try:
        if a.cmd == "deposit":
            path = deposit(root, a.channel, a.kind, a.agent, a.strength, a.ttl_s, a.note)
            print(json.dumps({"trace": path.name}))
        elif a.cmd == "react":
            path = react(root, a.channel, a.agent, a.seq, a.kind, a.strength, a.ttl_s, a.note)
            print(json.dumps({"trace": path.name, "target_seq": a.seq}))
        elif a.cmd == "claim":
            path = record_claim(root, a.channel, a.agent, a.task_type, a.task_id, a.note)
            print(json.dumps({"trace": path.name, "task_type": a.task_type}))
        elif a.cmd == "help":
            path = help_wanted(root, a.channel, a.agent, a.task_type, a.note)
            print(json.dumps({"trace": path.name, "task_type": a.task_type}))
        elif a.cmd == "read":
            traces = read_traces(root, a.channel, kind=a.kind, agent=a.agent,
                                 visible_only=not a.all)
            for t in traces:
                t.pop("_path", None)
            print(json.dumps(traces, indent=2))
        elif a.cmd == "hist":
            print(json.dumps(claim_histogram(root, a.channel), indent=2))
        elif a.cmd == "suggest":
            s = suggest_role(root, a.channel, a.agent)
            print(json.dumps(s, indent=2))
        elif a.cmd == "reap":
            print(json.dumps({"reapable": [p.name for p in reapable(root, a.channel)]}))
        elif a.cmd == "sweep":
            print(json.dumps({"swept": sweep(root, a.channel)}))
    except StigmergyError as e:
        print(f"fleet_stigmergy: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
