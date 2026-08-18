#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

EVENTS = ("SubagentStart", "SubagentStop")
MARKER = "codex-mcp-guard/scripts/hook.py"
ENV_MARKER = "CODEX_MCP_GUARD_HOOK"
MAX_HOOKS_BYTES = 1024 * 1024
COMMAND_MARKER_RE = re.compile(
    rf"(?<![A-Za-z0-9_]){re.escape(ENV_MARKER)}=1(?![A-Za-z0-9_])"
)


class FileRevision(NamedTuple):
    device: int
    inode: int
    size: int
    modified_ns: int
    sha256: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install Codex MCP Guard user hooks.")
    parser.add_argument(
        "--hooks-file",
        type=Path,
        default=Path.home() / ".codex" / "hooks.json",
        help="Codex hooks.json path",
    )
    parser.add_argument(
        "--uninstall", action="store_true", help="remove only this project's hooks"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the merged JSON without writing"
    )
    return parser


def load_hooks(path: Path) -> dict[str, Any]:
    return load_hooks_snapshot(path)[0]


def load_hooks_snapshot(
    path: Path,
) -> tuple[dict[str, Any], FileRevision | None, bytes | None]:
    encoded, revision = _read_hooks_bytes(path)
    if encoded is None:
        return {"description": "User-level Codex hooks.", "hooks": {}}, None, None
    data = json.loads(encoded.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise TypeError(f"{path} is not a supported Codex hooks file")
    data.setdefault("hooks", {})
    _validate_hook_groups(data["hooks"], path)
    return data, revision, encoded


def _read_hooks_bytes(path: Path) -> tuple[bytes | None, FileRevision | None]:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return None, None
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ValueError(f"{path} must be a regular, non-symlink file")
    if file_stat.st_size > MAX_HOOKS_BYTES:
        raise ValueError(f"{path} exceeds {MAX_HOOKS_BYTES} bytes")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as input_file:
        opened_stat = os.fstat(input_file.fileno())
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ValueError(f"{path} must be a regular file")
        encoded = input_file.read(MAX_HOOKS_BYTES + 1)
    if len(encoded) > MAX_HOOKS_BYTES:
        raise ValueError(f"{path} exceeds {MAX_HOOKS_BYTES} bytes")
    revision = FileRevision(
        device=int(opened_stat.st_dev),
        inode=int(opened_stat.st_ino),
        size=int(opened_stat.st_size),
        modified_ns=int(
            getattr(opened_stat, "st_mtime_ns", int(opened_stat.st_mtime * 1e9))
        ),
        sha256=hashlib.sha256(encoded).hexdigest(),
    )
    return encoded, revision


def assert_hooks_unchanged(path: Path, expected: FileRevision | None) -> None:
    _, current = _read_hooks_bytes(path)
    if current != expected:
        raise ValueError(f"{path} changed after it was read; retry the merge")


def _validate_hook_groups(hooks: dict[str, Any], path: Path) -> None:
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            raise TypeError(f"{path} contains an invalid hook event")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(
                group.get("hooks", []), list
            ):
                raise TypeError(f"{path} contains an invalid {event} hook group")
            if any(not isinstance(handler, dict) for handler in group.get("hooks", [])):
                raise TypeError(f"{path} contains an invalid {event} hook handler")


def handler_config() -> dict[str, Any]:
    hook_script = Path(__file__).resolve().with_name("hook.py")
    unix_command = f"{ENV_MARKER}=1 {shlex.quote(str(hook_script))}"
    windows_command = f'set "{ENV_MARKER}=1" && "{sys.executable}" "{hook_script}"'
    return {
        "type": "command",
        "command": unix_command,
        "commandWindows": windows_command,
        "timeout": 15,
        "statusMessage": "Auditing MCP helper candidates",
    }


def plugin_is_installed() -> bool:
    try:
        completed = subprocess.run(
            ["codex", "plugin", "list", "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        data = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return False
    installed = data.get("installed", []) if isinstance(data, dict) else []
    return any(
        isinstance(entry, dict)
        and entry.get("pluginId") == "codex-mcp-guard@xjm-free"
        and entry.get("installed") is not False
        for entry in installed
    )


def is_guard_handler(handler: Any) -> bool:
    if not isinstance(handler, dict):
        return False
    environment = handler.get("env")
    if isinstance(environment, dict) and environment.get(ENV_MARKER) == "1":
        return True
    command = str(handler.get("command", ""))
    return bool(COMMAND_MARKER_RE.search(command)) or MARKER in command.replace(
        "\\", "/"
    )


def write_hooks(
    path: Path, rendered: str, *, expected_revision: FileRevision | None
) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        path_stat = None
    if path_stat is not None and stat.S_ISLNK(path_stat.st_mode):
        raise ValueError(f"refusing symlinked hooks file: {path}")
    fd, temporary = tempfile.mkstemp(prefix="hooks.", suffix=".tmp", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(rendered)
            output.flush()
            os.fsync(output.fileno())
        assert_hooks_unchanged(path, expected_revision)
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def backup_hooks(path: Path, encoded: bytes) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    destination = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(destination, "wb") as output_file:
            output_file.write(encoded)
            output_file.flush()
            os.fsync(output_file.fileno())
    except BaseException:
        try:
            os.close(destination)
        except OSError:
            pass
        try:
            os.unlink(backup)
        except FileNotFoundError:
            pass
        raise
    return backup


def remove_guard_handlers(data: dict[str, Any]) -> None:
    hooks = data.setdefault("hooks", {})
    for event in EVENTS:
        retained_groups = []
        for group in hooks.get(event, []):
            if not isinstance(group, dict):
                retained_groups.append(group)
                continue
            handlers = [
                handler
                for handler in group.get("hooks", [])
                if not is_guard_handler(handler)
            ]
            if handlers:
                retained = dict(group)
                retained["hooks"] = handlers
                retained_groups.append(retained)
        if retained_groups:
            hooks[event] = retained_groups
        else:
            hooks.pop(event, None)


def install_guard_handlers(data: dict[str, Any]) -> None:
    remove_guard_handlers(data)
    hooks = data.setdefault("hooks", {})
    for event in EVENTS:
        hooks.setdefault(event, []).append({"hooks": [handler_config()]})


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.hooks_file.expanduser().is_absolute():
            raise ValueError("hooks file must be an absolute path")
        args.hooks_file = args.hooks_file.expanduser()
        data, initial_revision, original = load_hooks_snapshot(args.hooks_file)
        if args.uninstall:
            remove_guard_handlers(data)
        else:
            if not args.dry_run and plugin_is_installed():
                raise ValueError(
                    "codex-mcp-guard@xjm-free is installed; do not add duplicate user hooks"
                )
            install_guard_handlers(data)
        rendered = json.dumps(data, indent=2, sort_keys=True) + "\n"
        if args.dry_run:
            print(rendered, end="")
            return 0
        args.hooks_file.parent.mkdir(parents=True, exist_ok=True)
        assert_hooks_unchanged(args.hooks_file, initial_revision)
        if original is not None:
            backup = backup_hooks(args.hooks_file, original)
            print(f"Backed up {args.hooks_file} to {backup}")
        write_hooks(args.hooks_file, rendered, expected_revision=initial_revision)
        action = "Removed" if args.uninstall else "Installed"
        print(f"{action} Codex MCP Guard hooks in {args.hooks_file}")
        return 0
    except (
        OSError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        print(f"install-user-hooks: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
