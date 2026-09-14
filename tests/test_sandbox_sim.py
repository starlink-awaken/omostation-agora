"""Tests for agora sandbox_sim core (BET-Y2Q1-T5-01)."""

from __future__ import annotations

import pytest

from agora.orchestration.sandbox_sim import (
    ROLES,
    extract_factors,
    run_simulation,
    simulate_proposal,
)

PROPOSAL = "预算 60 万元，工期 4 个月，替换第三方订阅，引入大模型 POC 验证，预期降本增收。"


def test_extract_factors_hits():
    f = extract_factors(PROPOSAL)
    assert f["signal_hits"], "规则信号词应命中"
    assert f["text_digest"].startswith("sha256:")
    assert all(k in f for k in ("gain", "cost", "risk", "variance"))


def test_simulation_deterministic():
    a = simulate_proposal(PROPOSAL, rounds=100, seed=42)
    b = simulate_proposal(PROPOSAL, rounds=100, seed=42)
    assert a == b, "同 seed 同文本必须输出完全一致"
    assert set(a["role_means"]) == set(ROLES)
    for key in ("mean", "stdev", "p5", "p50", "p95", "stop_loss"):
        assert isinstance(a[key], float), key
    assert a["p5"] <= a["p50"] <= a["p95"], "分位单调性"
    assert a["stop_loss"] == a["p5"], "止损线语义 = p5"
    assert len(a["sensitivity"]) == 4
    assert a["elapsed_s"] < 30, "100 轮必须 30 秒内完成"


def test_simulation_seed_varies():
    a = simulate_proposal(PROPOSAL, rounds=100, seed=1)
    b = simulate_proposal(PROPOSAL, rounds=100, seed=2)
    assert a["mean"] != b["mean"], "不同 seed 应产生不同分布"


def test_empty_proposal_honest_failure():
    with pytest.raises(ValueError, match="empty_proposal"):
        simulate_proposal("   ", rounds=10)


def test_run_simulation_rounds_floor():
    r = run_simulation(extract_factors(PROPOSAL), rounds=0, seed=7)
    assert r["rounds"] == 1
