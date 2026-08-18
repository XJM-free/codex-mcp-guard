from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from .models import ProcessCohort, ProcessInfo

_UNIX_PS_RE = re.compile(
    r"^\s*(?P<pid>\d+)\s+(?P<ppid>\d+)\s+(?P<pgid>\d+)\s+(?P<rss>\d+)\s+"
    r"(?P<started>\w{3}\s+\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+"
    r"(?P<command>.*)$"
)
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_STDERR_BYTES = 64 * 1024
UNIX_SNAPSHOT_TIMEOUT_SECONDS = 5.0
WINDOWS_SNAPSHOT_TIMEOUT_SECONDS = 8.0
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
_NODE_OPTIONS_WITH_VALUE = {
    "--conditions",
    "--diagnostic-dir",
    "--eval",
    "--experimental-loader",
    "--heapsnapshot-signal",
    "--icu-data-dir",
    "--import",
    "--inspect-port",
    "--loader",
    "--openssl-config",
    "--print",
    "--redirect-warnings",
    "--require",
    "--title",
    "-e",
    "-p",
    "-r",
}
_PYTHON_OPTIONS_WITH_VALUE = {"--check-hash-based-pycs", "-W", "-X"}
_RUNNER_PACKAGE_OPTIONS = {"--from", "--package", "-p"}
_RUNNER_OPTIONS_WITH_VALUE = {
    "--cache",
    "--prefix",
    "--registry",
    "--workspace",
    "-C",
}
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


