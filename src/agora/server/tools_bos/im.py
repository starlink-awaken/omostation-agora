"""tools_bos: im domain — IM session perception & directive triage (BET-Y1Q4-T2-02).

Privacy red lines (BET non_goals):
  - WHITELIST double gate: only whitelisted chats AND keyword hits enter triage
  - no auto-reply / silent outbound — every card parks in "pending_approval"
  - real account hooks (WeCom/Feishu/WeChat desktop) route through human_gate;
    this module ships the protocol surface first.

CLI (verify contract):
  python -m agora.server.tools_bos.im test_session_ingress
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA = "agora.bos.im.v1"
GATEWAY_URI = "bos://im/session/triage"
E2E_BUDGET_S = 2.0  # done_when: message → structured card ≤ 2s

# ── Privacy whitelist (single source) ─────────────────────────────────
WHITELIST_CHATS = {
    "wecom:work-core",  # 企微核心工作群
    "feishu:proj-digitalbrain",  # 飞书数字大脑项目群
    "wecom:dm-xiamingxing",  # 企微私聊（本人）
    "wechat:dm-xiamingxing",  # 微信私聊（本人）
}
WHITELIST_KEYWORDS = (
    "任务",
    "审批",
    "催办",
    "报告",
    "方案",
    "纪要",
    "上线",
    "故障",
    "评审",
    "截止",
    "查一下",
    "看看",
    "搜",
    "起草",
    "拟稿",
    "写一份",
    "生成",
    "同意",
    "通过",
    "提醒",
)
DIRECTIVE_ACTIONS = ("query", "draft", "approve", "urge", "none")

# Rule routes: first hit wins (order matters — approve/urge are unambiguous)
_DIRECTIVE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("approve", ("同意", "批准", "通过一下", "批一下")),
    ("urge", ("催一下", "催办", "提醒", "催", "盯一下")),
    ("draft", ("起草", "拟稿", "写一份", "生成", "拟一版", "写个")),
    ("query", ("查一下", "查查", "看看", "搜", "查", "检索")),
)
_TIME_PATTERNS = (
    (r"今天|today", 0.0),
    (r"明天|tomorrow", 1.0),
    (r"本周五|friday", 3.0),
    (r"下周|\d+天后", 7.0),
)


@dataclass(frozen=True, slots=True)
class ImMessage:
    """One chat message from a monitored IM platform."""

    id: str
    platform: str  # wecom | feishu | wechat
    chat_id: str  # e.g. wecom:work-core
    sender: str
    text: str
    ts: float = field(default_factory=time.time)
    is_group: bool = True
    mentions_me: bool = False


@dataclass(frozen=True, slots=True)
class Directive:
    """Parsed one-shot directive from a message."""

    action: str  # query | draft | approve | urge | none
    payload: str  # the actionable core (directive phrase stripped)


@dataclass(frozen=True, slots=True)
class TaskCard:
    """Structured to-do card pending human approval (never auto-sent)."""

    message_id: str
    platform: str
    chat_id: str
    sender: str
    action: str
    payload: str
    priority: str  # high | normal
    deadline_hint_days: float | None
    status: str = "pending_approval"  # red line: no auto outbound
    created_at: float = field(default_factory=time.time)


def whitelist_gate(msg: ImMessage) -> bool:
    """Double gate: whitelisted chat AND (mention | keyword | directive hit)."""
    if msg.chat_id not in WHITELIST_CHATS:
        return False
    if msg.mentions_me:
        return True  # direct mention in a whitelisted chat always passes
    if any(kw in msg.text for kw in WHITELIST_KEYWORDS):
        return True
    # a parseable directive is itself a strong signal (e.g. 催一下 without 催办)
    return parse_directive(msg.text).action != "none"


def parse_directive(text: str) -> Directive:
    """Rule-route one-shot directives; unmatched text is informational (none)."""
    for action, phrases in _DIRECTIVE_RULES:
        for phrase in phrases:
            idx = text.find(phrase)
            if idx >= 0:
                payload = (text[:idx] + " " + text[idx + len(phrase) :]).strip(
                    "，。 ,."
                )
                payload = re.sub(r"\s+", " ", payload).strip()
                return Directive(action=action, payload=payload or text.strip())
    return Directive(action="none", payload=text.strip())


def _deadline_hint(text: str) -> float | None:
    for pattern, days in _TIME_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return days
    return None


def to_task_card(msg: ImMessage, directive: Directive) -> TaskCard:
    """Message + directive → pending-approval card (never auto-sent)."""
    priority = (
        "high"
        if (msg.mentions_me or directive.action in ("urge", "approve"))
        else "normal"
    )
    return TaskCard(
        message_id=msg.id,
        platform=msg.platform,
        chat_id=msg.chat_id,
        sender=msg.sender,
        action=directive.action,
        payload=directive.payload,
        priority=priority,
        deadline_hint_days=_deadline_hint(msg.text),
    )


def triage_session(messages: list[ImMessage]) -> dict[str, Any]:
    """bos://im/session/triage gateway: whitelist → parse → cards.

    circuit_breaker: any per-message parse failure degrades that message to
    passive mode (pending_review, no action) — the batch never aborts.
    """
    started = time.monotonic()
    cards: list[dict[str, Any]] = []
    dropped: list[str] = []
    passive: list[dict[str, Any]] = []
    for msg in messages:
        try:
            if not whitelist_gate(msg):
                dropped.append(msg.id)
                continue
            directive = parse_directive(msg.text)
            if directive.action == "none":
                # whitelisted chat but no directive → passive digest entry
                passive.append({"message_id": msg.id, "note": "no_directive"})
                continue
            cards.append(asdict(to_task_card(msg, directive)))
        except Exception as exc:  # circuit_breaker → passive, never abort batch
            passive.append({"message_id": msg.id, "note": f"parse_error:{exc}"})
    elapsed_ms = (time.monotonic() - started) * 1000
    return {
        "schema": SCHEMA,
        "gateway": GATEWAY_URI,
        "cards": cards,
        "passive": passive,
        "dropped": dropped,
        "elapsed_ms": round(elapsed_ms, 2),
        "within_e2e_budget": elapsed_ms / 1000 <= E2E_BUDGET_S,
        "red_line": "all cards pending_approval — no auto outbound",
    }


async def bos_im_session_triage(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """BOS tool: triage raw message dicts through the unified gateway."""
    from agora.server._response import FORMAT_VERSION, _error, _ok

    try:
        parsed = [ImMessage(**m) for m in messages]
        result = triage_session(parsed)
        return _ok({"format_version": FORMAT_VERSION, "status": "ok", **result})
    except Exception as exc:  # pragma: no cover — bridge guard
        return _error(f"IM triage exception: {exc}")


# ── Offline verify contract ───────────────────────────────────────────


def _eval_set() -> list[tuple[str, str, str]]:
    """(text, expected_action, label) — 20 positives + 5 adversarial."""
    positives = [
        ("帮我查一下医保目录的最新版本", "query", "q1"),
        ("看看今天的政策雷达晨报", "query", "q2"),
        ("搜一下上季度的报销流程文档", "query", "q3"),
        ("查查项目文档里的接口定义", "query", "q4"),
        ("检索一下相关的 ADR 决议", "query", "q5"),
        ("起草一份给卫健委的复函", "draft", "d1"),
        ("帮我拟稿季度工作总结", "draft", "d2"),
        ("写一份技术评审纪要", "draft", "d3"),
        ("生成明天的会议议程", "draft", "d4"),
        ("写个上线前的检查清单", "draft", "d5"),
        ("拟一版新的数据安全方案", "draft", "d6"),
        ("这份申请我同意了", "approve", "a1"),
        ("批准采购预算", "approve", "a2"),
        ("下午的变更单通过一下", "approve", "a3"),
        ("这个方案批一下", "approve", "a4"),
        ("催一下运维的工单", "urge", "u1"),
        ("提醒大家明天的评审会", "urge", "u2"),
        ("盯一下发布进度", "urge", "u3"),
        ("这事得催办了", "urge", "u4"),
        ("催一下对方单位的回函", "urge", "u5"),
    ]
    adversarial = [
        ("今天天气不错", "none", "x1"),
        ("中午吃什么", "none", "x2"),
        ("这篇文章写得不错", "none", "x3"),  # '写' without directive frame
        ("通过了很久的隧道", "none", "x4"),  # '通过' in non-directive sense
        ("看看就好，别当真", "none", "x5"),  # borderline, expected passive
    ]
    return positives + adversarial


def test_session_ingress() -> dict[str, Any]:
    """Offline self-test: accuracy ≥95% + e2e ≤2s + red-line assertions."""
    hits = 0
    total = 0
    accuracy_rows: list[dict[str, str]] = []
    for text, expected, label in _eval_set():
        got = parse_directive(text).action
        total += 1
        if got == expected:
            hits += 1
        accuracy_rows.append({"label": label, "expected": expected, "got": got})

    # full pipeline latency over a synthetic batch
    batch = [
        ImMessage(
            id=f"m{i}",
            platform="wecom",
            chat_id="wecom:work-core",
            sender="同事A",
            text="帮我查一下医保目录的最新版本",
            is_group=True,
            mentions_me=False,
        )
        for i in range(50)
    ] + [
        ImMessage(
            id=f"noise{i}",
            platform="wechat",
            chat_id="wechat:random-stranger",
            sender="陌生人",
            text="查一下广告",
            is_group=True,
            mentions_me=False,
        )
        for i in range(50)
    ]
    result = triage_session(batch)

    checks = {
        "accuracy_ge_95": hits / total >= 0.95,
        "e2e_within_2s": result["within_e2e_budget"],
        "noise_dropped": len(result["dropped"]) == 50,
        "cards_pending_approval": all(
            c["status"] == "pending_approval" for c in result["cards"]
        ),
        "gateway_uri": result["gateway"] == GATEWAY_URI,
    }
    return {
        "schema": SCHEMA,
        "accuracy": round(hits / total, 3),
        "checks": checks,
        "triage": {k: result[k] for k in ("cards", "dropped", "elapsed_ms")},
        "accuracy_rows": accuracy_rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["test_session_ingress"])
    parser.parse_args(argv)  # validates the command choice
    report = test_session_ingress()
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if all(report["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
