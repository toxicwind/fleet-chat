"""fleet_presence.py -- Liveness + gossip-targeting for the fleet chat fork.

Paper steals:
  (a) Jelasity, Voulgaris, Guerraoui, Kermarrec, van Steen (2007),
      "Gossip-based peer sampling" (ACM TOCS): each agent keeps a small
      random *peer view* in its own state file and gossips views; there is
      NO central registry for who is around.
  (b) Das, Gupta, Motivala (2002), "SWIM: Scalable Weakly-consistent
      Infection-style Process Group Membership Protocol": gossip-style
      failure detection with alive / suspect / dead marks, and NO
      watchdog process -- everything is evaluated on the read path.

TRUST vs LIVENESS SPLIT -- READ BEFORE TOUCHING AUTHORIZATION
-------------------------------------------------------------
fleet_roster.py is the TRUST registry: identity, who may speak, revocation.
This module is LIVENESS / GOSSIP-TARGETING ONLY: who *seems* alive and who
to gossip with. Heartbeat files, peer views, and suspect marks are *rumor*,
not credentials. A fresh heartbeat from an unregistered agent means
"there is traffic on the channel", never "it is allowed to speak".
NEVER consult peer views, heartbeat files, or suspect files for
authorization decisions. Authorization lives in the roster; nothing here
touches it, overrides it, or even reads it.

NO DAEMON -- POLLING IMPLICATION (stated honestly)
--------------------------------------------------
There is no background process. Every function below is evaluated on the
read path: a heartbeat is only as fresh as the last agent turn that wrote
it, and staleness is only *observed* when some peer calls
``is_alive`` / ``alive_agents``. Consequences the coordinator must know:
  * An agent that crashes between heartbeats stays "alive" in the eyes of
    peers until T_SUSPECT seconds have elapsed since its last write. There
    is no real-time notification.
  * An agent that is merely idle (no turns, no polls) looks exactly like a
    crashed agent. This module cannot distinguish "dead" from "quiet":
    ``dead`` means "no heartbeat in T_DEAD seconds", not "process exited".
  * The recommended pattern is: the chat layer calls ``heartbeat()`` at the
    top of every agent turn (cheap: one small atomic file write), and
    peers call ``alive_agents()`` when they need to pick gossip targets.
  * Suspect marks are best-effort hints, not verdicts: a fresh heartbeat
    always refutes a suspect mark (SWIM's indirect probing, file version).

File layout under <root>:
    .heartbeats/<agent>.json   {"ts": float, "incarnation": int}
    .peers/<agent>.json        {"view": [peer-ids], "ts": float}
    .suspects/<peer>.json      {"by": agent, "ts": float, "reason": str}

Constants: VIEW_K=8, T_SUSPECT=60s, T_DEAD=300s, SUSPECT_TTL=300s.

Determinism / seeding: ``update_view`` takes an optional ``rng`` argument
(default: a fresh ``random.Random()`` instance). Passing
``random.Random(seed)`` makes the sample deterministic for tests. This
module NEVER seeds the global random state -- no global seeding side
effects, so importing it cannot perturb other modules' randomness.

Stdlib only. All writes are atomic (tmp file + os.replace) and race-safe
for the single-writer-per-file case (each agent writes only its own files).
"""

import json
import os
import random
import re
import time

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

VIEW_K = 8          # max peers kept in a view (Jelasity's view size)
T_SUSPECT = 60.0    # heartbeat older than this -> suspect (SWIM suspicion)
T_DEAD = 300.0      # heartbeat older than this -> dead (SWIM: declare dead)
SUSPECT_TTL = 300.0  # suspect marks older than this are stale gossip, ignore

HEARTBEATS_DIR = ".heartbeats"
PEERS_DIR = ".peers"
SUSPECTS_DIR = ".suspects"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _check_safe_name(name, what="agent"):
    """Refuse names that could escape the state dirs (.. / / . prefixes)."""
    if not isinstance(name, str) or not _SAFE_NAME.match(name) \
            or ".." in name:
        raise ValueError("unsafe %s name: %r" % (what, name))
    return name


