#!/usr/bin/env python3
"""Tests for squawk-feed (zipfs-vault writer). Runnable via pytest or plain.

Uses a fake runner.sh (argv logger) instead of the real zipfs-vault skill,
so no Drive/network is touched. Covers: envelope schema + sender
preference + 500-char truncation, alias format, put/sync call sequence,
cursor advance, startup cursor from vault aliases, failure keeps cursor.
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat
import fleet_identity
import squawk_feed

FAKE_RUNNER = """#!/usr/bin/env bash
set -euo pipefail
echo "CALL $*" >> "$SQUAWK_FEED_TEST_LOG"
if [ "${1:-}" = "put" ] && [ "${SQUAWK_FEED_TEST_FAIL_PUT:-0}" = "1" ]; then
  exit 3
elif [ "${1:-}" = "sync" ] && [ "${SQUAWK_FEED_TEST_FAIL_SYNC:-0}" = "1" ]; then
  exit 3
elif [ "${1:-}" = "list" ]; then
  printf '%s' "$SQUAWK_FEED_TEST_LIST_JSON"
elif [ "${1:-}" = "put" ]; then
  # argv: put --text <json> -a <alias>
  alias="$5"
  safe="$(printf '%s' "$alias" | tr '/' '_')"
  printf '%s' "$3" > "$SQUAWK_FEED_TEST_PUTDIR/${safe}.json"
fi
"""


class SquawkFeedWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.root = self.td / "chat-root"
        self.keys = self.td / "keys"
        self.keys.mkdir()
        fleet_identity.keygen("relay", kd=self.keys)
        fleet_identity.keygen("alice", kd=self.keys)
        chat.cmd_init(
            self.root,
            SimpleNamespace(channel="fleet", members="relay,alice",
                            topic="test", ephemeral=None),
        )
        # fake zipfs-vault skill dir
        self.zipfs = self.td / "zipfs-vault"
        self.zipfs.mkdir()
        runner = self.zipfs / "runner.sh"
        runner.write_text(FAKE_RUNNER)
        runner.chmod(runner.stat().st_mode | stat.S_IEXEC)
        self.log = self.td / "calls.log"
        self.putdir = self.td / "puts"
        self.putdir.mkdir()
        self.env = {
            "SQUAWK_FEED_TEST_LOG": str(self.log),
            "SQUAWK_FEED_TEST_PUTDIR": str(self.putdir),
            "SQUAWK_FEED_TEST_LIST_JSON": "{}",
        }
        self._old_env = dict(os.environ)
        os.environ.update(self.env)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._old_env)

    def tearDown(self):
        self.tmp.cleanup()

    def _post(self, body, sender="relay"):
        return chat._post_message(
            self.root, "fleet", body=body, sender=sender,
            title="test", key_dir=self.keys)

    def _state(self):
        return squawk_feed.FeedState(
            self.root / "fleet", "fleet", "relay", self.keys)

    def _calls(self):
        if not self.log.exists():
            return []
        return self.log.read_text().splitlines()

    def _envelope(self, alias):
        safe = alias.replace("/", "_")
        return json.loads((self.putdir / f"{safe}.json").read_text())

    # -- envelope schema ----------------------------------------------------

    def test_envelope_schema_and_sender_preference(self):
        rec = {"seq": 7, "channel": "fleet", "from": "relay",
               "human": "chris", "body": "hi", "ts": "2026-09-14T00:00:00Z",
               "sealed": False}
        env = squawk_feed.build_envelope(rec)
        self.assertEqual(env, {
            "seq": 7, "channel": "fleet", "sender": "chris",
            "text": "hi", "ts": "2026-09-14T00:00:00Z", "sealed": False,
        })
        rec["human"] = None
        env = squawk_feed.build_envelope(rec)
        self.assertEqual(env["sender"], "relay")

    def test_envelope_truncates_at_500_chars(self):
        rec = {"seq": 1, "channel": "fleet", "from": "alice", "human": None,
               "body": "x" * 600, "ts": "t", "sealed": True}
        env = squawk_feed.build_envelope(rec)
        self.assertEqual(len(env["text"]), 500)
        self.assertTrue(env["text"].endswith("…"))
        self.assertTrue(env["sealed"])

    def test_alias_zero_padded(self):
        self.assertEqual(squawk_feed.alias_for(1), "fleet/000001")
        self.assertEqual(squawk_feed.alias_for(123), "fleet/000123")
        self.assertEqual(squawk_feed.alias_for(1234567), "fleet/1234567")

    # -- writer flow --------------------------------------------------------

    def test_process_new_puts_and_syncs(self):
        self._post("hello vault")
        self._post("second message", sender="alice")
        state = self._state()
        cursor = squawk_feed.process_new(
            state, self.zipfs, "rclone", 0)
        self.assertEqual(cursor, 2)

        calls = self._calls()
        puts = [c for c in calls if c.startswith("CALL put ")]
        syncs = [c for c in calls if c == "CALL sync"]
        self.assertEqual(len(puts), 2)
        self.assertIn("-a fleet/000001", puts[0])
        self.assertIn("-a fleet/000002", puts[1])
        self.assertEqual(len(syncs), 1)

        env1 = self._envelope("fleet/000001")
        self.assertEqual(env1["seq"], 1)
        self.assertEqual(env1["sender"], "relay")
        self.assertEqual(env1["text"], "hello vault")
        self.assertEqual(env1["channel"], "fleet")
        env2 = self._envelope("fleet/000002")
        self.assertEqual(env2["sender"], "alice")

        # idempotent: nothing new, no more puts, cursor unchanged
        cursor2 = squawk_feed.process_new(
            state, self.zipfs, "rclone", cursor)
        self.assertEqual(cursor2, 2)
        self.assertEqual(len([c for c in self._calls()
                              if c.startswith("CALL put ")]), 2)

    def test_process_new_failure_keeps_cursor(self):
        self._post("doomed")
        state = self._state()
        os.environ["SQUAWK_FEED_TEST_FAIL_PUT"] = "1"
        with self.assertRaises(squawk_feed.VaultError):
            squawk_feed.process_new(state, self.zipfs, "rclone", 0)
        # no sync happened after the failed put, cursor must be re-driven
        self.assertNotIn("CALL sync", self._calls())
        del os.environ["SQUAWK_FEED_TEST_FAIL_PUT"]
        cursor = squawk_feed.process_new(state, self.zipfs, "rclone", 0)
        self.assertEqual(cursor, 1)

    def test_startup_cursor_from_vault_aliases(self):
        os.environ["SQUAWK_FEED_TEST_LIST_JSON"] = json.dumps({
            "fleet/000007": {}, "fleet/000009": {}, "other/1": {},
            "fleet/notanumber": {},
        })
        self.assertEqual(
            squawk_feed.startup_cursor(self.zipfs, "rclone"), 9)

    def test_pull_first_calls_pull(self):
        self._post("one")
        state = self._state()
        squawk_feed.process_new(
            state, self.zipfs, "rclone", 0, pull_first=True)
        self.assertIn("CALL pull", self._calls())


if __name__ == "__main__":
    unittest.main(verbosity=2)
