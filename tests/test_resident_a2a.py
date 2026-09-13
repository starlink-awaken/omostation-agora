"""Resident A2A delegation tests (BET-Y1Q4-T5-03).

Verifies the A2A 委托与结果拉取闭环 for the resident-orchestrator:
- list_agent_cards / get_agent_card expose the complete resident card
  (Capabilities + skills: 后台分析 / 深度巡检 / 对账);
- a2a_send_task submits resident tasks with valid task_id + status tracking;
- idempotency_key dedups repeated submissions (circuit_breaker 幂等);
- timeout / impl errors fail closed (task failed, caller never hangs);
- unknown resident.* tool names fail closed without creating a task.

Unit-level: mocks the service registry + resident impls, uses a real
metaos TaskManager backed by tmp storage (same pattern as test_a2a_smoke.py).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastmcp import FastMCP

# 与 test_a2a_smoke.py 对齐: metaos 缺席 (admission extra 未装) 时跳过本模块。
pytest.importorskip(
    "metaos.a2a.task_manager",
    reason="metaos 不可用 (admission optional extra, 非 agora 核心依赖)",
)
from metaos.a2a.task_manager import TaskManager  # noqa: E402


async def _call_tool(mcp: FastMCP, name: str, arguments: dict) -> dict:
    result = await mcp.call_tool(name, arguments)
    return json.loads(result.content[0].text)


@pytest.fixture
def mcp_app():
    """FastMCP instance with governance tools registered."""
    mcp = FastMCP("test-resident-a2a")
    from agora.server.tools_governance import register_governance_tools

    register_governance_tools(mcp)
    return mcp


@pytest.fixture
def task_manager(tmp_path, monkeypatch):
    """Real TaskManager (tmp storage) wired into tools_governance."""
    manager = TaskManager(MagicMock(), storage_path=str(tmp_path / "tasks.json"))
    monkeypatch.setattr(
        "agora.server.tools_governance._get_task_manager", lambda: manager
    )
    return manager


@pytest.fixture
def empty_registry(monkeypatch):
    """Service registry with no services (resident card must still appear)."""
    monkeypatch.setattr(
        "agora.server.tools_governance._get_registry",
        lambda: SimpleNamespace(
            list_all=list,
            get=lambda name: None,
        ),
    )


@pytest.fixture
def fresh_idempotency(monkeypatch):
    """Isolate the resident idempotency index per test."""
    from agora.server import tools_governance as tg

    monkeypatch.setattr(tg, "_RESIDENT_IDEMPOTENCY_INDEX", {})


@pytest.fixture
def fake_resident_impl(monkeypatch):
    """Route resident.status / resident.roles to a deterministic fake impl."""
    from agora.server import tools_governance as tg

    calls: list[str] = []

    def _fake_status() -> dict:
        calls.append("resident.status")
        return {"ok": True, "health": "recovered"}

    def _fake_roles() -> dict:
        calls.append("resident.roles")
        return {"ok": True, "roles": ["sediment"]}

    table = {"resident.status": _fake_status, "resident.roles": _fake_roles}
    monkeypatch.setattr(tg, "resolve_resident_tool", table.get)
    return calls


class TestResidentAgentCard:
    async def test_list_agent_cards_contains_resident_orchestrator(
        self, mcp_app, task_manager, empty_registry
    ):
        out = await _call_tool(mcp_app, "list_agent_cards", {})
        assert out["status"] == "ok"
        card = out["agent_cards"]["resident-orchestrator"]
        assert card["name"] == "resident-orchestrator"
        assert "a2a-task-delegation" in card["capabilities"]
        skill_ids = {
            s["id"] if isinstance(s, dict) else s for s in card["skills"]
        }
        assert {
            "background-analysis",
            "deep-inspection",
            "reconciliation",
        } <= skill_ids

    async def test_get_agent_card_resident_orchestrator(
        self, mcp_app, task_manager, empty_registry
    ):
        out = await _call_tool(
            mcp_app, "get_agent_card", {"name": "resident-orchestrator"}
        )
        assert out["status"] == "ok"
        card = out["agent_card"]
        assert card["name"] == "resident-orchestrator"
        assert card["capabilities"]
        assert card["skills"]


class TestResidentTaskDelegation:
    async def test_deferred_resident_task_submitted_and_tracked(
        self, mcp_app, task_manager, fake_resident_impl, fresh_idempotency
    ):
        sent = await _call_tool(
            mcp_app,
            "a2a_send_task",
            {
                "tool_name": "resident.status",
                "arguments": "{}",
                "session_id": "t5-03-loop",
                "execute_immediately": False,
            },
        )
        assert sent["status"] == "ok"
        task_id = sent["task"]["id"]
        assert task_id.startswith("task_")
        assert sent["task"]["status"] == "submitted"
        assert sent["task"]["service_name"] == "resident-orchestrator"

        queried = await _call_tool(mcp_app, "a2a_get_task", {"task_id": task_id})
        assert queried["task"]["id"] == task_id
        assert queried["task"]["status"] == "submitted"

        canceled = await _call_tool(mcp_app, "a2a_cancel_task", {"task_id": task_id})
        assert canceled["status"] == "ok"
        assert canceled["task"]["status"] == "canceled"

    async def test_immediate_resident_task_completes_with_result(
        self, mcp_app, task_manager, fake_resident_impl, fresh_idempotency
    ):
        sent = await _call_tool(
            mcp_app,
            "a2a_send_task",
            {"tool_name": "resident.status", "arguments": "{}"},
        )
        assert sent["status"] == "ok"
        assert sent["task"]["status"] == "completed"
        assert sent["task"]["result"] == {"ok": True, "health": "recovered"}
        assert sent["task"]["bos_uri"] == "bos://governance/resident/status"
        assert fake_resident_impl == ["resident.status"]

    async def test_idempotency_key_dedups_repeat_submission(
        self, mcp_app, task_manager, fake_resident_impl, fresh_idempotency
    ):
        base = {
            "tool_name": "resident.roles",
            "arguments": "{}",
            "idempotency_key": "t5-03-idem-001",
        }
        first = await _call_tool(mcp_app, "a2a_send_task", dict(base))
        second = await _call_tool(mcp_app, "a2a_send_task", dict(base))
        assert first["status"] == "ok" and second["status"] == "ok"
        assert first["task"]["id"] == second["task"]["id"]
        assert second.get("deduplicated") is True
        # 只执行一次、只建一个 task。
        assert fake_resident_impl == ["resident.roles"]
        assert len(task_manager.list_tasks()) == 1

    async def test_unknown_resident_tool_fails_closed_without_task(
        self, mcp_app, task_manager, fresh_idempotency
    ):
        out = await _call_tool(
            mcp_app,
            "a2a_send_task",
            {"tool_name": "resident.nonexistent", "arguments": "{}"},
        )
        assert out["status"] == "error"
        assert task_manager.list_tasks() == []

    async def test_impl_error_marks_task_failed(
        self, mcp_app, task_manager, fresh_idempotency, monkeypatch
    ):
        from agora.server import tools_governance as tg

        def _boom() -> dict:
            raise RuntimeError("resident backend offline")

        monkeypatch.setattr(
            tg, "resolve_resident_tool", lambda name: _boom
        )
        sent = await _call_tool(
            mcp_app,
            "a2a_send_task",
            {"tool_name": "resident.status", "arguments": "{}"},
        )
        assert sent["status"] == "ok"
        assert sent["task"]["status"] == "failed"
        assert "offline" in sent["task"]["error"]

    async def test_dispatch_timeout_fails_closed(
        self, mcp_app, task_manager, fresh_idempotency, monkeypatch
    ):
        import time

        from agora.server import tools_governance as tg

        def _slow() -> dict:
            time.sleep(5)
            return {"ok": True}

        monkeypatch.setattr(tg, "resolve_resident_tool", lambda name: _slow)
        sent = await asyncio.wait_for(
            _call_tool(
                mcp_app,
                "a2a_send_task",
                {
                    "tool_name": "resident.status",
                    "arguments": "{}",
                    "timeout_s": 0.2,
                },
            ),
            timeout=30,
        )
        assert sent["status"] == "ok"
        assert sent["task"]["status"] == "failed"
        assert "timed out" in sent["task"]["error"]


class TestResidentTaskWorkingProgression:
    """Long-running resident task semantics: working status progression + result retrieval."""

    async def test_deferred_task_can_be_set_to_working(
        self, mcp_app, task_manager, fake_resident_impl, fresh_idempotency
    ):
        sent = await _call_tool(
            mcp_app,
            "a2a_send_task",
            {
                "tool_name": "resident.status",
                "arguments": "{}",
                "session_id": "t5-03-batch2-working",
                "execute_immediately": False,
            },
        )
        assert sent["status"] == "ok"
        task_id = sent["task"]["id"]
        assert sent["task"]["status"] == "submitted"

        updated = await _call_tool(
            mcp_app,
            "a2a_update_task",
            {"task_id": task_id, "status": "working"},
        )
        assert updated["status"] == "ok"
        assert updated["task"]["status"] == "working"

        queried = await _call_tool(mcp_app, "a2a_get_task", {"task_id": task_id})
        assert queried["task"]["status"] == "working"

    async def test_working_task_can_be_completed_with_result(
        self, mcp_app, task_manager, fake_resident_impl, fresh_idempotency
    ):
        sent = await _call_tool(
            mcp_app,
            "a2a_send_task",
            {
                "tool_name": "resident.status",
                "arguments": "{}",
                "session_id": "t5-03-batch2-complete",
                "execute_immediately": False,
            },
        )
        assert sent["status"] == "ok"
        task_id = sent["task"]["id"]

        await _call_tool(
            mcp_app,
            "a2a_update_task",
            {"task_id": task_id, "status": "working"},
        )
        result_payload = {"ok": True, "health": "degraded", "note": "batch2-result"}
        completed = await _call_tool(
            mcp_app,
            "a2a_update_task",
            {"task_id": task_id, "status": "completed", "result": result_payload},
        )
        assert completed["status"] == "ok"
        assert completed["task"]["status"] == "completed"
        assert completed["task"]["result"] == result_payload

        queried = await _call_tool(mcp_app, "a2a_get_task", {"task_id": task_id})
        assert queried["task"]["status"] == "completed"
        assert queried["task"]["result"] == result_payload
