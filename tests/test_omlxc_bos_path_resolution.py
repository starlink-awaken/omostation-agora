"""Guard OMLXC BOS declarations against stale demo paths."""

from __future__ import annotations

from pathlib import Path

import yaml


REGISTRY = Path(__file__).resolve().parents[1] / "etc" / "bos-services.yaml"
EXPECTED = {
    "bos://compute/omlxc/tree": "examples/live_nextgen_compute_engine_benchmark.py",
    "bos://compute/omlxc/stream": "examples/live_nextgen_compute_engine_benchmark.py",
    "bos://compute/omlxc/swarm": "examples/live_nextgen_compute_engine_benchmark.py",
    "bos://compute/omlxc/dma": "examples/live_v5_evolution_verification.py",
    "bos://compute/omlxc/lora": "examples/live_v5_evolution_verification.py",
}
UNIMPLEMENTED = {
    "bos://compute/omlxc/cache",
    "bos://compute/omlxc/dflash",
    "bos://compute/omlxc/cluster",
}


def test_omlxc_bos_commands_use_existing_example_scripts():
    payload = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    services = {item["uri"]: item for item in payload["services"]}

    for uri, expected_script in EXPECTED.items():
        command = services[uri]["command"]
        assert command[command.index("--directory") + 3] == expected_script


def test_stale_omlxc_demo_routes_are_explicitly_non_routable():
    payload = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    services = {item["uri"]: item for item in payload["services"]}

    for uri in UNIMPLEMENTED:
        assert services[uri]["status"] == "unimplemented"
        assert services[uri]["description"].startswith("[UNIMPLEMENTED]")
