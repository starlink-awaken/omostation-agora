"""Resident Agent System MCP tools — resident 常驻 Agent 体系 (WP-A~I / ADR-0396).

Exposes resident runtime status and role configuration through Agora MCP:
- resident_status — 运行状态快照 (daemon/events/sediment/alert/ledger)
- resident_roles — 五类角色配置 (sediment/decision/execute/monitor/heartbeat)

Implementation delegates to `omo resident status/roles` (SSOT entry point,
see docs/architecture/resident-agent-system-v1.md).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import structlog
from fastmcp import FastMCP

from agora.server._response import _error, _ok

logger = structlog.get_logger(__name__)


# ── Resident Orchestrator A2A identity (BET-Y1Q4-T5-03) ──────────────
# Well-known Agent Card for the resident 常驻 Agent 体系 so external
# transient agents (Antigravity / Claude Code / Codex) can discover it via
# list_agent_cards / get_agent_card and delegate via a2a_send_task.

RESIDENT_ORCHESTRATOR_NAME = "resident-orchestrator"

RESIDENT_ORCHESTRATOR_SKILLS = [
    {
        "id": "background-analysis",
        "name": "后台分析",
        "description": "长时后台分析任务：沉淀事件流并产出结构化结论。",
    },
    {
        "id": "deep-inspection",
        "name": "深度巡检",
        "description": "深度巡检：治理漂移、债务与健康面的系统性检查。",
    },
    {
        "id": "reconciliation",
        "name": "对账",
        "description": "对账：跨源状态核对与差异收敛。",
    },
]

RESIDENT_ORCHESTRATOR_CAPABILITIES = [
    "a2a-task-delegation",
    "background-analysis",
    "deep-inspection",
    "reconciliation",
    "status-snapshot",
    "role-introspection",
]


def build_resident_orchestrator_card(
    registry_card: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical resident-orchestrator A2A Agent Card.

    When the service registry already carries a live `resident-orchestrator`
    entry, its fields take precedence (merge); otherwise the static
    canonical card below is returned so discovery never comes back empty.
    """
    card: dict[str, Any] = {
        "name": RESIDENT_ORCHESTRATOR_NAME,
        "description": "Resident 常驻 Agent 体系编排器：后台分析、深度巡检、对账。",
        "capabilities": list(RESIDENT_ORCHESTRATOR_CAPABILITIES),
        "skills": [dict(s) for s in RESIDENT_ORCHESTRATOR_SKILLS],
        "provider": {"organization": "Agora Hub"},
        "protocol": "a2a",
        "documentation_url": "https://github.com/starlink-awaken/agora",
    }
    if registry_card:
        merged = dict(card)
        merged.update({k: v for k, v in registry_card.items() if v is not None})
        # Union capabilities/skills so static skills are never dropped.
        for key, static_vals in (
            ("capabilities", RESIDENT_ORCHESTRATOR_CAPABILITIES),
            ("skills", RESIDENT_ORCHESTRATOR_SKILLS),
        ):
            seen: set[str] = set()
            union: list[Any] = []
            for item in list(merged.get(key) or []) + list(static_vals):
                marker = (
                    item.get("id", item.get("name", item))
                    if isinstance(item, dict)
                    else item
                )
                if marker not in seen:
                    seen.add(marker)
                    union.append(item)
            merged[key] = union
        return merged
    return card


def _resolve_workspace_root() -> str:
    this_file = Path(__file__).resolve()
    default_root = this_file.parent.parent.parent.parent.parent
    return os.environ.get("WORKSPACE_AUDIT_ROOT") or str(default_root)


def _run_omo_resident(args: list[str]) -> dict[str, Any]:
    """委派到 omo.cli resident（SSOT 入口）。

    Returns parsed JSON payload; on failure returns an error-shaped dict.
    """
    ws_root = Path(_resolve_workspace_root())
    omo_project = ws_root / "projects" / "omo"
    cmd = [
        "uv",
        "run",
        "--directory",
        str(omo_project),
        "python",
        "-W",
        "ignore::DeprecationWarning",
        "-m",
        "omo.cli",
        "resident",
    ] + args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.exception("resident_exec_error")
        return {"ok": False, "detail": f"resident exec failed: {e}"}
    if proc.returncode != 0:
        return {
            "ok": False,
            "detail": proc.stderr.strip()[-2000:] or proc.stdout.strip()[-2000:],
        }
    try:
        data = json.loads(proc.stdout)
        return data if isinstance(data, dict) else {"ok": True, "payload": data}
    except json.JSONDecodeError:
        return {"ok": True, "text": proc.stdout.strip()[-2000:]}


def fetch_resident_status() -> dict[str, Any]:
    """Return the resident runtime status snapshot (testable sync impl)."""
    return _run_omo_resident(["status"])


def fetch_resident_roles() -> dict[str, Any]:
    """Return the five resident role configs (testable sync impl)."""
    return _run_omo_resident(["roles", "--json"])


# A2A-local dispatch table (BET-Y1Q4-T5-03): resident.* tool names that
# a2a_send_task short-circuits locally instead of routing through the core
# Router. Unknown resident.* names resolve to None → caller fails closed.
RESIDENT_A2A_TOOLMAP: dict[str, Any] = {
    "resident.status": fetch_resident_status,
    "resident.roles": fetch_resident_roles,
}


def resolve_resident_tool(tool_name: str) -> Any | None:
    """Resolve a resident.* A2A tool name to its sync impl, or None."""
    return RESIDENT_A2A_TOOLMAP.get(tool_name)


def register_resident_tools(mcp: FastMCP) -> None:
    """Register resident system MCP tools."""

    @mcp.tool()
    async def resident_status() -> dict:
        """Resident 常驻 Agent 体系运行状态快照。

        返回 daemon (byte_offset 水位活性) / events (事件流规模) / sediment
        (知识沉淀计数) / alert (告警转发水位) / ledger (event-ledger 哈希链完整性)
        五组件 + health (recovered/degraded)。基于 omo resident status。
        """
        try:
            data = fetch_resident_status()
            return _ok(data)
        except (OSError, ValueError) as e:  # defensive fallback
            logger.exception("resident_status_error")
            return _error(f"Resident status failed: {e}")

    @mcp.tool()
    async def resident_roles() -> dict:
        """Resident 五类常驻角色配置 (M4.3)。

        返回 sediment (记忆沉淀) / decision (大脑决策) / execute (手执行) /
        monitor (眼睛监控) / heartbeat (心脏心跳) 五角色, 各含 projector /
        topic_filter / handler。基于 omo resident roles。
        """
        try:
            data = fetch_resident_roles()
            return _ok(data)
        except (OSError, ValueError) as e:  # defensive fallback
            logger.exception("resident_roles_error")
            return _error(f"Resident roles failed: {e}")
