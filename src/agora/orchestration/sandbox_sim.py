"""agora.orchestration.sandbox_sim — 战略决策沙盘蒙特卡洛核心 (BET-Y2Q1-T5-01).

商业 / 研发 / 安全 / 财务 4 角对抗 Agent 并发推演：提案文本 → 规则信号词
提取 4 角基础参数 → seeded RNG 蒙特卡洛 N 轮博弈 → 收益分布 / 最坏情况
止损线 / 敏感性因子。纯标准库、零模型调用、确定性可复现。
"""

from __future__ import annotations

import hashlib
import math
import random
import re
import time
from typing import Any

ROLES = ("business", "developer", "security", "finance")
ROLE_LABELS = {
    "business": "商业 (Business)",
    "developer": "研发 (Developer)",
    "security": "安全 (Security)",
    "finance": "财务 (Finance)",
}

# 信号词 → (参数, 增量, 因子名)
_SIGNALS: list[tuple[str, str, float, str]] = [
    (r"预算|成本|费用|万元|报价|投入", "cost", 1.0, "预算规模"),
    (r"外包|采购|第三方|供应商|订阅", "cost", 0.5, "外部依赖成本"),
    (r"工期|里程碑|deadline|上线时间|交付期", "schedule", 1.0, "工期压力"),
    (r"重构|迁移|替换|下线|升级", "schedule", 0.5, "变更复杂度"),
    (r"涉密|敏感|身份证|密钥|鉴权|脱敏|合规", "risk", 1.0, "合规与数据风险"),
    (r"外发|对外|公网|开放接口|开源", "risk", 0.5, "外部暴露面"),
    (r"试验|探索|预研|POC|验证|试点", "variance", 1.0, "探索不确定性"),
    (r"AI|大模型|LoRA|推理|智能体", "variance", 0.5, "模型行为不确定性"),
    (r"裁员|组织调整|合并|收购", "risk", 1.0, "组织变动风险"),
    (r"增长|增收|提效|降本|ROI", "gain", 1.0, "收益信号强度"),
]


def extract_factors(proposal_text: str) -> dict[str, Any]:
    """提案文本 → 4 角基础参数。空文本诚实失败，由调用方判 exit 码。"""
    text = (proposal_text or "").strip()
    params = {
        "gain": 50.0,  # 基准期望收益分
        "cost": 20.0,  # 基准成本分
        "risk": 10.0,  # 基准风险分
        "variance": 8.0,  # 基准波动
        "schedule": 0.0,
    }
    hits: list[str] = []
    for pat, dim, delta, name in _SIGNALS:
        if re.search(pat, text, re.IGNORECASE):
            if dim == "gain":
                params["gain"] += 4.0 * delta
            elif dim == "cost":
                params["cost"] += 4.0 * delta
            elif dim == "risk":
                params["risk"] += 5.0 * delta
            elif dim == "variance":
                params["variance"] += 3.0 * delta
            elif dim == "schedule":
                params["schedule"] += delta
                params["variance"] += 1.5 * delta
            hits.append(name)
    params["text_len"] = len(text)
    params["text_digest"] = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    params["signal_hits"] = sorted(set(hits))
    return params


def _role_draw(rng: random.Random, role: str, f: dict[str, float]) -> float:
    """单角单轮博弈 draws：收益 − 成本分摊 − 风险实现 + 噪声。"""
    gain = rng.gauss(f["gain"], f["variance"])
    cost = rng.gauss(f["cost"], max(1.0, f["variance"] / 2))
    risk_event = rng.random() < min(0.9, f["risk"] / 100.0)
    risk_loss = rng.gauss(f["risk"] * 1.5, f["variance"]) if risk_event else 0.0
    role_bias = {"business": 2.0, "developer": -1.0, "security": -3.0, "finance": -2.0}[role]
    return gain - cost * 0.6 - risk_loss + role_bias


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def run_simulation(
    factors: dict[str, Any],
    rounds: int = 100,
    seed: int = 42,
) -> dict[str, Any]:
    """N 轮蒙特卡洛推演。返回分布 / 止损线 / 敏感性 / 计时。"""
    t0 = time.perf_counter()
    rounds = max(1, int(rounds))
    rng = random.Random(seed)
    totals: list[float] = []
    role_sums = dict.fromkeys(ROLES, 0.0)
    for _ in range(rounds):
        draws = {role: _role_draw(rng, role, factors) for role in ROLES}
        total = sum(draws.values()) / len(ROLES)
        totals.append(total)
        for role, val in draws.items():
            role_sums[role] += val
    ordered = sorted(totals)
    mean = sum(totals) / len(totals)
    var = sum((v - mean) ** 2 for v in totals) / len(totals)
    p5 = _percentile(ordered, 5)
    result: dict[str, Any] = {
        "rounds": rounds,
        "seed": seed,
        "mean": round(mean, 2),
        "stdev": round(math.sqrt(var), 2),
        "p5": round(p5, 2),
        "p50": round(_percentile(ordered, 50), 2),
        "p95": round(_percentile(ordered, 95), 2),
        # 最坏情况止损线：5% 分位下界（95% 的推演不差于此）
        "stop_loss": round(p5, 2),
        "role_means": {r: round(role_sums[r] / rounds, 2) for r in ROLES},
        "sensitivity": _sensitivity(factors, rounds, seed),
        "elapsed_s": round(time.perf_counter() - t0, 3),
        "factors_digest": factors.get("text_digest", ""),
    }
    return result


def _sensitivity(factors: dict[str, Any], rounds: int, seed: int) -> list[dict[str, Any]]:
    """单因子 +20% 扰动对均值的影响排序（同 seed，确定性）。"""
    base = run_mean(factors, rounds, seed)
    items = []
    for key in ("gain", "cost", "risk", "variance"):
        perturbed = dict(factors)
        perturbed[key] = factors[key] * 1.2
        shifted = run_mean(perturbed, rounds, seed)
        items.append({"factor": key, "delta_mean": round(shifted - base, 2)})
    items.sort(key=lambda d: abs(d["delta_mean"]), reverse=True)
    return items


def run_mean(factors: dict[str, Any], rounds: int, seed: int) -> float:
    rng = random.Random(seed)
    acc = 0.0
    for _ in range(max(1, rounds)):
        acc += sum(_role_draw(rng, role, factors) for role in ROLES) / len(ROLES)
    return acc / max(1, rounds)


def simulate_proposal(proposal_text: str, rounds: int = 100, seed: int = 42) -> dict[str, Any]:
    """提案文本 → 端到端推演；空文本抛 ValueError(empty_proposal)。"""
    if not (proposal_text or "").strip():
        raise ValueError("empty_proposal: 提案文本为空，拒绝伪造分布")
    factors = extract_factors(proposal_text)
    result = run_simulation(factors, rounds=rounds, seed=seed)
    result["factors"] = {k: v for k, v in factors.items() if k != "text_digest"}
    result["factors"]["text_digest"] = factors.get("text_digest", "")
    return result
