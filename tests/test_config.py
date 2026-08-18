from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from codex_mcp_guard.config import load_config


class ConfigTests(unittest.TestCase):
    def test_legacy_enforce_setting_cannot_enable_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.json"
            path.write_text(json.dumps({"mode": "enforce"}), encoding="utf-8")
            if os.name != "nt":
                path.chmod(0o600)
            config = load_config(root)
            self.assertFalse(hasattr(config, "mode"))

    def test_boolean_is_not_accepted_as_a_numeric_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.json"
            path.write_text('{"cohort_window_seconds":true}\n', encoding="utf-8")
            if os.name != "nt":
                path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "finite number"):
                load_config(root)

    def test_symlinked_config_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text("{}\n", encoding="utf-8")
            path = root / "config.json"
            try:
                path.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symlinks unavailable: {error}")
            with self.assertRaisesRegex(ValueError, "non-symlink"):
                load_config(root)

    def test_group_readable_config_is_rejected_on_posix(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX modes are not available on Windows")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.json"
            path.write_text("{}\n", encoding="utf-8")
            path.chmod(0o640)
            with self.assertRaisesRegex(ValueError, "0600"):
                load_config(root)


if __name__ == "__main__":
    unittest.main()
