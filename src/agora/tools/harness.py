"""Agora Harness Tools — 将 Harness 能力暴露为 MCP tool.

暴露 Phase 8 Harness 合规检查能力到 Agora MCP 协议:
  - harness_compliance_check: 运行 12 章节合规检查
  - harness_status: 获取合规状态总览
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

# ── Tool Definitions ──

TOOL_DEFINITIONS = [
    {
        "name": "harness_compliance_check",
        "description": "Run Harness 12-section compliance check (Phase 8)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["full", "compliance", "mof", "omo", "enforce"],
                    "description": "Check mode (default: full)",
                },
                "strict": {
                    "type": "boolean",
                    "description": "Strict mode (warnings also fail)",
                },
            },
        },
    },
    {
        "name": "harness_status",
        "description": "Get Harness compliance status overview",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]

# ── Tool Handlers ──


def _get_workspace_root() -> Path:
    """Resolve workspace root from agora module location."""
    return Path(__file__).resolve().parents[4]


def handle_harness_compliance_check(arguments: dict) -> dict:
    """Handle harness_compliance_check tool call."""
    mode = arguments.get("mode", "full")
    strict = arguments.get("strict", False)

    workspace_root = _get_workspace_root()

    if mode == "compliance":
        cmd = ["python3", str(workspace_root / "bin/gac/harness-compliance-check.py")]
    elif mode == "mof":
        cmd = ["python3", str(workspace_root / "bin/gac/harness-mof-bridge.py")]
    elif mode == "omo":
        cmd = ["python3", str(workspace_root / "bin/gac/harness-omo-bridge.py")]
    elif mode == "enforce":
        cmd = ["python3", str(workspace_root / "bin/gac/harness-constraint-enforcer.py"), "--ci"]
    else:  # full
        cmd = ["python3", str(workspace_root / "bin/gac/harness-constraint-enforcer.py"), "--ci"]

    if strict:
        cmd.append("--strict")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(workspace_root), timeout=120)
        return {
            "ok": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": result.stdout[-2000:] if result.stdout else "",
            "stderr": result.stderr[-1000:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Command timed out (120s)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def handle_harness_status(arguments: dict) -> dict:
    """Handle harness_status tool call."""
    workspace_root = _get_workspace_root()
    cmd = ["python3", str(workspace_root / "bin/gac/harness-compliance-check.py")]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(workspace_root), timeout=60)
        return {
            "ok": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": result.stdout[-2000:] if result.stdout else "",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Handler Registry ──

TOOL_HANDLERS = {
    "harness_compliance_check": handle_harness_compliance_check,
    "harness_status": handle_harness_status,
}
