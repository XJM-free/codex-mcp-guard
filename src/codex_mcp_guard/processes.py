from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Iterable, Mapping
from datetime import datetime

from .models import ProcessCohort, ProcessInfo

_UNIX_PS_RE = re.compile(
    r"^\s*(?P<pid>\d+)\s+(?P<ppid>\d+)\s+(?P<pgid>\d+)\s+"
    r"(?P<started>\w{3}\s+\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+"
    r"(?P<command>.*)$"
)
_RUNTIME_EXECUTABLES = {
    "bun",
    "bun.exe",
    "deno",
    "deno.exe",
    "node",
    "node.exe",
}
_PYTHON_EXECUTABLES = {
    "py",
    "py.exe",
    "python",
    "python.exe",
    "python3",
    "python3.exe",
}
_PACKAGE_RUNNERS = {
    "bunx",
    "bunx.exe",
    "npm",
    "npm.cmd",
    "npx",
    "npx.cmd",
    "pnpm",
    "pnpm.cmd",
    "uvx",
    "uvx.exe",
    "yarn",
    "yarn.cmd",
}
_SHELL_EXECUTABLES = {"bash", "dash", "fish", "sh", "zsh"}
_RUNNABLE_SUFFIXES = {
    "",
    ".bat",
    ".cjs",
    ".cmd",
    ".exe",
    ".js",
    ".mjs",
    ".py",
    ".ts",
}


class ProcessBackend:
    """Read processes through an injectable, read-only boundary."""

    def snapshot(self) -> dict[int, ProcessInfo]:
        raise NotImplementedError


class SystemProcessBackend(ProcessBackend):
    def snapshot(self) -> dict[int, ProcessInfo]:
        if os.name == "nt":
            return _snapshot_windows()
        return _snapshot_unix()


def _snapshot_unix() -> dict[int, ProcessInfo]:
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    completed = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,lstart=,command="],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    local_timezone = datetime.now().astimezone().tzinfo
    processes: dict[int, ProcessInfo] = {}
    for line in completed.stdout.splitlines():
        match = _UNIX_PS_RE.match(line)
        if not match:
            continue
        started = datetime.strptime(
            match.group("started"), "%a %b %d %H:%M:%S %Y"
        ).replace(tzinfo=local_timezone)
        process = ProcessInfo(
            pid=int(match.group("pid")),
            ppid=int(match.group("ppid")),
            pgid=int(match.group("pgid")),
            started_at=started.timestamp(),
            command=match.group("command").strip(),
        )
        processes[process.pid] = process
    return processes


