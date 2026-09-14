"""CLI regressions for parseable state JSON with default audit writes."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import chat


class StateJsonOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        chat.cmd_init(
            self.root,
            SimpleNamespace(ephemeral=None, 
                channel="review",
                members="alice,bob",
                topic="JSON output contract",
            ),
        )
        self.channel = self.root / "review"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run_json_compaction(self, command):
        messages_before = len(chat.message_files(self.channel))
        output = io.StringIO()

        with redirect_stdout(output):
            chat.main(
                [
                    "--root",
                    str(self.root),
                    command,
                    "review",
                    *(["--write"] if command == "state" else []),
                    "--as",
                    "alice",
                    "--json",
                ]
            )

        payload = json.loads(output.getvalue())
        messages = chat.message_files(self.channel)
        self.assertEqual(payload["channel"], "review")
        self.assertEqual(len(messages), messages_before + 1)
        audit = chat.parse_frontmatter(messages[-1])
        self.assertEqual(audit["from"], "alice")
        self.assertEqual(audit["status"], "state.compacted")
        self.assertTrue((self.channel / "state.md").is_file())

    def test_state_write_json_is_parseable_and_preserves_default_audit(self):
        self._run_json_compaction("state")

    def test_compact_json_is_parseable_and_preserves_default_audit(self):
        self._run_json_compaction("compact")


if __name__ == "__main__":
    unittest.main()
