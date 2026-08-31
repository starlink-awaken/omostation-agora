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
import threading
import time
from typing import BinaryIO

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
STDERR_CAP_BYTES = 65_536


class _CappedStreamDrain:
    """Drain a pipe fully while retaining only enough output for the cap check."""

    def __init__(self, cap_bytes: int) -> None:
        self._limit = cap_bytes + 1
        self._chunks: list[bytes] = []
        self._captured = 0
        self.overflowed = False

    def drain(self, stream: BinaryIO) -> None:
        while chunk := os.read(stream.fileno(), 65_536):
            remaining = self._limit - self._captured
            if remaining > 0:
                captured = chunk[:remaining]
                self._chunks.append(captured)
                self._captured += len(captured)
            if len(chunk) > remaining:
                self.overflowed = True

    @property
    def captured(self) -> bytes:
        return b"".join(self._chunks)


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


def _process_group_is_gone(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return True
    return False


def _terminate_process_group(process: subprocess.Popen[bytes]) -> bool:
    """Terminate every process in the child session, even after its leader exits."""
    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            return False
    if not _process_group_is_gone(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if _process_group_is_gone(process_group):
            return True
        time.sleep(0.01)
    return _process_group_is_gone(process_group)


def _tree_fingerprint(path: Path) -> tuple[bool, str]:
    """Return a stable metadata fingerprint without inspecting user file contents."""
    if not path.exists():
        return False, "missing"
    digest = hashlib.sha256()
    for entry in sorted(path.rglob("*"), key=lambda candidate: str(candidate)):
        stat = entry.lstat()
        digest.update(str(entry.relative_to(path)).encode())
        digest.update(f"{stat.st_mode}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    return True, digest.hexdigest()


def _worktree_status(project_root: Path) -> bytes:
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(project_root),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ]
    )


def _run_probe(
    project_root: Path, root: Path
) -> tuple[list[str], bytes, bool, int | None, float, bool]:
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
    stderr_drain = _CappedStreamDrain(STDERR_CAP_BYTES)
    assert process.stderr is not None
    stderr_thread = threading.Thread(
        target=stderr_drain.drain,
        args=(process.stderr,),
        name="agora-mcp-stderr-drain",
    )
    stderr_thread.start()
    stdout_buffer = b""
    tail_sent = False
    response_two_seen = False
    started = time.monotonic()
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
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
        exchange_elapsed = time.monotonic() - started
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
    finally:
        selector.close()
        group_gone = _terminate_process_group(process)
        stderr_thread.join(timeout=2.0)
    if stdout_buffer:
        stdout_lines.append(stdout_buffer.decode(errors="replace"))
    assert not stderr_thread.is_alive(), "stderr drain did not reach EOF after teardown"
    return (
        stdout_lines,
        stderr_drain.captured,
        stderr_drain.overflowed,
        process.returncode,
        exchange_elapsed,
        group_gone,
    )


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_inventory_probe_stdio_transcript_is_clean_and_bounded() -> None:
    """The private probe emits only JSON-RPC through tools/list."""
    project_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="agora-mcp-inventory-") as temp_root:
        root = Path(temp_root)
        env = _probe_environment(root)
        configured_destinations = (
            "HOME",
            "TMPDIR",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "AGORA_DATA_DIR",
            "AGORA_STORAGE_PATH",
            "AGORA_FORGE_REGISTRY",
            "WORKSPACE",
            "WORKSPACE_ROOT",
        )
        resolved_root = root.resolve()
        assert all(
            Path(env[name]).resolve().is_relative_to(resolved_root)
            for name in configured_destinations
        )
        real_agora = Path.home() / ".agora"
        external_before = _tree_fingerprint(real_agora)
        worktree_before = _worktree_status(project_root)
        (
            stdout_lines,
            stderr,
            stderr_overflowed,
            returncode,
            exchange_elapsed,
            group_gone,
        ) = _run_probe(project_root, root)
        assert not stderr_overflowed and len(stderr) <= STDERR_CAP_BYTES, (
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
        assert exchange_elapsed < 4.0, f"probe exchange took {exchange_elapsed:.3f}s"
        assert returncode == 0, stderr.decode(errors="replace")
        assert group_gone, "process group still has surviving descendants"

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

        assert _tree_fingerprint(real_agora) == external_before
        assert _worktree_status(project_root) == worktree_before


def test_flagless_inventory_probe_import_is_a_fresh_process_noop() -> None:
    """An import in normal mode preserves preconfigured stdlib and structlog state."""
    project_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="agora-mcp-normal-") as temp_root:
        env = _probe_environment(Path(temp_root))
        env.pop("AGORA_MCP_INVENTORY_PROBE")
        script = """
import json
import logging
import structlog
root = logging.getLogger()
handler = logging.StreamHandler()
root.handlers[:] = [handler]
root.setLevel(logging.INFO)
structlog.configure(
    processors=[structlog.processors.JSONRenderer()],
    cache_logger_on_first_use=False,
)
before = structlog.get_config()
import agora.server.mcp
print(json.dumps({
    'stdlib_unchanged': root.level == logging.INFO and root.handlers == [handler],
    'structlog_unchanged': structlog.get_config() == before,
}))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=project_root,
            env=env,
            capture_output=True,
            check=False,
            timeout=15.0,
        )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    result = json.loads(completed.stdout.decode().splitlines()[-1])
    assert result == {"stdlib_unchanged": True, "structlog_unchanged": True}
