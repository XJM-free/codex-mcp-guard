from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from codex_mcp_guard.cli import MAX_HOOK_BYTES, main
from codex_mcp_guard.guard import _event_reference_time


class CliTests(unittest.TestCase):
    def test_oversized_hook_input_is_rejected(self) -> None:
        error_output = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO("x" * (MAX_HOOK_BYTES + 1))),
            redirect_stderr(error_output),
        ):
            result = main(["hook"])
        self.assertEqual(result, 1)
        self.assertIn("exceeds", error_output.getvalue())

    def test_transcript_symlink_is_not_used_as_a_time_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.jsonl"
            target.write_text("{}\n", encoding="utf-8")
            link = root / "link.jsonl"
            try:
                link.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symlinks unavailable: {error}")
            reference, source = _event_reference_time(
                {"transcript_path": str(link)}, 1000.0
            )
        self.assertEqual(reference, 1000.0)
        self.assertEqual(source, "hook-time")


if __name__ == "__main__":
    unittest.main()
