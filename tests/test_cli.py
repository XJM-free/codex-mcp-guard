from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from codex_mcp_guard.cli import (
    MAX_HOOK_BYTES,
    doctor_report,
    main,
    redact_doctor_report,
    status_summary,
)
from codex_mcp_guard.models import ProcessInfo
from codex_mcp_guard.processes import ProcessBackend


class FakeBackend(ProcessBackend):
    def __init__(self, processes: dict[int, ProcessInfo]) -> None:
        self.processes = processes

    def snapshot(self) -> dict[int, ProcessInfo]:
        return dict(self.processes)


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

    def test_symlinked_hook_input_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.jsonl"
            target.write_text("{}\n", encoding="utf-8")
            link = root / "link.jsonl"
            try:
                link.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symlinks unavailable: {error}")
            error_output = io.StringIO()
            with redirect_stderr(error_output):
                result = main(["hook", "--input", str(link)])
        self.assertEqual(result, 1)
        self.assertIn("non-symlink", error_output.getvalue())

    def test_status_summary_is_redacted_and_can_revalidate(self) -> None:
        process = ProcessInfo(300, 100, 300, 20, "node /srv/mcp-server.js", 4096)
        state = {
            "version": 2,
            "agents": {
                "secret-key": {
                    "session_id": "secret-session",
                    "agent_id": "secret-agent",
                    "status": "retained-candidate",
                    "live_process_count": 1,
                    "live_group_rss_bytes": 4096,
                    "processes": [process.identity("mcp")],
                },
                "malformed": {
                    "status": "secret-status-value",
                    "evidence_grade": "secret-evidence-value",
                },
            },
            "history": [{"at": 25.0, "event": "stop-audit"}],
        }

        report = status_summary(
            state,
            backend=FakeBackend({300: process}),
            ledger_present=True,
            clock=lambda: 30.0,
        )
        rendered = json.dumps(report)

        self.assertNotIn("secret-session", rendered)
        self.assertNotIn("secret-agent", rendered)
        self.assertNotIn("secret-status-value", rendered)
        self.assertNotIn("secret-evidence-value", rendered)
        self.assertNotIn('"pid"', rendered)
        self.assertEqual(report["revalidation"]["still_matching_count"], 1)

    def test_doctor_report_includes_totals_age_and_group_rss(self) -> None:
        host = ProcessInfo(100, 1, 100, 10, "/usr/bin/codex", 1024)
        root = ProcessInfo(300, 100, 300, 20, "node /srv/mcp-server.js", 2048)
        child = ProcessInfo(301, 300, 300, 21, "browser child", 4096)

        report = doctor_report(
            FakeBackend({100: host, 300: root, 301: child}),
            clock=lambda: 30.0,
        )

        self.assertEqual(report["totals"]["helper_root_count"], 1)
        self.assertEqual(report["totals"]["group_rss_bytes"], 6144)
        helper = report["codex_hosts"][0]["cohorts"][0]["processes"][0]
        self.assertEqual(helper["age_seconds"], 10.0)
        self.assertEqual(helper["group_rss_bytes"], 6144)

        redacted = redact_doctor_report(report)
        redacted_json = json.dumps(redacted)
        self.assertNotIn('"pid"', redacted_json)
        self.assertEqual(redacted["hosts"][0]["cohorts"][0]["kind_counts"], {"mcp": 1})

    def test_unknown_rss_is_not_reported_as_zero_or_a_partial_sum(self) -> None:
        host = ProcessInfo(100, 1, 100, 10, "/usr/bin/codex", 1024)
        root = ProcessInfo(300, 100, 300, 20, "node /srv/mcp-server.js", 2048)
        child = ProcessInfo(301, 300, 300, 21, "browser child", None)

        report = doctor_report(
            FakeBackend({100: host, 300: root, 301: child}), clock=lambda: 30.0
        )

        self.assertIsNone(report["totals"]["group_rss_bytes"])
        self.assertEqual(report["totals"]["rss_known_root_count"], 0)
        redacted = redact_doctor_report(report)
        self.assertIsNone(redacted["hosts"][0]["group_rss_bytes"])

        status = status_summary(
            {
                "version": 2,
                "agents": {
                    "record": {
                        "status": "retained-candidate",
                        "live_process_count": 1,
                        "live_group_rss_bytes": None,
                        "processes": [],
                    }
                },
                "history": [],
            },
            ledger_present=True,
        )
        self.assertIsNone(status["recorded_live_group_rss_bytes"])
        self.assertEqual(status["recorded_rss_known_record_count"], 0)


if __name__ == "__main__":
    unittest.main()