def _snapshot_windows() -> dict[int, ProcessInfo]:
    script = r"""
$items = Get-CimInstance Win32_Process | ForEach-Object {
  [PSCustomObject]@{
    pid = [int]$_.ProcessId
    ppid = [int]$_.ParentProcessId
    started_at = if ($_.CreationDate) { $_.CreationDate.ToUniversalTime().ToString('o') } else { $null }
    command = if ($_.CommandLine) { $_.CommandLine } elseif ($_.ExecutablePath) { $_.ExecutablePath } else { $_.Name }
  }
}
$items | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
    )
    raw = json.loads(completed.stdout or "[]")
    if isinstance(raw, dict):
        raw = [raw]
    processes: dict[int, ProcessInfo] = {}
    for item in raw:
        if not item.get("started_at"):
            continue
        started_at = datetime.fromisoformat(
            item["started_at"].replace("Z", "+00:00")
        ).timestamp()
        process = ProcessInfo(
            pid=int(item["pid"]),
            ppid=int(item["ppid"]),
            pgid=None,
            started_at=started_at,
            command=str(item.get("command") or ""),
        )
        processes[process.pid] = process
    return processes


def is_codex_host(process: ProcessInfo) -> bool:
    tokens = _split_command(process.command)
    if not tokens:
        return False
    executable = _basename(tokens[0]).lower()
    return executable in {"codex", "codex.exe"}


def ancestry(
    snapshot: Mapping[int, ProcessInfo], pid: int, limit: int = 16
) -> list[ProcessInfo]:
    result: list[ProcessInfo] = []
    seen: set[int] = set()
    current = snapshot.get(pid)
    while current is not None and current.pid not in seen and len(result) < limit:
        result.append(current)
        seen.add(current.pid)
        current = snapshot.get(current.ppid)
    return result


def find_codex_host(
    snapshot: Mapping[int, ProcessInfo], pid: int
) -> ProcessInfo | None:
    return next(
        (process for process in ancestry(snapshot, pid) if is_codex_host(process)), None
    )


def classify_mcp_root(process: ProcessInfo) -> str | None:
    lowered = process.command.lower()
    if "codex-mcp-guard" in lowered or "codex_mcp_guard" in lowered:
        return None
    tokens = _split_command(process.command)
    if not tokens:
        return None
    executable = tokens[0]
    executable_name = _basename(executable).lower()
    if executable_name in _SHELL_EXECUTABLES:
        return None
    if executable_name in {"node_repl", "node_repl.exe"}:
        normalized_path = executable.replace("\\", "/").lower()
        if any(
            marker in normalized_path for marker in ("/cua_node/", "chatgpt", "codex")
        ):
            return "node-repl"
        return None
    return "mcp" if _looks_like_mcp_launch(tokens) else None


def _looks_like_mcp_launch(tokens: list[str]) -> bool:
    cleaned = [token.strip("\"'") for token in tokens]
    executable_path = cleaned[0]
    executable = _basename(executable_path).lower()

    if executable not in _RUNTIME_EXECUTABLES | _PYTHON_EXECUTABLES | _PACKAGE_RUNNERS:
        return _contains_mcp_component(executable_path)

    if executable in _RUNTIME_EXECUTABLES:
        if _contains_mcp_component(executable_path):
            return True
        script = _first_positional(cleaned[1:])
        return bool(script and _is_mcp_runnable(script))

    if executable in _PYTHON_EXECUTABLES:
        arguments = cleaned[1:]
        if "-m" in arguments[:3]:
            module_index = arguments.index("-m") + 1
            return module_index < len(arguments) and _contains_mcp_component(
                arguments[module_index]
            )
        script = _first_positional(arguments)
        return bool(script and _is_mcp_runnable(script))

    arguments = cleaned[1:]
    if arguments and arguments[0].lower() in {"exec", "x", "dlx"}:
        arguments = arguments[1:]
    package = _first_positional(arguments)
    return bool(package and _contains_mcp_component(package))


def _split_command(command: str) -> list[str]:
    try:
        return [
            token.strip("\"'") for token in shlex.split(command, posix=os.name != "nt")
        ]
    except ValueError:
        return []


def _basename(token: str) -> str:
    return re.split(r"[/\\]", token)[-1]


def _first_positional(tokens: list[str]) -> str | None:
    return next(
        (token for token in tokens if token and not token.startswith("-")), None
    )


def _is_mcp_runnable(token: str) -> bool:
    if not _contains_mcp_component(token):
        return False
    suffix = os.path.splitext(_basename(token))[1].lower()
    return suffix in _RUNNABLE_SUFFIXES


def _contains_mcp_component(token: str) -> bool:
    for component in re.split(r"[/\\]", token):
        normalized = component.lower()
        if (
            "modelcontextprotocol" in normalized
            or "model-context-protocol" in normalized
        ):
            return True
        stem = os.path.splitext(normalized)[0]
        if (
            stem == "mcp"
            or stem.endswith("mcp")
            or re.search(r"(?:^|[-_.@])mcp(?:$|[-_.])", stem)
        ):
            return True
    return False


def candidate_roots(
    snapshot: Mapping[int, ProcessInfo],
    codex_pid: int,
    excluded_pids: Iterable[int] = (),
) -> list[tuple[ProcessInfo, str]]:
    excluded = set(excluded_pids)
    candidates: list[tuple[ProcessInfo, str]] = []
    for process in snapshot.values():
        if process.ppid != codex_pid or process.pid in excluded:
            continue
        kind = classify_mcp_root(process)
        if kind is None:
            continue
        if os.name != "nt" and process.pgid != process.pid:
            continue
        candidates.append((process, kind))
    return sorted(candidates, key=lambda item: (item[0].started_at, item[0].pid))


def cluster_processes(
    processes: Iterable[ProcessInfo], window_seconds: float
) -> list[ProcessCohort]:
    ordered = sorted(processes, key=lambda process: (process.started_at, process.pid))
    if not ordered:
        return []
    clusters: list[list[ProcessInfo]] = [[ordered[0]]]
    for process in ordered[1:]:
        if process.started_at - clusters[-1][0].started_at <= window_seconds:
            clusters[-1].append(process)
        else:
            clusters.append([process])
    return [ProcessCohort(tuple(cluster)) for cluster in clusters]


def verify_identity(
    current: ProcessInfo | None, identity: Mapping[str, object]
) -> str | None:
    if current is None:
        return "process already exited"
    if abs(current.started_at - float(identity["started_at"])) > 0.01:
        return "PID was reused"
    if current.ppid != int(identity["ppid"]):
        return "parent process changed"
    expected_pgid = identity.get("pgid")
    if expected_pgid is not None and current.pgid != int(expected_pgid):
        return "process group changed"
    if current.fingerprint != identity["command_sha256"]:
        return "command fingerprint changed"
    if classify_mcp_root(current) is None:
        return "command no longer matches an MCP helper"
    return None
