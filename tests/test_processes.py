from __future__ import annotations

import os
import unittest

from codex_mcp_guard.models import ProcessInfo
from codex_mcp_guard.processes import (
    candidate_roots,
    classify_mcp_root,
    cluster_processes,
    find_codex_host,
    is_codex_host,
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

    def test_verify_identity_detects_one_second_pid_reuse(self) -> None:
        original = process(300, 100, 20, "/opt/xcodebuildmcp/cli")
        reused = process(300, 100, 21, "/opt/xcodebuildmcp/cli")
        identity = original.identity("mcp")
        self.assertEqual(verify_identity(original, identity), None)
        self.assertEqual(verify_identity(reused, identity), "PID was reused")


if __name__ == "__main__":
    unittest.main()
