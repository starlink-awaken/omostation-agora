"""Audit/Governance/A2A MCP tools — extracted from server/mcp.py (God Module Phase 1).

Provides tools for audit queries, push notifications, A2A task management,
and agent card discovery.
"""

from __future__ import annotations

import asyncio
import json
import os

import structlog
from fastmcp import FastMCP

from agora.core.service_base import is_safe_url  # type: ignore[import-not-found]
from agora.plugins.identity.agent_card import (
    service_to_agent_card,  # type: ignore[import-not-found]
)
from agora.server._response import FORMAT_VERSION, _error, _ok
from agora.server.tools_resident import (
    RESIDENT_ORCHESTRATOR_NAME,
    build_resident_orchestrator_card,
    resolve_resident_tool,
)

logger = structlog.get_logger(__name__)


# ── Resident A2A dispatch guards (BET-Y1Q4-T5-03, circuit_breaker) ──
# 幂等 ID → task_id 索引：重复提交同一 idempotency_key 直接返回原 task，
# 不重复执行。进程内索引 + TaskManager 持久化双层保障。
_RESIDENT_IDEMPOTENCY_INDEX: dict[str, str] = {}

# 本地 resident 派发默认超时 (秒)：超时只标记 task failed，严禁挂起主进程。
RESIDENT_DISPATCH_TIMEOUT_S = 120.0


def _get_registry():
    """Lazy-import ServiceRegistry from mcp.py."""
    from agora.server.mcp import registry  # type: ignore[import-not-found]

    return registry


def _get_bus():
    """Lazy-import EventBus from mcp.py."""
    from agora.server.mcp import _bus  # type: ignore[import-not-found]

    return _bus


def _get_auditor():
    """Lazy-import AuditSubscriber from mcp.py."""
    from agora.server.mcp import _auditor  # type: ignore[import-not-found]

    return _auditor


def _get_proxy_manager():
    """Lazy-import ProxyManager from dependencies.py."""
    from agora.server.dependencies import get_proxy_manager

    return get_proxy_manager()


def _get_task_manager():
    """Lazy-init and return the global TaskManager from mcp.py."""
    from agora.server.mcp import _get_task_manager  # type: ignore[import-not-found]

    return _get_task_manager()


# ── A2A Convergence Helper (ADR-0300) ──────────────────────────────


def _resolve_convergence_meta(
    tool_name: str, run_id: str = "", bos_uri: str = ""
) -> dict[str, str]:
    """Derive OMO Agent Workflow Run-ID and BOS 5-domain convergence metadata (ADR-0300)."""
    resolved_run_id = (
        run_id
        or os.environ.get("AGCP_RUN_ID", "")
        or os.environ.get("OMO_WORKFLOW_RUN_ID", "")
    )
    resolved_uri = bos_uri
    if not resolved_uri and tool_name:
        parts = tool_name.split(".", 1)
        pkg = parts[0] if parts else "unknown"
        action = parts[1] if len(parts) > 1 else "execute"
        domain_map = {
            "kems": "memory",
            "kos": "memory",
            "eidos": "memory",
            "minerva": "analysis",
            "codeanalyze": "analysis",
            "iris": "analysis",
            "ontoderive": "analysis",
            "metaos": "governance",
            "omo": "governance",
            "resident": "governance",
            "cockpit": "capability",
            "aetherforge": "compute",
        }
        dom = domain_map.get(pkg, "capability")
        resolved_uri = f"bos://{dom}/{pkg}/{action}"

    domain = "capability"
    if resolved_uri.startswith("bos://"):
        segments = resolved_uri[len("bos://") :].split("/", 1)
        if segments:
            domain = segments[0]

    return {
        "run_id": resolved_run_id,
        "bos_uri": resolved_uri,
        "domain": domain,
        "adr_policy": "ADR-0300",
    }


# ── Agent Card helpers ──────────────────────────────────────────────


def _get_proxy_tools(service_name: str) -> list[dict]:
    """Collect proxy tool descriptions for a service.

    Returns list of tool dicts with name/description/inputSchema keys,
    or empty list if proxy manager is not initialized or has no matching tools.
    """
    pm = _get_proxy_manager()
    if not pm:
        return []
    tools = []
    for entry in pm.registry.entries.values():
        if entry.service_name == service_name:
            tools.append(
                {
                    "name": entry.original_name,
                    "description": entry.description,
                    "inputSchema": entry.parameters,
                }
            )
    return tools


