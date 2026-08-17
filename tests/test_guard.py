from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_mcp_guard.config import GuardConfig
from codex_mcp_guard.guard import Guard, _agent_key
from codex_mcp_guard.models import ProcessInfo
from codex_mcp_guard.processes import ProcessBackend
from codex_mcp_guard.state import StateStore


def process(pid: int, ppid: int, started_at: float, command: str) -> ProcessInfo:
    return ProcessInfo(pid, ppid, pid, started_at, command)


class FakeBackend(ProcessBackend):
    def __init__(self, snapshot: dict[int, ProcessInfo]) -> None:
        self.processes = snapshot

    def snapshot(self) -> dict[int, ProcessInfo]:
        return dict(self.processes)


def base_snapshot(duplicate: bool = False) -> dict[int, ProcessInfo]:
    second_command = (
        "/opt/xcodebuildmcp/node cli.js mcp"
        if duplicate
        else "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node_repl"
    )
    return {
        100: process(100, 1, 900, "/usr/local/bin/codex resume root"),
        200: process(
            200, 100, 1004, "/bin/zsh -c python codex-mcp-guard/scripts/hook.py"
        ),
        201: process(201, 200, 1004, "python codex-mcp-guard/scripts/hook.py"),
        290: process(290, 100, 990, "/opt/xcodebuildmcp/node cli.js mcp"),
        291: process(291, 100, 990, second_command),
        300: process(300, 100, 1001, "/opt/xcodebuildmcp/node cli.js mcp"),
        301: process(301, 100, 1001, second_command),
    }


def event(
    name: str, *, session_id: str = "session-1", agent_id: str = "agent-1"
) -> dict[str, object]:
    return {
        "hook_event_name": name,
        "session_id": session_id,
        "agent_id": agent_id,
        "agent_type": "explorer",
        "transcript_path": "/not/used/in/test.jsonl",
    }


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_guard(self, backend: FakeBackend) -> Guard:
        return Guard(
            backend=backend,
            store=self.store,
            config=GuardConfig(),
            clock=lambda: 1005.0,
        )

    def start(self, guard: Guard, hook_event: dict[str, object] | None = None):
        with patch(
            "codex_mcp_guard.guard._event_reference_time",
            return_value=(1001.0, "transcript-birthtime"),
        ):
            return guard.handle_event(
                hook_event or event("SubagentStart"), hook_pid=201
            )

    def test_correlated_candidate_is_reported_retained_without_signal_api(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)

        started = self.start(guard)
        stopped = guard.handle_event(event("SubagentStop"), hook_pid=201)

        self.assertEqual(started.outcome, "candidate-recorded")
        self.assertEqual(stopped.outcome, "retained-candidate")
        self.assertEqual(stopped.process_count, 2)
        self.assertIn("no signal sent", stopped.detail)
        self.assertFalse(hasattr(backend, "terminate"))
        self.assertFalse((self.store.root / "events.jsonl").exists())
        persisted = self.store.path.read_text(encoding="utf-8")
        self.assertNotIn("/opt/xcodebuildmcp/node", persisted)
        self.assertNotIn("node_repl", persisted)

    def test_candidate_that_exited_before_stop_is_recorded_as_exited(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)
        self.start(guard)
        backend.processes.pop(300)
        backend.processes.pop(301)

        stopped = guard.handle_event(event("SubagentStop"), hook_pid=201)

        self.assertEqual(stopped.outcome, "candidate-exited")
        self.assertEqual(stopped.process_count, 0)

    def test_preexisting_cohort_before_transcript_never_becomes_candidate(self) -> None:
        snapshot = base_snapshot()
        snapshot[290] = process(290, 100, 700, snapshot[290].command)
        snapshot[291] = process(291, 100, 700, snapshot[291].command)
        snapshot[300] = process(300, 100, 900, snapshot[300].command)
        snapshot[301] = process(301, 100, 900, snapshot[301].command)
        backend = FakeBackend(snapshot)
        guard = self.make_guard(backend)
        with patch(
            "codex_mcp_guard.guard._event_reference_time",
            return_value=(1000.0, "transcript-birthtime"),
        ):
            started = guard.handle_event(event("SubagentStart"), hook_pid=201)

        self.assertEqual(started.outcome, "report-only")
        self.assertEqual(started.process_count, 0)

    def test_duplicate_signatures_remain_report_only(self) -> None:
        backend = FakeBackend(base_snapshot(duplicate=True))
        guard = self.make_guard(backend)

        started = self.start(guard)
        stopped = guard.handle_event(event("SubagentStop"), hook_pid=201)

        self.assertEqual(started.outcome, "report-only")
        self.assertEqual(stopped.outcome, "report-only")

    def test_cohort_without_older_fingerprint_match_remains_report_only(self) -> None:
        snapshot = base_snapshot()
        snapshot.pop(290)
        snapshot.pop(291)
        backend = FakeBackend(snapshot)
        guard = self.make_guard(backend)

        started = self.start(guard)
        stopped = guard.handle_event(event("SubagentStop"), hook_pid=201)

        self.assertEqual(started.outcome, "report-only")
        self.assertIn("older helper cohort", started.detail)
        self.assertEqual(stopped.outcome, "report-only")

    def test_pid_reuse_is_reported_and_never_acted_on(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)
        self.start(guard)
        backend.processes[300] = process(
            300, 100, 1002, "/opt/xcodebuildmcp/node cli.js mcp"
        )

        stopped = guard.handle_event(event("SubagentStop"), hook_pid=201)

        self.assertEqual(stopped.outcome, "skipped")
        self.assertIn("PID was reused", stopped.detail)

    def test_concurrent_agent_cannot_claim_the_same_candidate(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)

        first = self.start(guard)
        second = self.start(
            guard,
            event("SubagentStart", session_id="session-1", agent_id="agent-2"),
        )

        self.assertEqual(first.outcome, "candidate-recorded")
        self.assertEqual(second.outcome, "report-only")

    def test_composite_key_is_not_vulnerable_to_colon_collisions(self) -> None:
        self.assertNotEqual(_agent_key("a:b", "c"), _agent_key("a", "b:c"))

    def test_identifier_length_is_bounded(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)
        oversized = event("SubagentStart", agent_id="a" * 257)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            guard.handle_event(oversized, hook_pid=201)


if __name__ == "__main__":
    unittest.main()
