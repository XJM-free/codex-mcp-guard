#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVENTS = ("SubagentStart", "SubagentStop")
MARKER = "codex-mcp-guard/scripts/hook.py"
ENV_MARKER = "CODEX_MCP_GUARD_HOOK"
COMMAND_MARKER_RE = re.compile(
    rf"(?<![A-Za-z0-9_]){re.escape(ENV_MARKER)}=1(?![A-Za-z0-9_])"
)


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
    if not path.exists():
        return {"description": "User-level Codex hooks.", "hooks": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise TypeError(f"{path} is not a supported Codex hooks file")
    data.setdefault("hooks", {})
    return data


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


def write_hooks(path: Path, rendered: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix="hooks.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(rendered)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


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
        data = load_hooks(args.hooks_file)
        if args.uninstall:
            remove_guard_handlers(data)
        else:
            install_guard_handlers(data)
        rendered = json.dumps(data, indent=2, sort_keys=True) + "\n"
        if args.dry_run:
            print(rendered, end="")
            return 0
        args.hooks_file.parent.mkdir(parents=True, exist_ok=True)
        if args.hooks_file.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = args.hooks_file.with_name(f"{args.hooks_file.name}.bak.{stamp}")
            shutil.copy2(args.hooks_file, backup)
            print(f"Backed up {args.hooks_file} to {backup}")
        write_hooks(args.hooks_file, rendered)
        if os.name != "nt":
            os.chmod(args.hooks_file, 0o600)
        action = "Removed" if args.uninstall else "Installed"
        print(f"{action} Codex MCP Guard hooks in {args.hooks_file}")
        return 0
    except (OSError, TypeError, UnicodeError, json.JSONDecodeError) as error:
        print(f"install-user-hooks: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
