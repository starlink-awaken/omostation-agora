"""Tests for agora mail.py (BET-Y1Q3-T10-113): 3-tier draft + table fidelity."""

from __future__ import annotations

from agora.server.tools_bos.mail import (
    bos_mail_draft,
    draft_three_tiers,
    extract_tables,
)


def test_three_tiers_structure_and_latency():
    body = "关于区域全民健康信息平台互联互通成熟度测评的工作方案，请研究并提出意见。预算 200 万元。"
    result = draft_three_tiers(body, subject="关于…方案的意见")
    assert set(result["tiers"]) == {"brief_confirm", "verbose_reply", "polite_decline"}
    assert all(len(v) >= 20 for v in result["tiers"].values())
    assert result["latency_ms"] <= 3000 and result["within_budget"] is True
    assert result["ready_for_signature"] is True


def test_three_tiers_tier_distinctness():
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


def test_bos_mail_draft_end_to_end_latency_budget():
    """done_when[0]: 邮件到达至 3 档草稿 ≤3 秒 (确定性实现应远低于预算)。"""
    body = "关于医共体建设的实施方案，涵盖预算、进度与考核要求，请研究反馈。" * 20
    result = bos_mail_draft(body)
    assert result["within_budget"] is True
    assert result["latency_ms"] < 50
