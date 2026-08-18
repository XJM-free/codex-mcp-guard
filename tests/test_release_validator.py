from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate-release.py"
SPEC = importlib.util.spec_from_file_location("validate_release", SCRIPT)
validator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(validator)


class ReleaseValidatorTests(unittest.TestCase):
    def test_semver_build_metadata_is_valid_for_local_cachebusters(self) -> None:
        self.assertIsNotNone(
            validator.VERSION_RE.fullmatch("0.1.0-alpha.2+codex.local-20260818-120000")
        )

    def test_ast_audit_detects_process_control_without_string_heuristics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unsafe.py"
            path.write_text(
                "import os\nimport subprocess\nos.kill(1, 9)\n"
                "subprocess.run(['taskkill', '/PID', '1'])\n",
                encoding="utf-8",
            )

            violations = validator._audit_only_violations(path)

            self.assertTrue(any("os.kill" in item for item in violations))
            self.assertTrue(
                any("process-control command" in item for item in violations)
            )

    def test_ast_audit_allows_fixed_read_only_process_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "safe.py"
            path.write_text(
                "import subprocess\nsubprocess.run(['ps', '-axo', 'pid='], check=True)\n",
                encoding="utf-8",
            )

            self.assertEqual(validator._audit_only_violations(path), [])

    def test_ast_audit_allows_only_the_owned_inventory_child_kill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            allowed = Path(temporary) / "processes.py"
            allowed.write_text("inventory_child.kill()\n", encoding="utf-8")
            denied = Path(temporary) / "other.py"
            denied.write_text("candidate.kill()\n", encoding="utf-8")

            self.assertEqual(validator._audit_only_violations(allowed), [])
            self.assertTrue(
                any(
                    "candidate.kill" in item
                    for item in validator._audit_only_violations(denied)
                )
            )


if __name__ == "__main__":
    unittest.main()
