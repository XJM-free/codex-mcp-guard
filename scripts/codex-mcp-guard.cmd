@echo off
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"
python -m codex_mcp_guard.cli %*
