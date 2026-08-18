from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from codex_mcp_guard.models import ProcessInfo
from codex_mcp_guard.processes import (
    _parse_unix_started_at,
    _run_capped_snapshot,
    candidate_roots,
    classify_mcp_root,
    cluster_processes,
    find_codex_host,
    is_codex_host,
    process_group_rss_bytes,
    verify_identity,
)


def process(
    pid: int,
    ppid: int,
    started_at: float,
    command: str,
    pgid: int | None = None,
) -> ProcessInfo:
    return ProcessInfo(pid, ppid, pid if pgid is None else pgid, started_at, command)


class ProcessTests(unittest.TestCase):
    def test_finds_nearest_codex_ancestor(self) -> None:
        snapshot = {
            100: process(100, 1, 10, "/usr/local/bin/codex resume abc"),
            200: process(200, 100, 20, "/bin/zsh -c python hook.py"),
            201: process(201, 200, 21, "python hook.py"),
        }
        host = find_codex_host(snapshot, 201)
        self.assertIsNotNone(host)
        self.assertEqual(host.pid, 100)

    def test_codex_host_requires_the_actual_executable(self) -> None:
        self.assertTrue(is_codex_host(process(1, 0, 0, "/usr/bin/codex app-server")))
        self.assertFalse(is_codex_host(process(1, 0, 0, "python tool.py --tag codex")))
        self.assertFalse(is_codex_host(process(1, 0, 0, "/tmp/codex-code-mode-host")))

    def test_classifier_uses_executable_script_or_module_positions(self) -> None:
        cases = {
            "/opt/xcodebuildmcp/libexec/node cli.js mcp": "mcp",
            "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node_repl": "node-repl",
            "node /repo/zstack-dev-mcp/start.mjs": "mcp",
            "python -m mcp_server": "mcp",
            "npx -y @modelcontextprotocol/server-filesystem": "mcp",
            "python /repo/codex-mcp-guard/scripts/hook.py": None,
            "/bin/zsh -c npx some-mcp": None,
            "python /repo/tools/run.py --tag release-mcp": None,
            "node app.js --profile team-mcp": None,
            "node --require /tmp/mcp-bootstrap.js /srv/plain-app.js": None,
            "python -X dev /srv/mcp_server.py": "mcp",
            "npx --cache /tmp/mcp-cache eslint": None,
            "npx --package @modelcontextprotocol/server-filesystem launcher": "mcp",
            "python /tmp/mcp-notes.txt": None,
            "/tmp/node_repl": None,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(classify_mcp_root(process(1, 0, 0, command)), expected)

    def test_candidate_roots_are_direct_children(self) -> None:
        snapshot = {
            100: process(100, 1, 10, "/usr/local/bin/codex"),
            300: process(300, 100, 20, "/opt/xcodebuildmcp/cli"),
            301: process(301, 300, 20, "node child-mcp"),
            302: process(302, 100, 20, "node another-mcp", pgid=300),
        }
        expected = [300, 302] if os.name == "nt" else [300]
        self.assertEqual(
            [item[0].pid for item in candidate_roots(snapshot, 100)], expected
        )

    def test_cluster_processes_uses_a_bounded_span(self) -> None:
        cohorts = cluster_processes(
            [
                process(1, 100, 10.0, "mcp-a"),
                process(2, 100, 11.5, "mcp-b"),
                process(3, 100, 13.0, "mcp-c"),
            ],
            2.0,
        )
        self.assertEqual([[1, 2], [3]], [[p.pid for p in c.processes] for c in cohorts])
        self.assertNotIn("command", cohorts[0].as_dict()["processes"][0])
        self.assertIn("command_sha256", cohorts[0].as_dict()["processes"][0])

    def test_verify_identity_detects_one_second_pid_reuse(self) -> None:
        original = process(300, 100, 20, "/opt/xcodebuildmcp/cli")
        reused = process(300, 100, 21, "/opt/xcodebuildmcp/cli")
        identity = original.identity("mcp")
        self.assertEqual(verify_identity(original, identity), None)
        self.assertEqual(verify_identity(reused, identity), "PID was reused")

    def test_group_rss_includes_descendants_in_the_same_process_group(self) -> None:
        root = ProcessInfo(300, 100, 300, 20, "node mcp-server.js", 1024)
        child = ProcessInfo(301, 300, 300, 21, "browser child", 2048)
        unrelated = ProcessInfo(302, 100, 302, 21, "other", 4096)
        snapshot = {item.pid: item for item in (root, child, unrelated)}

        self.assertEqual(process_group_rss_bytes(snapshot, root), 3072)

    def test_group_rss_is_unknown_when_any_member_is_unknown(self) -> None:
        root = ProcessInfo(300, 100, 300, 20, "node mcp-server.js", 1024)
        child = ProcessInfo(301, 300, 300, 21, "browser child", None)
        snapshot = {item.pid: item for item in (root, child)}

        self.assertIsNone(process_group_rss_bytes(snapshot, root))

    def test_unix_snapshot_has_an_internal_timeout(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_capped_snapshot(
                [sys.executable, "-c", "import time; time.sleep(1)"],
                timeout=0.05,
                max_stdout_bytes=1024,
            )

    def test_snapshot_output_is_capped_while_the_child_is_running(self) -> None:
        with self.assertRaisesRegex(ValueError, "snapshot exceeds"):
            _run_capped_snapshot(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write('x' * 65536); sys.stdout.flush()",
                ],
                timeout=1,
                max_stdout_bytes=1024,
            )

    def test_unix_start_time_uses_historical_dst_offset(self) -> None:
        if not hasattr(time, "tzset"):
            self.skipTest("tzset is unavailable")
        try:
            with patch.dict(os.environ, {"TZ": "America/New_York"}):
                time.tzset()
                parsed = _parse_unix_started_at("Thu Jan 15 12:00:00 2026")
                expected = datetime(
                    2026, 1, 15, 12, tzinfo=ZoneInfo("America/New_York")
                ).timestamp()
        finally:
            time.tzset()
        self.assertEqual(parsed, expected)


if __name__ == "__main__":
    unittest.main()
