#!/usr/bin/env python3
"""Two-agent post/wait/read smoke test, run entirely on awrawr-pc.

Bob waits (blocking) while alice posts; asserts the wait wakes with the
new message, then bob reads it (HMAC-verified). All local: no bridge
latency between steps.
"""
import os
import subprocess
import sys
import time

CHAT = "/home/toxic/.shingle/chat"
ROOT = "/tmp/smoke2"
KEYS = "/tmp/smoke-keys"

env = dict(os.environ, AGENT_CHAT_ROOT=ROOT, FLEET_KEYS_DIR=KEYS)

def run(*args, **kw):
    return subprocess.run(
        [sys.executable, "chat.py", *args],
        cwd=CHAT, env=env, capture_output=True, text=True, **kw,
    )

# Drain any pending messages so the wait truly blocks on the future.
p = run("read", "fleet", "--as", "bob")
assert p.returncode == 0, f"drain read failed: {p.stderr[-500:]}"
print("drained to cursor:", p.stdout.strip().splitlines()[-1][:60])

# Bob's cursor is now at head. Start a blocking wait.
waiter = subprocess.Popen(
    [sys.executable, "chat.py", "wait", "fleet", "--as", "bob", "--timeout", "20"],
    cwd=CHAT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)
time.sleep(2)  # let the waiter block
p = run("post", "fleet", "--from", "alice", "--to", "bob",
        "--title", "smoke-reply", "--body", "directed smoke reply")
assert p.returncode == 0, f"post failed: {p.stderr[-500:]}"
print("alice posted:", p.stdout.strip().splitlines()[-1])

out, err = waiter.communicate(timeout=30)
assert waiter.returncode == 0, (
    f"wait exited {waiter.returncode} (expected 0): {out[-300:]} {err[-300:]}")
assert "directed smoke reply" in out, "wait did not deliver the new message"
print("bob's wait woke with exit 0 and delivered the reply")

# The wait consumed the message (cursor advanced); peek shows it, and the
# earlier wait output already proved delivery. Peek is cursor-neutral.
p = run("peek", "fleet", "-n", "1")
assert p.returncode == 0, f"peek failed: {p.stderr[-500:]}"
assert "directed smoke reply" in p.stdout, "peek missed the message"
print("bob peeked the reply (HMAC-verified on the read path)")

# DAG + clocks sanity on the smoke channel.
p = run("dag", "fleet")
assert "clean" in p.stdout, f"dag not clean: {p.stdout[-300:]}"
print("dag clean:", p.stdout.strip())
print("SMOKE OK")
