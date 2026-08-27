from __future__ import annotations

from pathlib import Path

import pytest


def _source_file(root: Path) -> Path:
    return root / "projects" / "agora" / "src" / "agora" / "mcp" / "mcp_bootstrap.py"


def test_workspace_ssot_wins_over_nested_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agora.mcp import mcp_bootstrap

    workspace = tmp_path / "workspace"
    (workspace / ".omo" / "_truth" / "registry").mkdir(parents=True)
    (workspace / ".omo" / "_truth" / "registry" / "documents-domain-projects.yaml").write_text("{}\n")
    canonical = workspace / "projects" / "l4-kernel"
    canonical.mkdir(parents=True)
    nested = workspace / "projects" / "agora" / "projects" / "l4-kernel"
    nested.mkdir(parents=True)

    monkeypatch.delenv("L4_KERNEL_ROOT", raising=False)
    monkeypatch.delenv("OMOSTATION_WORKSPACE_ROOT", raising=False)

    root, mode = mcp_bootstrap._resolve_l4_kernel_root(_source_file(workspace))

    assert root == canonical
    assert mode == "canonical-workspace"


def test_explicit_l4_root_has_highest_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agora.mcp import mcp_bootstrap

    explicit = tmp_path / "explicit-l4"
    explicit.mkdir()
    monkeypatch.setenv("L4_KERNEL_ROOT", str(explicit))

    root, mode = mcp_bootstrap._resolve_l4_kernel_root(_source_file(tmp_path / "standalone"))

    assert root == explicit
    assert mode == "explicit"


def test_invalid_explicit_l4_root_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agora.mcp import mcp_bootstrap

    monkeypatch.setenv("L4_KERNEL_ROOT", "relative/l4-kernel")

    with pytest.raises(ValueError, match="L4_KERNEL_ROOT"):
        mcp_bootstrap._resolve_l4_kernel_root(_source_file(tmp_path))


def test_standalone_nested_fallback_is_explicitly_legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agora.mcp import mcp_bootstrap

    agora_root = tmp_path / "agora"
    nested = agora_root / "projects" / "l4-kernel"
    nested.mkdir(parents=True)
    monkeypatch.delenv("L4_KERNEL_ROOT", raising=False)
    monkeypatch.delenv("OMOSTATION_WORKSPACE_ROOT", raising=False)

    root, mode = mcp_bootstrap._resolve_l4_kernel_root(_source_file(agora_root))

    assert root == nested
    assert mode == "legacy-nested"


def test_l4_service_metadata_contains_route_mode_and_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agora.mcp import mcp_bootstrap

    workspace = tmp_path / "workspace"
    (workspace / ".omo" / "_truth" / "registry").mkdir(parents=True)
    (workspace / ".omo" / "_truth" / "registry" / "documents-domain-projects.yaml").write_text("{}\n")
    canonical = workspace / "projects" / "l4-kernel"
    canonical.mkdir(parents=True)
    monkeypatch.delenv("L4_KERNEL_ROOT", raising=False)
    monkeypatch.delenv("OMOSTATION_WORKSPACE_ROOT", raising=False)

    service = mcp_bootstrap._build_l4_kernel_service(_source_file(workspace))

    assert service["l4_route_mode"] == "canonical-workspace"
    assert service["l4_kernel_root"] == str(canonical)
    assert str(canonical) in service["args"]
