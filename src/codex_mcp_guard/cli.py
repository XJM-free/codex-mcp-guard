from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .guard import Guard
from .processes import (
    ProcessBackend,
    SystemProcessBackend,
    candidate_roots,
    cluster_processes,
    is_codex_host,
)
from .state import StateStore

MAX_HOOK_BYTES = 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-mcp-guard",
        description="Audit MCP helper candidates around Codex subagent lifecycle events.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--state-dir", type=Path, help="override the guard state directory"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    hook = subparsers.add_parser("hook", help=argparse.SUPPRESS)
    hook.add_argument(
        "--input", type=Path, help="read hook JSON from a file instead of stdin"
    )

    subparsers.add_parser(
        "status", help="show recorded lifecycle correlations and recent outcomes"
    )
    doctor = subparsers.add_parser(
        "doctor", help="inspect current Codex MCP helper cohorts without changing them"
    )
    doctor.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store = StateStore(args.state_dir) if args.state_dir else StateStore()
        if args.command == "hook":
            raw = _read_hook_input(args.input)
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("hook input must contain a JSON object")
            Guard(store=store).handle_event(event)
            return 0
        if args.command == "status":
            print(json.dumps(store.read(), indent=2, sort_keys=True))
            return 0
        if args.command == "doctor":
            report = doctor_report(SystemProcessBackend())
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                _print_doctor(report)
            return 0
    except (
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        OSError,
    ) as error:
        print(f"codex-mcp-guard: {error}", file=sys.stderr)
        return 1
    return 2


def _read_hook_input(path: Path | None) -> str:
    if path is None:
        raw = sys.stdin.read(MAX_HOOK_BYTES + 1)
    else:
        file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("hook input must be a regular, non-symlink file")
        if file_stat.st_size > MAX_HOOK_BYTES:
            raise ValueError(f"hook input exceeds {MAX_HOOK_BYTES} bytes")
        raw = path.read_text(encoding="utf-8")
    if len(raw.encode("utf-8")) > MAX_HOOK_BYTES:
        raise ValueError(f"hook input exceeds {MAX_HOOK_BYTES} bytes")
    return raw


def doctor_report(backend: ProcessBackend) -> dict[str, object]:
    snapshot = backend.snapshot()
    hosts: list[dict[str, Any]] = []
    for host in sorted(
        (process for process in snapshot.values() if is_codex_host(process)),
        key=lambda process: process.pid,
    ):
        classified = candidate_roots(snapshot, host.pid)
        cohorts = cluster_processes(
            [process for process, _ in classified], window_seconds=2.0
        )
        if not classified:
            continue
        kind_by_pid = {process.pid: kind for process, kind in classified}
        hosts.append(
            {
                "pid": host.pid,
                "started_at": host.started_at,
                "helper_count": len(classified),
                "cohorts": [
                    {
                        "started_at": cohort.started_at,
                        "processes": [
                            {"pid": process.pid, "kind": kind_by_pid[process.pid]}
                            for process in cohort.processes
                        ],
                    }
                    for cohort in cohorts
                ],
            }
        )
    return {
        "mode": "audit-only",
        "ownership": "unproven",
        "codex_hosts": hosts,
    }


def _print_doctor(report: dict[str, object]) -> None:
    hosts = report["codex_hosts"]
    if not hosts:
        print("No Codex child processes matched the MCP helper classifier.")
        return
    print("Read-only inventory; listed processes are candidates, not proven ownership.")
    for host in hosts:
        print(
            f"Codex PID {host['pid']}: {host['helper_count']} candidate helper roots "
            f"in {len(host['cohorts'])} cohort(s)"
        )
        for cohort in host["cohorts"]:
            details = ", ".join(
                f"{process['pid']}:{process['kind']}" for process in cohort["processes"]
            )
            print(f"  {int(cohort['started_at'])}: {details}")


if __name__ == "__main__":
    raise SystemExit(main())
