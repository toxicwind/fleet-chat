"""Focused tests for adapter-neutral capability/status events."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import chat


TIMESTAMP = "2026-08-21T12:00:00+00:00"


class CapabilityEventTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        chat.cmd_init(
            self.root,
            SimpleNamespace(ephemeral=None, channel="review", members="alice,bob", topic="Capability"),
        )
        self.channel = self.root / "review"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_capability_handshake_has_versioned_portable_primitives(self):
        event = chat.make_capability_event("alice", "omp", timestamp=TIMESTAMP)
        self.assertEqual(event["schema_version"], 1)
        self.assertEqual(event["event"], "capability")
        self.assertEqual(event["agent"], "alice")
        self.assertEqual(event["harness"], "omp")
        self.assertEqual(
            event["primitives"],
            [
                "messages",
                "cursors",
                "wait",
                "tasks",
                "dependencies",
                "leases",
                "path_locks",
                "state_summary",
            ],
        )
        self.assertNotIn("mcp", event)
        self.assertNotIn("acp", event)
        self.assertEqual(chat.validate_adapter_event(event), event)

    def test_status_event_round_trips_with_deterministic_json(self):
        event = chat.make_status_event(
            "bob", "generic-shell", "ready", detail="waiting", timestamp=TIMESTAMP
        )
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True)
        decoded = json.loads(encoded)
        self.assertEqual(chat.validate_adapter_event(decoded), event)
        self.assertEqual(decoded["status"], "ready")
        self.assertEqual(decoded["detail"], "waiting")

    def test_unknown_fields_and_capabilities_have_stable_errors(self):
        event = chat.make_capability_event("alice", "omp", timestamp=TIMESTAMP)
        unknown = {**event, "extra": True}
        with self.assertRaises(chat.AdapterEventError) as unknown_error:
            chat.validate_adapter_event(unknown)
        self.assertEqual(unknown_error.exception.code, "EVENT_UNKNOWN_FIELD")

        invalid = {**event, "primitives": ["unknown"]}
        with self.assertRaises(chat.AdapterEventError) as primitive_error:
            chat.validate_adapter_event(invalid)
        self.assertEqual(primitive_error.exception.code, "EVENT_UNKNOWN_PRIMITIVE")

        malformed_primitives = {**event, "primitives": [[]]}
        with self.assertRaises(chat.AdapterEventError) as malformed_error:
            chat.validate_adapter_event(malformed_primitives)
        self.assertEqual(malformed_error.exception.code, "EVENT_INVALID_PRIMITIVES")

        malformed_detail = chat.make_status_event(
            "alice", "omp", "ready", detail="ok", timestamp=TIMESTAMP
        )
        malformed_detail["detail"] = "\ud800"
        with self.assertRaises(chat.AdapterEventError) as detail_error:
            chat.validate_adapter_event(malformed_detail)
        self.assertEqual(detail_error.exception.code, "EVENT_INVALID_TEXT")

    def test_malformed_version_type_timestamp_and_status_fail_closed(self):
        base = chat.make_capability_event("alice", "omp", timestamp=TIMESTAMP)
        cases = [
            ({**base, "schema_version": 2}, "EVENT_UNSUPPORTED_VERSION"),
            ({**base, "event": "other"}, "EVENT_INVALID_TYPE"),
            ({**base, "ts": "not-a-timestamp"}, "EVENT_INVALID_TIMESTAMP"),
            ({**chat.make_status_event("alice", "omp", "ready", timestamp=TIMESTAMP), "status": "wat"}, "EVENT_INVALID_STATUS"),
        ]
        for value, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(chat.AdapterEventError) as error:
                    chat.validate_adapter_event(value)
                self.assertEqual(error.exception.code, code)

    def test_event_post_and_read_preserve_ordinary_messages(self):
        chat.main(
            [
                "--root",
                str(self.root),
                "post",
                "review",
                "--from",
                "alice",
                "--title",
                "ordinary",
                "--body",
                "not an event",
            ]
        )
        chat.main(
            [
                "--root",
                str(self.root),
                "event",
                "post",
                "review",
                "--from",
                "alice",
                "--type",
                "capability",
                "--harness",
                "omp",
            ]
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            chat.main(
                [
                    "--root",
                    str(self.root),
                    "event",
                    "read",
                    "review",
                    "--type",
                    "capability",
                ]
            )
        rows = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "capability")
        self.assertEqual(rows[0]["agent"], "alice")

    def test_schema_declares_required_fields_and_rejects_unknown_event_type(self):
        schema_path = Path(__file__).parents[1] / "schemas" / "agent-chat-event.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(
            schema["required"],
            ["schema_version", "event", "agent", "harness", "ts"],
        )
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(
            set(schema["properties"]["event"]["enum"]), {"capability", "status"}
        )


if __name__ == "__main__":
    unittest.main()