def _agent_file(root, subdir, agent):
    _check_safe_name(agent)
    d = os.path.join(root, subdir)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, agent + ".json")


def _atomic_write_json(path, payload):
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as fh:
        json.dump(payload, fh, sort_keys=True)
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _now():
    return time.time()


# --------------------------------------------------------------------------
# (1) Heartbeats
# --------------------------------------------------------------------------

def heartbeat(root, agent):
    """Write (or refresh) this agent's heartbeat.

    ``incarnation`` bumps by one on every call where the previous file
    survives, so a restart that preserves .heartbeats/ is distinguishable
    from a single long-lived process: peers (and the coordinator) can tell
    "agent restarted 3 times" from "agent alive 3 days". If no prior file
    exists the incarnation starts at 0.

    Returns the heartbeat dict that was written.
    """
    _check_safe_name(agent)
    path = _agent_file(root, HEARTBEATS_DIR, agent)
    prev = _read_json(path) or {}
    try:
        incarnation = int(prev.get("incarnation", -1)) + 1
    except (TypeError, ValueError):
        incarnation = 0
    hb = {"ts": _now(), "incarnation": incarnation}
    _atomic_write_json(path, hb)
    return hb


def heartbeat_age(root, agent):
    """Seconds since the agent's last heartbeat; float('inf') if none."""
    hb = _read_json(_agent_file(root, HEARTBEATS_DIR, agent))
    if not hb or not isinstance(hb.get("ts"), (int, float)):
        return float("inf")
    return max(0.0, _now() - hb["ts"])


# --------------------------------------------------------------------------
# (2) Peer views (Jelasity-style gossip peer sampling)
# --------------------------------------------------------------------------

def get_view(root, agent):
    """Return this agent's peer view (list of up to VIEW_K peer ids)."""
    data = _read_json(_agent_file(root, PEERS_DIR, agent)) or {}
    view = data.get("view") or []
    return [p for p in view if isinstance(p, str)][:VIEW_K]


def _write_view(root, agent, view):
    _atomic_write_json(_agent_file(root, PEERS_DIR, agent),
                       {"view": sorted(view), "ts": _now()})


def update_view(root, agent, seen_peer, rng=None):
    """Gossip one exchange (Jelasity's peer-sampling round, file version).

    Read ``seen_peer``'s view file, union it with my own view, keep both
    of us in the pool (an agent never samples itself out of existence),
    then random-sample back down to VIEW_K entries with
    ``random.sample``. ``rng`` is a ``random.Random``-like instance;
    passing ``random.Random(seed)`` makes the exchange deterministic.
    The global random state is never touched.

    Returns the new view. Safe when the peer has no view file yet (their
    contribution is just themselves).
    """
    _check_safe_name(agent)
    _check_safe_name(seen_peer, "seen_peer")
    if rng is None:
        rng = random.Random()

    pool = set(get_view(root, agent))
    their_view = _read_json(_agent_file(root, PEERS_DIR, seen_peer)) or {}
    pool.update(p for p in (their_view.get("view") or [])
                if isinstance(p, str))
    pool.add(seen_peer)
    pool.discard(agent)  # never gossip to myself

    if len(pool) > VIEW_K:
        pool = set(rng.sample(sorted(pool), VIEW_K))
    view = sorted(pool)
    _write_view(root, agent, view)
    return view


def gossip_round(root, agent, rng=None):
    """One Jelasity round against a random peer from my current view.

    Picks a random peer from my view, then ``update_view`` with them.
    Returns (peer, new_view), or (None, view) if my view is empty.
    """
    view = get_view(root, agent)
    if not view:
        return None, view
    if rng is None:
        rng = random.Random()
    peer = rng.choice(view)
    return peer, update_view(root, agent, peer, rng=rng)


