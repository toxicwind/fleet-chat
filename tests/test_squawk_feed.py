#!/usr/bin/env python3
"""Tests for squawk-feed (bearer-authed fat long-poll). pytest or plain.

Covers: /ping public + content-free; /wait + /subscribe alias both 404
without/invalid bearer and 200 with it; fat response shape (per-message
seq, 50-cap cursor protocol); wake on post; timeout; 500-char truncation;
sealed envelopes unsealed server-side (fail closed when unopenable).
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

_tmp = tempfile.TemporaryDirectory()
_KEYS = Path(_tmp.name) / "keys"
_KEYS.mkdir()
# squawk_seal reads FLEET_KEYS_DIR at import time: set before ANY import.
os.environ["FLEET_KEYS_DIR"] = str(_KEYS)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat  # noqa: E402
import fleet_identity  # noqa: E402
import squawk_feed  # noqa: E402

TOKEN = "test-bearer-token-xyz"

try:
    import squawk_seal
    _nacl_ok = True
    try:
        squawk_seal._nacl()
    except RuntimeError:
        _nacl_ok = False
except ImportError:
    squawk_seal = None
    _nacl_ok = False


def _get(port, path, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=70) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None


class SquawkFeedFatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(_tmp.name) / "chat-root"
        fleet_identity.keygen("relay", kd=_KEYS)
        fleet_identity.keygen("alice", kd=_KEYS)
        if _nacl_ok:
            squawk_seal.keygen("relay", keys_dir=_KEYS)
        chat.cmd_init(
            cls.root,
            SimpleNamespace(channel="fleet", members="relay,alice",
                            topic="test", ephemeral=None),
        )

    def setUp(self):
        self.state_server = None
        self.srv_thread = None

    def tearDown(self):
        if self.state_server is not None:
            self.state_server.feed_state.stop.set()
            self.state_server.shutdown()
            self.state_server.server_close()
        if self.srv_thread is not None:
            self.srv_thread.join(timeout=10)

    def _serve(self, hold=55.0):
        server = squawk_feed.serve(
            root=self.root, channel="fleet", identity="relay",
            key_dir=_KEYS, bind="127.0.0.1", port=0, token=TOKEN, hold=hold)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.state_server = server
        self.srv_thread = t
        return server.server_address[1]

    def _post(self, body, sender="relay"):
        return chat._post_message(
            self.root, "fleet", body=body, sender=sender,
            title="test", key_dir=_KEYS)

    def _high(self, port):
        status, obj = _get(port, "/squawk-feed/ping")
        self.assertEqual(status, 200)
        return obj["seq"]

    # -- /ping: public, content-free ----------------------------------------

    def test_ping_public_content_free(self):
        port = self._serve()
        status, obj = _get(port, "/squawk-feed/ping")  # no auth header
        self.assertEqual(status, 200)
        self.assertEqual(set(obj.keys()), {"seq"})
        before = obj["seq"]
        seq, _fname = self._post("hello")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._high(port) >= seq:
                break
            time.sleep(0.05)
        self.assertGreaterEqual(self._high(port), seq)
        self.assertGreaterEqual(self._high(port), before)

    # -- auth: 404 without / with wrong bearer -------------------------------

    def test_wait_and_subscribe_require_bearer(self):
        port = self._serve()
        for path in ("/squawk-feed/wait?since=0",
                     "/squawk-feed/subscribe?since=0"):
            status, _obj = _get(port, path)
            self.assertEqual(status, 404, path)
            status, _obj = _get(port, path, token="wrong-token")
            self.assertEqual(status, 404, path)
            status, obj = _get(port, path, token=TOKEN)
            self.assertEqual(status, 200, path)
            self.assertIn("seq", obj)
            self.assertIn("messages", obj)

    def test_unknown_path_404(self):
        port = self._serve()
        status, _obj = _get(port, "/nope", token=TOKEN)
        self.assertEqual(status, 404)

    # -- fat response shape ---------------------------------------------------

    def test_fat_response_per_message_seq(self):
        port = self._serve()
        base = self._high(port)
        self._post("one")
        self._post("two", sender="alice")
        self._post("three")
        status, obj = _get(port, f"/squawk-feed/wait?since={base}",
                           token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(obj["seq"], base + 3)
        self.assertEqual(len(obj["messages"]), 3)
        seqs = [m["seq"] for m in obj["messages"]]
        self.assertEqual(seqs, [base + 1, base + 2, base + 3])
        self.assertEqual(obj["messages"][0]["body"], "one")
        self.assertEqual(obj["messages"][1]["from"], "alice")
        self.assertTrue(
            all(m["signature"] == "valid" for m in obj["messages"]))

    def test_text_truncated_at_500(self):
        port = self._serve()
        base = self._high(port)
        self._post("y" * 600)
        _status, obj = _get(port, f"/squawk-feed/wait?since={base}",
                            token=TOKEN)
        text = obj["messages"][-1]["body"]
        self.assertEqual(len(text), 500)
        self.assertTrue(text.endswith("…"))

    # -- wake + timeout --------------------------------------------------------

    def test_wait_wakes_on_post(self):
        port = self._serve()
        base = self._high(port)
        result = {}

        def waiter():
            _status, obj = _get(port, f"/squawk-feed/wait?since={base}",
                                token=TOKEN)
            result.update(obj)

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        time.sleep(0.5)  # let the long-poll park
        seq, _fname = self._post("wake me")
        t.join(timeout=15)
        self.assertFalse(t.is_alive(), "long-poll did not wake on post")
        self.assertEqual(result["seq"], seq)
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["messages"][0]["body"], "wake me")

    def test_wait_timeout_returns_current_empty(self):
        port = self._serve(hold=1.5)
        base = self._high(port)
        start = time.monotonic()
        status, obj = _get(port, f"/squawk-feed/wait?since={base}",
                           token=TOKEN)
        elapsed = time.monotonic() - start
        self.assertEqual(status, 200)
        self.assertEqual(obj, {"seq": base, "messages": []})
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertLess(elapsed, 15.0)

    # -- sealed envelopes -------------------------------------------------------

    def test_sealed_unopenable_fail_closed(self):
        self.assertIsNotNone(squawk_seal, "squawk_seal module required")
        port = self._serve()
        base = self._high(port)
        # envelope for relay, but garbage ciphertext (or no seal key):
        # must NOT be served as content.
        body = squawk_seal.build_envelope("relay", b"\x00" * 64)
        self._post(body)
        _status, obj = _get(port, f"/squawk-feed/wait?since={base}",
                            token=TOKEN)
        rec = obj["messages"][-1]
        self.assertTrue(rec["sealed"])
        self.assertIsNone(rec["body"])
        self.assertEqual(rec["signature"], "valid")

    @unittest.skipUnless(_nacl_ok, "pynacl not available")
    def test_sealed_unsealed_server_side(self):
        port = self._serve()
        base = self._high(port)
        pub = squawk_seal.load_public_key("relay", keys_dir=_KEYS)
        ct = squawk_seal.seal_bytes(pub, b"secret for relay")
        body = squawk_seal.build_envelope("relay", ct)
        self._post(body)
        _status, obj = _get(port, f"/squawk-feed/wait?since={base}",
                            token=TOKEN)
        rec = obj["messages"][-1]
        self.assertTrue(rec["sealed"])
        self.assertEqual(rec["body"], "secret for relay")


if __name__ == "__main__":
    unittest.main(verbosity=2)
