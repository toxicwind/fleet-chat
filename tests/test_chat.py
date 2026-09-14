"""Regression tests for the stdlib-only agent-chat CLI."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import chat
import fleet_identity


class _FakeStdin(io.StringIO):
    def __init__(self, value, is_tty):
        super().__init__(value)
        self._is_tty = is_tty

    def isatty(self):
        return self._is_tty


class ChatRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _channel(self, name):
        channel = self.root / name
        channel.mkdir()
        (channel / "_meta.json").write_text(
            json.dumps({"channel": name, "members": [], "topic": ""}),
            encoding="utf-8",
        )
        return channel

    def _post_args(self, **overrides):
        values = {
            "channel": "general",
            "sender": "alice",
            "to": "bob",
            "title": "Status update",
            "reply": None,
            "status": "discussion",
            "body": "Message body",
            "body_file": None,
        }
        values.update(overrides)
        return SimpleNamespace(ephemeral=None, **values)

    def test_post_title_newlines_cannot_forge_frontmatter_fields(self):
        """A newline in a title must remain title content, never new metadata."""
        channel = self._channel("general")
        args = self._post_args(title="Status\nfrom: mallory\nto: eve")

        with patch.object(chat, "now_iso", return_value="2026-08-09T00:00:00+00:00"):
            with contextlib.redirect_stdout(io.StringIO()):
                chat.cmd_post(self.root, args)

        message = next(channel.glob("*.md"))
        meta = chat.parse_frontmatter(message)
        self.assertEqual(meta["from"], "alice")
        self.assertEqual(meta["to"], "bob")
        self.assertEqual(meta["title"], "Status from: mallory to: eve")

    def test_max_seq_uses_highest_valid_sequence_with_gaps_and_malformed_files(self):
        """Only numbered message filenames affect the maximum sequence."""
        channel = self._channel("general")
        for name in (
            "0002-alice-first.md",
            "0010-bob-last.md",
            "broken.md",
            "12x-nope.md",
            "²-peer.md",
        ):
            (channel / name).write_text("body", encoding="utf-8")

        self.assertEqual(chat.max_seq(channel), 10)

    def test_channels_reports_counts_and_last_message_for_empty_and_gapped_channels(
        self,
    ):
        """Channel summaries count valid messages and select the highest sequence."""
        alpha = self._channel("alpha")
        self._channel("beta")
        (alpha / "0002-alice-first.md").write_text(
            "---\nfrom: alice\ntitle: First\n---\nbody\n", encoding="utf-8"
        )
        (alpha / "0010-bob-last.md").write_text(
            "---\nfrom: bob\ntitle: Latest update\n---\nbody\n", encoding="utf-8"
        )
        (alpha / "not-a-message.md").write_text("ignored", encoding="utf-8")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            chat.cmd_channels(self.root, SimpleNamespace(ephemeral=None, ))

        rendered = output.getvalue()
        self.assertIn("alpha", rendered)
        self.assertIn("   2", rendered)
        self.assertIn("last: #10 bob: Latest update", rendered)
        self.assertIn("beta", rendered)
        self.assertIn("last: -", rendered)

    def test_channels_marks_truncated_titles_with_ellipsis(self):
        """Long channel titles retain an ASCII marker after truncation."""
        channel = self._channel("general")
        (channel / "0001-bob-long-title.md").write_text(
            "---\nseq: 1\nfrom: bob\ntitle: " + ("A" * 41) + "\n---\nbody\n",
            encoding="utf-8",
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            chat.cmd_channels(self.root, SimpleNamespace(ephemeral=None, ))

        self.assertIn("last: #1 bob: " + ("A" * 37) + "...", output.getvalue())

    def test_channels_keeps_titles_at_the_display_limit(self):
        """Titles at the 40-character limit are not shortened."""
        channel = self._channel("general")
        title = "B" * 40
        (channel / "0001-bob-boundary.md").write_text(
            f"---\nseq: 1\nfrom: bob\ntitle: {title}\n---\nbody\n",
            encoding="utf-8",
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            chat.cmd_channels(self.root, SimpleNamespace(ephemeral=None, ))

        self.assertIn(f"last: #1 bob: {title}", output.getvalue())

    def test_init_formats_member_list_for_cli_output(self):
        """Init output uses a readable member list, not Python repr syntax."""
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            chat.cmd_init(
                self.root,
                SimpleNamespace(ephemeral=None, channel="general", members="alice,bob", topic=None),
            )

        rendered = output.getvalue()
        self.assertIn("members=alice, bob", rendered)
        self.assertNotIn("members=['alice', 'bob']", rendered)

    def test_main_maps_missing_body_file_to_application_error(self):
        """A missing body file returns the CLI error contract, not a traceback."""
        channel = self._channel("general")
        missing = self.root / "missing.md"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                chat.main(
                    [
                        "--root",
                        str(self.root),
                        "post",
                        "general",
                        "--from",
                        "alice",
                        "--to",
                        "bob",
                        "--title",
                        "Missing",
                        "--body-file",
                        str(missing),
                    ]
                )

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("could not read body file", stderr.getvalue())
        self.assertEqual(list(channel.glob("*.md")), [])

    def test_main_maps_keyboard_interrupt_to_cancelled_exit(self):
        """Ctrl-C is reported as a clean cancellation with exit code 130."""
        stderr = io.StringIO()
        with patch.object(chat, "cmd_init", side_effect=KeyboardInterrupt):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    chat.main(["--root", str(self.root), "init", "cancelled"])

        self.assertEqual(raised.exception.code, 130)
        self.assertIn("cancelled by user", stderr.getvalue())

    def test_init_rejects_reserved_channel_prefixes(self):
        """User channels cannot collide with dotfiles or internal directories."""
        for channel in ("_internal", ".hidden"):
            with self.subTest(channel=channel), self.assertRaises(chat.AgentChatError):
                chat.cmd_init(
                    self.root,
                    SimpleNamespace(ephemeral=None, channel=channel, members=None, topic=None),
                )
            self.assertFalse((self.root / channel).exists())

    def _write_signed(self, channel, filename, seq, sender, to, title, body="body"):
        """Hand-write a message file with a valid v1 HMAC.

        Lets tests control on-disk layout (e.g. seq-out-of-order filenames)
        while satisfying verify_on_read's fail-closed signature check.
        """
        ts = "2026-09-14T00:00:00+00:00"
        canon = fleet_identity.canonical_message(
            seq=seq, sender=sender, to=to, reply_to="", channel="general",
            ts=ts, status="discussion", title=title, body=body,
        )
        hmac = fleet_identity.sign(sender, canon)
        (channel / filename).write_text(
            f"---\nseq: {seq}\nfrom: {sender}\nto: {to}\nchannel: general\n"
            f"ts: {ts}\nstatus: discussion\ntitle: {title}\n"
            f"hmac: {hmac}\n---\n{body}\n",
            encoding="utf-8",
        )

    def test_read_preserves_sequence_order_and_advances_cursor(self):
        """Unread messages are rendered in sequence order and advance the cursor."""
        channel = self._channel("general")
        # Written out of order on disk; the read path must still render by
        # seq (parsed from the filename) and verify each signature.
        self._write_signed(channel, "0002-bob-second.md", 2, "bob", "alice", "Second")
        self._write_signed(channel, "0001-bob-first.md", 1, "bob", "alice", "First")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            chat.cmd_read(
                self.root,
                SimpleNamespace(ephemeral=None, 
                    channel="general", agent="alice", all=False, peek=False
                ),
            )

        rendered = output.getvalue()
        self.assertLess(rendered.index("title: First"), rendered.index("title: Second"))
        self.assertEqual((channel / ".cursors" / "alice.txt").read_text(), "2")

    def test_peek_zero_messages_is_empty(self):
        """A zero-size peek must not index an empty heap."""
        channel = self._channel("general")
        (channel / "0001-bob-message.md").write_text(
            "---\nseq: 1\nfrom: bob\ntitle: Message\n---\nbody\n",
            encoding="utf-8",
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            chat.cmd_peek(
                self.root,
                SimpleNamespace(ephemeral=None, channel="general", n=0),
            )

        self.assertEqual(output.getvalue(), "")

    def test_claim_rejects_internal_channel_files(self):
        """Claim must not rename channel metadata or cursor files."""
        channel = self._channel("general")
        args = SimpleNamespace(ephemeral=None, channel="general", task="_meta.json", agent="mallory")

        with self.assertRaises(chat.AgentChatError):
            chat.cmd_claim(self.root, args)

        self.assertTrue((channel / "_meta.json").exists())

    def test_claim_rejects_non_task_files(self):
        """Claim accepts only the documented task marker filename shape."""
        channel = self._channel("general")
        for name in ("README.md", "0001-bob-message.md", "task-.md"):
            path = channel / name
            path.write_text("original", encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(chat.AgentChatError):
                chat.cmd_claim(
                    self.root,
                    SimpleNamespace(ephemeral=None, channel="general", task=name, agent="mallory"),
                )
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="utf-8"), "original")

    def test_claim_renames_task_marker_without_overwriting_existing_claim(self):
        """A destination collision is a lost claim, never an overwrite."""
        channel = self._channel("general")
        source = channel / "task-12.md"
        claimed = channel / "task-12.CLAIMED-mallory.md"
        source.write_text("task", encoding="utf-8")
        claimed.write_text("other agent", encoding="utf-8")

        with self.assertRaises(SystemExit) as raised:
            chat.cmd_claim(
                self.root,
                SimpleNamespace(ephemeral=None, channel="general", task=source.name, agent="mallory"),
            )

        self.assertEqual(raised.exception.code, 3)
        self.assertEqual(source.read_text(encoding="utf-8"), "task")
        self.assertEqual(claimed.read_text(encoding="utf-8"), "other agent")

    def test_claim_renames_valid_task_marker(self):
        """A valid marker is renamed to the agent-specific claimed name."""
        channel = self._channel("general")
        source = channel / "task-12.md"
        source.write_text("task", encoding="utf-8")

        chat.cmd_claim(
            self.root,
            SimpleNamespace(ephemeral=None, channel="general", task=source.name, agent="alice"),
        )

        self.assertFalse(source.exists())
        self.assertEqual(
            (channel / "task-12.CLAIMED-alice.md").read_text(encoding="utf-8"),
            "task",
        )

    def test_post_prompts_for_tty_stdin_but_not_piped_stdin(self):
        """Interactive body entry gets guidance; a pipeline stays quiet."""
        channel = self._channel("general")
        tty_stderr = io.StringIO()
        with patch.object(chat.sys, "stdin", _FakeStdin("typed body", True)):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                tty_stderr
            ):
                chat.cmd_post(self.root, self._post_args(body=None))
        self.assertIn("Enter message body", tty_stderr.getvalue())

        pipe_stderr = io.StringIO()
        with patch.object(chat.sys, "stdin", _FakeStdin("piped body", False)):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                pipe_stderr
            ):
                chat.cmd_post(self.root, self._post_args(body=None, title="Piped"))
        self.assertEqual(pipe_stderr.getvalue(), "")
        self.assertIn(
            "piped body", (channel / "0002-alice-piped.md").read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
