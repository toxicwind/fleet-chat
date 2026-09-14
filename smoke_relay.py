#!/usr/bin/env python3
"""End-to-end CLI smoke: relay-in -> relay-out round trip in a temp root.

1. temp root + temp keys; keygen relay + alice (ephemeral, temp only)
2. init fleet channel
3. relay-in a human message via the real CLI
4. normal post from alice via the real CLI
5. relay-out --since 0 -> JSONL; assert schema, valid sigs, relay metadata
6. tamper relayed_from on disk -> relay-out must report signature invalid
   (proves relay metadata is HMAC-covered)
7. stdin body path: relay-in --text - < pipe
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path("/home/toxic/squawk-relay-5f9a2c")
CHAT = [sys.executable, str(REPO / "chat.py")]


def run(*args, **kw):
    env = dict(os.environ)
    env["FLEET_KEYS_DIR"] = str(KEYS)
    kw.setdefault("env", env)
    r = subprocess.run(CHAT + list(args), capture_output=True, text=True,
                       timeout=60, **kw)
    if r.returncode != 0:
        print("FAILED:", args, "\n", r.stderr[-2000:])
        sys.exit(1)
    return r


def main():
    tmp = Path(tempfile.mkdtemp(prefix="squawk-smoke-"))
    root = tmp / "root"
    keys = tmp / "keys"
    keys.mkdir()
    global KEYS
    KEYS = keys
    run("keygen", "relay")
    run("keygen", "alice")
    run("--root", str(root), "init", "fleet",
        "--members", "relay,alice")

    r = run("--root", str(root), "relay-in", "--channel", "fleet",
            "--from", "chris", "--identity", "relay",
            "--key-dir", str(keys), "--text", "hello squawk, this is the human")
    print("relay-in:", r.stdout.strip())
    assert "human: chris" in r.stdout

    # stdin body path
    r = run("--root", str(root), "relay-in", "--channel", "fleet",
            "--from", "chris", "--identity", "relay",
            "--key-dir", str(keys), "--text", "-",
            input="piped body here\n")
    print("relay-in(stdin):", r.stdout.strip())

    run("--root", str(root), "post", "fleet", "--from", "alice",
        "--body", "hi chris", "--title", "greet")

    r = run("--root", str(root), "relay-out", "fleet",
            "--since", "0", "--identity", "relay",
            "--key-dir", str(keys), "--format", "json")
    lines = [json.loads(line) for line in r.stdout.splitlines() if line.strip()]
    recs = [line for line in lines if "seq" in line]
    cursor = [line for line in lines if "cursor" in line]
    assert len(recs) == 3, recs
    assert cursor and cursor[0]["cursor"] == 3
    m1 = recs[0]
    assert m1["from"] == "relay" and m1["human"] == "chris"
    assert m1["relayed_from"] == "muse-side-chat"
    assert m1["body"] == "hello squawk, this is the human"
    assert m1["signature"] == "valid", m1
    assert m1["sealed"] is False
    assert "piped body here" in recs[1]["body"]
    assert recs[2]["from"] == "alice" and recs[2]["human"] is None
    assert all(m["signature"] == "valid" for m in recs)
    print("relay-out records OK:",
          [(m["seq"], m["from"], m["human"], m["signature"]) for m in recs])

    # cursor behaviour
    r = run("--root", str(root), "relay-out", "fleet",
            "--since", "2", "--identity", "relay",
            "--key-dir", str(keys), "--format", "json")
    lines = [json.loads(line) for line in r.stdout.splitlines() if line.strip()]
    assert [line["seq"] for line in lines if "seq" in line] == [3]
    print("cursor --since 2 OK")

    # tamper: relay metadata is HMAC-covered, must fail verification
    f1 = sorted((root / "fleet").glob("0001-*.md"))[0]
    txt = f1.read_text()
    assert "human: chris" in txt
    f1.write_text(txt.replace("human: chris", "human: mallory"))
    r = run("--root", str(root), "relay-out", "fleet",
            "--since", "0", "--identity", "relay",
            "--key-dir", str(keys), "--format", "json")
    recs = [json.loads(line) for line in r.stdout.splitlines()
            if line.strip() and '"seq"' in line]
    assert recs[0]["signature"] == "invalid", recs[0]
    assert recs[0]["sealed"] is False
    print("tamper detected OK: signature =", recs[0]["signature"])

    print("SMOKE-OK")


if __name__ == "__main__":
    main()
