"""BOS 旧名别名与注册表一致性 (2026-09-28 待登记积压清理)。

代码仍用旧名 (bos://voice/memo/ingest 等), 注册表早已改用规范名; 别名把旧名映射到
规范名。别名目标必须在 SSOT 中存在, 否则解析仍会落空。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
import yaml

from agora.mcp.resolver.api import normalize_bos_uri

ROOT = Path(__file__).resolve().parents[1]


def _registry() -> dict[str, dict]:
    data = yaml.safe_load(
        (ROOT / "etc" / "bos-services.yaml").read_text(encoding="utf-8")
    )
    return {s["uri"]: s for s in data["services"]}


def _literal_aliases() -> dict[str, str]:
    tree = ast.parse(
        (ROOT / "src" / "agora" / "mcp" / "resolver" / "api.py").read_text(
            encoding="utf-8"
        )
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_LEGACY_BOS_URI_ALIASES"
            for t in node.targets
        ):
            assert isinstance(node.value, ast.Dict)
            return {
                k.value: v.value
                for k, v in zip(node.value.keys, node.value.values, strict=True)
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
            }
    raise AssertionError("_LEGACY_BOS_URI_ALIASES not found")


@pytest.mark.parametrize("legacy,canonical", sorted(_literal_aliases().items()))
def test_alias_target_registered(legacy: str, canonical: str) -> None:
    assert canonical in _registry(), f"{legacy} → {canonical} 目标未登记"
    assert normalize_bos_uri(legacy) == canonical


def test_revived_mail_draft_is_callable() -> None:
    svc = _registry()["bos://documents/inbox-mail/draft"]
    assert svc["status"] == "active" and svc["transport"] == "internal"
    mod = importlib.import_module(svc["module_path"])
    assert callable(getattr(mod, svc["func_name"]))
