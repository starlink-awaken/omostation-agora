"""BOS Documents read-only facade — ``bos://documents/{domain}/{resource}``.

Exposes structured, schema-validated read access to the Documents domain
registry, runtime jobs, and runtime state through the Agora MCP hub.

All routes are **read-only**; non-GET / mutation requests are fail-closed.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Workspace root resolution
# ---------------------------------------------------------------------------

_WORKSPACE_ROOT = Path(__file__).resolve().parents[5]  # ws-bet-…/ → Workspace
if not (_WORKSPACE_ROOT / ".omo").exists():
    # Fallback: walk up from agora submodule
    _WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

_REGISTRY_BASE = _WORKSPACE_ROOT / ".omo" / "_truth" / "registry"

# Canonical file paths per resource
_RESOURCE_FILES: dict[str, dict[str, Path]] = {}

# Domain registry — shared across all domains
_DOMAIN_REGISTRY_FILE = _REGISTRY_BASE / "documents-domain-projects.yaml"


def _resolve_workspace_root() -> Path:
    """Resolve the workspace root (search upward for .omo directory)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / ".omo").is_dir():
            return parent
    return _WORKSPACE_ROOT


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load and return a YAML file as a dict, or empty dict on error."""
    if not path.exists():
        return {}
    try:
        with open(path) as fh:
            return yaml.safe_load(fh) or {}
    except (yaml.YAMLError, OSError) as exc:
        _log.warning("Failed to load %s: %s", path, exc)
        return {}


def _schema_validate(data: dict[str, Any], resource: str) -> list[str]:
    """Validate the loaded data against expected schema constraints.

    Returns a list of validation error strings (empty = valid).
    """
    errors: list[str] = []
    if resource == "registry":
        if "apiVersion" not in data:
            errors.append("Missing 'apiVersion' field")
        if "domains" not in data:
            errors.append("Missing 'domains' field")
        if "clients" not in data:
            errors.append("Missing 'clients' field")
    elif resource == "jobs":
        if "runtime_jobs" not in data:
            errors.append("Missing 'runtime_jobs' field")
    elif resource == "state":
        # State is a free-form dict; no strict schema
        pass
    return errors


# ---------------------------------------------------------------------------
# BOS route handlers
# ---------------------------------------------------------------------------

def handle_documents_registry(domain: str | None = None) -> dict[str, Any]:
    """Handle ``bos://documents/{domain}/registry`` — returns the domain
    registry configuration.

    Args:
        domain: Optional domain ID filter. If provided, returns only that
                domain's entry. If None, returns the full registry.

    Returns:
        Structured dict with registry data and metadata.
    """
    root = _resolve_workspace_root()
    registry_path = root / ".omo" / "_truth" / "registry" / "documents-domain-projects.yaml"
    data = _load_yaml(registry_path)

    if not data:
        return {"error": "Registry file not found or empty", "path": str(registry_path)}

    errors = _schema_validate(data, "registry")
    if errors:
        return {"error": "Schema validation failed", "errors": errors, "path": str(registry_path)}

    if domain:
        domains = data.get("domains", [])
        matched = [d for d in domains if d.get("id") == domain]
        if not matched:
            return {"error": f"Domain '{domain}' not found", "available": [d.get("id") for d in domains]}
        return {
            "domain": matched[0],
            "client_contracts": {
                k: v for k, v in data.get("clients", {}).items()
            },
            "schema_valid": True,
            "source": str(registry_path),
        }

    return {
        "domains": data.get("domains", []),
        "client_contracts": data.get("clients", {}),
        "capability_routes": data.get("capability_routes", {}),
        "workspace_mcp": data.get("workspace_mcp", {}),
        "schema_valid": True,
        "source": str(registry_path),
    }


def handle_documents_jobs(domain: str | None = None) -> dict[str, Any]:
    """Handle ``bos://documents/{domain}/jobs`` — returns runtime job
    configuration.

    Args:
        domain: Optional domain ID filter.

    Returns:
        Structured dict with job definitions.
    """
    root = _resolve_workspace_root()
    registry_path = root / ".omo" / "_truth" / "registry" / "documents-domain-projects.yaml"
    data = _load_yaml(registry_path)

    if not data:
        return {"error": "Registry file not found or empty", "path": str(registry_path)}

    errors = _schema_validate(data, "jobs")
    if errors:
        return {"error": "Schema validation failed", "errors": errors}

    jobs = data.get("runtime_jobs", [])
    if domain:
        jobs = [j for j in jobs if j.get("domain_id") == domain]

    return {
        "jobs": jobs,
        "total": len(jobs),
        "schema_valid": True,
        "source": str(registry_path),
    }


def handle_documents_state(domain: str | None = None) -> dict[str, Any]:
    """Handle ``bos://documents/{domain}/state`` — returns runtime state.

    The runtime state is stored per the registry's ``runtime_state`` section
    and uses the ``OMOSTATION_RUNTIME_STATE_ROOT`` environment variable or
    the default home-relative path.

    Args:
        domain: Optional domain ID filter.

    Returns:
        Structured dict with runtime state data.
    """
    root = _resolve_workspace_root()
    registry_path = root / ".omo" / "_truth" / "registry" / "documents-domain-projects.yaml"
    data = _load_yaml(registry_path)

    runtime_state_config = data.get("runtime_state", {})
    env_override = runtime_state_config.get("environment_override", "")
    default_rel = runtime_state_config.get("default_home_relative", ".local/state/omostation/runtime")

    state_root = Path(os.environ.get(env_override, str(Path.home() / default_rel))) if env_override else Path.home() / default_rel

    if not state_root.exists():
        return {
            "error": "Runtime state root not found",
            "path": str(state_root),
            "hint": f"Set {env_override} or ensure {state_root} exists",
        }

    # Collect state files
    state_data: dict[str, Any] = {}
    for state_file in sorted(state_root.glob("*.json")):
        try:
            with open(state_file) as fh:
                state_data[state_file.stem] = json.load(fh)
        except (json.JSONDecodeError, OSError):
            state_data[state_file.stem] = {"error": f"Cannot read {state_file.name}"}

    if domain:
        # Filter to domain-specific state entries
        filtered = {k: v for k, v in state_data.items() if domain in k}
        return {
            "domain": domain,
            "state": filtered,
            "path": str(state_root),
        }

    return {
        "state": state_data,
        "path": str(state_root),
    }


# ---------------------------------------------------------------------------
# MCP tool definitions (for registration in Agora)
# ---------------------------------------------------------------------------

BOS_DOCUMENTS_TOOLS: dict[str, dict[str, Any]] = {
    "bos_documents_registry": {
        "name": "bos_documents_registry",
        "description": "Read-only access to Documents domain registry (bos://documents/{domain}/registry). Returns domain metadata, client contracts, and capability routes.",
        "handler": handle_documents_registry,
        "parameters": {
            "domain": {
                "type": "string",
                "description": "Optional domain ID to filter (e.g. 'work-weijian'). Omit for full registry.",
                "required": False,
            }
        },
        "bos_uri": "bos://documents/{domain}/registry",
        "read_only": True,
    },
    "bos_documents_jobs": {
        "name": "bos_documents_jobs",
        "description": "Read-only access to Documents runtime job configuration (bos://documents/{domain}/jobs). Returns job definitions, schedules, and evidence paths.",
        "handler": handle_documents_jobs,
        "parameters": {
            "domain": {
                "type": "string",
                "description": "Optional domain ID to filter jobs.",
                "required": False,
            }
        },
        "bos_uri": "bos://documents/{domain}/jobs",
        "read_only": True,
    },
    "bos_documents_state": {
        "name": "bos_documents_state",
        "description": "Read-only access to Documents runtime state (bos://documents/{domain}/state). Returns persistent runtime state data.",
        "handler": handle_documents_state,
        "parameters": {
            "domain": {
                "type": "string",
                "description": "Optional domain ID to filter state entries.",
                "required": False,
            }
        },
        "bos_uri": "bos://documents/{domain}/state",
        "read_only": True,
    },
}
