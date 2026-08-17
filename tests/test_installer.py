from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-user-hooks.py"
SPEC = importlib.util.spec_from_file_location("install_user_hooks", SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_environment_marker_survives_a_renamed_checkout(self) -> None:
        handler = {
            "type": "command",
            "command": (
                f"{installer.ENV_MARKER}=1 /tmp/renamed-project/scripts/hook.py"
            ),
        }
        self.assertTrue(installer.is_guard_handler(handler))

    def test_legacy_env_marker_remains_removable(self) -> None:
        handler = {
            "type": "command",
            "command": "/tmp/renamed-project/scripts/hook.py",
            "env": {installer.ENV_MARKER: "1"},
        }
        self.assertTrue(installer.is_guard_handler(handler))

    def test_similar_marker_text_does_not_match(self) -> None:
        handler = {
            "type": "command",
            "command": "echo CODEX_MCP_GUARD_HOOK=10",
        }
        self.assertFalse(installer.is_guard_handler(handler))

    def test_install_is_idempotent_and_preserves_other_hooks(self) -> None:
        data = {
            "hooks": {
                "SubagentStart": [
                    {"hooks": [{"type": "command", "command": "python other.py"}]}
                ]
            }
        }
        installer.install_guard_handlers(data)
        installer.install_guard_handlers(data)

        groups = data["hooks"]["SubagentStart"]
        guard_handlers = [
            handler
            for group in groups
            for handler in group["hooks"]
            if installer.is_guard_handler(handler)
        ]
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(guard_handlers), 1)
        self.assertEqual(
            data["hooks"]["SubagentStop"][0]["hooks"][0]["type"], "command"
        )
        self.assertNotIn("env", data["hooks"]["SubagentStop"][0]["hooks"][0])

    def test_uninstall_removes_only_guard_handlers(self) -> None:
        data = {"hooks": {}}
        installer.install_guard_handlers(data)
        data["hooks"]["SubagentStop"].append(
            {"hooks": [{"type": "command", "command": "python keep.py"}]}
        )
        installer.remove_guard_handlers(data)
        self.assertNotIn("SubagentStart", data["hooks"])
        self.assertEqual(
            data["hooks"]["SubagentStop"],
            [{"hooks": [{"type": "command", "command": "python keep.py"}]}],
        )


if __name__ == "__main__":
    unittest.main()
