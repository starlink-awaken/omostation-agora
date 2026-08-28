"""Anti-Corrosion MCP tools — 防腐治理 (ADR-0431, 2026-08-28).

Exposes governance anti-corrosion surfaces through Agora MCP:
- rules_lifecycle — L4 约束层规则生命周期 (expired/due-soon/healthy)
- proposal_triage — 提案池质量分层 (keep/archive/invalid)

Implementation delegates to bin/gac/rules-lifecycle.py --json and
bin/bc-os/proposal-triage.py --analyze (SSOT entry points, see
.omo/_knowledge/decisions/0431-anti-corrosion-five-layer-framework.md).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

WORKSPACE = Path(__file__).resolve().parents[4]


def _run_cli(args: list[str]) -> dict[str, Any]:
    """Run an anti-corrosion CLI and return its JSON payload."""
    try:
        result = subprocess.run(
            ["python3", *args],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode not in (0, 1):  # 1 = 有过期规则, 仍是有效输出
            return {
                "error": f"exit {result.returncode}",
                "stderr": (result.stderr or "")[:200],
            }
        payload = json.loads(result.stdout or "{}")
        return payload
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"error": str(exc)[:200]}


def register_anticorrosion_tools(mcp: Any) -> None:
    """Register anti-corrosion MCP tools."""

    @mcp.tool()
    async def rules_lifecycle() -> dict:
        """L4 约束层规则生命周期 (ADR-0431 D2)。

        返回 expired (减法候选) / due-soon (30 天内) / healthy 三层 +
        counts。规则携带 added_at/review_before/justification, 过期未续
        即进入减法候选 (Lehman 定律 7 工程化)。
        """
        return _run_cli(["bin/gac/rules-lifecycle.py", "--json"])

    @mcp.tool()
    async def proposal_triage() -> dict:
        """提案池质量分层 (ADR-0431 第 0 段清淤)。

        返回 keep (真提案) / archive (机器流水账候选) / invalid 三层计数。
        基线: 3951 文件 → 8 真提案 (98.3% 重复)。--execute 需人工授权。
        """
        return _run_cli(["bin/bc-os/proposal-triage.py", "--analyze"])
