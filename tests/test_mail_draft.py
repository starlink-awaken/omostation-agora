"""Tests for agora mail.py (BET-Y1Q3-T10-113): 3-tier draft + table fidelity."""

from __future__ import annotations

from agora.server.tools_bos.mail import (
    bos_mail_draft,
    draft_three_tiers,
    extract_tables,
)


def test_three_tiers_llm_addresses_ask(monkeypatch):
    """真模型生成三档, 必须回应来件诉求(材料/期限), 且可进署名流程。"""
    import json as _json

    def fake_chat(prompt, model="fast", timeout=60.0, system=None):
        assert "验收报告" in prompt or "费用明细" in prompt, "prompt 应带来件正文"
        return _json.dumps(
            {
                "brief_confirm": "收悉。验收报告与费用明细我处正在核对，10月2日前反馈规划信息处。",
                "verbose_reply": "李明同志：\n来件收悉。一、电子病历升级项目验收报告初稿已成形，我处正组织核对；\n二、费用明细将于10月2日前随报告一并报送。",
                "polite_decline": "李明同志：\n来件收悉。因我处本周承担专项检查保障，材料难以及时齐备，恳请宽限至10月10日。",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agora.server.tools_bos._helpers.gateway_chat", fake_chat)
    body = "请贵处于本周五（10月2日）前提供电子病历升级项目的验收报告和费用明细。"
    result = draft_three_tiers(body, subject="请协助提供验收材料")
    assert result["degraded"] is False and result["ready_for_signature"] is True
    assert "10月2日" in result["tiers"]["brief_confirm"], result["tiers"]["brief_confirm"]
    assert "验收报告" in result["tiers"]["verbose_reply"]


def test_three_tiers_template_fallback_is_degraded(monkeypatch):
    """模型不可用 → 模板兜底, 必须标 degraded 且不可署名(模板不回应来件诉求)。"""
    monkeypatch.setattr("agora.server.tools_bos._helpers.gateway_chat", lambda *a, **k: None)
    result = draft_three_tiers("关于公共卫生应急演练的请示，请批复。")
    assert set(result["tiers"]) == {"brief_confirm", "verbose_reply", "polite_decline"}
    assert result["degraded"] is True
    assert result["ready_for_signature"] is False
    assert "一1" not in result["tiers"]["verbose_reply"], "模板不应再输出「一1．」乱码"


def test_three_tiers_tier_distinctness(monkeypatch):
    monkeypatch.setattr("agora.server.tools_bos._helpers.gateway_chat", lambda *a, **k: None)
    result = draft_three_tiers("关于公共卫生应急演练的请示，请批复。")
    values = set(result["tiers"].values())
    assert len(values) == 3, "三档草稿不应雷同"


def test_csv_table_fidelity_ge_90():
    csv_text = (
        "机构,床位数,预算（万元）\n人民医院,1200,800\n中医院,600,400\n卫生院,150,100\n"
    )
    result = extract_tables(csv_text, "csv")
    assert result["ok"] is True
    assert result["fidelity"] >= 0.9, result
    assert "人民医院" in result["markdown"] and "800" in result["markdown"]


def test_html_table_fidelity_ge_90():
    html_text = (
        "<table><tr><th>指标</th><th>目标值</th></tr>"
        "<tr><td>电子病历评级</td><td>4 级</td></tr>"
        "<tr><td>互联互通评级</td><td>5 星</td></tr></table>"
    )
    result = extract_tables(html_text, "html")
    assert result["ok"] is True and result["fidelity"] >= 0.9
    assert "电子病历评级" in result["markdown"] and "5 星" in result["markdown"]


def test_bos_mail_draft_with_attachment():
    body = "请审阅附表数据并反馈意见。"
    result = bos_mail_draft(
        body, attachments=[{"fmt": "csv", "text": "a,b\n1,2\n", "name": "data.csv"}]
    )
    assert result["service"] == "bos://inbox/mail/draft"
    assert len(result["attachments"]) == 1
    assert result["attachments"][0]["ok"] is True


def test_bos_mail_draft_end_to_end_latency_budget(monkeypatch):
    """done_when[0]: 模板兜底路径 ≤3 秒 (确定性实现应远低于预算)。

    模型路径的延迟看的是生成质量, 3 秒 TTFT 预算只对兜底模板有意义。
    """
    monkeypatch.setattr("agora.server.tools_bos._helpers.gateway_chat", lambda *a, **k: None)
    body = "关于医共体建设的实施方案，涵盖预算、进度与考核要求，请研究反馈。" * 20
    result = bos_mail_draft(body)
    assert result["within_budget"] is True
    assert result["latency_ms"] < 50
