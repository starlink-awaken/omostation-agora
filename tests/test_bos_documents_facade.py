"""Tests for agora.tools_bos.documents — BOS Documents read-only facade."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml


@pytest.fixture()
def mock_registry(tmp_path: Path) -> Path:
    """Create a mock documents-domain-projects.yaml."""
    registry = {
        "apiVersion": "workspace.omostation/v1",
        "kind": "DocumentsDomainProjects",
        "id": "documents-domain-projects",
        "status": "active",
        "owner": "cockpit",
        "clients": {
            "claude": {
                "config_contract": {
                    "config_path": "~/Library/Application Support/Claude/claude_desktop_config.json",
                    "managed_mcp_server": "cockpit",
                }
            }
        },
        "capability_routes": {
            "skills": {"owner": "workspace-skills"},
            "workflows": {"owner": "workspace-workflow-mesh"},
        },
        "workspace_mcp": {"owner": "cockpit", "server": "cockpit"},
        "domains": [
            {"id": "shared", "profile": "content-domain"},
            {"id": "personal", "profile": "content-domain"},
            {"id": "work-weijian", "profile": "content-domain"},
        ],
        "runtime_jobs": [
            {"id": "test-job-1", "domain_id": "shared", "schedule": "manual", "action": "validate"},
            {"id": "test-job-2", "domain_id": "work-weijian", "schedule": "manual", "action": "audit"},
        ],
        "runtime_state": {
            "owner": "runtime",
            "environment_override": "OMOSTATION_RUNTIME_STATE_ROOT",
            "default_home_relative": ".local/state/omostation/runtime",
        },
    }
    path = tmp_path / "documents-domain-projects.yaml"
    with open(path, "w") as fh:
        yaml.dump(registry, fh)
    return path


class TestDocumentsRegistry:
    """Tests for handle_documents_registry."""

    def test_registry_full(self, mock_registry: Path) -> None:
        """Full registry returns all domains and client contracts."""
        from agora.tools_bos.documents import handle_documents_registry

        with patch("agora.tools_bos.documents._resolve_workspace_root") as mock_root:
            mock_root.return_value = mock_registry.parent.parent
            # Patch the registry path resolution
            import agora.tools_bos.documents as mod
            original = mod._DOMAIN_REGISTRY_FILE
            mod._DOMAIN_REGISTRY_FILE = mock_registry
            try:
                result = handle_documents_registry()
            finally:
                mod._DOMAIN_REGISTRY_FILE = original

        assert "domains" in result
        assert len(result["domains"]) == 3
        assert "client_contracts" in result
        assert result["schema_valid"] is True

    def test_registry_filter_domain(self, mock_registry: Path) -> None:
        """Filtering by domain returns that domain's entry."""
        from agora.tools_bos.documents import handle_documents_registry

        import agora.tools_bos.documents as mod
        original = mod._DOMAIN_REGISTRY_FILE
        mod._DOMAIN_REGISTRY_FILE = mock_registry
        try:
            result = handle_documents_registry(domain="work-weijian")
        finally:
            mod._DOMAIN_REGISTRY_FILE = original

        assert "domain" in result
        assert result["domain"]["id"] == "work-weijian"
        assert result["schema_valid"] is True

    def test_registry_unknown_domain(self, mock_registry: Path) -> None:
        """Filtering by unknown domain returns error with available list."""
        from agora.tools_bos.documents import handle_documents_registry

        import agora.tools_bos.documents as mod
        original = mod._DOMAIN_REGISTRY_FILE
        mod._DOMAIN_REGISTRY_FILE = mock_registry
        try:
            result = handle_documents_registry(domain="nonexistent")
        finally:
            mod._DOMAIN_REGISTRY_FILE = original

        assert "error" in result
        assert "nonexistent" in result["error"]
        assert "available" in result

    def test_registry_missing_file(self, tmp_path: Path) -> None:
        """Missing registry file returns error."""
        from agora.tools_bos.documents import handle_documents_registry

        import agora.tools_bos.documents as mod
        original = mod._DOMAIN_REGISTRY_FILE
        mod._DOMAIN_REGISTRY_FILE = tmp_path / "nonexistent.yaml"
        try:
            result = handle_documents_registry()
        finally:
            mod._DOMAIN_REGISTRY_FILE = original

        assert "error" in result


