"""Memory OS 环境解析 — bos://memory/mos/* 子进程的 env 注入 (ADR-0372).

`bin/memory-os-env.sh` (shell 版) 的 Python 单一事实源。此前只有
``agora.server.mcp._load_memory_os_env`` 在 MCP proxy lifespan 里把 Memory OS
env 写进**父进程** os.environ — 任何不经过该 lifespan 的调用路径
(CLI / 内部直调 / 测试 / 独立进程) spawn 的 mos stdio 子进程都拿不到
``MOS_LIVE_KOS`` 等开关, 于是 live backend 静默降级为 fixture。

合并语义 (与 shell 版一致, 进程 env 永远优先):

    内置默认
      < docs/operations/memory-os.env.example
      < projects/cockpit/.env
      < config/memory-os.env

多根 (WORKSPACE / WORKSPACE_ROOT / WORKSPACE_HOME / 向上爬取 / ~/Workspace)
按优先级从高到低取, **同 key 只由最高优先级的文件决定** (example 模板不会
覆盖低优先级根里已有的本地 secrets)。

只读模块: 不写文件、不改 os.environ (除非调用方显式 ``load_into_process_env``)。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

# 仅对 Memory OS 命名空间的 stdio 子进程注入 (与 mcp.py 文档约定一致)
MOS_URI_PREFIX = "bos://memory/mos/"

# 代码默认值 — 任何文件都没写时的兜底 (mirror bin/memory-os-env.sh)
DEFAULTS: dict[str, str] = {
    "NEO4J_URI": "bolt://localhost:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "changeme",
    "NEO4J_HTTP_PORT": "7474",
    "NEO4J_BOLT_PORT": "7687",
    "MOS_TEMPORAL": "1",
    "MOS_RBAC": "1",
    "MOS_MEM0": "0",
    "MOS_GRAPHITI": "0",
}

# 单根内的来源文件, **低优先级在前** (memory-os-env.sh 加载顺序)
_SOURCE_FILES = (
    "docs/operations/memory-os.env.example",
    "projects/cockpit/.env",
    "config/memory-os.env",
)

_ROOT_ENV_KEYS = ("WORKSPACE", "WORKSPACE_ROOT", "WORKSPACE_HOME")


def workspace_roots() -> list[Path]:
    """候选 workspace 根, **高优先级在前** (只读定位器)。

    顺序: 显式 env (WORKSPACE → WORKSPACE_ROOT → WORKSPACE_HOME)
    → 从本文件向上爬 (``projects/agora`` marker, 同 pool.py) → ``~/Workspace``。
    不硬编码绝对路径; 每个候选都会在使用前做存在性检查。
    """
    roots: list[Path] = []

    def _add(candidate: Path) -> None:
        if candidate not in roots:
            roots.append(candidate)

    for key in _ROOT_ENV_KEYS:
        raw = (os.environ.get(key) or "").strip()
        if raw:
            _add(Path(raw))

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "projects" / "agora").is_dir():
            _add(parent)
            break

    _add(Path.home() / "Workspace")
    return roots


def _read_env_file(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 行 (跳过注释/空行; 去成对引号)。读失败视为该文件缺席。"""
    values: dict[str, str] = {}
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in raw_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip().strip('"').strip("'")
    return values


def resolved_memory_os_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """返回 ``base`` (默认进程 env) + Memory OS 非空值补齐后的完整 env 副本。

    进程 env 中已有的**非空**值永远不被覆盖。
    """
    env = dict(os.environ if base is None else base)

    merged = dict(DEFAULTS)
    assigned: set[str] = set()
    for root in workspace_roots():
        if not root.is_dir():
            continue
        # 高优先级文件先写, 低优先级文件只能补 assigned 之外的 key
        for rel in reversed(_SOURCE_FILES):
            values = _read_env_file(root / rel)
            for key, value in values.items():
                if key not in assigned:
                    merged[key] = value
                    assigned.add(key)

    for key, value in merged.items():
        if not env.get(key):
            env[key] = value
    return env


def child_env(
    uri: str, base: Mapping[str, str] | None = None
) -> dict[str, str] | None:
    """stdio 子进程 env; 非 mos URI 返回 ``None`` (继承进程 env, 零行为变化)。"""
    if not uri.startswith(MOS_URI_PREFIX):
        return None
    return resolved_memory_os_env(base)


def load_into_process_env() -> None:
    """把 Memory OS 非空值写进 ``os.environ`` (仅填充空缺, 不覆盖已有值)。

    MCP proxy lifespan 的行为保持不变 — 子进程既继承进程 env, 也拿到
    spawn 时按 URI 注入的 env (双保险)。
    """
    for key, value in resolved_memory_os_env().items():
        if not os.environ.get(key):
            os.environ[key] = value
