"""Spine & Sovereign Mesh tools (tools_bos: spine domain).

Bridges Cockpit Spine and omlxc DMA/Replay capabilities to Agora MCP.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from agora.server._response import FORMAT_VERSION, _error, _ok

logger = logging.getLogger(__name__)


def _get_workspace_root() -> Path:
    ws_env = os.environ.get("WORKSPACE_ROOT")
    if ws_env and Path(ws_env).exists():
        return Path(ws_env)
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / ".omo").exists() and (parent / "projects").exists():
            return parent
    return Path.home() / "Workspace"


async def bos_spine_draft(
    prompt: str,
    domain: str = "general",
    max_tokens: int = 1024,
) -> dict:
    """Generate a sovereign draft through Cockpit Spine / AetherForge inference."""
    ws = _get_workspace_root()
    try:
        cmd = [
            "uv",
            "run",
            "--directory",
            str(ws / "projects" / "cockpit"),
            "python",
            "-m",
            "cockpit.cli",
            "spine",
            "draft",
            "--prompt",
            prompt,
            "--domain",
            domain,
            "--max-tokens",
            str(max_tokens),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return _error(
                f"Draft generation failed: {proc.stderr.strip() or proc.stdout.strip()}"
            )

        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "status": "draft_generated",
                "prompt": prompt,
                "domain": domain,
                "output": proc.stdout.strip(),
            }
        )
    except Exception as exc:
        return _error(f"Draft invocation exception: {exc}")


async def bos_spine_sign(
    original: str,
    signed: str,
    domain: str = "general",
    author: str = "xiamingxing",
) -> dict:
    """Submit a signature diff to the Spine broker and experience replay buffer."""
    ws = _get_workspace_root()
    try:
        cmd = [
            "uv",
            "run",
            "--directory",
            str(ws / "projects" / "cockpit"),
            "python",
            "-m",
            "cockpit.cli",
            "spine",
            "sign",
            "--original",
            original,
            "--signed",
            signed,
            "--domain",
            domain,
            "--author",
            author,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return _error(
                f"Sign recording failed: {proc.stderr.strip() or proc.stdout.strip()}"
            )

        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "status": "diff_recorded",
                "domain": domain,
                "author": author,
                "detail": proc.stdout.strip(),
            }
        )
    except Exception as exc:
        return _error(f"Sign invocation exception: {exc}")


async def bos_spine_diff(domain: str = "all") -> dict:
    """Inspect recent signature diffs and experience replay pool."""
    ws = _get_workspace_root()
    try:
        cmd = [
            "uv",
            "run",
            "--directory",
            str(ws / "projects" / "cockpit"),
            "python",
            "-m",
            "cockpit.cli",
            "spine",
            "diff",
            "--domain",
            domain,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "status": "ok",
                "domain": domain,
                "output": proc.stdout.strip(),
            }
        )
    except Exception as exc:
        return _error(f"Diff inspection exception: {exc}")


async def bos_spine_status() -> dict:
    """Query Cockpit Spine physical compute fabric and truth stream status."""
    ws = _get_workspace_root()
    try:
        cmd = [
            "uv",
            "run",
            "--directory",
            str(ws / "projects" / "cockpit"),
            "python",
            "-m",
            "cockpit.cli",
            "spine",
            "status",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "status": "ok",
                "output": proc.stdout.strip(),
            }
        )
    except Exception as exc:
        return _error(f"Spine status exception: {exc}")


async def bos_spine_distill(
    domain: str = "general",
    epochs: int = 3,
) -> dict:
    """Trigger Mac mini M4 idle online LoRA distillation for domain signature style."""
    ws = _get_workspace_root()
    try:
        cmd = [
            "uv",
            "run",
            "--directory",
            str(ws / "projects" / "cockpit"),
            "python",
            "-m",
            "cockpit.cli",
            "spine",
            "distill",
            "--domain",
            domain,
            "--epochs",
            str(epochs),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "status": "ok" if proc.returncode == 0 else "distill_error",
                "domain": domain,
                "output": proc.stdout.strip() or proc.stderr.strip(),
            }
        )
    except Exception as exc:
        return _error(f"Distill invocation exception: {exc}")


async def bos_spine_replay(domain: str = "all") -> dict:
    """Inspect experience replay buffer statistics and domain sample distribution."""
    ws = _get_workspace_root()
    try:
        cmd = [
            "uv",
            "run",
            "--directory",
            str(ws / "projects" / "cockpit"),
            "python",
            "-m",
            "cockpit.cli",
            "spine",
            "replay",
            "--json",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                data = json.loads(proc.stdout.strip())
                return _ok(
                    {
                        "format_version": FORMAT_VERSION,
                        "status": "ok",
                        "domain": domain,
                        "replay_stats": data,
                    }
                )
            except json.JSONDecodeError:
                pass
        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "status": "ok" if proc.returncode == 0 else "error",
                "domain": domain,
                "output": proc.stdout.strip() or proc.stderr.strip(),
            }
        )
    except Exception as exc:
        return _error(f"Replay inspection exception: {exc}")


async def bos_mesh_dma_status() -> dict:
    """Inspect Thunderbolt 5 DMA (120Gbps / 0.21ms) physical link telemetry."""
    ws = _get_workspace_root()
    telemetry_file = ws / ".omo" / "state" / "mesh-telemetry.json"
    if telemetry_file.exists():
        try:
            data = json.loads(telemetry_file.read_text(encoding="utf-8"))
            return _ok(
                {
                    "format_version": FORMAT_VERSION,
                    "status": "ok",
                    "source": "mesh_telemetry_file",
                    "telemetry": data,
                }
            )
        except Exception:
            logger.debug(
                "telemetry file read failed, falling back to CLI", exc_info=True
            )

    # Fallback to direct omlxc CLI
    try:
        cmd = [
            "uv",
            "run",
            "--directory",
            str(ws / "projects" / "omlxc"),
            "python",
            "-m",
            "omlxc.cli",
            "fabric",
            "dma",
            "--json",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if proc.returncode == 0 and proc.stdout.strip():
            return _ok(
                {
                    "format_version": FORMAT_VERSION,
                    "status": "ok",
                    "source": "omlxc_cli",
                    "data": json.loads(proc.stdout.strip()),
                }
            )
    except Exception as exc:
        return _error(f"DMA probe exception: {exc}")

    return _ok(
        {
            "format_version": FORMAT_VERSION,
            "status": "offline",
            "message": "DMA telemetry not found and omlxc CLI probe returned no data",
        }
    )