class TestDocumentsJobs:
    """Tests for handle_documents_jobs."""

    def test_jobs_full(self, mock_registry: Path) -> None:
        """Returns all runtime jobs."""
        from agora.tools_bos.documents import handle_documents_jobs

        import agora.tools_bos.documents as mod
        original = mod._DOMAIN_REGISTRY_FILE
        mod._DOMAIN_REGISTRY_FILE = mock_registry
        try:
            result = handle_documents_jobs()
        finally:
            mod._DOMAIN_REGISTRY_FILE = original

        assert "jobs" in result
        assert result["total"] == 2
        assert result["schema_valid"] is True

    def test_jobs_filter_domain(self, mock_registry: Path) -> None:
        """Filtering by domain returns matching jobs only."""
        from agora.tools_bos.documents import handle_documents_jobs

        import agora.tools_bos.documents as mod
        original = mod._DOMAIN_REGISTRY_FILE
        mod._DOMAIN_REGISTRY_FILE = mock_registry
        try:
            result = handle_documents_jobs(domain="work-weijian")
        finally:
            mod._DOMAIN_REGISTRY_FILE = original

        assert result["total"] == 1
        assert result["jobs"][0]["domain_id"] == "work-weijian"


class TestDocumentsState:
    """Tests for handle_documents_state."""

    def test_state_with_empty_root(self, mock_registry: Path, tmp_path: Path) -> None:
        """State handler works when state root is empty."""
        from agora.tools_bos.documents import handle_documents_state

        state_root = tmp_path / "state"
        state_root.mkdir()

        import agora.tools_bos.documents as mod
        original = mod._DOMAIN_REGISTRY_FILE
        mod._DOMAIN_REGISTRY_FILE = mock_registry
        try:
            with patch.dict("os.environ", {"OMOSTATION_RUNTIME_STATE_ROOT": str(state_root)}):
                result = handle_documents_state()
        finally:
            mod._DOMAIN_REGISTRY_FILE = original

        assert "state" in result
        assert result["path"] == str(state_root)

    def test_state_missing_root(self, mock_registry: Path) -> None:
        """State handler returns error when state root doesn't exist."""
        from agora.tools_bos.documents import handle_documents_state

        import agora.tools_bos.documents as mod
        original = mod._DOMAIN_REGISTRY_FILE
        mod._DOMAIN_REGISTRY_FILE = mock_registry
        try:
            with patch.dict("os.environ", {"OMOSTATION_RUNTIME_STATE_ROOT": "/nonexistent/path"}):
                result = handle_documents_state()
        finally:
            mod._DOMAIN_REGISTRY_FILE = original

        assert "error" in result
        assert "not found" in result["error"]


class TestSchemaValidation:
    """Tests for schema validation."""

    def test_valid_registry(self) -> None:
        """Valid registry passes schema validation."""
        from agora.tools_bos.documents import _schema_validate

        data = {"apiVersion": "v1", "domains": [], "clients": {}}
        errors = _schema_validate(data, "registry")
        assert errors == []

    def test_invalid_registry_missing_fields(self) -> None:
        """Registry missing required fields returns errors."""
        from agora.tools_bos.documents import _schema_validate

        data = {"kind": "test"}
        errors = _schema_validate(data, "registry")
        assert len(errors) == 3
        assert any("apiVersion" in e for e in errors)
        assert any("domains" in e for e in errors)
        assert any("clients" in e for e in errors)

    def test_valid_jobs(self) -> None:
        """Valid jobs passes schema validation."""
        from agora.tools_bos.documents import _schema_validate

        data = {"runtime_jobs": []}
        errors = _schema_validate(data, "jobs")
        assert errors == []

    def test_invalid_jobs(self) -> None:
        """Jobs missing runtime_jobs field returns error."""
        from agora.tools_bos.documents import _schema_validate

        data = {"other": "value"}
        errors = _schema_validate(data, "jobs")
        assert len(errors) == 1


class TestToolDefinitions:
    """Tests for BOS_DOCUMENTS_TOOLS definitions."""

    def test_tools_defined(self) -> None:
        """All three BOS document tools are defined."""
        from agora.tools_bos.documents import BOS_DOCUMENTS_TOOLS

        assert "bos_documents_registry" in BOS_DOCUMENTS_TOOLS
        assert "bos_documents_jobs" in BOS_DOCUMENTS_TOOLS
        assert "bos_documents_state" in BOS_DOCUMENTS_TOOLS

    def test_tools_are_read_only(self) -> None:
        """All tools are marked as read-only."""
        from agora.tools_bos.documents import BOS_DOCUMENTS_TOOLS

        for name, tool in BOS_DOCUMENTS_TOOLS.items():
            assert tool.get("read_only") is True, f"{name} should be read_only"

    def test_tools_have_handlers(self) -> None:
        """All tools have handler callables."""
        from agora.tools_bos.documents import BOS_DOCUMENTS_TOOLS

        for name, tool in BOS_DOCUMENTS_TOOLS.items():
            assert callable(tool.get("handler")), f"{name} should have a callable handler"
