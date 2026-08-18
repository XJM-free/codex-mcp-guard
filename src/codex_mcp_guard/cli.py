from __future__ import annotations

import argparse
import json
import math
import os
import platform
import stat
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import load_config
from .guard import Guard
from .processes import (
    ProcessBackend,
    SystemProcessBackend,
    candidate_roots,
    cluster_processes,
    is_codex_host,
    process_group_rss_bytes,
    verify_identity,
)
from .state import StateStore

MAX_HOOK_BYTES = 1024 * 1024
SAFE_STATUSES = {
    "abandoned",
    "completed-report-only",
    "legacy-report-only",
    "observing",
    "retained-candidate",
    "starting",
    "verification-failed",
}
SAFE_EVIDENCE_GRADES = {
    "ambiguous",
    "baseline-only",
    "invalid",
    "none",
    "retired",
    "window-delta",
}


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

    status = subparsers.add_parser(
        "status", help="show recorded lifecycle correlations and recent outcomes"
    )
    status.add_argument(
        "--summary", action="store_true", help="show a redacted aggregate summary"
    )
    status.add_argument(
        "--revalidate",
        action="store_true",
        help="recheck retained identities in a fresh read-only snapshot",
    )
    status.add_argument(
        "--json", action="store_true", help="emit a machine-readable summary"
    )
    doctor = subparsers.add_parser(
        "doctor", help="inspect current Codex MCP helper cohorts without changing them"
    )
    doctor.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    doctor.add_argument(
        "--summary", action="store_true", help="omit PIDs and emit aggregate evidence"
    )
    doctor.add_argument(
        "--host-pid", type=int, help="limit inventory to one Codex host PID"
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
            state = store.read()
            if args.revalidate and not args.summary:
                raise ValueError("--revalidate requires --summary")
            if args.json and not args.summary:
                raise ValueError("--json requires --summary for status")
            if args.summary:
                report = status_summary(
                    state,
                    backend=SystemProcessBackend() if args.revalidate else None,
                    ledger_present=store.path.exists(),
                )
                if args.json:
                    print(json.dumps(report, indent=2, sort_keys=True))
                else:
                    _print_status_summary(report)
            else:
                print(json.dumps(state, indent=2, sort_keys=True))
            return 0
        if args.command == "doctor":
            report = doctor_report(
                SystemProcessBackend(),
                host_pid=args.host_pid,
                window_seconds=load_config(store.root).cohort_window_seconds,
            )
            if args.summary:
                report = redact_doctor_report(report)
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            elif args.summary:
                _print_doctor_summary(report)
            else:
                _print_doctor(report)
            return 0
    except (
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
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
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as input_file:
            opened_stat = os.fstat(input_file.fileno())
            if not stat.S_ISREG(opened_stat.st_mode):
                raise ValueError("hook input must be a regular file")
            encoded = input_file.read(MAX_HOOK_BYTES + 1)
        raw = encoded.decode("utf-8")
    if len(raw.encode("utf-8")) > MAX_HOOK_BYTES:
        raise ValueError(f"hook input exceeds {MAX_HOOK_BYTES} bytes")
    return raw


def doctor_report(
    backend: ProcessBackend,
    *,
    host_pid: int | None = None,
    window_seconds: float = 2.0,
    clock: Any = time.time,
) -> dict[str, object]:
    snapshot = backend.snapshot()
    observed_at = float(clock())
    hosts: list[dict[str, Any]] = []
    for host in sorted(
        (
            process
            for process in snapshot.values()
            if is_codex_host(process) and (host_pid is None or process.pid == host_pid)
        ),
        key=lambda process: process.pid,
    ):
        classified = candidate_roots(snapshot, host.pid)
        cohorts = cluster_processes(
            [process for process, _ in classified], window_seconds=window_seconds
        )
        if not classified:
            continue
        kind_by_pid = {process.pid: kind for process, kind in classified}
        rss_by_pid = {
            process.pid: process_group_rss_bytes(snapshot, process)
            for process, _ in classified
        }
        known_host_rss = [value for value in rss_by_pid.values() if value is not None]
        complete_host_rss = (
            sum(known_host_rss)
            if known_host_rss and len(known_host_rss) == len(classified)
            else None
        )
        hosts.append(
            {
                "pid": host.pid,
                "started_at": host.started_at,
                "age_seconds": max(0.0, observed_at - host.started_at),
                "helper_count": len(classified),
                "group_rss_bytes": complete_host_rss,
                "rss_known_root_count": len(known_host_rss),
                "cohorts": [
                    {
                        "started_at": cohort.started_at,
                        "age_seconds": max(0.0, observed_at - cohort.started_at),
                        "group_rss_bytes": _complete_rss_sum(
                            [rss_by_pid[process.pid] for process in cohort.processes]
                        ),
                        "rss_known_root_count": sum(
                            rss_by_pid[process.pid] is not None
                            for process in cohort.processes
                        ),
                        "processes": [
                            {
                                "pid": process.pid,
                                "kind": kind_by_pid[process.pid],
                                "started_at": process.started_at,
                                "age_seconds": max(
                                    0.0, observed_at - process.started_at
                                ),
                                "rss_bytes": process.rss_bytes,
                                "group_rss_bytes": rss_by_pid[process.pid],
                            }
                            for process in cohort.processes
                        ],
                    }
                    for cohort in cohorts
                ],
            }
        )
    total_roots = sum(host["helper_count"] for host in hosts)
    known_roots = sum(host["rss_known_root_count"] for host in hosts)
    host_rss_values = [host["group_rss_bytes"] for host in hosts]
    return {
        "mode": "audit-only",
        "ownership": "unproven",
        "platform": platform.system().lower(),
        "observed_at": observed_at,
        "observed_at_iso": _iso_timestamp(observed_at),
        "totals": {
            "host_count": len(hosts),
            "helper_root_count": total_roots,
            "cohort_count": sum(len(host["cohorts"]) for host in hosts),
            "group_rss_bytes": _complete_rss_sum(host_rss_values),
            "rss_known_root_count": known_roots,
            "rss_total_root_count": total_roots,
        },
        "codex_hosts": hosts,
    }


def status_summary(
    state: Mapping[str, Any],
    *,
    backend: ProcessBackend | None = None,
    ledger_present: bool = True,
    clock: Any = time.time,
) -> dict[str, object]:
    agents = state.get("agents", {})
    history = state.get("history", [])
    records = list(agents.values()) if isinstance(agents, Mapping) else []
    valid_records = [record for record in records if isinstance(record, Mapping)]
    retained_records = [
        record
        for record in valid_records
        if record.get("status") == "retained-candidate"
    ]
    recorded_rss = [
        _safe_optional_nonnegative_int(record.get("live_group_rss_bytes"))
        for record in retained_records
    ]
    counts = Counter(
        _safe_label(record.get("status"), SAFE_STATUSES) for record in valid_records
    )
    latest_event_at = None
    if isinstance(history, list):
        timestamps = []
        for item in history:
            if not isinstance(item, Mapping):
                continue
            try:
                timestamp = float(item["at"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(timestamp):
                timestamps.append(timestamp)
        latest_event_at = max(timestamps) if timestamps else None
    observed_at = float(clock())
    report: dict[str, object] = {
        "mode": "audit-only",
        "ownership": "unproven",
        "ledger_present": ledger_present,
        "state_version": state.get("version"),
        "record_count": len(valid_records),
        "status_counts": dict(sorted(counts.items())),
        "recorded_live_identity_count": sum(
            _safe_int(record.get("live_process_count")) for record in retained_records
        ),
        "recorded_live_group_rss_bytes": _complete_rss_sum(recorded_rss),
        "recorded_rss_known_record_count": sum(
            value is not None for value in recorded_rss
        ),
        "recorded_rss_total_record_count": len(recorded_rss),
        "latest_event_at": latest_event_at,
        "latest_event_at_iso": (
            _iso_timestamp(latest_event_at) if latest_event_at is not None else None
        ),
        "recent_records": [
            {
                "status": _safe_label(record.get("status"), SAFE_STATUSES),
                "evidence_grade": _safe_label(
                    record.get("evidence_grade"), SAFE_EVIDENCE_GRADES
                ),
                "started_event_at": _safe_timestamp(record.get("started_event_at")),
                "stopped_event_at": _safe_timestamp(record.get("stopped_event_at")),
                "live_process_count": _safe_int(record.get("live_process_count")),
                "live_group_rss_bytes": _safe_optional_nonnegative_int(
                    record.get("live_group_rss_bytes")
                ),
            }
            for record in sorted(
                valid_records,
                key=lambda item: _record_time(item),
                reverse=True,
            )[:5]
        ],
    }
    if backend is not None:
        snapshot = backend.snapshot()
        checked = live = exited = changed = 0
        for record in valid_records:
            if record.get("status") != "retained-candidate":
                continue
            processes = record.get("processes", [])
            if not isinstance(processes, list):
                continue
            for identity in processes:
                if not isinstance(identity, Mapping):
                    checked += 1
                    changed += 1
                    continue
                try:
                    pid = int(identity["pid"])
                except (KeyError, TypeError, ValueError):
                    checked += 1
                    changed += 1
                    continue
                checked += 1
                try:
                    error = verify_identity(snapshot.get(pid), identity)
                except (KeyError, TypeError, ValueError):
                    changed += 1
                    continue
                if error is None:
                    live += 1
                elif error == "process already exited":
                    exited += 1
                else:
                    changed += 1
        report["revalidation"] = {
            "observed_at": observed_at,
            "observed_at_iso": _iso_timestamp(observed_at),
            "scope": "stored retained-candidate identities",
            "checked_identity_count": checked,
            "still_matching_count": live,
            "exited_count": exited,
            "changed_or_reused_count": changed,
            "liveness": (
                "not-applicable-no-identities"
                if checked == 0
                else (
                    "some-identities-still-match"
                    if live
                    else "no-identities-still-match"
                )
            ),
        }
    return report


def redact_doctor_report(report: Mapping[str, object]) -> dict[str, object]:
    redacted_hosts: list[dict[str, object]] = []
    hosts = report.get("codex_hosts", [])
    if isinstance(hosts, list):
        for host in hosts:
            if not isinstance(host, Mapping):
                continue
            redacted_cohorts: list[dict[str, object]] = []
            cohorts = host.get("cohorts", [])
            if isinstance(cohorts, list):
                for cohort in cohorts:
                    if not isinstance(cohort, Mapping):
                        continue
                    processes = cohort.get("processes", [])
                    kind_counts: Counter[str] = Counter()
                    if isinstance(processes, list):
                        for process in processes:
                            if isinstance(process, Mapping):
                                kind_counts[str(process.get("kind") or "unknown")] += 1
                    redacted_cohorts.append(
                        {
                            "age_seconds": _safe_int(cohort.get("age_seconds")),
                            "group_rss_bytes": _safe_optional_nonnegative_int(
                                cohort.get("group_rss_bytes")
                            ),
                            "rss_known_root_count": _safe_int(
                                cohort.get("rss_known_root_count")
                            ),
                            "process_count": sum(kind_counts.values()),
                            "kind_counts": dict(sorted(kind_counts.items())),
                        }
                    )
            redacted_hosts.append(
                {
                    "host_age_seconds": _safe_int(host.get("age_seconds")),
                    "helper_count": _safe_int(host.get("helper_count")),
                    "group_rss_bytes": _safe_optional_nonnegative_int(
                        host.get("group_rss_bytes")
                    ),
                    "rss_known_root_count": _safe_int(host.get("rss_known_root_count")),
                    "cohorts": redacted_cohorts,
                }
            )
    return {
        "mode": report.get("mode"),
        "ownership": report.get("ownership"),
        "platform": report.get("platform"),
        "observed_at": report.get("observed_at"),
        "observed_at_iso": report.get("observed_at_iso"),
        "totals": report.get("totals"),
        "hosts": redacted_hosts,
    }


def _print_doctor(report: dict[str, object]) -> None:
    hosts = report["codex_hosts"]
    if not hosts:
        print("No Codex child processes matched the MCP helper classifier.")
        return
    print("Read-only inventory; listed processes are candidates, not proven ownership.")
    totals = report["totals"]
    print(
        f"Observed {totals['helper_root_count']} helper roots in "
        f"{totals['cohort_count']} cohort(s) under {totals['host_count']} Codex host(s); "
        f"group RSS {_format_bytes(totals['group_rss_bytes'])} "
        f"(coverage {totals['rss_known_root_count']}/{totals['rss_total_root_count']})."
    )
    for host in hosts:
        print(
            f"Codex PID {host['pid']}: {host['helper_count']} candidate helper roots "
            f"in {len(host['cohorts'])} cohort(s)"
        )
        for cohort in host["cohorts"]:
            details = ", ".join(
                f"{process['pid']}:{process['kind']}:{_format_bytes(process['group_rss_bytes'])}"
                for process in cohort["processes"]
            )
            print(f"  age {int(cohort['age_seconds'])}s: {details}")


def _print_status_summary(report: Mapping[str, object]) -> None:
    print("Redacted audit summary; lifecycle correlations are not process ownership.")
    if not report["ledger_present"]:
        print("No state ledger exists; no lifecycle evidence has been recorded.")
        return
    counts = report["status_counts"]
    rendered = ", ".join(f"{key}={value}" for key, value in counts.items()) or "none"
    print(f"Records: {report['record_count']} ({rendered})")
    print(
        "Recorded live identities at Stop: "
        f"{report['recorded_live_identity_count']} "
        f"({_format_bytes(report['recorded_live_group_rss_bytes'])})"
    )
    revalidation = report.get("revalidation")
    if isinstance(revalidation, Mapping):
        print(
            "Fresh revalidation: "
            f"{revalidation['still_matching_count']} still match, "
            f"{revalidation['exited_count']} exited, "
            f"{revalidation['changed_or_reused_count']} changed or reused."
        )
    else:
        print("Historical Stop results were not revalidated against a fresh snapshot.")


def _print_doctor_summary(report: Mapping[str, object]) -> None:
    print("Redacted inventory; classifier matches are not known leaks or ownership.")
    totals = report["totals"]
    print(
        f"Observed {totals['helper_root_count']} helper roots in "
        f"{totals['cohort_count']} cohort(s) under {totals['host_count']} Codex host(s); "
        f"group RSS {_format_bytes(totals['group_rss_bytes'])} "
        f"(coverage {totals['rss_known_root_count']}/{totals['rss_total_root_count']})."
    )


def _record_time(record: Mapping[str, Any]) -> float:
    for field in ("stopped_event_at", "started_event_at"):
        try:
            value = float(record.get(field, 0.0))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return 0.0


def _iso_timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    )


def _format_bytes(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if amount < 1024.0 or unit == "GiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024.0
    return "unknown"


def _safe_int(value: object) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _safe_optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, number)


def _complete_rss_sum(values: list[object]) -> int | None:
    normalized = [_safe_optional_nonnegative_int(value) for value in values]
    if not normalized or any(value is None for value in normalized):
        return None
    return sum(int(value) for value in normalized)


def _safe_timestamp(value: object) -> float | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def _safe_label(value: object, allowed: set[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
