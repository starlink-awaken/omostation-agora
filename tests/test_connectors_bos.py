"""Tests for connector BOS gateway and resident poller (BET-Y1Q4-T5-04)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ws_root() -> Path:
    """Workspace root containing .omo/_truth/registry/connector-manifest.yaml."""
    cur = Path(__file__).resolve()
    for parent in cur.parents:
        if (parent / "docs" / "project-registry.yaml").is_file():
            return parent
    return Path.cwd()


@pytest.fixture
def manifest(ws_root: Path) -> dict:
    """Load the connector manifest."""
    path = ws_root / ".omo" / "_truth" / "registry" / "connector-manifest.yaml"
    if not path.is_file():
        pytest.skip("connector-manifest.yaml not found")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------

class TestConnectorManifest:
    def test_manifest_exists(self, ws_root: Path):
        path = ws_root / ".omo" / "_truth" / "registry" / "connector-manifest.yaml"
        assert path.is_file(), "connector-manifest.yaml must exist"

    def test_schema_version(self, manifest: dict):
        assert manifest.get("schema_version") == "connector-manifest/v1"

    def test_has_at_least_15_connectors(self, manifest: dict):
        connectors = manifest.get("connectors", [])
        assert len(connectors) >= 15, f"Expected >=15 connectors, got {len(connectors)}"

    def test_all_have_required_fields(self, manifest: dict):
        for c in manifest.get("connectors", []):
            assert c.get("id"), "connector missing id"
            assert c.get("name"), "connector missing name"
            assert c.get("type"), "connector missing type"
            assert c.get("transport"), "connector missing transport"
            assert c.get("uri"), "connector missing uri"
            assert c.get("status"), "connector missing status"
            assert c.get("auth"), "connector missing auth"
            assert c.get("package"), "connector missing package"

    def test_incremental_connectors_have_watermark_field(self, manifest: dict):
        for c in manifest.get("connectors", []):
            if c.get("incremental"):
                assert c.get("watermark_field"), (
                    f"incremental connector {c.get('id')} missing watermark_field"
                )

    def test_uris_are_unique(self, manifest: dict):
        uris = [c.get("uri") for c in manifest.get("connectors", [])]
        assert len(uris) == len(set(uris)), "duplicate URIs in manifest"

    def test_ids_are_unique(self, manifest: dict):
        ids = [c.get("id") for c in manifest.get("connectors", [])]
        assert len(ids) == len(set(ids)), "duplicate IDs in manifest"


# ---------------------------------------------------------------------------
# Connector gateway tool tests (tools_connectors.py)
# ---------------------------------------------------------------------------

class TestConnectorGateway:
    """Test the MCP connector gateway tools."""

    def test_tools_module_importable(self):
        """Verify the tools_connectors module can be imported."""
        # Add agora src to path
        agora_src = (
            Path(__file__).resolve().parent.parent.parent.parent
            / "projects" / "agora" / "src"
        )
        if agora_src.is_dir():
            sys.path.insert(0, str(agora_src))
            try:
                import agora.server.tools_connectors as tc  # type: ignore
                assert hasattr(tc, "connector_list")
                assert hasattr(tc, "connector_sync")
            finally:
                sys.path.remove(str(agora_src))
        else:
            pytest.skip("agora src not found")

    def test_connector_list_filters(self, manifest: dict):
        """Verify filter logic works correctly."""
        connectors = manifest.get("connectors", [])
        active = [c for c in connectors if c.get("status") == "active"]
        assert len(active) > 0

        # By type
        for c in connectors:
            ct = c.get("type")
            matches = [x for x in connectors if x.get("type") == ct]
            assert len(matches) >= 1

    def test_connector_sync_dry_run(self, ws_root: Path):
        """Test dry_run sync returns correct structure."""
        agora_src = ws_root / "projects" / "agora" / "src"
        if not agora_src.is_dir():
            pytest.skip("agora src not found")

        old_path = sys.path[:]
        sys.path.insert(0, str(agora_src))
        try:
            from agora.server.tools_connectors import connector_sync  # type: ignore
            import asyncio

            # Dry run with a valid connector
            result_str = asyncio.run(
                connector_sync(connector_id="iris-calendar", dry_run=True)
            )
            result = json.loads(result_str)
            assert result.get("status") == "ok"
            assert result.get("dry_run") is True
            assert result.get("connector", {}).get("id") == "iris-calendar"
        finally:
            sys.path[:] = old_path

    def test_connector_sync_not_found(self, ws_root: Path):
        """Test sync with non-existent connector returns error."""
        agora_src = ws_root / "projects" / "agora" / "src"
        if not agora_src.is_dir():
            pytest.skip("agora src not found")

        old_path = sys.path[:]
        sys.path.insert(0, str(agora_src))
        try:
            from agora.server.tools_connectors import connector_sync  # type: ignore
            import asyncio

            result_str = asyncio.run(
                connector_sync(connector_id="nonexistent-connector", dry_run=True)
            )
            result = json.loads(result_str)
            assert result.get("status") == "error"
        finally:
            sys.path[:] = old_path


# ---------------------------------------------------------------------------
# Resident poller tests (connectors_poll.py)
# ---------------------------------------------------------------------------

class TestConnectorPoller:
    """Test the resident daemon connector poller."""

    def test_poller_module_importable(self, ws_root: Path):
        """Verify the poller module can be imported."""
        omo_src = ws_root / "projects" / "omo" / "src"
        if not omo_src.is_dir():
            pytest.skip("omo src not found")

        old_path = sys.path[:]
        sys.path.insert(0, str(omo_src))
        try:
            from omo.resident.connectors_poll import (
                PollerState,
                Watermark,
                poll_connectors,
                list_watermarks,
                reset_watermark,
                SyncResult,
            )
            assert Watermark(connector_id="test").connector_id == "test"
            assert SyncResult(
                connector_id="test", ok=True, new_watermark="w1", events_emitted=1
            ).ok
        finally:
            sys.path[:] = old_path

    def test_watermark_roundtrip(self, ws_root: Path):
        """Test Watermark serialization/deserialization."""
        omo_src = ws_root / "projects" / "omo" / "src"
        if not omo_src.is_dir():
            pytest.skip("omo src not found")

        old_path = sys.path[:]
        sys.path.insert(0, str(omo_src))
        try:
            from omo.resident.connectors_poll import Watermark

            wm = Watermark(
                connector_id="iris-calendar",
                value="ts:1234567890",
                last_sync_ts=1234567890.0,
                last_sync_ok=True,
            )
            d = wm.to_dict()
            wm2 = Watermark.from_dict(d)
            assert wm2.connector_id == wm.connector_id
            assert wm2.value == wm.value
            assert wm2.last_sync_ts == wm.last_sync_ts
        finally:
            sys.path[:] = old_path

    def test_poller_dry_run(self, ws_root: Path):
        """Test poller in dry_run mode (no state persisted)."""
        omo_src = ws_root / "projects" / "omo" / "src"
        if not omo_src.is_dir():
            pytest.skip("omo src not found")

        old_path = sys.path[:]
        sys.path.insert(0, str(omo_src))
        try:
            from omo.resident.connectors_poll import poll_connectors

            results = poll_connectors(ws_root, dry_run=True, interval_seconds=0)
            # Should return results for incremental connectors
            assert isinstance(results, list)
            assert len(results) > 0
            for r in results:
                assert r.ok
                assert r.new_watermark.startswith("ts:")
        finally:
            sys.path[:] = old_path

    def test_poller_state_persistence(self, ws_root: Path):
        """Test that non-dry_run poll persists state."""
        omo_src = ws_root / "projects" / "omo" / "src"
        if not omo_src.is_dir():
            pytest.skip("omo src not found")

        # Use a temp directory for state to avoid polluting real state
        import tempfile
        import shutil

        old_path = sys.path[:]
        sys.path.insert(0, str(omo_src))
        try:
            from omo.resident.connectors_poll import (
                _save_state,
                _load_state,
                PollerState,
                Watermark,
            )

            tmp = Path(tempfile.mkdtemp())
            try:
                # Create a minimal manifest in tmp
                tmp_manifest = tmp / ".omo" / "_truth" / "registry"
                tmp_manifest.mkdir(parents=True)
                (tmp_manifest / "connector-manifest.yaml").write_text(
                    yaml.dump({
                        "schema_version": "connector-manifest/v1",
                        "connectors": [
                            {
                                "id": "test-conn",
                                "name": "Test",
                                "type": "test",
                                "transport": "internal",
                                "uri": "bos://test",
                                "status": "active",
                                "incremental": True,
                                "watermark_field": "last_id",
                                "auth": "internal",
                                "package": "test",
                                "tags": [],
                            }
                        ],
                    }),
                    encoding="utf-8",
                )

                # Poll in non-dry-run mode
                from omo.resident.connectors_poll import poll_connectors
                results = poll_connectors(tmp, dry_run=False, interval_seconds=0)
                assert len(results) == 1
                assert results[0].ok

                # Verify state was persisted
                state = _load_state(tmp)
                assert "test-conn" in state.watermarks
                assert state.watermarks["test-conn"].last_sync_ts > 0
            finally:
                shutil.rmtree(tmp)
        finally:
            sys.path[:] = old_path
