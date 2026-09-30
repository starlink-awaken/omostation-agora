"""tools_bos 共享状态与基础工具 (tools_bos 拆分)。

提供: _PROJECTS_DIR, logger, _AGORA_API_KEY, _bos_router 引用,
以及被 inbox/bdsk/routing/registration 共用的基础函数。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog

from agora.mcp.bos_router import (  # type: ignore[import-not-found]
    bos_router as _bos_router,
)

# 根仓 projects/ 目录: .../projects/agora/src/agora/server/tools_bos/_helpers.py → ../../../../../..
_PROJECTS_DIR = Path(__file__).resolve().parents[5]

logger = structlog.get_logger(__name__)

_AGORA_API_KEY = os.environ.get("AGORA_API_KEY", "")


def _get_proxy_manager() -> Any | None:
    """获取全局 ProxyManager (来自 agora.server.dependencies)。"""
    try:
        from agora.server.dependencies import get_proxy_manager

        return get_proxy_manager()
    except Exception:  # defensive fallback
        return None


def _registry_has_domain(domain: str) -> bool:
    """注册表本体 (etc/bos-services.yaml → POC_SERVICES) 是否声明了该域.

    BOSRouter 只是这份声明表的**运行时缓存** (由 MCP proxy lifespan 的
    seed_from_poc 播种)。调用方不经过 lifespan (CLI / 内部直调 / 独立测试
    进程) 或 admission fail-closed 拒绝播种时缓存为空 — 不能据此判域不存在。
    注册表自身加载失败时返回 False (fail-closed, 与旧行为一致)。
    """
    try:
        from agora.mcp.resolver.services import POC_SERVICES

        return any(getattr(s, "domain", "") == domain for s in POC_SERVICES)
    except Exception:  # noqa: BLE001 — 域鉴权只允许两条路: 缓存有 or 注册表有
        return False


def _bos_domain_authorized(uri: str, operation: str = "read") -> tuple[bool, str]:
    """检查 BOS URI 的域级别权限，并执行 CR-RBAC-01 鉴权。"""
    from agora.server.tools_auth import agora_role_ctx, auth_permissive

    role = agora_role_ctx.get()

    # CR-RBAC-01 强制拦截
    if uri.startswith("bos://capability/evaluator") and role != "evaluator":
        if role != "admin":  # admin can bypass
            return (
                False,
                f"CR-RBAC-01 violation: Role '{role}' cannot access evaluator domain.",
            )

    if not _AGORA_API_KEY:
        if auth_permissive():
            return True, ""  # 显式 permissive (本地开发)
        return False, "AGORA_API_KEY not configured (auth required)"  # fail-closed

    domain = uri.split("/")[2] if uri.startswith("bos://") and "/" in uri else ""
    if not domain:
        return False, "Invalid URI format: Missing domain"

    # CR-DOMAIN-AUTH-01: 注册表驱动的域鉴权
    # 如果 bos_router 中存在该 domain 的任何路由，即视为合法域
    # (更精细的 read/write 权限由 L0 审计与 IAM 中间件后续接管)
    routes = _bos_router.list_all(prefix_filter=f"bos://{domain}/")
    if not routes and not _registry_has_domain(domain):
        # 缓存为空 ≠ 域不存在 (见 _registry_has_domain); 注册表也查不到才拒。
        return False, f"Domain '{domain}' is not registered in BOSRouter"

    return True, ""


def _get_inbox_paths() -> tuple[Path, Path]:
    """获取本地 Inbox 与 @公共/_runtime 数据目录。"""
    doc_root = Path(
        os.environ.get("BOS_DOCUMENTS_ROOT", str(Path.home() / "Documents"))
    )
    runtime_dir = doc_root / "@公共" / "_runtime"
    inbox_dir = doc_root / "_inbox"
    return runtime_dir, inbox_dir


def _bos_uri_to_event_type(uri: str) -> str:
    """将 bos:// URI 转为事件类型标识 (bos:domain:pkg:action)。"""
    return uri.replace("bos://", "bos:", 1).replace("/", ":")


def _publish_bos_event(
    bus: Any,
    uri: str,
    action: str = "called",
    status: str = "ok",
    duration_ms: int = 0,
) -> None:
    """发布 BOS 事件到事件总线.

    兼容调用方 5 参形式: (bus, uri, action, status, duration_ms)。
    (god-module split 曾将签名改窄为 (bus, uri, status, **extra), 导致
    调用方 5 位置参数 TypeError — 此处按调用约定恢复。)
    """
    try:
        event_type = _bos_uri_to_event_type(uri)
        payload = {
            "uri": uri,
            "action": action,
            "status": status,
            "duration_ms": int(duration_ms or 0),
        }
        bus.publish(event_type, payload)
    except Exception as exc:  # noqa: BLE001 — 事件发布失败不影响主流程
        logger.warning("bos_event_publish_failed", uri=uri, error=str(exc))


def gateway_chat(
    prompt: str,
    model: str = "fast",
    timeout: float = 60.0,
    system: str | None = None,
) -> str | None:
    """经 aetherforge 门面调一次本机模型; 任何失败返回 None(调用方自备兜底)。

    与 tools_bos/voice.py 的补标点同一路径: LLM_GATEWAY_URL + Keychain 密钥。
    """
    import httpx

    from .voice import _gateway_key  # 复用同一密钥解析

    base = (os.environ.get("LLM_GATEWAY_URL") or "http://127.0.0.1:4000").rstrip("/").removesuffix("/v1")
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            resp = client.post(
                f"{base}/v1/chat/completions",
                headers={"Authorization": f"Bearer {_gateway_key()}"},
                json={"model": model, "messages": messages, "max_tokens": 800, "temperature": 0.3},
            )
            resp.raise_for_status()
            return (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        logger.warning("gateway_chat model=%s failed", model)
        return None
