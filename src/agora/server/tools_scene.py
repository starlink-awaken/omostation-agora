"""agora tools_scene — 场景导航锚点 BOS 网关 (BET-Y1Q4-T7-04).

BOS 工具契约: ``bos://scene/anchor``
    recommend: 场景意图自动推荐 (确定性评分, 零模型调用)
        入参: text(意图文本), top_k(可选, 默认 3)
        出参: [{scene_id, name, domain, score, confidence}]
    bind: 锚定会话到场景, 签发锚令牌
        入参: session_id, scene_id, allowed_roots(可选写沙盒)
        出参: 锚令牌 (含 digest, 供护栏校验)
    verify: 锚令牌校验 (digest 防篡改 + 场景存活性)
        入参: token
    guard: 运行时护栏裁决 (CapabilityJail/DataScopeGuard/DriftRadar)
        入参: token, tool, path(可选), mode(可选), domain(可选)

内核逻辑在 omo.scene.anchor / omo.guardrail.enforcer (L2 治理内核)。
L3 织层 special:true 可调用任何层 —— 本 facade 优先 in-process 动态导入
omo 内核; omo 不可用时返回 ``kernel_unavailable`` 诚实失败,
绝不在 facade 层伪造护栏判定。
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

SCHEMA = "agora.tools_scene.anchor.v1"
SERVICE_URI = "bos://scene/anchor"

_ACTIONS = ("recommend", "bind", "verify", "guard")


def _kernel():
    """动态加载 omo 内核模块; 不可用时抛 ImportError 由调用方诚实失败."""
    anchor = importlib.import_module("omo.scene.anchor")
    guardrail = importlib.import_module("omo.guardrail.enforcer")
    return anchor, guardrail


def _find_scene_cards() -> Path | None:
    """在 workspace 内向上定位 scene-cards-v3.yaml."""
    anchor, _ = _kernel()
    return anchor.find_scene_cards_path()


def scene_anchor(action: str, **kwargs: Any) -> dict[str, Any]:
    """bos://scene/anchor 统一入口.

    返回 {ok, ...} 或 {ok: False, error: kernel_unavailable/..., detail}。
    """
    if action not in _ACTIONS:
        return {"ok": False, "error": "unknown_action", "detail": f"expected one of {_ACTIONS}"}
    try:
        anchor_mod, guard_mod = _kernel()
    except ImportError as exc:
        return {
            "ok": False,
            "error": "kernel_unavailable",
            "detail": f"omo kernel not importable: {exc}; guard verdicts are never fabricated here",
        }
    try:
        if action == "recommend":
            registry = anchor_mod.SceneAnchorRegistry.load(_find_scene_cards())
            recs = registry.recommend(str(kwargs.get("text") or ""), int(kwargs.get("top_k") or 3))
            return {
                "ok": True,
                "schema": SCHEMA,
                "recommendations": [
                    {
                        "scene_id": r.scene_id,
                        "name": r.name,
                        "domain": r.domain,
                        "score": r.score,
                        "confidence": r.confidence,
                    }
                    for r in recs
                ],
            }
        if action == "bind":
            registry = anchor_mod.SceneAnchorRegistry.load(_find_scene_cards())
            token = registry.bind(
                str(kwargs.get("session_id") or ""),
                str(kwargs.get("scene_id") or ""),
                allowed_roots=[str(r) for r in (kwargs.get("allowed_roots") or [])],
            )
            return {"ok": True, "schema": SCHEMA, "token": token}
        if action == "verify":
            token = kwargs.get("token") or {}
            registry = anchor_mod.SceneAnchorRegistry.load(_find_scene_cards())
            return {"ok": True, "schema": SCHEMA, "verification": registry.verify(token)}
        # guard
        token = kwargs.get("token") or {}
        enforcer = guard_mod.GuardrailEnforcer(token=token)
        verdict = enforcer.enforce(
            tool=str(kwargs.get("tool") or ""),
            path=kwargs.get("path"),
            mode=str(kwargs.get("mode") or "write"),
            required_capability=kwargs.get("required_capability"),
            domain=kwargs.get("domain"),
        )
        return {
            "ok": True,
            "schema": SCHEMA,
            "decision": verdict.decision,
            "reason": verdict.reason,
            "code": verdict.code,
        }
    except anchor_mod.SceneAnchorError as exc:  # type: ignore[attr-defined]
        return {"ok": False, "error": exc.code, "detail": str(exc)}
    except (KeyError, ValueError, TypeError) as exc:
        return {"ok": False, "error": "bad_request", "detail": str(exc)}


def scene_anchor_contract() -> dict[str, Any]:
    """BOS 能力目录元数据 (external.resources 契约面)."""
    return {
        "uri": SERVICE_URI,
        "schema": SCHEMA,
        "actions": list(_ACTIONS),
        "kernel": "omo.scene.anchor + omo.guardrail.enforcer",
        "honest_failure": "kernel_unavailable",
    }


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


__all__ = ["SCHEMA", "SERVICE_URI", "scene_anchor", "scene_anchor_contract"]
