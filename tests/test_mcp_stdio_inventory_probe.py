"""Subprocess contract for the clean Agora MCP stdio inventory probe."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import selectors
import signal
import subprocess
import structlog
import sys
import tempfile
import time

import pytest


EXPECTED_PROTOCOL_VERSION = "2024-11-05"
EXPECTED_SERVER_IDENTITY_DIGEST = (
    "sha256:2b6afc7cb86fa3f489bc300e346c69456b3320bb9a8d94e737e9b6ef5c02a8ae"
)
EXPECTED_TOOL_INVENTORY_DIGEST = (
    "sha256:69d6598c5de6d6fdcb9639647df0c68326cd676b6090e2cdd410a59490461533"
)
EXPECTED_TOOL_NAMES = frozenset(
    """
    a2a_cancel_task a2a_get_task a2a_list_tasks a2a_send_task add_route
    agora_capability_discover agora_execute audit_query audit_stats bcos_evolve
    bcos_north_star bcos_signals bos_health bos_inbox_archive bos_inbox_draft
    bos_inbox_pending bos_inbox_search bos_inbox_triage bos_inbox_watch
    bos_mesh_dma_status bos_metrics_status bos_middleware_status bos_reload_discovery
    bos_reload_m1 bos_reload_routes bos_spine_diff bos_spine_draft bos_spine_sign
    bos_spine_status cartridge_pack check_health create_api_key daemon_bus_publish
    debt_auto_seed_tool entropy_cleanup_tool get_agent_card get_bos_schema
    get_event_log get_state_transitions governance_auto_fix health_check
    lifecycle_load_all lifecycle_start_watch lifecycle_status lifecycle_stop_watch
    lifecycle_unload_all list_agent_cards list_api_keys list_bos_domains
    list_bos_resources list_bos_tools list_routes list_services mutate_resource
    persona_bdsk_evaluate proposal_triage proxy_add_service proxy_arch_health
    proxy_backend_health proxy_call proxy_connect proxy_governance_status
    proxy_list_tools proxy_omo_debt proxy_remove_service proxy_status publish_event
    read_resource register_push_notification register_service registry_find_agent_tool
    registry_health_tool registry_heartbeat_tool registry_list_agents_tool
    registry_list_tasks_tool registry_register_agent_tool registry_submit_task_tool
    repo_discover repo_install repo_load repo_pipeline repo_search repo_status
    repo_unload resident_roles resident_status resolve_bos_uri revoke_api_key
    route_call rules_lifecycle subscribe_event swarm_nodes swarm_resolve swarm_status
    unwatch_resource watch_resource workflow_capability_health workspace_audit_gitlink
    workspace_audit_governance workspace_audit_lint workspace_audit_ops
    workspace_audit_radar workspace_audit_run workspace_audit_ssot
    """.split()  # noqa: SIM905 - keep the fixed inventory readable by groups.
)


def _request(
    method: str, request_id: int | None = None, params: dict | None = None
) -> str:
    message: dict[str, object] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    return json.dumps(message, separators=(",", ":"))


def _probe_environment(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "AGORA_MCP_INVENTORY_PROBE": "1",
            "FASTMCP_SHOW_SERVER_BANNER": "false",
            "FASTMCP_CHECK_FOR_UPDATES": "off",
            "FASTMCP_LOG_LEVEL": "ERROR",
            "FASTMCP_ENABLE_RICH_LOGGING": "false",
            "HOME": str(root / "home"),
            "TMPDIR": str(root / "tmp"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
            "AGORA_DATA_DIR": str(root / "data" / "agora"),
            "AGORA_STORAGE_PATH": str(root / "data" / "agora" / "services.json"),
            "AGORA_FORGE_REGISTRY": str(root / "config" / "forge-registry.json"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "WORKSPACE": str(root),
            "WORKSPACE_ROOT": str(root),
        }
    )
    source = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source, env.get("PYTHONPATH", "")) if part
    )
    for name in (
        "HOME",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    ):
        Path(env[name]).mkdir(parents=True, exist_ok=True)
    return env


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1.0)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass


def _run_probe(
    project_root: Path, root: Path
) -> tuple[list[str], bytes, int | None, float]:
    env = _probe_environment(root)
    initialize = _request(
        "initialize",
        1,
        {
            "protocolVersion": EXPECTED_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "agora-inventory-test", "version": "1.0"},
        },
    )
    remainder = "\n".join(
        (_request("notifications/initialized"), _request("tools/list", 2))
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "agora.server.mcp"],
        cwd=project_root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    stdout_lines: list[str] = []
    stderr_chunks: list[bytes] = []
    stdout_buffer = b""
    tail_sent = False
    response_two_seen = False
    started = time.monotonic()
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    assert process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    try:
        assert process.stdin is not None
        process.stdin.write((initialize + "\n").encode())
        process.stdin.flush()
        deadline = started + 3.8
        while time.monotonic() < deadline and not response_two_seen:
            events = selector.select(max(0.0, deadline - time.monotonic()))
            if not events:
                break
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    stderr_chunks.append(chunk)
                    continue
                stdout_buffer += chunk
                while b"\n" in stdout_buffer:
                    raw_line, stdout_buffer = stdout_buffer.split(b"\n", 1)
                    line = raw_line.decode(errors="replace")
                    stdout_lines.append(line)
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        not tail_sent
                        and isinstance(message, dict)
                        and message.get("id") == 1
                    ):
                        process.stdin.write((remainder + "\n").encode())
                        process.stdin.flush()
                        tail_sent = True
                    if isinstance(message, dict) and message.get("id") == 2:
                        response_two_seen = True
                        break
        if process.stdin is not None:
            process.stdin.close()
        process.wait(timeout=1.0)
    finally:
        selector.close()
        _terminate_process_group(process)
    if stdout_buffer:
        stdout_lines.append(stdout_buffer.decode(errors="replace"))
    return (
        stdout_lines,
        b"".join(stderr_chunks),
        process.returncode,
        time.monotonic() - started,
    )


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def test_inventory_probe_stdio_transcript_is_clean_and_bounded() -> None:
    """The private probe emits only JSON-RPC through tools/list."""
    project_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="agora-mcp-inventory-") as temp_root:
        root = Path(temp_root)
        stdout_lines, stderr, returncode, elapsed = _run_probe(project_root, root)
        assert len(stderr) <= 65_536, (
            f"stderr exceeded 64 KiB ({len(stderr)} bytes): "
            f"{stderr[:512].decode(errors='replace')!r}"
        )
        responses: list[dict[str, object]] = []
        for line_number, line in enumerate(stdout_lines, 1):
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"stdout line {line_number} is not JSON: {line!r}"
                ) from exc
            assert isinstance(message, dict), (
                f"stdout line {line_number} is not an object"
            )
            assert message.get("jsonrpc") == "2.0", message
            if "id" in message:
                responses.append(message)
        assert [message.get("id") for message in responses] == [1, 2]
        assert elapsed < 4.0, f"probe took {elapsed:.3f}s"
        assert returncode == 0, stderr.decode(errors="replace")

        initialize = responses[0]["result"]
        listing = responses[1]["result"]
        assert isinstance(initialize, dict)
        assert isinstance(listing, dict)
        assert initialize["protocolVersion"] == EXPECTED_PROTOCOL_VERSION
        assert isinstance(initialize["capabilities"], dict)
        server_info = initialize["serverInfo"]
        assert isinstance(server_info, dict)
        assert _canonical_digest(server_info) == EXPECTED_SERVER_IDENTITY_DIGEST
        tools = listing["tools"]
        assert isinstance(tools, list)
        assert len(tools) == 104
        tool_names = [tool["name"] for tool in tools if isinstance(tool, dict)]
        assert frozenset(tool_names) == EXPECTED_TOOL_NAMES
        assert (
            _canonical_digest(sorted(tools, key=lambda tool: tool["name"]))
            == EXPECTED_TOOL_INVENTORY_DIGEST
        )

        assert all(path.is_relative_to(root) for path in root.rglob("*"))


def test_flagless_inventory_probe_logging_hook_is_noop(monkeypatch, tmp_path) -> None:
    """Normal mode keeps the existing logging configuration untouched."""
    monkeypatch.delenv("AGORA_MCP_INVENTORY_PROBE", raising=False)
    monkeypatch.setenv("AGORA_DATA_DIR", str(tmp_path / "agora"))
    import agora.server.mcp as server_mcp

    root_logger = logging.getLogger()
    before = (root_logger.level, tuple(root_logger.handlers))
    monkeypatch.setattr(
        logging, "basicConfig", lambda **kwargs: pytest.fail("configured")
    )
    monkeypatch.setattr(
        structlog, "configure", lambda **kwargs: pytest.fail("configured")
    )
    server_mcp._configure_inventory_probe_logging()
    assert (root_logger.level, tuple(root_logger.handlers)) == before
