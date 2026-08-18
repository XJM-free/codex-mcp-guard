from __future__ import annotations

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_mcp_guard.state import (
    MAX_STATE_BYTES,
    STATE_VERSION,
    StateStore,
    default_state_dir,
    empty_state,
)


class StateTests(unittest.TestCase):
    def test_plugin_and_shell_commands_share_the_home_state_default(self) -> None:
        expected_home = Path("/private/example-home")
        with (
            patch.dict(os.environ, {"PLUGIN_DATA": "/private/plugin-data"}, clear=True),
            patch.object(Path, "home", return_value=expected_home),
        ):
            self.assertEqual(default_state_dir(), expected_home / ".codex-mcp-guard")

    def test_relative_state_directory_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute"):
            StateStore(Path("relative-state"))

    def test_state_files_are_private_on_posix(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX modes are not available on Windows")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            store = StateStore(root)
            with store.locked() as state:
                state["history"].append({"event": "test"})

            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.lock_path.stat().st_mode), 0o600)

    def test_symlinked_state_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "target"
            target.mkdir(mode=0o700)
            link = base / "link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlinks unavailable: {error}")
            store = StateStore(link)
            with self.assertRaisesRegex(ValueError, "non-symlink"), store.locked():
                pass

    def test_state_record_count_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary))
            state = empty_state()
            state["agents"] = {str(index): {} for index in range(550)}
            store.write(state)
            self.assertEqual(len(store.read()["agents"]), 500)

    def test_pruning_preserves_active_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary))
            state = empty_state()
            state["agents"]["active"] = {
                "status": "observing",
                "started_event_at": 1.0,
                "baseline_processes": [],
            }
            for index in range(550):
                state["agents"][f"terminal-{index}"] = {
                    "status": "completed-report-only",
                    "stopped_event_at": float(index),
                    "processes": [],
                }

            store.write(state)
            restored = store.read()["agents"]

            self.assertEqual(len(restored), 500)
            self.assertIn("active", restored)
            self.assertIn("terminal-549", restored)
            self.assertNotIn("terminal-0", restored)

    def test_oversized_write_is_rejected_without_replacing_last_good_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary))
            good = empty_state()
            good["history"].append({"event": "good"})
            store.write(good)
            original = store.path.read_bytes()
            oversized = empty_state()
            oversized["agents"]["large"] = {
                "status": "completed-report-only",
                "detail": "x" * MAX_STATE_BYTES,
                "processes": [],
            }

            with self.assertRaisesRegex(ValueError, "would exceed"):
                store.write(oversized)

            self.assertEqual(store.path.read_bytes(), original)
            self.assertEqual(store.read()["history"][0]["event"], "good")

    def test_byte_budget_prunes_terminal_records_before_active_observation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary))
            state = empty_state()
            state["agents"]["active"] = {
                "status": "starting",
                "started_event_at": 10.0,
                "baseline_processes": [],
                "processes": [],
            }
            for index in range(4):
                state["agents"][f"terminal-{index}"] = {
                    "status": "completed-report-only",
                    "stopped_event_at": float(index),
                    "detail": "x" * 600_000,
                    "processes": [],
                }

            store.write(state)
            restored = store.read()["agents"]

            self.assertIn("active", restored)
            self.assertLess(store.path.stat().st_size, MAX_STATE_BYTES)
            self.assertLess(len(restored), 5)

    def test_v1_evidence_is_migrated_to_report_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            if os.name != "nt":
                root.chmod(0o700)
            path = root / "state.json"
            path.write_text(
                '{"version":1,"agents":{"legacy":{"status":"retained-candidate",'
                '"processes":[{"pid":1}]}},"history":[]}\n',
                encoding="utf-8",
            )
            if os.name != "nt":
                path.chmod(0o600)

            migrated = StateStore(root).read()

            self.assertEqual(migrated["version"], STATE_VERSION)
            self.assertEqual(
                migrated["agents"]["legacy"]["status"], "legacy-report-only"
            )
            self.assertEqual(migrated["agents"]["legacy"]["processes"], [])
            self.assertEqual(migrated["history"][-1]["at"], 0.0)

    def test_malformed_nested_record_is_dropped_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            if os.name != "nt":
                root.chmod(0o700)
            path = root / "state.json"
            path.write_text(
                '{"version":2,"agents":{"bad":[]},"history":[]}\n',
                encoding="utf-8",
            )
            if os.name != "nt":
                path.chmod(0o600)

            restored = StateStore(root).read()

            self.assertEqual(restored["agents"], {})
            self.assertEqual(restored["history"][-1]["event"], "state-repair")

    def test_incomplete_terminal_identity_array_is_downgraded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary))
            state = empty_state()
            state["agents"]["record"] = {
                "status": "retained-candidate",
                "evidence_grade": "window-delta",
                "live_process_count": 257,
                "processes": [{"pid": index} for index in range(257)],
            }

            store.write(state)
            record = store.read()["agents"]["record"]

            self.assertEqual(record["status"], "completed-report-only")
            self.assertEqual(record["evidence_grade"], "invalid")
            self.assertEqual(record["processes"], [])
            self.assertEqual(record["live_process_count"], 0)

    def test_state_lock_has_an_internal_deadline_on_posix(self) -> None:
        if os.name == "nt":
            self.skipTest("fcntl is POSIX-only")
        import fcntl

        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary))
            store.root.chmod(0o700)
            with store.lock_path.open("a+b") as holder:
                holder.flush()
                holder_fd = holder.fileno()
                os.fchmod(holder_fd, 0o600)
                fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                started = time.monotonic()
                with (
                    patch("codex_mcp_guard.state.LOCK_TIMEOUT_SECONDS", 0.05),
                    self.assertRaisesRegex(TimeoutError, "timed out"),
                    store.locked(),
                ):
                    pass
                self.assertLess(time.monotonic() - started, 0.5)

    def test_symlinked_state_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "state"
            root.mkdir(mode=0o700)
            target = base / "target.json"
            target.write_text('{"version": 1, "agents": {}, "history": []}\n')
            path = root / "state.json"
            try:
                path.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symlinks unavailable: {error}")
            store = StateStore(root)
            with self.assertRaisesRegex(ValueError, "symlinked"):
                store.read()


if __name__ == "__main__":
    unittest.main()