# --------------------------------------------------------------------------
# (3) SWIM failure detection (file-adapted)
# --------------------------------------------------------------------------

def suspect(root, by, peer, reason):
    """Record a suspicion mark against ``peer``, SWIM-style.

    Direct-detection rule: the mark is only written when ``peer``'s
    heartbeat is older than T_SUSPECT. This mirrors SWIM's requirement
    that suspicion follows a *failed direct probe* -- we never suspect on
    hearsay alone.

    Returns True if the mark was written, False if the peer's heartbeat
    is fresh (nothing to suspect) or the peer has never heartbeated
    (nothing to probe -- see ``is_alive`` which calls those "dead").

    The mark is *indirect-probe evidence* for other readers (see
    ``is_alive`` / ``suspect_marks``): SWIM's indirect probing, file
    version -- instead of asking a third party to probe on our behalf,
    we read the third party's recorded suspicion and weigh it against the
    heartbeat file, which is the ultimate refutation (a fresh heartbeat
    always wins over any mark).
    """
    _check_safe_name(by, "by")
    _check_safe_name(peer, "peer")
    age = heartbeat_age(root, peer)
    if age < T_SUSPECT or age == float("inf"):
        return False
    _atomic_write_json(
        _agent_file(root, SUSPECTS_DIR, peer),
        {"by": by, "ts": _now(), "reason": str(reason)})
    return True


def suspect_marks(root, peer):
    """Return the (possibly stale) suspicion mark for ``peer``, or None."""
    mark = _read_json(_agent_file(root, SUSPECTS_DIR, peer))
    if not mark or not isinstance(mark.get("ts"), (int, float)):
        return None
    if _now() - mark["ts"] > SUSPECT_TTL:
        return None  # stale gossip; SWIM would have gossiped it away
    return mark


def is_alive(root, agent):
    """SWIM membership state for ``agent``: "alive" | "suspect" | "dead".

    Read-path evaluation (no daemon):
      * heartbeat newer than T_SUSPECT        -> "alive" (fresh heartbeat
        refutes ANY suspect mark -- this is the indirect-probe
        refutation, file version).
      * heartbeat older than T_DEAD           -> "dead".
      * in between (or no heartbeat at all)   -> "suspect" for a stale
        heartbeat, "dead" if no heartbeat file exists (never seen --
        nothing to be suspicious of, we just have no evidence).

    Suspect marks from ``suspect()`` are *attribution and gossip hints*:
    they tell a peer *who* noticed the staleness and *why*, but the state
    decision above is driven by heartbeat age alone. This keeps one
    malicious or confused agent from driving a peer's state via marks.
    """
    age = heartbeat_age(root, agent)
    if age == float("inf"):
        return "dead"      # never heartbeated: no evidence of life
    if age < T_SUSPECT:
        return "alive"
    if age >= T_DEAD:
        return "dead"
    return "suspect"


def alive_agents(root):
    """Map every heartbeating agent id -> its SWIM state.

    Iterates the heartbeat dir (each agent writes only its own file).
    Returns {agent: "alive"|"suspect"|"dead"}.
    """
    d = os.path.join(root, HEARTBEATS_DIR)
    out = {}
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in sorted(names):
        if not name.endswith(".json"):
            continue
        agent = name[:-5]
        try:
            _check_safe_name(agent)
        except ValueError:
            continue
        out[agent] = is_alive(root, agent)
    return out


def peer_sample(root, agent, count=3, only_alive=True, rng=None):
    """Pick up to ``count`` gossip targets from my view, liveness-filtered.

    Default: only agents currently "alive". This is the intended use of
    views: *who to gossip with*, never *who may speak* (see module
    docstring: authorization is the roster's job).
    """
    view = get_view(root, agent)
    if only_alive:
        view = [p for p in view if is_alive(root, p) == "alive"]
    if rng is None:
        rng = random.Random()
    if len(view) > count:
        view = rng.sample(view, count)
    return sorted(view)
