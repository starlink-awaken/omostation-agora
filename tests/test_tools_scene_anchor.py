"""Tests for agora tools_scene.py (BET-Y1Q4-T7-04): bos://scene/anchor facade.

facade 契约面测试 —— omo 内核可导入时走真实内核; 不可导入时必须诚实返回
kernel_unavailable (绝不伪造护栏判定)。
"""

from __future__ import annotations

import pytest

from agora.server.tools_scene import (
    SCHEMA,
    SERVICE_URI,
    scene_anchor,
    scene_anchor_contract,
)


def test_contract_metadata():
    contract = scene_anchor_contract()
    assert contract["uri"] == SERVICE_URI == "bos://scene/anchor"
    assert contract["schema"] == SCHEMA
    assert set(contract["actions"]) == {"recommend", "bind", "verify", "guard"}
    assert contract["honest_failure"] == "kernel_unavailable"


def test_unknown_action_rejected():
    result = scene_anchor("nope")
    assert result["ok"] is False and result["error"] == "unknown_action"


def _kernel_available() -> bool:
    try:
        import omo.scene.anchor  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _kernel_available(), reason="omo kernel not importable")
class TestFacadeWithKernel:
    def test_recommend_returns_ranked_scenes(self):
        result = scene_anchor("recommend", text="审阅公文并归档", top_k=3)
        assert result["ok"] is True and result["schema"] == SCHEMA
        recs = result["recommendations"]
        assert isinstance(recs, list)
        assert all({"scene_id", "name", "domain", "score", "confidence"} == set(r) for r in recs)

    def test_bind_verify_roundtrip(self):
        bound = scene_anchor("bind", session_id="facade-test", scene_id="scene-document-review")
        if bound["ok"] is False and bound["error"] == "scene_cards_unavailable":
            pytest.skip("scene-cards-v3.yaml not found in workspace")
        assert bound["ok"] is True
        token = bound["token"]
        verified = scene_anchor("verify", token=token)
        assert verified["ok"] is True and verified["verification"]["ok"] is True

    def test_guard_denies_out_of_scope_tool(self):
        bound = scene_anchor("bind", session_id="facade-guard", scene_id="scene-document-review")
        if bound["ok"] is False and bound["error"] == "scene_cards_unavailable":
            pytest.skip("scene-cards-v3.yaml not found in workspace")
        verdict = scene_anchor("guard", token=bound["token"], tool="shell.exec")
        assert verdict["ok"] is True
        assert verdict["decision"] in ("deny", "intercept")


def test_guard_never_fabricates_without_kernel(monkeypatch: pytest.MonkeyPatch):
    """内核不可导入时 facade 必须诚实失败 —— 护栏判定零伪造."""
    import importlib

    def _blocked(name: str, *args, **kwargs):
        raise ImportError(f"blocked for test: {name}")

    monkeypatch.setattr(importlib, "import_module", _blocked)
    result = scene_anchor("guard", token={"scene_id": "x"}, tool="shell.exec")
    assert result["ok"] is False
    assert result["error"] == "kernel_unavailable"
    assert "never fabricated" in result["detail"]
