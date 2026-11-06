"""SIRI-51: daemon HTTP /health endpoint contract tests."""

from __future__ import annotations

import importlib
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import agora.daemon as daemon  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    if not daemon.HAS_FASTAPI:
        pytest.skip("fastapi not installed")
    return TestClient(daemon.app)


def test_health_returns_200_and_contract_keys(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    # SIRI-51 contract: status / uptime_s / udp_port
    assert set(body) == {"status", "uptime_s", "udp_port"}
    assert body["status"] == "ok"
    assert isinstance(body["uptime_s"], int) and body["uptime_s"] >= 0
    assert isinstance(body["udp_port"], int)


def test_health_uptime_monotonic(client: TestClient) -> None:
    first = client.get("/health").json()["uptime_s"]
    time.sleep(1.1)
    second = client.get("/health").json()["uptime_s"]
    assert second >= first + 1


def test_udp_port_follows_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGORA_SWARM_UDP_PORT", "7455")
    mod = importlib.reload(daemon)
    assert mod.SWARM_UDP_PORT == 7455
    body = TestClient(mod.app).get("/health").json()
    assert body["udp_port"] == 7455
    monkeypatch.setenv("AGORA_SWARM_UDP_PORT", "9999")
    mod = importlib.reload(daemon)
    assert mod.SWARM_UDP_PORT == 9999


def test_http_port_default_is_7433_not_ecos_7432() -> None:
    # 7432 is registered to ecos-event-listener (protocols/port-registry.yaml);
    # the daemon must not squat on it by default.
    assert daemon.DEFAULT_HTTP_PORT == 7433
