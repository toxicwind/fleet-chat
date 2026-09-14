"""Focused tests for derived channel state summary and non-destructive compaction."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import chat
from agent_chat.lease_store import LeaseStore
from agent_chat.path_locks import PathLockStore
from agent_chat.state_store import (
    STATE_FILENAME,
    StateError,
    StateStore,
    StateSummary,
    StateValidationError,
    compact_state,
    load_state,
    render_state,
)
from agent_chat.task_model import TaskRecord
from agent_chat.task_store import TaskStore


TIMESTAMP_FIXED = "2026-08-21T12:00:00+00:00"


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        chat.cmd_init(
            self.root,
            SimpleNamespace(ephemeral=None, 
                channel="review",
                members="alice,bob",
                topic="Review vNext architecture",
            ),
        )
        self.channel = self.root / "review"
        self.task_store = TaskStore(self.channel)
        self.lease_store = LeaseStore(self.channel)
        self.lock_store = PathLockStore(self.channel)
        self.state_store = StateStore(self.channel)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _post_msg(
        self,
        sender="alice",
        to="all",
        title="Update",
        body="Body text",
        status=None,
        msg_type=None,
        extra_frontmatter=None,
    ):
        """Helper to post a message with optional custom frontmatter."""
        args = SimpleNamespace(ephemeral=None, 
            channel="review",
            sender=sender,
            to=to,
            title=title,
            reply=None,
            status=status,
            body=body,
            body_file=None,
        )
        chat.cmd_post(self.root, args)
        files = chat.message_files(self.channel)
        latest = files[-1]
        if msg_type or extra_frontmatter:
            text = latest.read_text(encoding="utf-8")
            parts = text.split("---", 2)
            if len(parts) >= 3:
                fm_lines = parts[1].strip().splitlines()
                if msg_type:
                    fm_lines.append(f"type: {msg_type}")
                if extra_frontmatter:
                    for k, v in extra_frontmatter.items():
                        fm_lines.append(f"{k}: {v}")
                new_text = "---\n" + "\n".join(fm_lines) + f"\n---\n{parts[2].lstrip()}"
                latest.write_text(new_text, encoding="utf-8")
        return latest

    # -------------------------------------------------------------------------
    # 1. Deterministic Render Tests
    # -------------------------------------------------------------------------

    def test_deterministic_render_identical_across_repeated_runs(self):
        """Rendering on identical channel state produces byte-identical Markdown output."""
        # Create some tasks
        self.task_store.create(
            TaskRecord.from_dict(
                {
                    "id": "T-0001",
                    "channel": "review",
                    "title": "Design state schema",
                    "status": "open",
                    "owner": None,
                    "created_by": "alice",
                    "depends_on": [],
                    "files_hint": ["agent_chat/state_store.py"],
                    "acceptance": ["Schema defined"],
                    "lease_expires_at": None,
                    "branch": None,
                    "updated_at": TIMESTAMP_FIXED,
                }
            )
        )
        self.task_store.create(
            TaskRecord.from_dict(
                {
                    "id": "T-0002",
                    "channel": "review",
                    "title": "Implement compaction",
                    "status": "blocked",
                    "owner": "bob",
                    "created_by": "bob",
                    "depends_on": ["T-0001"],
                    "files_hint": ["chat.py"],
                    "acceptance": ["Compaction is non-destructive"],
                    "lease_expires_at": None,
                    "branch": None,
                    "updated_at": TIMESTAMP_FIXED,
                }
            )
        )
        # Lock paths
        self.lock_store.lock(
            owner="alice",
            paths=["agent_chat/state_store.py"],
            lease_seconds=300.0,
            now=TIMESTAMP_FIXED,
        )
        # Post a decision and verification message
        self._post_msg(
            sender="alice",
            title="Decision: Atomic replacement for state.md",
            body="We decided on temporary sibling replace.",
            msg_type="decision",
        )
        self._post_msg(
            sender="bob",
            title="Verification: tests pass",
            body="Verified that 100% of test cases pass cleanly.",
            msg_type="verification",
        )

        render1 = self.state_store.render(now=TIMESTAMP_FIXED)
        render2 = self.state_store.render(now=TIMESTAMP_FIXED)
        render3 = self.state_store.render(now=TIMESTAMP_FIXED)

        self.assertEqual(render1, render2)
        self.assertEqual(render2, render3)
        self.assertIsInstance(render1, str)
        self.assertTrue(render1.endswith("\n"))
        # Ensure only Unix newlines
        self.assertNotIn("\r\n", render1)

    def test_deterministic_sorting_across_sections(self):
        """Sections must be stably sorted by ID, seq, or owner."""
        # Create tasks in reverse ID order
        for tid, title in [("T-0003", "Task 3"), ("T-0001", "Task 1"), ("T-0002", "Task 2")]:
            self.task_store.create(
                TaskRecord.from_dict(
                    {
                        "id": tid,
                        "channel": "review",
                        "title": title,
                        "status": "open",
                        "owner": None,
                        "created_by": "alice",
                        "depends_on": [],
                        "files_hint": [],
                        "acceptance": [],
                        "lease_expires_at": None,
                        "branch": None,
                        "updated_at": TIMESTAMP_FIXED,
                    }
                )
            )

        # Post decisions in reverse logical order
        self._post_msg(
            sender="bob",
            title="Decision: Second decision",
            body="Detail 2",
            msg_type="decision",
        )
        self._post_msg(
            sender="alice",
            title="Decision: First decision",
            body="Detail 1",
            msg_type="decision",
        )

        # Lock multiple paths
        self.lock_store.lock(
            owner="bob",
            paths=["src/b.py"],
            lease_seconds=300.0,
            now=TIMESTAMP_FIXED,
        )
        self.lock_store.lock(
            owner="alice",
            paths=["src/a.py"],
            lease_seconds=300.0,
            now=TIMESTAMP_FIXED,
        )

        summary = self.state_store.summarize(now=TIMESTAMP_FIXED)
        # Open tasks must be sorted by ID
        task_ids = [t.id for t in summary.open_tasks]
        self.assertEqual(task_ids, ["T-0001", "T-0002", "T-0003"])

        # Decisions must be sorted by sequence number
        dec_seqs = [d.seq for d in summary.decisions]
        self.assertEqual(dec_seqs, sorted(dec_seqs))

        # Path locks must be sorted deterministically
        lock_ids = [lock.lock_id for lock in summary.path_locks]
        self.assertEqual(lock_ids, sorted(lock_ids))

        # Owners must be sorted alphabetically
        self.assertEqual(list(summary.owners.keys()), sorted(summary.owners.keys()))

    # -------------------------------------------------------------------------
    # 2. Content Preservation and Extraction Tests
    # -------------------------------------------------------------------------

    def test_state_contains_all_required_sections_and_fields(self):
        """Rendered state includes goal/topic, decisions, open tasks, blockers, owners, locks, evidence."""
        # 1. Goal/Topic from _meta.json
        # 2. Open task
        self.task_store.create(
            TaskRecord.from_dict(
                {
                    "id": "T-0001",
                    "channel": "review",
                    "title": "Build prototype",
                    "status": "in_progress",
                    "owner": "alice",
                    "created_by": "alice",
                    "depends_on": [],
                    "files_hint": ["proto.py"],
                    "acceptance": ["Works end-to-end"],
                    "lease_expires_at": "2026-08-21T12:05:00+00:00",
                    "branch": "feat/proto",
                    "updated_at": TIMESTAMP_FIXED,
                }
            )
        )
        # 3. Blocked task (creates a task blocker)
        self.task_store.create(
            TaskRecord.from_dict(
                {
                    "id": "T-0002",
                    "channel": "review",
                    "title": "Deploy prototype",
                    "status": "blocked",
                    "owner": None,
                    "created_by": "bob",
                    "depends_on": ["T-0001"],
                    "files_hint": ["deploy.sh"],
                    "acceptance": ["Deployed to staging"],
                    "lease_expires_at": None,
                    "branch": None,
                    "updated_at": TIMESTAMP_FIXED,
                }
            )
        )
        # 4. Path lock
        self.lock_store.lock(
            owner="alice",
            paths=["proto.py"],
            lease_seconds=300.0,
            now=TIMESTAMP_FIXED,
        )
        # 5. Decision message
        self._post_msg(
            sender="alice",
            title="Decision: Use stdlib only",
            body="Stdlib ensures maximum portability across Windows and POSIX.",
            msg_type="decision",
        )
        # 6. Blocker message
        self._post_msg(
            sender="bob",
            title="Blocker: Need API credentials",
            body="Waiting on staging environment token.",
            msg_type="blocker",
        )
        # 7. Verification evidence message
        self._post_msg(
            sender="alice",
            title="Verification: Unit test suite green",
            body="Ran 42 tests, 0 failures, 100% pass rate.",
            msg_type="verification",
        )

        md = self.state_store.render(now=TIMESTAMP_FIXED)

        # Check Goal / Topic
        self.assertIn("Review vNext architecture", md)
        self.assertIn("## Goal & Topic", md)
        # Check Decisions
        self.assertIn("## Decisions", md)
        self.assertIn("Use stdlib only", md)
        self.assertIn("alice", md)
        # Check Open Tasks
        self.assertIn("## Open Tasks", md)
        self.assertIn("T-0001", md)
        self.assertIn("Build prototype", md)
        self.assertIn("in_progress", md)
        self.assertIn("T-0002", md)
        self.assertIn("Deploy prototype", md)
        # Check Blockers
        self.assertIn("## Blockers", md)
        self.assertIn("T-0002", md)
        self.assertIn("Need API credentials", md)
        # Check Owners & Leases
        self.assertIn("## Owners & Leases", md)
        self.assertIn("alice", md)
        # Check Path Locks
        self.assertIn("## Path Locks", md)
        self.assertIn("proto.py", md)
        # Check Verification Evidence
        self.assertIn("## Verification Evidence", md)
        self.assertIn("Unit test suite green", md)
        self.assertIn("Ran 42 tests", md)

    def test_empty_channel_state_renders_cleanly_with_placeholders(self):
        """An empty channel without tasks or messages renders valid markdown with (none)."""
        md = self.state_store.render(now=TIMESTAMP_FIXED)
        self.assertIn("# State: review", md)
        self.assertIn("Review vNext architecture", md)
        self.assertIn("## Decisions", md)
        self.assertIn("*(none)*", md)
        self.assertIn("## Open Tasks", md)
        self.assertIn("## Blockers", md)
        self.assertIn("## Owners & Leases", md)
        self.assertIn("## Path Locks", md)
        self.assertIn("## Verification Evidence", md)

    def test_decision_extraction_various_patterns(self):
        """Extract decisions from frontmatter type, status, decision field, title prefix, or body header."""
        cases = [
            ("type_decision", "Decision A", "Body A", {"msg_type": "decision"}),
            ("status_decision", "Decision B", "Body B", {"status": "decision"}),
            ("frontmatter_field", "Decision C", "Body C", {"extra_frontmatter": {"decision": "true"}}),
            ("title_prefix", "Decision: Choose SQLite", "Body D", {}),
            ("bracket_prefix", "[Decision] Architecture choice", "Body E", {}),
            ("body_header", "General Title", "# Decision\nWe choose pattern X.", {}),
        ]
        for name, title, body, kwargs in cases:
            with self.subTest(pattern=name):
                self._post_msg(sender="alice", title=title, body=body, **kwargs)

        summary = self.state_store.summarize(now=TIMESTAMP_FIXED)
        self.assertGreaterEqual(len(summary.decisions), 6)

    def test_blocker_extraction_various_patterns(self):
        """Extract blockers from task dependencies, task status, frontmatter, title, or body header."""
        # 1. Unfinished task dependency blocker
        self.task_store.create(
            TaskRecord.from_dict(
                {
                    "id": "T-0010",
                    "channel": "review",
                    "title": "Dep A",
                    "status": "open",
                    "owner": None,
                    "created_by": "alice",
                    "depends_on": [],
                    "files_hint": [],
                    "acceptance": [],
                    "lease_expires_at": None,
                    "branch": None,
                    "updated_at": TIMESTAMP_FIXED,
                }
            )
        )
        self.task_store.create(
            TaskRecord.from_dict(
                {
                    "id": "T-0020",
                    "channel": "review",
                    "title": "Dep B",
                    "status": "open",
                    "owner": None,
                    "created_by": "bob",
                    "depends_on": ["T-0010"],
                    "files_hint": [],
                    "acceptance": [],
                    "lease_expires_at": None,
                    "branch": None,
                    "updated_at": TIMESTAMP_FIXED,
                }
            )
        )
        # 2. Blocked task status
        self.task_store.create(
            TaskRecord.from_dict(
                {
                    "id": "T-0030",
                    "channel": "review",
                    "title": "Explicitly Blocked",
                    "status": "blocked",
                    "owner": None,
                    "created_by": "alice",
                    "depends_on": [],
                    "files_hint": [],
                    "acceptance": [],
                    "lease_expires_at": None,
                    "branch": None,
                    "updated_at": TIMESTAMP_FIXED,
                }
            )
        )
        # 3. Message blockers
        self._post_msg(sender="bob", title="Blocker: Missing secret", body="Blocked on env var", msg_type="blocker")
        self._post_msg(sender="alice", title="General msg", body="## Blocker\nWaiting for upstream PR.")

        summary = self.state_store.summarize(now=TIMESTAMP_FIXED)
        self.assertGreaterEqual(len(summary.blockers), 4)

    def test_verification_evidence_various_patterns(self):
        """Extract verification evidence from messages and completed tasks."""
        # Completed task with acceptance
        self.task_store.create(
            TaskRecord.from_dict(
                {
                    "id": "T-0099",
                    "channel": "review",
                    "title": "Verified Task",
                    "status": "done",
                    "owner": "alice",
                    "created_by": "alice",
                    "depends_on": [],
                    "files_hint": [],
                    "acceptance": ["Tests pass 100%", "Lint clean"],
                    "lease_expires_at": None,
                    "branch": None,
                    "updated_at": TIMESTAMP_FIXED,
                }
            )
        )
        # Message verifications
        self._post_msg(sender="alice", title="Verification: E2E suite passed", body="All green", msg_type="verification")
        self._post_msg(sender="bob", title="Evidence: benchmark output", body="1000 ops/sec", msg_type="evidence")
        self._post_msg(sender="alice", title="Audit report", body="### Verification\nCode review signed off.")

        summary = self.state_store.summarize(now=TIMESTAMP_FIXED)
        self.assertGreaterEqual(len(summary.verification), 4)

    def test_completed_task_verification_uses_completion_actor(self):
        """Completed lease evidence is attributed to the actor who completed it."""
        self.task_store.create(
            TaskRecord.from_dict(
                {
                    "id": "T-0100",
                    "channel": "review",
                    "title": "Actor attribution",
                    "status": "open",
                    "owner": None,
                    "created_by": "alice",
                    "depends_on": [],
                    "files_hint": [],
                    "acceptance": ["Attribution is accurate"],
                    "lease_expires_at": None,
                    "branch": None,
                    "updated_at": TIMESTAMP_FIXED,
                }
            ),
            actor="alice",
        )
        self.lease_store.claim("T-0100", "bob", actor="bob")
        self.lease_store.complete("T-0100", "bob", actor="bob")

        summary = self.state_store.summarize()
        verification = next(
            record for record in summary.verification if record.source_id == "T-0100"
        )

        self.assertEqual(verification.author, "bob")

    # -------------------------------------------------------------------------
    # 3. Compaction and Non-Destructive Preservation Invariants
    # -------------------------------------------------------------------------

    def test_compaction_never_deletes_or_mutates_authoritative_sources(self):
        """Compaction writes state.md but NEVER deletes or mutates messages, tasks, claims, locks, or cursors."""
        # 1. Post messages
        self._post_msg(sender="alice", title="Msg 1", body="First message")
        self._post_msg(
            sender="bob",
            title="Decision: Chosen path",
            body="Details",
            msg_type="decision",
        )
        self._post_msg(
            sender="alice",
            title="Verification: Verified",
            body="Evidence",
            msg_type="verification",
        )

        # 2. Create tasks and claim
        self.task_store.create(
            TaskRecord.from_dict(
                {
                    "id": "T-0001",
                    "channel": "review",
                    "title": "Task One",
                    "status": "open",
                    "owner": None,
                    "created_by": "alice",
                    "depends_on": [],
                    "files_hint": ["foo.py"],
                    "acceptance": ["Accepted"],
                    "lease_expires_at": None,
                    "branch": None,
                    "updated_at": TIMESTAMP_FIXED,
                }
            )
        )
        self.lease_store.claim("T-0001", "alice", 300.0, now=TIMESTAMP_FIXED)

        # 3. Create lock
        self.lock_store.lock("alice", ["foo.py"], 300.0, now=TIMESTAMP_FIXED)

        # 4. Write cursor
        chat.write_cursor(self.channel, "alice", 2)
        chat.write_cursor(self.channel, "bob", 1)

        # Snapshot before compaction
        messages_before = {p.name: p.read_bytes() for p in chat.message_files(self.channel)}
        tasks_before = {p.name: p.read_bytes() for p in (self.channel / "tasks").glob("*.json")}
        claims_before = {p.name: p.read_bytes() for p in (self.channel / "claims").glob("*.json")}
        locks_before = {p.name: p.read_bytes() for p in (self.channel / "locks").glob("*.json")}
        cursors_before = {p.name: p.read_bytes() for p in (self.channel / ".cursors").glob("*.txt")}
        meta_before = (self.channel / "_meta.json").read_bytes()

        # Run compaction with audit=False to verify exact non-mutation of message set
        summary = self.state_store.compact(actor="alice", audit=False, now=TIMESTAMP_FIXED)

        # state.md must exist and contain valid state
        state_file = self.channel / STATE_FILENAME
        self.assertTrue(state_file.exists())
        self.assertEqual(state_file.read_text(encoding="utf-8"), summary.render_markdown(now=TIMESTAMP_FIXED))

        # Check all authoritative source files are identical
        messages_after = {p.name: p.read_bytes() for p in chat.message_files(self.channel)}
        tasks_after = {p.name: p.read_bytes() for p in (self.channel / "tasks").glob("*.json")}
        claims_after = {p.name: p.read_bytes() for p in (self.channel / "claims").glob("*.json")}
        locks_after = {p.name: p.read_bytes() for p in (self.channel / "locks").glob("*.json")}
        cursors_after = {p.name: p.read_bytes() for p in (self.channel / ".cursors").glob("*.txt")}
        meta_after = (self.channel / "_meta.json").read_bytes()

        self.assertEqual(messages_before, messages_after)
        self.assertEqual(tasks_before, tasks_after)
        self.assertEqual(claims_before, claims_after)
        self.assertEqual(locks_before, locks_after)
        self.assertEqual(cursors_before, cursors_after)
        self.assertEqual(meta_before, meta_after)

    def test_compaction_with_audit_appends_audit_message(self):
        """When audit=True (default), compaction emits an auditable message to the channel."""
        self._post_msg(sender="alice", title="Start", body="Let us start")
        msg_count_before = len(chat.message_files(self.channel))

        self.state_store.compact(actor="alice", audit=True, now=TIMESTAMP_FIXED)

        msg_files = chat.message_files(self.channel)
        self.assertEqual(len(msg_files), msg_count_before + 1)
        latest = msg_files[-1]
        meta = chat.parse_frontmatter(latest)
        self.assertEqual(meta.get("status"), "state.compacted")
        self.assertEqual(meta.get("from"), "alice")

    def test_compaction_is_idempotent(self):
        """Repeated compaction runs update state.md consistently without corrupting state."""
        self._post_msg(sender="alice", title="Decision: Plan A", body="Plan A selected", msg_type="decision")
        res1 = self.state_store.compact(actor="alice", audit=False, now=TIMESTAMP_FIXED)
        content1 = (self.channel / STATE_FILENAME).read_text(encoding="utf-8")

        res2 = self.state_store.compact(actor="alice", audit=False, now=TIMESTAMP_FIXED)
        content2 = (self.channel / STATE_FILENAME).read_text(encoding="utf-8")

        self.assertEqual(content1, content2)
        self.assertEqual(res1.render_markdown(now=TIMESTAMP_FIXED), res2.render_markdown(now=TIMESTAMP_FIXED))

    # -------------------------------------------------------------------------
    # 4. Atomic Temporary Sibling Write
    # -------------------------------------------------------------------------

    def test_atomic_write_uses_temporary_sibling_and_replaces(self):
        """state.md write uses atomic replacement and leaves no leftover temporary files."""
        self.state_store.compact(actor="alice", audit=False, now=TIMESTAMP_FIXED)
        state_file = self.channel / STATE_FILENAME
        self.assertTrue(state_file.exists())

        # No dangling temporary files in channel root
        tmp_files = list(self.channel.glob(".*tmp*")) + list(self.channel.glob("*.tmp*"))
        stray_tmps = [p for p in tmp_files if "state" in p.name and p != state_file]
        self.assertEqual(stray_tmps, [])

    def test_read_saved_state_file(self):
        """read_saved() returns saved state.md content or raises STATE_RECORD_NOT_FOUND."""
        # When state.md does not exist yet
        with self.assertRaises(StateValidationError) as cm:
            self.state_store.read_saved()
        self.assertEqual(cm.exception.code, "STATE_RECORD_NOT_FOUND")

        # After compact
        self.state_store.compact(actor="alice", audit=False, now=TIMESTAMP_FIXED)
        saved = self.state_store.read_saved()
        self.assertIn("# State: review", saved)

    # -------------------------------------------------------------------------
    # 5. Malformed Source Handling & Stable Errors
    # -------------------------------------------------------------------------

    def test_nonexistent_channel_raises_state_channel_not_found(self):
        """Accessing a non-existent channel raises STATE_CHANNEL_NOT_FOUND."""
        store = StateStore(self.root / "does_not_exist", root=self.root)
        with self.assertRaises(StateValidationError) as cm:
            store.render()
        self.assertEqual(cm.exception.code, "STATE_CHANNEL_NOT_FOUND")

    def test_invalid_channel_name_raises_state_invalid_channel(self):
        """Invalid channel names raise STATE_INVALID_CHANNEL."""
        with self.assertRaises(StateValidationError) as cm:
            StateStore(self.root / "../escape", root=self.root)
        self.assertEqual(cm.exception.code, "STATE_INVALID_CHANNEL")

        with self.assertRaises(StateValidationError) as cm:
            StateStore("invalid/slash", root=self.root)
        self.assertEqual(cm.exception.code, "STATE_INVALID_CHANNEL")

    def test_malformed_meta_json_raises_or_handles_gracefully(self):
        """Malformed _meta.json raises STATE_INVALID_RECORD or STATE_MALFORMED_SOURCE when strict."""
        meta_path = self.channel / "_meta.json"
        meta_path.write_text("invalid json content {{{", encoding="utf-8")

        # In strict mode, should raise stable error
        with self.assertRaises(StateValidationError) as cm:
            self.state_store.summarize(strict=True)
        self.assertIn(cm.exception.code, {"STATE_MALFORMED_SOURCE", "STATE_INVALID_RECORD", "STATE_INVALID_META"})

        # In non-strict mode, handles gracefully with default topic / note
        summary = self.state_store.summarize(strict=False)
        self.assertIsInstance(summary, StateSummary)
        self.assertIn("review", summary.channel)

    def test_malformed_task_file_handling(self):
        """Malformed task file in tasks/ is caught with stable error in strict mode."""
        tasks_dir = self.channel / "tasks"
        tasks_dir.mkdir(exist_ok=True)
        bad_task = tasks_dir / "T-9999.json"
        bad_task.write_text("not json", encoding="utf-8")

        with self.assertRaises(StateValidationError) as cm:
            self.state_store.summarize(strict=True)
        self.assertIn(cm.exception.code, {"STATE_MALFORMED_SOURCE", "STATE_INVALID_RECORD"})

        # Non-strict mode skips without crashing
        summary = self.state_store.summarize(strict=False)
        self.assertIsInstance(summary, StateSummary)

    def test_strict_state_rejects_task_files_hint_symlink_escape(self):
        """Strict state loading must enforce task workspace path boundaries."""
        tasks_dir = self.channel / "tasks"
        tasks_dir.mkdir(exist_ok=True)
        outside = self.root / "outside-task-target.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.channel / "linked-task-target.txt"
        try:
            link.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")

        task = {
            "id": "T-escape",
            "channel": "review",
            "title": "Escaping task",
            "status": "open",
            "owner": None,
            "created_by": "alice",
            "depends_on": [],
            "files_hint": [link.name],
            "acceptance": [],
            "lease_expires_at": None,
            "branch": None,
            "updated_at": TIMESTAMP_FIXED,
        }
        (tasks_dir / "T-escape.json").write_text(
            json.dumps(task), encoding="utf-8"
        )

        with self.assertRaises(StateValidationError) as raised:
            self.state_store.summarize(strict=True)
        self.assertEqual(raised.exception.code, "STATE_MALFORMED_SOURCE")

    def test_strict_state_rejects_path_lock_symlink_escape(self):
        """Strict state loading must use PathLockStore boundary validation."""
        self.lock_store.lock(
            owner="alice",
            paths=["state-target.txt"],
            lease_seconds=300.0,
            now=TIMESTAMP_FIXED,
        )
        lock_path = next((self.channel / "locks").glob("*.json"))
        outside = self.root / "outside-lock.json"
        outside.write_bytes(lock_path.read_bytes())
        lock_path.unlink()
        try:
            lock_path.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")

        with self.assertRaises(StateValidationError) as raised:
            self.state_store.summarize(strict=True)
        self.assertEqual(raised.exception.code, "STATE_MALFORMED_SOURCE")

    def test_error_hierarchy_and_properties(self):
        """StateError inherits from AgentChatError and has stable code and message properties."""
        err = StateValidationError("STATE_TEST_CODE", "Test error message", detail_key="detail_val")
        self.assertIsInstance(err, chat.AgentChatError)
        self.assertIsInstance(err, StateError)
        self.assertEqual(err.code, "STATE_TEST_CODE")
        self.assertEqual(err.message, "Test error message")
        self.assertEqual(err.details.get("detail_key"), "detail_val")
        self.assertIn("STATE_TEST_CODE: Test error message", str(err))

    # -------------------------------------------------------------------------
    # 6. Module Level Helpers
    # -------------------------------------------------------------------------

    def test_module_level_convenience_functions(self):
        """load_state, render_state, and compact_state functions work as expected."""
        rendered = render_state("review", root=self.root, now=TIMESTAMP_FIXED)
        self.assertIn("# State: review", rendered)

        summary = compact_state("review", actor="alice", audit=False, root=self.root, now=TIMESTAMP_FIXED)
        self.assertIsInstance(summary, StateSummary)
        self.assertTrue((self.channel / STATE_FILENAME).exists())

        loaded = load_state("review", root=self.root)
        self.assertEqual(loaded.channel, "review")

    # -------------------------------------------------------------------------
    # 7. CLI Integration Tests in chat.py
    # -------------------------------------------------------------------------

    def test_cli_state_command_prints_markdown(self):
        """chat.py state <channel> prints the derived state."""
        self.task_store.create(
            TaskRecord.from_dict(
                {
                    "id": "T-0001",
                    "channel": "review",
                    "title": "CLI test task",
                    "status": "open",
                    "owner": None,
                    "created_by": "alice",
                    "depends_on": [],
                    "files_hint": [],
                    "acceptance": [],
                    "lease_expires_at": None,
                    "branch": None,
                    "updated_at": TIMESTAMP_FIXED,
                }
            )
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            chat.main(["--root", str(self.root), "state", "review"])
        output = buf.getvalue()
        self.assertIn("# State: review", output)
        self.assertIn("CLI test task", output)

    def test_cli_state_write_and_compact_commands(self):
        """chat.py state <channel> --write and chat.py compact <channel> work."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            chat.main(["--root", str(self.root), "compact", "review", "--no-audit"])
        output = buf.getvalue()
        self.assertIn("compacted", output.lower())
        self.assertTrue((self.channel / STATE_FILENAME).exists())

    def test_cli_state_json_output(self):
        """chat.py state <channel> --json outputs valid JSON summary."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            chat.main(["--root", str(self.root), "state", "review", "--json"])
        output = buf.getvalue()
        data = json.loads(output)
        self.assertEqual(data["channel"], "review")
        self.assertIn("decisions", data)
        self.assertIn("open_tasks", data)
        self.assertIn("blockers", data)
        self.assertIn("owners", data)
        self.assertIn("path_locks", data)
        self.assertIn("verification", data)


if __name__ == "__main__":
    unittest.main()
