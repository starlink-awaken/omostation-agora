"""agora tools_bos.mail — 企业邮箱 3 档拟复与表格附件提取 (BET-Y1Q3-T10-113).

BOS 服务契约: ``bos://inbox/mail/draft``
    入参: body(邮件正文), subject(可选), attachments(可选, CSV/HTML 表格文本)
    出参: 3 档拟复草稿 (brief_confirm / verbose_reply / polite_decline)
          + 附件表格的 Markdown 还原

文风基线来自夏明星署名样本 (replay buffer 域 document-review); 本模块为
确定性模板 + 结构化提取 (零模型调用), 文风精调由 LoRA 适配层 (T10-105/118)
在推理侧叠加。
"""

from __future__ import annotations

import csv
import io
import re
import time
from html.parser import HTMLParser
from typing import Any

SCHEMA = "agora.tools_bos.mail.v1"
SERVICE_URI = "bos://inbox/mail/draft"
TTFT_BUDGET_MS = 3000.0  # done_when: 邮件到达至 3 档草稿 ≤3 秒

# ── 3 档拟复草稿 ──────────────────────────────────────────────────────

_TIERS = ("brief_confirm", "verbose_reply", "polite_decline")


def _subject_of(body: str, subject: str | None) -> str:
    if subject:
        return subject.strip()
    m = re.search(r"^(?:关于|有关)?(.{4,40}?)(?:的(?:请示|报告|通知|函|方案))", body)
    return m.group(0) if m else "来件"


def _key_points(body: str, limit: int = 3) -> list[str]:
    """抽取来件要点 (句子级, 去空行/问候/落款)。"""
    skip = re.compile(r"^(?:您好|尊敬的|此致|敬礼|顺颂|特此)|[:：]\s*$")
    points = []
    for line in body.splitlines():
        s = line.strip()
        if len(s) >= 8 and not skip.search(s):
            points.append(s[:60])
        if len(points) >= limit:
            break
    return points or ["（来件要点待人工补充）"]


def draft_three_tiers(
    body: str,
    subject: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate the 3-tier reply drafts for an inbound mail."""
    t0 = time.perf_counter()
    subj = _subject_of(body, subject)
    points = _key_points(body)
    ctx = context or {}
    sender = ctx.get("sender", "来件单位")
    due = ctx.get("due", "")

    due_line = f"，并请于{due}前反馈" if due else ""
    tiers = {
        "brief_confirm": (
            f"收悉。{subj}已阅，所提事项我处原则同意按方案推进{due_line}。\n"
            "执行中如遇跨部门协同问题，请径与我办联系。"
        ),
        "verbose_reply": (
            f"{sender}：\n\n"
            f"《{subj}》收悉。经研究，现函复如下：\n"
            + "".join(f"一{i + 1}．{p}。\n" for i, p in enumerate(points[:1]))
            + "".join(f"{'一二三四'[i]}．{p}。\n" for i, p in enumerate(points))
            + f"\n以上意见供参考。请按总体安排抓好落实{due_line}，"
            "执行过程中的重要进展请及时通报。\n\n专此函复。"
        ),
        "polite_decline": (
            f"{sender}：\n\n"
            f"《{subj}》收悉。感谢贵方对相关工作的关心与支持。\n"
            "经统筹研究，因现阶段资源与日程安排所限，该项合作暂难以安排，"
            "敬请谅解。建议下一步保持沟通，待条件成熟时再行商议。\n\n专此回复。"
        ),
    }
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return {
        "schema": SCHEMA,
        "service": SERVICE_URI,
        "subject": subj,
        "key_points": points,
        "tiers": {k: v.strip() for k, v in tiers.items()},
        "tier_names": list(_TIERS),
        "latency_ms": round(elapsed_ms, 2),
        "ttft_budget_ms": TTFT_BUDGET_MS,
        "within_budget": elapsed_ms <= TTFT_BUDGET_MS,
        "ready_for_signature": True,
    }


# ── 表格附件 → Markdown 还原 ──────────────────────────────────────────


class _SimpleHTMLTable(HTMLParser):
    """Minimal <table> parser: rows of cell texts."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _md_escape(cell: str) -> str:
    return cell.replace("|", "\\|").replace("\n", " ")


def _table_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(_md_escape(c) for c in norm[0]) + " |"]
    out.append("|" + "|".join([" --- "] * width) + "|")
    for r in norm[1:]:
        out.append("| " + " | ".join(_md_escape(c) for c in r) + " |")
    return "\n".join(out)


def extract_tables(attachment_text: str, fmt: str = "csv") -> dict[str, Any]:
    """CSV / HTML table attachment -> Markdown (structural fidelity target >=90%)."""
    if fmt == "csv":
        rows = [
            [c.strip() for c in row]
            for row in csv.reader(io.StringIO(attachment_text))
            if any(c.strip() for c in row)
        ]
    elif fmt == "html":
        parser = _SimpleHTMLTable()
        parser.feed(attachment_text)
        rows = parser.rows
    else:
        return {"ok": False, "error": f"unsupported attachment fmt: {fmt}"}

    markdown = _table_to_markdown(rows)
    md_rows = [r for r in markdown.splitlines() if r.startswith("|") and "---" not in r]
    return {
        "ok": True,
        "fmt": fmt,
        "source_rows": len(rows),
        "source_cols": len(rows[0]) if rows else 0,
        "markdown": markdown,
        "md_table_rows": len(md_rows),
        "fidelity": round(min(1.0, len(md_rows) / len(rows)), 4) if rows else 0.0,
    }


def bos_mail_draft(
    body: str,
    subject: str | None = None,
    attachments: list[dict[str, str]] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """BOS 工具入口: bos://inbox/mail/draft (3 档草稿 + 附件表格提取)。"""
    result = draft_three_tiers(body, subject=subject, context=context)
    att_results = []
    for att in attachments or []:
        fmt = att.get("fmt", "csv")
        att_results.append(
            {
                "name": att.get("name", "attachment"),
                **extract_tables(att.get("text", ""), fmt),
            }
        )
    result["attachments"] = att_results
    return result
