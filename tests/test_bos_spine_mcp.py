"""BOS Spine MCP API 与注册表单元测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import pytest
import yaml

from agora.server.tools_bos import (
    bos_spine_diff,
    bos_spine_distill,
    bos_spine_draft,
    bos_spine_replay,
    bos_spine_sign,
    bos_spine_status,
    bos_mesh_dma_status,
)


def test_bos_services_registry_spine_entries():
    """验证 bos-services.yaml 中正式注册了完整的 spine BOS 服务。"""
    cfg_file = Path(__file__).resolve().parents[1] / "etc" / "bos-services.yaml"
    assert cfg_file.exists()
    data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    services = data.get("services", [])
    uris = [s.get("uri") for s in services]

    expected_uris = [
        "bos://cockpit/spine/draft",
        "bos://cockpit/spine/sign",
        "bos://cockpit/spine/diff",
        "bos://cockpit/spine/status",
        "bos://cockpit/spine/distill",
        "bos://cockpit/spine/replay",
        "bos://compute/omlxc/replay",
        "bos://compute/omlxc/lora",
        "bos://compute/omlxc/dma",
    ]

    for uri in expected_uris:
        assert uri in uris, f"Missing {uri} in bos-services.yaml"


@pytest.mark.asyncio
async def test_bos_spine_replay_mcp_structure(monkeypatch):
    """验证 bos_spine_replay 输出格式遵循标准。"""
    fake_json = json.dumps({
        "status": "ok",
        "total_domains": 3,
        "domains": {"document-review": {"size": 12}}
    })

    class FakeProc:
        returncode = 0
        stdout = fake_json
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeProc())
    res = await bos_spine_replay()
    assert res["status"] == "ok"
    assert "replay_stats" in res
    assert res["replay_stats"]["total_domains"] == 3


@pytest.mark.asyncio
async def test_bos_spine_distill_mcp_structure(monkeypatch):
    """验证 bos_spine_distill 输出结构正确。"""
    class FakeProc:
        returncode = 0
        stdout = "distillation successful"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeProc())
    res = await bos_spine_distill(domain="document-review", epochs=2)
    assert res["status"] == "ok"
    assert res["domain"] == "document-review"
