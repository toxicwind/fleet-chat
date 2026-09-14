"""Focused Task 2 tests for the typed task model and atomic store."""

import contextlib
import io
import json
import multiprocessing
import os
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import chat
from agent_chat.task_model import (
    TASK_FIELDS,
    TaskError,
    TaskRecord,
    TaskValidationError,
)
from agent_chat.task_store import TaskStore

TIMESTAMP = "2026-08-21T12:00:00+00:00"


def _hold_task_lock(channel: str, root: str, ready, release) -> None:
    store = TaskStore(Path(channel), root=Path(root))
    handle = store._acquire_mutation_lock(timeout=2.0)
    ready.send("acquired")
    release.wait(5.0)
    store._release_mutation_lock(handle)


def _exit_with_task_lock(channel: str, root: str, ready) -> None:
    store = TaskStore(Path(channel), root=Path(root))
    store._acquire_mutation_lock(timeout=2.0)
    ready.send("acquired")
    ready.close()
    # Return without application-level release; process exit owns cleanup.


def _cleanup_process(process, *, release=None):
    if release is not None:
        release.set()
    process.join(5.0)
    if process.is_alive():
        process.terminate()
        process.join(5.0)
    if process.is_alive():
        process.kill()
        process.join(5.0)
    if process.is_alive():
        raise RuntimeError("spawned task-lock worker did not stop")
    exitcode = process.exitcode
    process.close()
    return exitcode


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        chat.cmd_init(
            self.root,
            SimpleNamespace(ephemeral=None, channel="review", members="alice,bob", topic="Task board"),
        )
        self.channel = self.root / "review"
        self.store = TaskStore(self.channel)

    def test_live_task_lock_is_not_reclaimed_from_mtime(self):
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe(duplex=False)
        release = ctx.Event()
        process = ctx.Process(
            target=_hold_task_lock,
            args=(str(self.channel), str(self.root), child, release),
        )
        process.start()
        child.close()
        try:
            self.assertTrue(
                parent.poll(5.0),
                "task lock worker did not signal acquisition",
            )
            self.assertEqual(parent.recv(), "acquired")
            os.utime(self.channel / "_tasks.lock", (0, 0))
            with self.assertRaises(TaskValidationError) as error:
                self.store._acquire_mutation_lock(timeout=0.05)
            self.assertEqual(error.exception.code, "TASK_LOCK_TIMEOUT")
        finally:
            try:
                exitcode = _cleanup_process(process, release=release)
            finally:
                parent.close()
        self.assertEqual(exitcode, 0)

    def test_task_lock_recovers_after_owner_process_exits(self):
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe(duplex=False)
        process = ctx.Process(
            target=_exit_with_task_lock,
            args=(str(self.channel), str(self.root), child),
        )
        process.start()
        child.close()
        try:
            self.assertTrue(
                parent.poll(5.0),
                "task lock owner did not signal acquisition",
            )
            self.assertEqual(parent.recv(), "acquired")
            process.join(5.0)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)

            handle = self.store._acquire_mutation_lock(timeout=0.5)
            self.store._release_mutation_lock(handle)
            self.assertTrue((self.channel / "_tasks.lock").is_file())
        finally:
            try:
                _cleanup_process(process)
            finally:
                parent.close()

    def test_task_lock_directory_fails_closed(self):
        lock_path = self.channel / "_tasks.lock"
        lock_path.mkdir()
        with self.assertRaises(TaskValidationError) as error:
            self.store._acquire_mutation_lock(timeout=0.01)
        self.assertEqual(error.exception.code, "TASK_STORAGE_INVALID")
        self.assertTrue(lock_path.is_dir())

    def test_task_lock_symlink_fails_closed(self):
        target = self.channel / "real-lock"
        target.write_bytes(b"\0")
        link = self.channel / "_tasks.lock"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        with self.assertRaises(TaskValidationError) as raised:
            self.store._acquire_mutation_lock(timeout=0.01)
        self.assertEqual(raised.exception.code, "TASK_PATH_OUTSIDE_WORKSPACE")
        self.assertTrue(link.is_symlink())

    def test_task_lock_junction_fails_closed(self):
        if os.name != "nt":
            self.skipTest("Windows junction semantics do not apply on POSIX")
        target = self.channel / "real-lock"
        target.mkdir()
        link = self.channel / "_tasks.lock"
        try:
            result = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(link),
                    str(target),
                ],
                capture_output=True,
                text=True,
            )
        except OSError as error:
            target.rmdir()
            self.skipTest(f"junction creation unavailable: {error}")
        if result.returncode != 0:
            target.rmdir()
            self.skipTest(
                f"junction creation unavailable: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        try:
            with self.assertRaises(TaskValidationError) as raised:
                self.store._acquire_mutation_lock(timeout=0.01)
            self.assertEqual(raised.exception.code, "TASK_PATH_OUTSIDE_WORKSPACE")
            self.assertTrue(link.is_dir())
        finally:
            if link.exists():
                link.rmdir()
            target.rmdir()

    def test_task_lock_release_is_idempotent(self):
        handle = self.store._acquire_mutation_lock(timeout=0.5)
        self.store._release_mutation_lock(handle)
        self.store._release_mutation_lock(handle)
        self.assertTrue((self.channel / "_tasks.lock").is_file())
        reacquired = self.store._acquire_mutation_lock(timeout=0.5)
        self.store._release_mutation_lock(reacquired)

    def tearDown(self):
        self.temp_dir.cleanup()

    def task_data(self, **overrides):
        data = {
            "id": "T-0001",
            "channel": "review",
            "title": "Document the review flow",
            "status": "open",
            "owner": None,
            "created_by": "alice",
            "depends_on": [],
            "files_hint": ["docs/review.md"],
            "acceptance": ["Review is documented"],
            "lease_expires_at": None,
            "branch": None,
            "updated_at": TIMESTAMP,
        }
        data.update(overrides)
        return data

    def make_task(self, **overrides):
        return TaskRecord.from_dict(self.task_data(**overrides))

    def test_task_record_preserves_exact_documented_shape(self):
        task = self.make_task()

        self.assertEqual(tuple(task.to_dict()), TASK_FIELDS)
        self.assertEqual(
            set(task.to_dict()),
            {
                "id",
                "channel",
                "title",
                "status",
                "owner",
                "created_by",
                "depends_on",
                "files_hint",
                "acceptance",
                "lease_expires_at",
                "branch",
                "updated_at",
            },
        )

    def test_missing_or_unknown_fields_have_stable_codes(self):
        missing = self.task_data()
        del missing["acceptance"]
        with self.assertRaises(TaskValidationError) as missing_error:
            TaskRecord.from_dict(missing)
        self.assertEqual(missing_error.exception.code, "TASK_REQUIRED_FIELD_MISSING")

        unknown = self.task_data(extra="not allowed")
        with self.assertRaises(TaskValidationError) as unknown_error:
            TaskRecord.from_dict(unknown)
        self.assertEqual(unknown_error.exception.code, "TASK_UNKNOWN_FIELD")

    def test_all_documented_statuses_are_valid(self):
        for status in ("open", "in_progress", "blocked", "done", "cancelled"):
            with self.subTest(status=status):
                self.assertEqual(self.make_task(status=status).status, status)

    def test_invalid_transition_is_rejected_without_changing_record(self):
        self.store.create(self.make_task(), actor="alice")

        with self.assertRaises(TaskValidationError) as error:
            self.store.update("T-0001", actor="alice", status="done")

        self.assertEqual(error.exception.code, "TASK_INVALID_TRANSITION")
        self.assertEqual(self.store.show("T-0001").status, "open")

    def test_dependency_reference_must_exist(self):
        with self.assertRaises(TaskValidationError) as error:
            self.store.create(
                self.make_task(id="T-0002", depends_on=["T-9999"]), actor="alice"
            )

        self.assertEqual(error.exception.code, "TASK_UNKNOWN_DEPENDENCY")
        self.assertFalse((self.channel / "tasks" / "T-0002.json").exists())

    def test_create_rejects_non_ready_dependency_status(self):
        self.store.create(self.make_task(id="T-0001"), actor="alice")

        with self.assertRaises(TaskValidationError) as error:
            self.store.create(
                self.make_task(
                    id="T-0002",
                    status="in_progress",
                    depends_on=["T-0001"],
                ),
                actor="alice",
            )

        self.assertEqual(error.exception.code, "TASK_DEPENDENCY_NOT_READY")
        self.assertFalse((self.channel / "tasks" / "T-0002.json").exists())

    def test_dependency_cycles_are_rejected(self):
        self.store.create(self.make_task(id="T-0001"), actor="alice")
        self.store.create(
            self.make_task(id="T-0002", depends_on=["T-0001"]), actor="alice"
        )

        with self.assertRaises(TaskValidationError) as error:
            self.store.update("T-0001", actor="alice", depends_on=["T-0002"])

        self.assertEqual(error.exception.code, "TASK_DEPENDENCY_CYCLE")
        self.assertEqual(self.store.show("T-0001").depends_on, [])

    def test_timestamps_require_iso8601_offsets(self):
        for value in ("not-a-timestamp", "2026-08-21T12:00:00"):
            with self.subTest(value=value):
                with self.assertRaises(TaskValidationError) as error:
                    TaskRecord.from_dict(self.task_data(updated_at=value))
                self.assertEqual(error.exception.code, "TASK_INVALID_TIMESTAMP")

    def test_files_hint_must_stay_inside_channel_workspace(self):
        for path in (
            "../outside.txt",
            "/tmp/outside.txt",
            "C:\\outside.txt",
            "C:boot.ini",
            "docs/review.md:private",
            "docs/C:boot.ini",
        ):
            with self.subTest(path=path):
                with self.assertRaises(TaskValidationError) as error:
                    self.store.create(
                        self.make_task(id="T-" + str(len(path)), files_hint=[path]),
                        actor="alice",
                    )
                self.assertEqual(error.exception.code, "TASK_PATH_OUTSIDE_WORKSPACE")

    def test_stream_path_update_preserves_task_and_audit(self):
        self.store.create(self.make_task(), actor="alice")
        task_path = self.channel / "tasks" / "T-0001.json"
        original_task = task_path.read_bytes()
        original_messages = chat.message_files(self.channel)

        with self.assertRaises(TaskValidationError) as error:
            self.store.update(
                "T-0001", actor="alice", files_hint=["docs/review.md:private"]
            )

        self.assertEqual(error.exception.code, "TASK_PATH_OUTSIDE_WORKSPACE")
        self.assertEqual(task_path.read_bytes(), original_task)
        self.assertEqual(chat.message_files(self.channel), original_messages)
        self.assertEqual(self.store.show("T-0001").files_hint, ["docs/review.md"])

    def test_symlinked_files_hint_cannot_escape_workspace(self):
        outside = Path(self.temp_dir.name).parent / (self.root.name + "-outside")
        outside.mkdir()
        link = self.channel / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this platform")
        try:
            with self.assertRaises(TaskValidationError) as error:
                self.store.create(
                    self.make_task(files_hint=["linked/secret.txt"]), actor="alice"
                )
            self.assertEqual(error.exception.code, "TASK_PATH_OUTSIDE_WORKSPACE")
        finally:
            link.unlink(missing_ok=True)
            outside.rmdir()

    def test_list_rejects_task_record_symlink_outside_channel(self):
        tasks = self.channel / "tasks"
        tasks.mkdir()
        outside = Path(self.temp_dir.name).parent / (self.root.name + "-records")
        outside.mkdir()
        external_record = outside / "T-0001.json"
        external_record.write_text(json.dumps(self.task_data()), encoding="utf-8")
        link = tasks / "T-0001.json"
        try:
            link.symlink_to(external_record)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this platform")
        try:
            with self.assertRaises(TaskValidationError) as error:
                self.store.list()
            self.assertEqual(error.exception.code, "TASK_PATH_OUTSIDE_WORKSPACE")
        finally:
            link.unlink(missing_ok=True)
            external_record.unlink(missing_ok=True)
            outside.rmdir()

    def test_store_enforces_explicit_root_boundary_and_channel_symlink(self):
        outside_root = Path(self.temp_dir.name).parent / (self.root.name + "-root")
        outside_root.mkdir()
        chat.cmd_init(
            outside_root,
            SimpleNamespace(ephemeral=None, channel="outside", members=None, topic=None),
        )
        outside_channel = outside_root / "outside"
        with self.assertRaises(TaskValidationError) as error:
            TaskStore(outside_channel, root=self.root)
        self.assertEqual(error.exception.code, "TASK_PATH_OUTSIDE_WORKSPACE")

        linked_channel = self.root / "linked"
        try:
            linked_channel.symlink_to(outside_channel, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this platform")
        try:
            with self.assertRaises(TaskValidationError) as error:
                TaskStore(linked_channel, root=self.root)
            self.assertEqual(error.exception.code, "TASK_PATH_OUTSIDE_WORKSPACE")
        finally:
            linked_channel.unlink(missing_ok=True)
            (outside_channel / "_meta.json").unlink(missing_ok=True)
            (outside_channel / ".cursors").rmdir()
            outside_channel.rmdir()
            # cmd_init writes root-level fleet state (op log, channel index)
            (outside_root / ".channels-index").unlink(missing_ok=True)
            (outside_root / ".ops.jsonl").unlink(missing_ok=True)
            outside_root.rmdir()

    def test_load_and_show_validate_persisted_dependency_graph(self):
        tasks = self.channel / "tasks"
        tasks.mkdir()
        first = self.task_data(id="T-0001", depends_on=["T-0002"])
        second = self.task_data(id="T-0002", depends_on=["T-0001"])
        (tasks / "T-0001.json").write_text(json.dumps(first), encoding="utf-8")
        (tasks / "T-0002.json").write_text(json.dumps(second), encoding="utf-8")

        for loader in (self.store.load, self.store.show):
            with self.subTest(loader=loader.__name__):
                with self.assertRaises(TaskValidationError) as error:
                    loader("T-0001")
                self.assertEqual(error.exception.code, "TASK_DEPENDENCY_CYCLE")

    def test_load_rejects_persisted_unknown_dependency(self):
        tasks = self.channel / "tasks"
        tasks.mkdir()
        record = self.task_data(depends_on=["T-9999"])
        (tasks / "T-0001.json").write_text(json.dumps(record), encoding="utf-8")

        for loader in (self.store.load, self.store.show):
            with self.subTest(loader=loader.__name__):
                with self.assertRaises(TaskValidationError) as error:
                    loader("T-0001")
                self.assertEqual(error.exception.code, "TASK_UNKNOWN_DEPENDENCY")

    def test_nested_validation_uses_canonical_error_codes(self):
        with self.assertRaises(TaskValidationError) as acceptance_error:
            TaskRecord.from_dict(self.task_data(acceptance=[123]))
        self.assertEqual(acceptance_error.exception.code, "TASK_INVALID_ACCEPTANCE")

        with self.assertRaises(TaskValidationError) as dependency_error:
            TaskRecord.from_dict(self.task_data(depends_on=[123]))
        self.assertEqual(dependency_error.exception.code, "TASK_INVALID_DEPENDENCY_ID")

    def test_audit_failure_rolls_back_create_and_update(self):
        def fail_audit(*args, **kwargs):
            raise TaskError("TASK_AUDIT_FAILED", "injected audit failure")

        self.store._post_event = fail_audit
        with self.assertRaises(TaskError) as create_error:
            self.store.create(self.make_task(), actor="alice")
        self.assertEqual(create_error.exception.code, "TASK_AUDIT_FAILED")
        self.assertFalse((self.channel / "tasks" / "T-0001.json").exists())

        self.store._post_event = TaskStore._post_event.__get__(self.store, TaskStore)
        self.store.create(self.make_task(), actor="alice")
        self.store._post_event = fail_audit
        with self.assertRaises(TaskError) as update_error:
            self.store.update("T-0001", actor="alice", status="in_progress")
        self.assertEqual(update_error.exception.code, "TASK_AUDIT_FAILED")
        self.assertEqual(self.store.show("T-0001").status, "open")

    def test_updates_serialize_authoritative_write_and_audit_event(self):
        self.store.create(self.make_task(), actor="alice")
        first_started = threading.Event()
        release_first = threading.Event()
        second_finished = threading.Event()
        observed_statuses = []
        first_errors = []
        second_errors = []

        def ordered_event(event, task, **kwargs):
            if task.status == "in_progress":
                first_started.set()
                release_first.wait(2)
            observed_statuses.append(task.status)

        self.store._post_event = ordered_event

        def first_update():
            try:
                self.store.update("T-0001", actor="alice", status="in_progress")
            except BaseException as error:
                first_errors.append(error)

        def second_update():
            try:
                self.store.update("T-0001", actor="alice", status="blocked")
            except BaseException as error:
                second_errors.append(error)
            finally:
                second_finished.set()

        first_thread = threading.Thread(target=first_update)
        second_thread = threading.Thread(target=second_update)
        first_thread.start()
        self.assertTrue(first_started.wait(2))
        second_thread.start()
        try:
            self.assertFalse(second_finished.wait(0.2))
        finally:
            release_first.set()
            first_thread.join(2)
            second_thread.join(2)

        self.assertEqual(first_errors, [])
        self.assertEqual(second_errors, [])
        self.assertEqual(observed_statuses, ["in_progress", "blocked"])

    def test_create_collision_is_deterministic_and_preserves_existing_record(self):
        original = self.store.create(self.make_task(), actor="alice")
        path = self.channel / "tasks" / "T-0001.json"
        original_bytes = path.read_bytes()

        with self.assertRaises(TaskValidationError) as error:
            self.store.create(self.make_task(title="Must not overwrite"), actor="bob")

        self.assertEqual(error.exception.code, "TASK_ALREADY_EXISTS")
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(self.store.show("T-0001").title, original.title)

    def test_mutations_write_human_json_and_auditable_messages(self):
        self.store.create(self.make_task(), actor="alice")
        self.store.update("T-0001", actor="bob", status="in_progress")

        task_path = self.channel / "tasks" / "T-0001.json"
        raw = task_path.read_text(encoding="utf-8")
        self.assertTrue(raw.startswith("{\n"))
        self.assertEqual(tuple(json.loads(raw)), TASK_FIELDS)

        messages = sorted(self.channel.glob("[0-9][0-9][0-9][0-9]-*.md"))
        self.assertEqual(len(messages), 2)
        bodies = [path.read_text(encoding="utf-8") for path in messages]
        self.assertTrue(any("task.created" in body for body in bodies))
        self.assertTrue(any("task.updated" in body for body in bodies))

    def test_show_dependency_snapshot_uses_one_authoritative_snapshot(self):

        self.store.create(self.make_task(id="T-0001"), actor="alice")
        self.store.create(
            self.make_task(id="T-0002", depends_on=["T-0001"]),
            actor="alice",
        )
        snapshot_started = threading.Event()
        release_snapshot = threading.Event()
        update_started = threading.Event()
        update_finished = threading.Event()
        snapshot_calls = []
        shown = []
        errors = []
        original_snapshot = self.store._read_snapshot

        def blocked_snapshot():
            snapshot_calls.append(True)
            snapshot_started.set()
            if not release_snapshot.wait(2):
                raise AssertionError("timed out waiting to release snapshot")
            return original_snapshot()

        self.store._read_snapshot = blocked_snapshot

        def show_task():
            try:
                shown.append(self.store.show_with_dependencies("T-0002"))
            except BaseException as error:
                errors.append(error)

        def update_dependency():
            update_started.set()
            try:
                self.store.update("T-0001", actor="alice", status="in_progress")
                self.store.update("T-0001", actor="alice", status="done")
            finally:
                update_finished.set()

        show_thread = threading.Thread(target=show_task)
        update_thread = threading.Thread(target=update_dependency)
        show_thread.start()
        self.assertTrue(snapshot_started.wait(2))
        update_thread.start()
        self.assertTrue(update_started.wait(2))
        try:
            self.assertFalse(update_finished.wait(0.2))
        finally:
            release_snapshot.set()
            show_thread.join(2)
            update_thread.join(2)
            self.store._read_snapshot = original_snapshot

        self.assertEqual(errors, [])
        self.assertEqual(len(snapshot_calls), 1)
        task, statuses, ready = shown[0]
        self.assertEqual(task.id, "T-0002")
        self.assertEqual(statuses, {"T-0001": "open"})
        self.assertFalse(ready)
        self.assertTrue(self.store.dependencies_ready("T-0002"))


class TaskCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        chat.cmd_init(
            self.root,
            SimpleNamespace(ephemeral=None, channel="review", members="alice,bob", topic="Task board"),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_cli(self, *argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                chat.main(["--root", str(self.root), *argv])
            except SystemExit as error:
                return error.code, stdout.getvalue(), stderr.getvalue()
        return 0, stdout.getvalue(), stderr.getvalue()

    def test_task_commands_create_list_show_update_and_status_transitions(self):
        code, created, error = self.run_cli(
            "task",
            "create",
            "review",
            "T-0001",
            "--from",
            "alice",
            "--title",
            "Document the review flow",
        )
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(created, "created task T-0001 [open]\n")

        first_show = self.run_cli("task", "show", "review", "T-0001")
        second_show = self.run_cli("task", "show", "review", "T-0001")
        self.assertEqual(first_show, second_show)
        self.assertIn("status: open", first_show[1])
        self.assertIn("dependencies: ready", first_show[1])

        first_list = self.run_cli("task", "list", "review")
        second_list = self.run_cli("task", "list", "review")
        self.assertEqual(first_list, second_list)
        self.assertTrue(
            re.search(
                r"T-0001\s+open\s+-\s+-\s+Document the review flow", first_list[1]
            )
        )

        code, updated, error = self.run_cli(
            "task",
            "update",
            "review",
            "T-0001",
            "--as",
            "bob",
            "--title",
            "Review flow",
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(updated, "updated task T-0001 [open]\n")

        code, blocked, error = self.run_cli(
            "task", "block", "review", "T-0001", "--as", "bob"
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(blocked, "blocked task T-0001 [blocked]\n")
        code, released, error = self.run_cli(
            "task", "release", "review", "T-0001", "--as", "bob"
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(released, "released task T-0001 [open]\n")

    def test_task_commands_reject_advance_until_dependencies_are_done(self):
        self.assertEqual(
            self.run_cli(
                "task",
                "create",
                "review",
                "T-0001",
                "--from",
                "alice",
                "--title",
                "Prerequisite",
            )[0],
            0,
        )
        self.assertEqual(
            self.run_cli(
                "task",
                "create",
                "review",
                "T-0002",
                "--from",
                "alice",
                "--title",
                "Dependent",
                "--depends-on",
                "T-0001",
            )[0],
            0,
        )

        code, _, error = self.run_cli(
            "task",
            "update",
            "review",
            "T-0002",
            "--as",
            "bob",
            "--status",
            "in_progress",
        )
        self.assertEqual(code, 2)
        self.assertIn("TASK_DEPENDENCY_NOT_READY", error)
        self.assertEqual(self.run_cli("task", "show", "review", "T-0002")[0], 0)
        self.assertIn(
            "status: open", self.run_cli("task", "show", "review", "T-0002")[1]
        )

        self.assertEqual(
            self.run_cli(
                "task",
                "update",
                "review",
                "T-0001",
                "--as",
                "alice",
                "--status",
                "in_progress",
            )[0],
            0,
        )
        self.assertEqual(
            self.run_cli("task", "done", "review", "T-0001", "--as", "alice")[0],
            0,
        )
        self.assertEqual(
            self.run_cli(
                "task",
                "update",
                "review",
                "T-0002",
                "--as",
                "bob",
                "--status",
                "in_progress",
            )[0],
            0,
        )

    def test_task_commands_report_stable_errors_and_preserve_existing_parser(self):
        code, _, error = self.run_cli("task", "show", "review", "missing")
        self.assertEqual(code, 2)
        self.assertIn("TASK_NOT_FOUND", error)

        code, _, error = self.run_cli(
            "task", "done", "review", "missing", "--as", "alice"
        )
        self.assertEqual(code, 2)
        self.assertIn("TASK_NOT_FOUND", error)

        code, output, error = self.run_cli(
            "post",
            "review",
            "--from",
            "alice",
            "--title",
            "legacy",
            "--body",
            "still works",
        )
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("posted #", output)

    def test_task_status_commands_allow_idempotent_same_state_writes(self):
        self.assertEqual(
            self.run_cli(
                "task",
                "create",
                "review",
                "T-0001",
                "--from",
                "alice",
                "--title",
                "Idempotent transitions",
            )[0],
            0,
        )
        for command in ("release", "block", "block", "release", "release"):
            with self.subTest(command=command):
                code, _, error = self.run_cli(
                    "task", command, "review", "T-0001", "--as", "alice"
                )
                self.assertEqual((code, error), (0, ""))

        self.assertEqual(
            self.run_cli(
                "task",
                "update",
                "review",
                "T-0001",
                "--as",
                "alice",
                "--status",
                "in_progress",
            )[0],
            0,
        )
        for command in ("done", "done"):
            with self.subTest(command=command):
                code, _, error = self.run_cli(
                    "task", command, "review", "T-0001", "--as", "alice"
                )
                self.assertEqual((code, error), (0, ""))

    def test_task_io_errors_use_stable_errors(self):
        with patch(
            "agent_chat.task_store.acquire_advisory_file_lock",
            side_effect=OSError("permission denied"),
        ):
            code, _, error = self.run_cli("task", "list", "review")
        self.assertEqual(code, 2)
        self.assertIn("TASK_IO_ERROR", error)

    def test_malformed_task_inputs_use_stable_errors(self):
        code, _, error = self.run_cli(
            "task",
            "create",
            "review",
            "T-0001",
            "--from",
            "alice",
        )
        self.assertEqual(code, 2)
        self.assertIn("TASK_INVALID_ARGUMENT", error)

        code, _, error = self.run_cli("task", "unknown", "review")
        self.assertEqual(code, 2)
        self.assertIn("TASK_INVALID_COMMAND", error)

        code, _, error = self.run_cli("task", "show", "../review", "T-0001")
        self.assertEqual(code, 2)
        self.assertIn("TASK_INVALID_CHANNEL", error)


if __name__ == "__main__":
    unittest.main()
