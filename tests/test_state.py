from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_mcp_guard.state import StateStore, default_state_dir, empty_state


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
