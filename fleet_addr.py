"""Direct addressing (--to) for the agent-chat fleet.

Ported from madnh/scratchpad's --to (steal list: cmd/scratchpad/pad.go:322,
internal/pad/pad.go:92,137,140, internal/pad/wake.go:44,85,143), minus their
turn-taking model, which fights swarm chatter. No turns, no ownership.

FRONTMATTER CONTRACT (written by chat.py's `post --to`, read here):
    to: alice,bob          optional; one agent id or comma-separated ids

SEMANTICS
  1. Broadcast is the default. Absent `to` (or the spellings "all", "*", "[]")
     addresses everyone. Swarm chatter is the norm; addressing is opt-in.
  2. `to` is a HINT, not a lock. Anyone can still read any message -- this is
     a chat, not email. Addressing decides only what is worth *waking* for,
     never what is visible. A named agent may ignore a direct note; an unnamed
     agent may answer one.
  3. No turn-taking, no ownership. `to` carries no "you must respond" state and
     never gates who may post next.
  4. Broadcasts wake everyone who didn't write them. `wait --for-me` filters
     out only *other agents'* directed notes; the channel's shared chatter
     still reaches you. Filtering never hides history: `read --all` ignores it.
  5. Your own posts never wake you. (A wait that fired on the post that armed
     it would be a busy loop with extra steps.)

Stdlib only. chat.py owns the CLI; this module owns the meaning of `to`.
"""

# Spellings that mean "everyone" in the raw `to` field.
BROADCAST_WORDS = frozenset(("", "all", "*", "[]"))


def parse_recipients(frontmatter):
    """Normalize the frontmatter `to` field -> [agent ids]. [] == broadcast.

    Accepts a comma-separated string ("alice, bob"), an already-split list,
    or a broadcast spelling. Dedupes, preserves order, exact string match
    (addressing is case-sensitive, like agent ids).
    """
    raw = frontmatter.get("to", "")
    if isinstance(raw, (list, tuple)):
        recips = [str(r).strip() for r in raw]
    else:
        raw = str(raw).strip()
        if raw in BROADCAST_WORDS:
            return []
        recips = [r.strip() for r in raw.strip("[]").split(",")]
    seen, out = set(), []
    for r in recips:
        if r and r not in BROADCAST_WORDS and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def is_broadcast(frontmatter):
    """True when the message addresses everyone (no `to` / broadcast spelling)."""
    return not parse_recipients(frontmatter)


def is_for_me(frontmatter, my_id):
    """Pure addressing predicate: True if broadcast to the channel, or I am named.

    Deliberately ignores `from`: excluding your own posts is a wait-path rule
    (addressed_wait_filter), not an addressing rule -- a message can be both
    addressed to you and written by you (e.g. notes-to-self).
    """
    return is_broadcast(frontmatter) or (my_id in parse_recipients(frontmatter))


def addressed_wait_filter(frontmatter, my_id):
    """The `wait --for-me` gate: addressed to me AND not written by me.

    Own posts are never news to you -- without this check, a background wait
    armed by your own post would wake on the very message that armed it.
    """
    if frontmatter.get("from") == my_id:
        return False
    return is_for_me(frontmatter, my_id)


if __name__ == "__main__":
    # Smoke test (no pytest dependency; run: python3 fleet_addr.py).
    def fm(**kw):
        return kw

    assert parse_recipients(fm()) == []
    assert parse_recipients(fm(to="")) == []
    assert parse_recipients(fm(to="all")) == []
    assert parse_recipients(fm(to="*")) == []
    assert parse_recipients(fm(to="[]")) == []
    assert parse_recipients(fm(to="alice")) == ["alice"]
    assert parse_recipients(fm(to="alice, bob ,alice")) == ["alice", "bob"]
    assert parse_recipients(fm(to="[a,b]")) == ["a", "b"]
    assert parse_recipients(fm(to=["a", "b"])) == ["a", "b"]

    assert is_for_me(fm(), "x")                       # broadcast: everyone
    assert is_for_me(fm(to="x"), "x")
    assert not is_for_me(fm(to="y,z"), "x")
    assert is_for_me(fm(to="x"), "x") and not is_for_me(fm(to="X"), "x")  # exact

    m = fm(**{"from": "x", "to": "x"})
    assert not addressed_wait_filter(m, "x")           # own post: never news
    assert addressed_wait_filter(fm(**{"from": "y", "to": "x"}), "x")
    assert addressed_wait_filter(fm(**{"from": "y"}), "x")       # broadcast wakes
    assert not addressed_wait_filter(fm(**{"from": "y", "to": "z"}), "x")
    print("fleet_addr: all smoke tests passed")