def _build_agent_card(service_name: str) -> tuple[dict | None, str | None]:
    """Build an A2A Agent Card dict for a registered service.

    Returns (card_dict, None) on success, or (None, error_message) on failure.
    """
    registry = _get_registry()
    svc = registry.get(service_name)
    if not svc:
        return None, f"Service '{service_name}' not found"
    try:
        tools = _get_proxy_tools(service_name)
        tags = (
            svc.tags
            if isinstance(svc.tags, list)
            else (svc.tags.split(",") if svc.tags else [])
        )

        # Check if authentication is configured
        from agora.governance import KeyManager  # type: ignore[import-not-found]

        has_auth = KeyManager().has_keys()

        card = service_to_agent_card(
            name=svc.name,
            description=svc.description,
            protocol=svc.protocol,
            mcp_endpoint=svc.mcp_endpoint,
            port=svc.port,
            tags=tags,
            tools=tools if tools else None,
            has_auth=has_auth,
            has_push_notifications=_get_bus().has_push_subscribers(),
            has_state_transitions=bool(registry.get_transitions(limit=1)),
            provider_info={"organization": "Agora Hub"},
            documentation_url="https://github.com/starlink-awaken/agora",
        )
        return card.to_dict(), None
    except Exception as e:  # defensive fallback
        return None, str(e)


async def _dispatch_resident_task(
    tm: object,
    tool_name: str,
    args: dict,
    session_id: str,
    run_id: str,
    bos_uri: str,
    execute_immediately: bool,
    idempotency_key: str = "",
    timeout_s: float = RESIDENT_DISPATCH_TIMEOUT_S,
) -> dict:
    """Local short-circuit dispatch for resident.* A2A tasks (BET-Y1Q4-T5-03).

    Bypasses the core Router (non-goal: 不重构核心调度协议) and invokes the
    resident impl from tools_resident.py directly:
    - unknown resident.* tool name → fail closed (no task created);
    - repeated idempotency_key → return the original task, never re-execute;
    - execution wrapped in asyncio.wait_for → timeout marks task failed,
      never hangs the caller.
    """
    impl = resolve_resident_tool(tool_name)
    if impl is None:
        return _error(
            f"Unknown resident tool '{tool_name}' "
            f"(available: resident.status, resident.roles)"
        )

    if idempotency_key:
        existing_id = _RESIDENT_IDEMPOTENCY_INDEX.get(idempotency_key)
        if existing_id:
            existing = tm.get_task(existing_id)  # type: ignore[reportAttributeAccessIssue]
            if existing is not None:
                return _ok(
                    {
                        "format_version": FORMAT_VERSION,
                        "task": existing.to_dict(),
                        "deduplicated": True,
                        "idempotency_key": idempotency_key,
                    }
                )

    task = tm.create_task(RESIDENT_ORCHESTRATOR_NAME, tool_name, args, session_id)  # type: ignore[reportCallIssue,reportAttributeAccessIssue]
    if idempotency_key:
        _RESIDENT_IDEMPOTENCY_INDEX[idempotency_key] = task.id

    if execute_immediately:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(impl), timeout=timeout_s
            )
            updated = tm.update_task(task.id, "completed", result=result if isinstance(result, dict) else {"payload": result})  # type: ignore[reportAttributeAccessIssue]
        except TimeoutError:
            logger.warning(
                "resident_dispatch_timeout",
                extra={"tool": tool_name, "timeout_s": timeout_s},
            )
            updated = tm.update_task(task.id, "failed", error=f"resident dispatch timed out after {timeout_s}s")  # type: ignore[reportAttributeAccessIssue]
        except Exception as e:  # defensive fallback → fail closed
            logger.exception("resident_dispatch_error")
            updated = tm.update_task(task.id, "failed", error=str(e)[:500])  # type: ignore[reportAttributeAccessIssue]
        if updated is None:
            return _error("Task execution returned no result")
        task = updated

    convergence_meta = _resolve_convergence_meta(tool_name, run_id, bos_uri)
    result_dict = task.to_dict()
    if convergence_meta["run_id"]:
        result_dict["run_id"] = convergence_meta["run_id"]
    result_dict["bos_uri"] = convergence_meta["bos_uri"]

    return _ok(
        {
            "format_version": FORMAT_VERSION,
            "task": result_dict,
            "convergence_meta": convergence_meta,
        }
    )


