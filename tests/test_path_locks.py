"""Focused tests for normalized, auditable channel path locks."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import chat
import agent_chat.path_locks as path_locks
from agent_chat.path_locks import PathLockError, PathLockStore


class PathLockStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        chat.cmd_init(
            self.root,
            SimpleNamespace(ephemeral=None, channel="review", members="alice,bob", topic="Path locks"),
        )
        self.channel = self.root / "review"
        (self.channel / "src").mkdir()
        (self.channel / "src" / "main.py").write_text("pass\n", encoding="utf-8")
        self.store = PathLockStore(self.channel, root=self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_file_file_collision_stores_normalized_and_display_paths_atomically(self):
        record = self.store.lock("alice", ["src/main.py"], lease_seconds=60)

        with self.assertRaises(PathLockError) as error:
            self.store.lock("bob", ["src/main.py"], lease_seconds=60)

        self.assertEqual(error.exception.code, "PATH_LOCK_CONFLICT")
        self.assertEqual(record.paths[0].normalized_path, "src/main.py")
        self.assertEqual(record.paths[0].display_path, "src/main.py")
        lock_files = list((self.channel / "locks").glob("*.json"))
        self.assertEqual(len(lock_files), 1)
        raw = lock_files[0].read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(json.loads(raw)["paths"][0]["normalized_path"], "src/main.py")

    def test_directory_file_overlap_conflicts(self):
        self.store.lock("alice", ["src"], lease_seconds=60)

        with self.assertRaises(PathLockError) as error:
            self.store.lock("bob", ["src/main.py"], lease_seconds=60)

        self.assertEqual(error.exception.code, "PATH_LOCK_CONFLICT")
        self.assertEqual(error.exception.details["conflicts"][0]["owner"], "alice")

    def test_case_normalization_follows_platform_semantics(self):
        first = self.store.lock("alice", ["src/Main.py"], lease_seconds=60)
        if os.name == "nt":
            with self.assertRaises(PathLockError) as error:
                self.store.lock("bob", ["SRC/main.PY"], lease_seconds=60)
            self.assertEqual(error.exception.code, "PATH_LOCK_CONFLICT")
            self.assertEqual(first.paths[0].normalized_path, "src/main.py")
        else:
            second = self.store.lock("bob", ["SRC/main.PY"], lease_seconds=60)
            self.assertNotEqual(
                first.paths[0].normalized_path, second.paths[0].normalized_path
            )

    def test_parent_traversal_and_absolute_paths_are_rejected(self):
        for requested in ("../outside.txt", "src/../outside.txt", str(self.root / "outside.txt")):
            with self.subTest(requested=requested):
                with self.assertRaises(PathLockError) as error:
                    self.store.lock("alice", [requested], lease_seconds=60)
                self.assertEqual(error.exception.code, "PATH_LOCK_INVALID_PATH")

    def test_symlink_escape_is_rejected(self):
        outside = Path(self.temp_dir.name).parent / (Path(self.temp_dir.name).name + "-outside")
        outside.mkdir()
        try:
            link = self.channel / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlink creation unavailable on this platform: {error}")

            with self.assertRaises(PathLockError) as error:
                self.store.lock("alice", ["escape/secret.txt"], lease_seconds=60)
            self.assertEqual(error.exception.code, "PATH_LOCK_PATH_OUTSIDE_WORKSPACE")
        finally:
            if outside.exists():
                outside.rmdir()

    def test_stale_recovery_records_previous_owner_expiry_reason_and_audit(self):
        current = [datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)]
        store = PathLockStore(self.channel, root=self.root, clock=lambda: current[0])
        original = store.lock("alice", ["src/main.py"], lease_seconds=1)
        current[0] = datetime(2026, 8, 21, 12, 0, 2, tzinfo=timezone.utc)

        recovered = store.recover(
            original.lock_id,
            "bob",
            "alice session expired",
            lease_seconds=60,
        )

        self.assertEqual(recovered.owner, "bob")
        self.assertEqual(recovered.previous_owner, "alice")
        self.assertEqual(recovered.previous_expires_at, original.expires_at)
        self.assertEqual(recovered.recovery_reason, "alice session expired")
        messages = [
            path.read_text(encoding="utf-8")
            for path in self.channel.glob("[0-9][0-9][0-9][0-9]-*.md")
        ]
        self.assertTrue(
            any(
                "path.lock.recovered" in body
                and "alice session expired" in body
                and '"previous_owner": "alice"' in body
                for body in messages
            )
        )

    def test_owner_only_unlock_and_audit(self):
        record = self.store.lock("alice", ["src/main.py"], lease_seconds=60)

        with self.assertRaises(PathLockError) as error:
            self.store.unlock(record.lock_id, "bob")

        self.assertEqual(error.exception.code, "PATH_LOCK_OWNER_MISMATCH")
        self.store.unlock(record.lock_id, "alice")
        self.assertEqual(self.store.list(), [])
        messages = [
            path.read_text(encoding="utf-8")
            for path in self.channel.glob("[0-9][0-9][0-9][0-9]-*.md")
        ]
        self.assertTrue(any("path.lock.unlocked" in body for body in messages))

    def test_cli_lock_check_unlock_and_recover_commands(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            chat.main(
                [
                    "--root",
                    str(self.root),
                    "lock",
                    "review",
                    "src/main.py",
                    "--as",
                    "alice",
                    "--lease-seconds",
                    "60",
                ]
            )
        lock_id = output.getvalue().strip().splitlines()[-1].split()[1]
        self.assertTrue(lock_id)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            chat.main(
                [
                    "--root",
                    str(self.root),
                    "check",
                    "review",
                    "src/main.py",
                ]
            )
        self.assertIn("locked", output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            chat.main(
                [
                    "--root",
                    str(self.root),
                    "unlock",
                    "review",
                    lock_id,
                    "--as",
                    "alice",
                ]
            )
        self.assertIn("unlocked", output.getvalue())


    def test_cli_recover_requires_reason_and_reports_prior_owner(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            chat.main(
                [
                    "--root",
                    str(self.root),
                    "lock",
                    "review",
                    "src/main.py",
                    "--as",
                    "alice",
                    "--lease-seconds",
                    "60",
                ]
            )
        lock_id = output.getvalue().strip().split()[1]
        lock_path = next((self.channel / "locks").glob("*.json"))
        record = json.loads(lock_path.read_text(encoding="utf-8"))
        record["expires_at"] = "2000-01-01T00:00:00+00:00"
        lock_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            chat.main(
                [
                    "--root",
                    str(self.root),
                    "recover",
                    "review",
                    lock_id,
                    "--as",
                    "bob",
                    "--reason",
                    "alice session expired",
                    "--lease-seconds",
                    "60",
                ]
            )
        self.assertIn("previous_owner=alice", output.getvalue())
        self.assertIn("alice session expired", output.getvalue())

    def test_new_lock_is_published_with_content_atomic_no_overwrite(self):
        with mock.patch.object(path_locks.os, "link", wraps=path_locks.os.link) as publish:
            self.store.lock("alice", ["src/new.py"], lease_seconds=60)
        self.assertTrue(publish.called)

    def test_lock_publish_failure_is_stable_and_leaves_no_record(self):
        with mock.patch.object(path_locks.os, "link", side_effect=OSError("publish failed")):
            with self.assertRaises(PathLockError) as error:
                self.store.lock("alice", ["src/new.py"], lease_seconds=60)
        self.assertEqual(error.exception.code, "PATH_LOCK_STORAGE_ERROR")
        self.assertEqual(self.store.list(), [])

    def test_storage_symlink_swap_is_rejected_before_temp_publish(self):
        outside = self.root.parent / (self.root.name + "-lock-outside")
        outside.mkdir()
        locks_dir = self.channel / "locks"
        real_mkstemp = path_locks.tempfile.mkstemp

        def tamper_mkstemp(*args, **kwargs):
            locks_dir.mkdir(exist_ok=True)
            fd, temporary_name = real_mkstemp(*args, **kwargs)
            os.close(fd)
            Path(temporary_name).unlink(missing_ok=True)
            locks_dir.rmdir()
            try:
                locks_dir.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlink creation unavailable on this platform: {error}")
            return real_mkstemp(*args, **kwargs)
        try:
            with mock.patch.object(path_locks.tempfile, "mkstemp", side_effect=tamper_mkstemp):
                with self.assertRaises(PathLockError) as error:
                    self.store.lock("alice", ["src/new.py"], lease_seconds=60)
            self.assertEqual(error.exception.code, "PATH_LOCK_PATH_OUTSIDE_WORKSPACE")
        finally:
            if locks_dir.is_symlink():
                locks_dir.unlink()
            elif locks_dir.exists():
                for child in locks_dir.iterdir():
                    child.unlink()
                locks_dir.rmdir()
            outside.rmdir()

    def test_contended_mutation_lock_is_not_reclaimed_from_mtime(self):
        first = self.store._acquire_mutation_lock()
        try:
            os.utime(self.store.mutation_lock_path, (0, 0))
            contender = PathLockStore(
                self.channel,
                root=self.root,
                mutation_timeout=0.02,
            )
            with self.assertRaises(PathLockError) as error:
                contender._acquire_mutation_lock()
            self.assertEqual(error.exception.code, "PATH_LOCK_TIMEOUT")
        finally:
            self.store._release_mutation_lock(first)

    def test_windows_aliases_are_rejected_for_missing_paths(self):
        if os.name != "nt":
            self.skipTest("Windows filename alias semantics do not apply on POSIX")
        for requested in ("draft.", "draft ", "CON", "report?"):
            with self.subTest(requested=requested):
                with self.assertRaises(PathLockError) as error:
                    self.store.lock("alice", [requested], lease_seconds=60)
                self.assertEqual(error.exception.code, "PATH_LOCK_INVALID_PATH")

    def test_control_and_surrogate_unicode_are_rejected_at_boundary(self):
        for requested in ("draft" + chr(1), "draft" + chr(0xD800)):
            with self.subTest(requested=requested):
                with self.assertRaises(PathLockError) as error:
                    self.store.lock("alice", [requested], lease_seconds=60)
                self.assertEqual(error.exception.code, "PATH_LOCK_INVALID_PATH")

    def test_interrupted_transaction_leaves_pending_marker_for_explicit_recovery(self):
        record = self.store.lock("alice", ["src/new.py"], lease_seconds=60)
        lock_path = self.channel / "locks" / f"{record.lock_id}.json"
        raw_bytes = lock_path.read_bytes()
        self.store._write_transaction(
            {
                "version": 1,
                "transaction_id": "tx-1234",
                "phase": "applied",
                "operation": "lock",
                "event": "path.locked",
                "target": f"{record.lock_id}.json",
                "before": None,
                "after": self.store._encode_bytes(raw_bytes),
                "actor": "alice",
            }
        )
        pending = self.channel / "locks" / ".path-lock-transaction.json"
        self.assertTrue(pending.exists())
        with self.assertRaises(PathLockError) as blocked:
            self.store.list()
        self.assertEqual(blocked.exception.code, "PATH_LOCK_TRANSACTION_PENDING")

        recovered = PathLockStore(self.channel, root=self.root)
        recovered.recover_pending(actor="recovery")
        self.assertFalse(pending.exists())
        self.assertEqual(recovered.list(), [])

    def test_cleanup_failure_after_audit_keeps_new_state_and_pending_marker(self):
        with mock.patch.object(
            self.store,
            "_remove_transaction",
            side_effect=PathLockError("PATH_LOCK_STORAGE_ERROR", "cleanup failed"),
        ):
            with self.assertRaises(PathLockError) as error:
                self.store.lock("alice", ["src/new2.py"], lease_seconds=60)
        self.assertEqual(error.exception.code, "PATH_LOCK_TRANSACTION_CLEANUP_FAILED")
        self.assertTrue(error.exception.details.get("transaction_pending"))
        self.assertTrue(self.store.transaction_path.exists())

        with self.assertRaises(PathLockError) as blocked:
            self.store.list()
        self.assertEqual(blocked.exception.code, "PATH_LOCK_TRANSACTION_PENDING")

        self.store.recover_pending(actor="recovery", publication_resolution="published")
        self.assertFalse(self.store.transaction_path.exists())
        self.assertEqual(len(self.store.list()), 1)

    def test_rollback_failure_is_stable_and_preserves_pending_transaction(self):
        with mock.patch.object(
            self.store,
            "_post_event",
            side_effect=PathLockError("PATH_LOCK_AUDIT_FAILED", "injected audit failure"),
        ), mock.patch.object(
            self.store,
            "_restore_bytes",
            side_effect=OSError("rollback failed"),
        ):
            with self.assertRaises(PathLockError) as error:
                self.store.lock("alice", ["src/new3.py"], lease_seconds=60)
        self.assertEqual(error.exception.code, "PATH_LOCK_AUDIT_ROLLBACK_FAILED")
        self.assertTrue((self.channel / "locks" / ".path-lock-transaction.json").exists())
    def test_storage_permission_errors_are_mapped_for_cli(self):
        stderr = io.StringIO()
        with mock.patch.object(
            path_locks.tempfile, "mkstemp", side_effect=PermissionError("denied")
        ), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exit_status:
                chat.main(
                    [
                        "--root",
                        str(self.root),
                        "lock",
                        "review",
                        "src/new.py",
                        "--as",
                        "alice",
                    ]
                )
        self.assertEqual(exit_status.exception.code, 1)
        self.assertIn("PATH_LOCK_STORAGE_ERROR", stderr.getvalue())

    def test_persisted_expiry_aliases_must_agree_and_recovery_fields_are_complete(self):
        record = self.store.lock("alice", ["src/new.py"], lease_seconds=60)
        lock_path = self.channel / "locks" / f"{record.lock_id}.json"
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
        raw["lease_expires_at"] = raw["expires_at"]
        lock_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.store.load(record.lock_id).expires_at, raw["expires_at"])

        raw["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        lock_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises(PathLockError) as error:
            self.store.load(record.lock_id)
        self.assertEqual(error.exception.code, "PATH_LOCK_INVALID_RECORD")

        raw.pop("lease_expires_at")
        raw["previous_owner"] = "alice"
        lock_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises(PathLockError) as error:
            self.store.load(record.lock_id)
        self.assertEqual(error.exception.code, "PATH_LOCK_INVALID_RECORD")
    def test_same_byte_publish_collision_does_not_remove_preexisting_record(self):
        record = self.store.lock("alice", ["src/same.py"], lease_seconds=60)
        path = self.channel / "locks" / f"{record.lock_id}.json"
        existing = path.read_bytes()

        with self.assertRaises(PathLockError) as error:
            self.store._run_transaction(
                operation="lock",
                event="path.locked",
                path=path,
                before=None,
                after=existing,
                actor="alice",
                record=record,
                apply=lambda: self.store._write_exclusive(path, record),
            )

        self.assertEqual(error.exception.code, "PATH_LOCK_CONFLICT")
        self.assertEqual(path.read_bytes(), existing)
        self.assertEqual(self.store.load(record.lock_id), record)

    def test_apply_failure_after_target_publish_rolls_back_cleanly(self):
        original_link = path_locks.os.link

        def link_and_raise(src, dst):
            original_link(src, dst)
            raise PathLockError("PATH_LOCK_STORAGE_ERROR", "injected failure after link")

        with mock.patch.object(path_locks.os, "link", side_effect=link_and_raise):
            with self.assertRaises(PathLockError) as error:
                self.store.lock("alice", ["src/new4.py"], lease_seconds=60)
        self.assertEqual(error.exception.code, "PATH_LOCK_STORAGE_ERROR")
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.store.transaction_path.exists())

    def test_apply_failure_after_target_publish_with_rollback_failure_preserves_journal(self):
        original_link = path_locks.os.link

        def link_and_raise(src, dst):
            original_link(src, dst)
            raise PathLockError("PATH_LOCK_STORAGE_ERROR", "injected failure after link")

        with mock.patch.object(
            path_locks.os, "link", side_effect=link_and_raise
        ), mock.patch.object(
            self.store,
            "_restore_bytes",
            side_effect=OSError("rollback failed"),
        ):
            with self.assertRaises(PathLockError) as error:
                self.store.lock("alice", ["src/new5.py"], lease_seconds=60)
        self.assertEqual(error.exception.code, "PATH_LOCK_AUDIT_ROLLBACK_FAILED")
        self.assertTrue(self.store.transaction_path.exists())
        recovered = PathLockStore(self.channel, root=self.root)
        recovered.recover_pending(actor="recovery")
        self.assertFalse(recovered.transaction_path.exists())
        self.assertEqual(recovered.list(), [])
    def test_apply_failure_with_readback_failure_preserves_journal(self):
        original_link = path_locks.os.link

        def link_and_raise(src, dst):
            original_link(src, dst)
            raise PathLockError("PATH_LOCK_STORAGE_ERROR", "injected failure after link")

        original_read_bytes = Path.read_bytes

        def failing_read_bytes(path_obj):
            if path_obj.parent.resolve() == self.store.locks_dir.resolve() and path_obj.suffix == ".json" and not path_obj.name.startswith("."):
                raise OSError("unreadable target during rollback check")
            return original_read_bytes(path_obj)

        with mock.patch.object(
            path_locks.os, "link", side_effect=link_and_raise
        ), mock.patch.object(
            Path, "read_bytes", side_effect=failing_read_bytes
        ):
            with self.assertRaises(PathLockError) as error:
                self.store.lock("alice", ["src/new_unreadable.py"], lease_seconds=60)
        self.assertEqual(error.exception.code, "PATH_LOCK_AUDIT_ROLLBACK_FAILED")
        self.assertTrue(self.store.transaction_path.exists())
        recovered = PathLockStore(self.channel, root=self.root)
        recovered.recover_pending(actor="recovery")
        self.assertFalse(recovered.transaction_path.exists())
        self.assertEqual(recovered.list(), [])
    def test_transient_snapshot_read_failure_keeps_stable_pending_state(self):
        record = self.store.lock("alice", ["src/transient.py"], lease_seconds=60)
        path = self.channel / "locks" / f"{record.lock_id}.json"
        existing = path.read_bytes()
        original_read_bytes = Path.read_bytes
        self.assertTrue(path.exists())
        calls = {"count": 0}

        def transient_read():
            if calls["count"] == 0:
                calls["count"] += 1
                raise OSError("transient snapshot failure")
            return original_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", side_effect=transient_read):
            with self.assertRaises(PathLockError) as error:
                self.store._run_transaction(
                    operation="recover",
                    event="path.lock.recovered",
                    path=path,
                    before=existing,
                    after=existing,
                    actor="alice",
                    record=record,
                    apply=lambda: False,
                )
        self.assertEqual(calls["count"], 1, repr(error.exception))
        self.assertEqual(error.exception.code, "PATH_LOCK_AUDIT_ROLLBACK_FAILED")
        self.assertTrue(self.store.transaction_path.exists())
        self.assertEqual(path.read_bytes(), existing)
        self.store.recover_pending(actor="recovery")
        self.assertFalse(self.store.transaction_path.exists())
        self.assertEqual(self.store.load(record.lock_id), record)

    def test_recovery_reason_rejects_control_and_surrogate_unicode(self):
        record = self.store.lock("alice", ["src/main.py"], lease_seconds=1)
        lock_path = self.channel / "locks" / f"{record.lock_id}.json"
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
        raw["expires_at"] = "2000-01-01T00:00:00+00:00"
        lock_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")

        for bad_reason in ("", "   ", "bad" + chr(1), "bad" + chr(0xD800), "bad\nreason"):
            with self.subTest(bad_reason=bad_reason):
                with self.assertRaises(PathLockError) as error:
                    self.store.recover(record.lock_id, "bob", bad_reason, lease_seconds=60)
                self.assertEqual(error.exception.code, "PATH_LOCK_INVALID_REASON")

    def test_recover_pending_rejects_unsupported_publication_resolution(self):
        record = self.store.lock("alice", ["src/new6.py"], lease_seconds=60)
        lock_path = self.channel / "locks" / f"{record.lock_id}.json"
        raw_bytes = lock_path.read_bytes()
        self.store._write_transaction(
            {
                "version": 1,
                "transaction_id": "tx-bad-res",
                "phase": "applied",
                "operation": "lock",
                "event": "path.locked",
                "target": f"{record.lock_id}.json",
                "before": None,
                "after": self.store._encode_bytes(raw_bytes),
                "actor": "alice",
            }
        )
        with self.assertRaises(PathLockError) as error:
            self.store.recover_pending(actor="recovery", publication_resolution="invalid_resolution")
        self.assertEqual(error.exception.code, "PATH_LOCK_TRANSACTION_INVALID")
        # Clean up
        self.store.recover_pending(actor="recovery", publication_resolution="published")
        self.assertFalse(self.store.transaction_path.exists())

    def test_cli_recover_pending_command(self):
        record = self.store.lock("alice", ["src/new7.py"], lease_seconds=60)
        lock_path = self.channel / "locks" / f"{record.lock_id}.json"
        raw_bytes = lock_path.read_bytes()
        self.store._write_transaction(
            {
                "version": 1,
                "transaction_id": "tx-cli-pending",
                "phase": "applied",
                "operation": "lock",
                "event": "path.locked",
                "target": f"{record.lock_id}.json",
                "before": None,
                "after": self.store._encode_bytes(raw_bytes),
                "actor": "alice",
            }
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exit_status:
                chat.main(
                    [
                        "--root",
                        str(self.root),
                        "check",
                        "review",
                        "src/new7.py",
                    ]
                )
        self.assertEqual(exit_status.exception.code, 1)
        self.assertIn("PATH_LOCK_TRANSACTION_PENDING", stderr.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            chat.main(
                [
                    "--root",
                    str(self.root),
                    "recover-pending",
                    "review",
                    "--as",
                    "recovery",
                    "--resolve-publication",
                    "published",
                ]
            )
        self.assertIn("recovered pending path-lock transaction in review", output.getvalue())
        self.assertFalse(self.store.transaction_path.exists())

        check_output = io.StringIO()
        with contextlib.redirect_stdout(check_output):
            chat.main(
                [
                    "--root",
                    str(self.root),
                    "check",
                    "review",
                    "src/new7.py",
                ]
            )
        self.assertIn("locked", check_output.getvalue())
    def test_create_collision_does_not_unlink_existing_record(self):
        first = self.store.lock("alice", ["src/main.py"], lease_seconds=60)
        lock_path = self.channel / "locks" / f"{first.lock_id}.json"
        original_bytes = lock_path.read_bytes()

        with mock.patch("uuid.uuid4", return_value=SimpleNamespace(ephemeral=None, hex=first.lock_id)):
            with self.assertRaises(PathLockError) as error:
                self.store.lock("bob", ["src/different.py"], lease_seconds=60)
            self.assertEqual(error.exception.code, "PATH_LOCK_CONFLICT")

        self.assertTrue(lock_path.exists())
        self.assertEqual(lock_path.read_bytes(), original_bytes)
        self.assertEqual(self.store.load(first.lock_id).owner, "alice")

if __name__ == "__main__":
    unittest.main()
