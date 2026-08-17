#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_ALLOWED_FIELDS = {
    "apps",
    "author",
    "description",
    "homepage",
    "interface",
    "keywords",
    "license",
    "mcpServers",
    "name",
    "repository",
    "skills",
    "version",
}
REQUIRED_FILES = {
    ".agents/plugins/marketplace.json",
    ".codex-plugin/plugin.json",
    "hooks/hooks.json",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "skills/codex-mcp-guard/SKILL.md",
}
VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def fail(message: str) -> None:
    raise ValueError(message)


def main() -> int:
    missing = sorted(path for path in REQUIRED_FILES if not (ROOT / path).is_file())
    if missing:
        fail(f"missing release files: {', '.join(missing)}")

    manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text("utf-8"))
    unknown = set(manifest) - MANIFEST_ALLOWED_FIELDS
    if unknown:
        fail(f"unsupported plugin manifest fields: {', '.join(sorted(unknown))}")
    version = manifest.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        fail("plugin version is not strict semantic versioning")
    if manifest.get("author", {}).get("name") != "Jesse Vale":
        fail("public author alias is not Jesse Vale")
    if manifest.get("skills") != "./skills/":
        fail("plugin skill path must be ./skills/")

    marketplace = json.loads(
        (ROOT / ".agents/plugins/marketplace.json").read_text("utf-8")
    )
    entries = marketplace.get("plugins", [])
    matching_entries = [
        entry for entry in entries if entry.get("name") == "codex-mcp-guard"
    ]
    if len(matching_entries) != 1:
        fail("marketplace must contain exactly one codex-mcp-guard entry")
    marketplace_entry = matching_entries[0]
    if marketplace_entry.get("source") != {"source": "local", "path": "./"}:
        fail("dedicated repository marketplace source must point to its root")
    if marketplace_entry.get("policy") != {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }:
        fail("marketplace policy is invalid")

    pyproject = (ROOT / "pyproject.toml").read_text("utf-8")
    package_match = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    init_match = re.search(
        r'^__version__ = "([^"]+)"$',
        (ROOT / "src/codex_mcp_guard/__init__.py").read_text("utf-8"),
        re.MULTILINE,
    )
    versions = {
        version,
        package_match.group(1) if package_match else None,
        init_match.group(1) if init_match else None,
    }
    if versions != {version}:
        fail(f"release versions are not synchronized: {sorted(map(str, versions))}")

    hooks = json.loads((ROOT / "hooks/hooks.json").read_text("utf-8"))
    configured_events = set(hooks.get("hooks", {}))
    if configured_events != {"SubagentStart", "SubagentStop"}:
        fail("hooks must contain exactly SubagentStart and SubagentStop")
    for event in sorted(configured_events):
        groups = hooks["hooks"][event]
        handlers = [handler for group in groups for handler in group.get("hooks", [])]
        if not handlers or any(
            handler.get("type") != "command" for handler in handlers
        ):
            fail(f"{event} must contain command hooks")
        if any("env" in handler for handler in handlers):
            fail(f"{event} contains unsupported env metadata")
        if any(int(handler.get("timeout", 0)) > 15 for handler in handlers):
            fail(f"{event} hook timeout exceeds 15 seconds")

    source = "\n".join(path.read_text("utf-8") for path in (ROOT / "src").rglob("*.py"))
    forbidden = ("killpg(", "taskkill", "os.kill(", ".terminate(")
    found = [token for token in forbidden if token in source]
    if found:
        fail(
            f"audit-only source contains process termination paths: {', '.join(found)}"
        )

    skill = (ROOT / "skills/codex-mcp-guard/SKILL.md").read_text("utf-8")
    if not skill.startswith("---\nname: codex-mcp-guard\n"):
        fail("skill frontmatter name is missing or invalid")
    if "automatic cleanup" in skill.lower() or "mode enforce" in skill.lower():
        fail("skill still recommends automatic cleanup")

    ignored_parts = {
        ".git",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
    }
    for path in ROOT.rglob("*"):
        if (
            not path.is_file()
            or any(part in ignored_parts for part in path.parts)
            or any(part.endswith(".egg-info") for part in path.parts)
        ):
            continue
        if path.suffix.lower() in {".pyc", ".png", ".jpg", ".jpeg", ".gif"}:
            continue
        text = path.read_text("utf-8", errors="ignore")
        placeholder = "[" + "TODO:"
        if placeholder in text:
            fail(f"unresolved TODO placeholder in {path.relative_to(ROOT)}")
        private_home = str(Path.home())
        if private_home != "/" and private_home in text:
            fail(f"private absolute path in {path.relative_to(ROOT)}")

    print(f"release validation passed for {version}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"validate-release: {error}", file=sys.stderr)
        raise SystemExit(1) from error