def _run_capped_snapshot(
    command: list[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    max_stdout_bytes: int = MAX_SNAPSHOT_BYTES,
    max_stderr_bytes: int = MAX_SNAPSHOT_STDERR_BYTES,
) -> str:
    inventory_child = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env) if env is not None else None,
    )
    assert inventory_child.stdout is not None
    assert inventory_child.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()
    reader_errors: list[OSError | ValueError] = []

    def read_capped(stream: Any, destination: bytearray, limit: int) -> None:
        try:
            with stream:
                while chunk := stream.read(64 * 1024):
                    remaining = limit + 1 - len(destination)
                    if remaining > 0:
                        destination.extend(chunk[:remaining])
                    if len(destination) > limit or len(chunk) > remaining:
                        overflow.set()
                        return
        except (OSError, ValueError) as error:
            reader_errors.append(error)

    readers = [
        threading.Thread(
            target=read_capped,
            args=(inventory_child.stdout, stdout, max_stdout_bytes),
            daemon=True,
        ),
        threading.Thread(
            target=read_capped,
            args=(inventory_child.stderr, stderr, max_stderr_bytes),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + timeout
    timed_out = False
    while inventory_child.poll() is None:
        if overflow.is_set():
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        overflow.wait(0.01)

    if inventory_child.poll() is None:
        try:
            inventory_child.kill()
        except ProcessLookupError:
            pass
    inventory_child.wait()
    for reader in readers:
        reader.join(timeout=0.2)
    if any(reader.is_alive() for reader in readers):
        for stream in (inventory_child.stdout, inventory_child.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        for reader in readers:
            reader.join(timeout=0.8)

    decoded_stdout = bytes(stdout[:max_stdout_bytes]).decode("utf-8", errors="replace")
    decoded_stderr = bytes(stderr[:max_stderr_bytes]).decode("utf-8", errors="replace")
    if timed_out:
        raise subprocess.TimeoutExpired(
            command, timeout, output=decoded_stdout, stderr=decoded_stderr
        )
    if overflow.is_set():
        raise ValueError(
            f"process snapshot exceeds {max_stdout_bytes} stdout bytes or "
            f"{max_stderr_bytes} stderr bytes"
        )
    if any(reader.is_alive() for reader in readers):
        raise RuntimeError("process snapshot reader did not stop")
    if reader_errors:
        raise OSError(f"process snapshot reader failed: {reader_errors[0]}")
    if inventory_child.returncode:
        raise subprocess.CalledProcessError(
            inventory_child.returncode,
            command,
            output=decoded_stdout,
            stderr=decoded_stderr,
        )
    return decoded_stdout


def _snapshot_unix() -> dict[int, ProcessInfo]:
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    output = _run_capped_snapshot(
        ["ps", "-axo", "pid=,ppid=,pgid=,rss=,lstart=,command="],
        env=environment,
        timeout=UNIX_SNAPSHOT_TIMEOUT_SECONDS,
    )
    processes: dict[int, ProcessInfo] = {}
    for line in output.splitlines():
        match = _UNIX_PS_RE.match(line)
        if not match:
            continue
        try:
            started_at = _parse_unix_started_at(match.group("started"))
        except ValueError:
            continue
        process = ProcessInfo(
            pid=int(match.group("pid")),
            ppid=int(match.group("ppid")),
            pgid=int(match.group("pgid")),
            started_at=started_at,
            command=match.group("command").strip(),
            rss_bytes=int(match.group("rss")) * 1024,
        )
        processes[process.pid] = process
    return processes


def _parse_unix_started_at(value: str) -> float:
    return datetime.strptime(value, "%a %b %d %H:%M:%S %Y").astimezone().timestamp()


def _snapshot_windows() -> dict[int, ProcessInfo]:
    script = r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$items = Get-CimInstance Win32_Process | ForEach-Object {
  [PSCustomObject]@{
    pid = [int]$_.ProcessId
    ppid = [int]$_.ParentProcessId
    started_at = if ($_.CreationDate) { $_.CreationDate.ToUniversalTime().ToString('o') } else { $null }
    command = if ($_.CommandLine) { $_.CommandLine } elseif ($_.ExecutablePath) { $_.ExecutablePath } else { $_.Name }
    rss = if ($_.WorkingSetSize) { [int64]$_.WorkingSetSize } else { $null }
  }
}
$items | ConvertTo-Json -Compress
"""
    output = _run_capped_snapshot(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=WINDOWS_SNAPSHOT_TIMEOUT_SECONDS,
    )
    raw = json.loads(output or "[]")
    if raw is None:
        raw = []
    elif isinstance(raw, dict):
        raw = [raw]
    elif not isinstance(raw, list):
        raise ValueError("Windows process snapshot must be a JSON array")
    processes: dict[int, ProcessInfo] = {}
    for item in raw:
        if not isinstance(item, dict) or not item.get("started_at"):
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
            rss_bytes=(int(item["rss"]) if item.get("rss") is not None else None),
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
        script = _runtime_entrypoint(cleaned[1:])
        return bool(script and _is_mcp_runnable(script))

    if executable in _PYTHON_EXECUTABLES:
        kind, entrypoint = _python_entrypoint(cleaned[1:])
        if kind == "module":
            return bool(entrypoint and _contains_mcp_component(entrypoint))
        return bool(entrypoint and _is_mcp_runnable(entrypoint))

    return any(
        _contains_mcp_component(package)
        for package in _runner_package_candidates(cleaned[1:])
    )


def _split_command(command: str) -> list[str]:
    try:
        return [
            token.strip("\"'") for token in shlex.split(command, posix=os.name != "nt")
        ]
    except ValueError:
        return []


def _basename(token: str) -> str:
    return re.split(r"[/\\]", token)[-1]


def _runtime_entrypoint(tokens: list[str]) -> str | None:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1] if index + 1 < len(tokens) else None
        option = token.split("=", 1)[0]
        if option in {"--eval", "--print", "-e", "-p"}:
            return None
        if option in _NODE_OPTIONS_WITH_VALUE:
            index += 1 if "=" in token else 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return None


def _python_entrypoint(tokens: list[str]) -> tuple[str | None, str | None]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return (
                ("script", tokens[index + 1])
                if index + 1 < len(tokens)
                else (None, None)
            )
        if token == "-m":
            return (
                ("module", tokens[index + 1])
                if index + 1 < len(tokens)
                else (None, None)
            )
        if token == "-c" or token.startswith("-c"):
            return None, None
        option = token.split("=", 1)[0]
        if option in _PYTHON_OPTIONS_WITH_VALUE:
            index += 1 if "=" in token else 2
            continue
        if token.startswith(("-W", "-X")):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return "script", token
    return None, None


def _runner_package_candidates(tokens: list[str]) -> list[str]:
    arguments = list(tokens)
    if arguments and arguments[0].lower() in {"exec", "x", "dlx"}:
        arguments = arguments[1:]
    packages: list[str] = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        option, separator, attached = token.partition("=")
        if option in _RUNNER_PACKAGE_OPTIONS:
            if separator:
                packages.append(attached)
                index += 1
            elif index + 1 < len(arguments):
                packages.append(arguments[index + 1])
                index += 2
            else:
                index += 1
            continue
        if option in _RUNNER_OPTIONS_WITH_VALUE:
            index += 1 if separator else 2
            continue
        if token == "--":
            packages.extend(arguments[index + 1 : index + 2])
            break
        if token.startswith("-"):
            index += 1
            continue
        packages.append(token)
        break
    return packages


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


def process_group_rss_bytes(
    snapshot: Mapping[int, ProcessInfo], root: ProcessInfo
) -> int | None:
    if root.pgid is not None:
        members = [
            process for process in snapshot.values() if process.pgid == root.pgid
        ]
    else:
        members = _process_tree(snapshot, root.pid)
    if not members or any(process.rss_bytes is None for process in members):
        return None
    return sum(int(process.rss_bytes) for process in members)


def _process_tree(
    snapshot: Mapping[int, ProcessInfo], root_pid: int
) -> list[ProcessInfo]:
    children: dict[int, list[ProcessInfo]] = {}
    for process in snapshot.values():
        children.setdefault(process.ppid, []).append(process)
    result: list[ProcessInfo] = []
    pending = [root_pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        process = snapshot.get(pid)
        if process is not None:
            result.append(process)
        pending.extend(child.pid for child in children.get(pid, []))
    return result


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