# ═══════════════════════════════════════════════════════════════
# Tool Registration
# ═══════════════════════════════════════════════════════════════


def register_governance_tools(mcp: FastMCP) -> None:
    """Register all audit/governance/A2A MCP tools."""

    # ── audit_query ───────────────────────────────────────────────

    @mcp.tool()
    def audit_query(
        actor: str = "",
        resource: str = "",
        event_type: str = "",
        since: str = "",
        limit: int = 50,
    ) -> dict:
        """Query the audit log for persisted events.

        Use this for debugging, compliance checks, and understanding
        what has happened in the system over time.

        Args:
            actor: Filter by actor (e.g., 'registry', 'pipeline', 'proxy', 'system')
            resource: Filter by resource type (e.g., 'service', 'route', 'proxy', 'system')
            event_type: Filter by event type pattern (e.g., 'registry:*', 'error:*')
            since: ISO timestamp (e.g., '2026-05-01T00:00:00Z')
            limit: Max results (default 50)

        Returns filtered audit log entries with metadata.
        """
        auditor = _get_auditor()
        entries = auditor.query(
            actor=actor,
            resource=resource,
            event_type=event_type,
            since=since,
            limit=limit,
        )
        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "entries": entries,
                "count": len(entries),
            }
        )

    # ── audit_stats ─────────────────────────────────────────────────

    @mcp.tool()
    def audit_stats(since: str = "") -> dict:
        """Get audit log statistics — counts grouped by risk level and event type.

        Args:
            since: ISO timestamp to filter from (e.g., '2026-05-01T00:00:00Z')

        Returns summary stats useful for dashboards and monitoring.
        """
        auditor = _get_auditor()
        stats = auditor.stats(since=since)
        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "stats": stats,
            }
        )

    # ── register_push_notification ─────────────────────────────────

    @mcp.tool()
    def register_push_notification(callback_url: str, event_types: str = "*") -> dict:
        """Register a webhook callback for push notification delivery.

        When matching events occur, Agora will POST the event payload
        to the specified callback URL (with retry up to 3 attempts).

        Args:
            callback_url: HTTP endpoint to receive push notifications
            event_types: Comma-separated event type patterns
                         (e.g. 'registry:*,route:call.failed' or '*' for all)
        """
        if not callback_url or not callback_url.startswith("http"):
            return _error("callback_url must be a valid HTTP URL")
        if not is_safe_url(callback_url):
            return _error(f"Unsafe callback URL: {callback_url}")

        patterns = [p.strip() for p in event_types.split(",") if p.strip()]

        bus = _get_bus()
        results = {}
        for pattern in patterns:
            sub_id = bus.subscribe("a2a-push", pattern, callback_url)
            results[pattern] = sub_id

        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "subscriptions": results,
                "callback_url": callback_url,
            }
        )

    # ── a2a_send_task ──────────────────────────────────────────────

    @mcp.tool()
    async def a2a_send_task(
        tool_name: str,
        arguments: str = "{}",
        session_id: str = "",
        run_id: str = "",
        bos_uri: str = "",
        execute_immediately: bool = True,
        idempotency_key: str = "",
        timeout_s: float = RESIDENT_DISPATCH_TIMEOUT_S,
    ) -> dict:
        """Submit an A2A task, optionally executing it immediately (ADR-0300).

        By default the task is routed synchronously for backward compatibility.
        Deferred tasks remain submitted so callers can query or cancel them.

        `resident.*` tool names are short-circuited to the local resident
        orchestrator (BET-Y1Q4-T5-03): idempotent via `idempotency_key`,
        timeout-isolated via `timeout_s`, fail-closed on unknown tools.

        Args:
            tool_name: Full tool name (e.g. 'minerva.research_now')
            arguments: JSON string of tool arguments
            session_id: Optional session identifier for grouping related tasks
            run_id: Optional OMO Agent Workflow run identifier for task convergence
            bos_uri: Optional BOS URI mapping for 5-domain convergence
            execute_immediately: Execute now when true; otherwise leave submitted
            idempotency_key: Optional idempotency ID (resident.* only) —
                repeated submissions return the original task without re-executing
            timeout_s: Local dispatch timeout in seconds (resident.* only)
        """
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
        except (json.JSONDecodeError, TypeError):
            return _error("arguments must be a valid JSON object")
        if not isinstance(args, dict):
            return _error("arguments must be a valid JSON object")

        tm = _get_task_manager()
        if tool_name == "resident" or tool_name.startswith("resident."):
            return await _dispatch_resident_task(
                tm,
                tool_name,
                args,
                session_id,
                run_id,
                bos_uri,
                execute_immediately,
                idempotency_key=idempotency_key,
                timeout_s=timeout_s,
            )
        task = tm.create_task("", tool_name, args, session_id)  # type: ignore[reportCallIssue]
        result = task
        if execute_immediately:
            result = await tm.execute_task(task.id)  # type: ignore[reportAttributeAccessIssue]
            if result is None:
                return _error("Task execution returned no result")

        convergence_meta = _resolve_convergence_meta(tool_name, run_id, bos_uri)
        result_dict = result.to_dict()
        if convergence_meta["run_id"]:
            result_dict["run_id"] = convergence_meta["run_id"]
        result_dict["bos_uri"] = convergence_meta["bos_uri"]

        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "task": result_dict,
                "convergence_meta": convergence_meta,
            }
        )

    # ── a2a_get_task ───────────────────────────────────────────────

    @mcp.tool()
    def a2a_get_task(task_id: str) -> dict:
        """Get an A2A task's current status and result.

        Args:
            task_id: The task ID to query
        """
        tm = _get_task_manager()
        task = tm.get_task(task_id)
        if task is None:
            return _error(f"Task '{task_id}' not found")

        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "task": task.to_dict(),  # type: ignore[reportAttributeAccessIssue]
            }
        )

    # ── a2a_cancel_task ────────────────────────────────────────────

    @mcp.tool()
    def a2a_cancel_task(task_id: str) -> dict:
        """Cancel a submitted or in-progress A2A task.

        Only tasks in 'submitted' or 'working' state can be canceled.

        Args:
            task_id: The task ID to cancel
        """
        tm = _get_task_manager()
        if tm.cancel_task(task_id):
            task = tm.get_task(task_id)
            return _ok(
                {
                    "format_version": FORMAT_VERSION,
                    "action": "canceled",
                    "task_id": task_id,
                    "task": task.to_dict() if task else None,  # type: ignore[reportAttributeAccessIssue]
                }
            )
        else:
            return _error(f"Task '{task_id}' not found or already completed")

    # ── a2a_list_tasks ─────────────────────────────────────────────

    @mcp.tool()
    def a2a_list_tasks(
        service: str = "", status: str = "", since: str = "", limit: int = 50
    ) -> dict:
        """List A2A tasks with optional filters.

        Args:
            service: Filter by service name (empty returns all)
            status: Filter by status — submitted | working | completed | failed | canceled (empty returns all)
            since: ISO timestamp lower bound (e.g. '2026-05-01T00:00:00Z')
            limit: Max results (default 50)
        """
        tm = _get_task_manager()
        tasks = tm.list_tasks(service=service, status=status, since=since, limit=limit)  # type: ignore[reportAttributeAccessIssue]
        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "tasks": [t.to_dict() for t in tasks],
                "count": len(tasks),
            }
        )

    # ── list_agent_cards ───────────────────────────────────────────

    @mcp.tool()
    def list_agent_cards() -> dict:
        """List all registered Agent Cards — A2A-compatible agent metadata.

        Returns a mapping of service name → Agent Card for every registered
        service, including basic identity, capabilities, and skills.

        Use this tool when you (or another agent) need to discover what
        agents/services are available through the Agora hub.
        """
        registry = _get_registry()
        services = registry.list_all()
        cards = {}
        for svc in services:
            card, err = _build_agent_card(svc.name)
            cards[svc.name] = card if card else {"error": err}

        # BET-Y1Q4-T5-03: resident-orchestrator is always discoverable, even
        # when no same-named service is registered in the hub registry.
        if RESIDENT_ORCHESTRATOR_NAME in cards and isinstance(
            cards[RESIDENT_ORCHESTRATOR_NAME], dict
        ):
            cards[RESIDENT_ORCHESTRATOR_NAME] = build_resident_orchestrator_card(
                cards[RESIDENT_ORCHESTRATOR_NAME]
            )
        else:
            cards[RESIDENT_ORCHESTRATOR_NAME] = build_resident_orchestrator_card()

        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "agent_cards": cards,
                "count": len(cards),
            }
        )

    # ── get_agent_card ─────────────────────────────────────────────

    @mcp.tool()
    def get_agent_card(name: str) -> dict:
        """Get the Agent Card for a specific service.

        Args:
            name: Service name (e.g., 'minerva', 'kos', 'sophia')

        Returns a single A2A-compatible Agent Card with identity, capabilities,
        and skills for the requested service.
        """
        # BET-Y1Q4-T5-03: well-known resident orchestrator card (registry merge
        # when a same-named service exists, canonical static card otherwise).
        if name == RESIDENT_ORCHESTRATOR_NAME:
            registry_card = None
            try:
                registry = _get_registry()
                if registry.get(name):
                    registry_card, _ = _build_agent_card(name)
            except Exception:  # defensive fallback → canonical static card
                registry_card = None
            return _ok(
                {
                    "format_version": FORMAT_VERSION,
                    "agent_card": build_resident_orchestrator_card(registry_card),
                }
            )
        card, err = _build_agent_card(name)
        if card is None:
            return _error(err or "Failed to build Agent Card")
        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "agent_card": card,
            }
        )

    # ── governance_auto_fix ────────────────────────────────────────

    @mcp.tool()
    def governance_auto_fix() -> dict:
        """Trigger the workspace self-healing auto-fix loop.

        Scans for structural drift (missing frontmatter, orphan scripts,
        stale cell state) and applies safe automatic repairs.

        Returns status and summary of repaired findings.
        """
        import subprocess

        workspace_root = os.environ.get("WORKSPACE_ROOT", os.getcwd())
        res = subprocess.run(
            ["uv", "run", "python", "bin/gac/auto-fix-loop.py", "--apply", "--json"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
        )
        try:
            data = json.loads(res.stdout) if res.stdout else {}
        except Exception:
            data = {"raw": res.stdout, "stderr": res.stderr}
        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "returncode": res.returncode,
                "result": data,
            }
        )

    # ── cartridge_pack ─────────────────────────────────────────────

    @mcp.tool()
    def cartridge_pack(domain_path: str) -> dict:
        """Hot-compile and sign a domain directory into a .cartridge capsule.

        Args:
            domain_path: Relative or absolute path to domain directory (e.g. 'domains/weijian-governance')

        Returns packaging status and output path.
        """
        import subprocess

        workspace_root = os.environ.get("WORKSPACE_ROOT", os.getcwd())
        res = subprocess.run(
            ["uv", "run", "cockpit", "cartridge", "pack", domain_path],
            cwd=workspace_root,
            capture_output=True,
            text=True,
        )
        return _ok(
            {
                "format_version": FORMAT_VERSION,
                "returncode": res.returncode,
                "output": res.stdout.strip(),
                "error": res.stderr.strip() if res.returncode != 0 else "",
            }
        )

    # ── daemon_bus_publish ─────────────────────────────────────────

    @mcp.tool()
    def daemon_bus_publish(topic: str, payload: dict) -> dict:
        """Publish a real-time event to the Agora 2.0 In-Memory Daemon bus.

        Args:
            topic: Channel topic (e.g., 'governance:drift', 'workflow:run')
            payload: JSON payload data dict

        Returns publish confirmation and notification count.
        """
        import urllib.request

        req_data = json.dumps({"topic": topic, "payload": payload}).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:7432/publish",
            data=req_data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return _ok({"format_version": FORMAT_VERSION, "bus_status": data})
        except Exception as exc:
            return _error(f"Daemon bus offline or unreachable on :7432 ({exc})")
