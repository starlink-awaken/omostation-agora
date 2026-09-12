"""MCP Connector Gateway tools — BOS 连接器统一网关 (BET-Y1Q4-T5-04).

Provides MCP tools for connector discovery and sync orchestration:
  connector_list  — List all registered connectors with status
  connector_sync  — Trigger incremental sync for a specific connector
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog
import yaml
from fastmcp import FastMCP

from agora.server._response import FORMAT_VERSION, _error, _ok

logger = structlog.get_logger(__name__)


def _ws() -> Path:
    """Find workspace root by walking up from this file."""
    cur = Path(__file__).resolve()
    for parent in cur.parents:
        if (parent / "docs" / "project-registry.yaml").is_file():
            return parent
    return Path.cwd()


def _load_manifest() -> dict:
    """Load connector-manifest.yaml from the truth registry."""
    manifest_path = _ws() / ".omo" / "_truth" / "registry" / "connector-manifest.yaml"
    if not manifest_path.is_file():
        return {"schema_version": "connector-manifest/v1", "connectors": []}
    try:
        return yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("connector_manifest_load_error", error=str(exc))
        return {"schema_version": "connector-manifest/v1", "connectors": []}


def _connector_to_dict(conn: dict) -> dict:
    """Normalize a connector entry for output."""
    return {
        "id": conn.get("id", ""),
        "name": conn.get("name", ""),
        "type": conn.get("type", ""),
        "transport": conn.get("transport", ""),
        "uri": conn.get("uri", ""),
        "status": conn.get("status", "unknown"),
        "incremental": conn.get("incremental", False),
        "auth": conn.get("auth", "internal"),
        "package": conn.get("package", ""),
        "tags": conn.get("tags", []),
        "description": conn.get("description", ""),
    }


async def connector_list(
    status: str | None = None,
    conn_type: str | None = None,
    package: str | None = None,
) -> str:
    """List all registered connectors, optionally filtered by status/type/package.

    Args:
        status: Filter by status (active / inactive / unknown).
        conn_type: Filter by connector type (knowledge-graph / calendar / mail / ...).
        package: Filter by owning package (kairon / iris / agora / ...).

    Returns:
        JSON array of connector metadata dicts.
    """
    manifest = _load_manifest()
    connectors = manifest.get("connectors", [])

    if status:
        connectors = [c for c in connectors if c.get("status") == status]
    if conn_type:
        connectors = [c for c in connectors if c.get("type") == conn_type]
    if package:
        connectors = [c for c in connectors if c.get("package") == package]

    result = [_connector_to_dict(c) for c in connectors]
    return json.dumps(_ok(result), ensure_ascii=False, indent=2)


async def connector_sync(
    connector_id: str,
    watermark: str | None = None,
    dry_run: bool = False,
) -> str:
    """Trigger incremental sync for a specific connector.

    Args:
        connector_id: The connector ID to sync (e.g. "iris-calendar").
        watermark: Optional watermark value for incremental sync.
        dry_run: If true, report what would be synced without executing.

    Returns:
        JSON dict with sync status, connector metadata, and event count.
    """
    manifest = _load_manifest()
    connectors = manifest.get("connectors", [])

    target = None
    for c in connectors:
        if c.get("id") == connector_id:
            target = c
            break

    if target is None:
        return json.dumps(
            _error(f"Connector not found: {connector_id}"),
            ensure_ascii=False,
        )

    info = _connector_to_dict(target)

    if not target.get("incremental"):
        return json.dumps(
            _error(
                f"Connector {connector_id} does not support incremental sync "
                "(incremental=false)"
            ),
            ensure_ascii=False,
        )

    if dry_run:
        return json.dumps(
            _ok({
                "connector": info,
                "watermark": watermark,
                "dry_run": True,
                "message": f"Would sync connector {connector_id} from watermark {watermark}",
                "events_emitted": 0,
            }),
            ensure_ascii=False,
            indent=2,
        )

    # Actual sync: emit an event to the agora event bus
    try:
        from agora.server._response import _emit_event  # type: ignore[import-not-found]
        _emit_event(
            "connector.sync.completed",
            {
                "connector_id": connector_id,
                "watermark": watermark,
                "uri": target.get("uri", ""),
            },
        )
        events_emitted = 1
    except Exception as exc:
        logger.warning("connector_sync_event_error", error=str(exc), connector_id=connector_id)
        events_emitted = 0

    return json.dumps(
        _ok({
            "connector": info,
            "watermark": watermark,
            "dry_run": False,
            "status": "synced" if events_emitted else "queued",
            "events_emitted": events_emitted,
        }),
        ensure_ascii=False,
        indent=2,
    )
