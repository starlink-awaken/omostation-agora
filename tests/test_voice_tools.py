"""Tests for agora voice.py (BET-Y2Q1-T2-01): polish, sorting, honest ASR failure."""

from __future__ import annotations

from agora.server.tools_bos.voice import ingest_memo, polish, transcribe


def test_polish_removes_fillers_and_keeps_content():
    raw = "呃 那个 我们就是说要落实那个数据迁移，嗯 下周前完成。"
    result = polish(raw)
    assert "呃" not in result["polished"] and "那个" not in result["polished"]
    assert "数据迁移" in result["polished"]
    assert result["kind"] == "task_list"
    assert result["task_items"] and result["task_items"][0]["deadline"]


def test_polish_essay_classification():
    result = polish("今天看了架构评审的记录，有些想法。微服务的边界还是要按团队规模来定。")
    assert result["kind"] == "essay"
    assert "微服务" in result["essay"]


def test_extract_time_and_owner():
    result = polish("夏明星 负责复审方案，周五前反馈。")
    assert result["owners"] == ["夏明星"]
    assert "周五前" in result["time_mentions"]


def test_transcribe_missing_audio_honest():
    result = transcribe("/nonexistent/audio.wav")
    assert result["ok"] is False and result["error_code"] == "audio_not_found"


def test_ingest_memo_needs_asr_backend_honest(tmp_path):
    fake = tmp_path / "a.wav"
    fake.write_bytes(b"fake")
    result = ingest_memo(fake, engine="__nonexistent__")
    # 引擎强制不存在 → 诚实失败，绝不伪造文本
    assert result["ok"] is False
    assert result["error_code"] == "needs_asr_backend"
    assert "安装" in result.get("install_hint", "") or "权重" in result.get("detail", "")
