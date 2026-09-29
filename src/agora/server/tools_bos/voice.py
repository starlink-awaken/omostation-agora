"""agora tools_bos.voice — 语音随想转录、润色与分拣 (BET-Y2Q1-T2-01).

BOS 工具契约: ``bos://voice/memo/ingest``
    入参: audio_path(音频文件), engine(可选, 强制指定 ASR 引擎)
    出参: 转录文本 + 润色稿 + 分拣结果(任务项/时间节点/责任人/随笔)

ASR 引擎可插拔（按序探测）: whisper-cli → whisper-cpp → funasr → gateway(aetherforge 门面 asr 档)。
全部缺失时返回 ``needs_asr_backend`` 诚实失败——绝不伪造转录文本。
润色与分拣为确定性规则（口水词表 + 句式匹配），零模型调用。
"""

from __future__ import annotations

import json
import re
import shutil
import os
import subprocess
import time
from pathlib import Path
from typing import Any

SCHEMA = "agora.tools_bos.voice.v1"
SERVICE_URI = "bos://voice/memo/ingest"
TTFT_BUDGET_S = 1.5  # done_when: 1 分钟语音端到端 ≤1.5s (引擎可用时)

# 口水词/冗余清洗表（按频次排序的中文口语填充词）
_FILLERS = [
    "那个那个",
    "这个这个",
    "就是就是",
    "然后然后",
    "呃",
    "嗯",
    "啊",
    "那个",
    "这个",
    "就是说",
    "怎么说呢",
    "反正",
    "其实呢",
    "对吧",
    "你知道吗",
    "我觉得吧",
]
_FILLER_RE = re.compile("|".join(_FILLERS))

# 口述待办常用「要把X发给Y / 记得 / 别忘了 / 报送 / 审核」, 此前只认公文动词,
# 「明天上午十点前要把初稿发给张磊审核」被当成随笔
_TASK_VERB = (
    r"(?:落实|跟进|牵头|负责|完成|提交|梳理|输出|反馈|组织|协调|编制|推动|采购|复审|预约|安排"
    r"|发给|发送|报送|上报|审核|审定|催办|回复|联系|记得|别忘|提醒一下|要把)"
)
_TIME_HINT = r"(?:(?:今天|明天|后天|[本下]?周[一二三四五六日天])(?:上午|下午|晚上)?(?:\d{1,2}|[一二三四五六七八九十]{1,3})点[之]?前?|(本周|下周|本月|月底|今天|明天|后天|周五|下周一)[之]?前|\d{1,2}月\d{1,2}日前|(\d+)\s*个?工作日[之]?内|[本下]?周[一二三四五六日天]|下午|上午|晚上)"
_RESPONSIBLE = r"([\u4e00-\u9fff]{2,4}(?:处|科|室|中心|组|团队|部门)|夏明星|[小老][\u4e00-\u9fff](?=负责|牵头|跟进)|[A-Z][a-z]+)"


def _detect_engines() -> list[str]:
    """按优先级返回本机可用的 ASR 引擎名。"""
    available = []
    for exe in ("whisper-cli", "whisper-cpp", "whisper"):
        if shutil.which(exe):
            available.append(exe)
            break
    try:
        import importlib.util

        if importlib.util.find_spec("funasr"):
            available.append("funasr")
    except Exception:
        pass
    return available


_GATEWAY_ASR_MODEL = os.environ.get("AGORA_ASR_MODEL", "asr")

# 领域热词表: 作为 whisper initial_prompt 传入(oMLX stt 转解码器前缀), 并在加标点前
# 做确定性纠错(全链路实测 2026-09-29: 等保复测→灯保附册、晨报→陈报、处务会→初五会)。
# 环境变量 AGORA_ASR_GLOSSARY 可整体覆盖, 格式「错词→正词」每行一条。
ASR_GLOSSARY: dict[str, str] = dict(
    line.split("→", 1) for line in (os.environ.get("AGORA_ASR_GLOSSARY") or "").splitlines() if "→" in line
) or {
    "灯保附册": "等保复测",
    "等保附册": "等保复测",
    "陈报": "晨报",
    "初五会": "处务会",
    "夜物与": "业务域",
    "资查": "自查",
    "卫健会": "卫健委",
    "信创办": "信创办",
}


def _asr_prompt() -> str:
    """whisper initial_prompt: 领域词表原文, 让解码器偏向这些词。"""
    return os.environ.get("AGORA_ASR_PROMPT") or "、".join(sorted(set(ASR_GLOSSARY.values())))[:200]


def apply_glossary(text: str) -> tuple[str, int]:
    """按词表确定性纠错, 返回 (纠错后文本, 纠错次数)。只替换词表命中的错词。"""
    n = 0
    for wrong, right in ASR_GLOSSARY.items():
        if wrong in text:
            n += text.count(wrong)
            text = text.replace(wrong, right)
    return text, n


def _gateway_key() -> str:
    key = (
        os.environ.get("AETHERFORGE_API_KEY") or os.environ.get("LLM_GATEWAY_KEY") or ""
    )
    if key:
        return key
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "aetherforge-gateway", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _transcribe_gateway(audio: Path) -> str:
    """经 aetherforge 门面 /v1/audio/transcriptions(oMLX whisper)。本机无端侧引擎时的默认路径 ——
    此前本机没装 whisper-cli/funasr, voice-memo 恒返回 needs_asr_backend。"""
    import httpx

    base = (
        (os.environ.get("LLM_GATEWAY_URL") or "http://127.0.0.1:4000")
        .rstrip("/")
        .removesuffix("/v1")
    )
    key = _gateway_key()
    with httpx.Client(timeout=600, trust_env=False) as client:
        resp = client.post(
            f"{base}/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"} if key else {},
            # language+prompt: oMLX whisper 支持(initial_prompt 弱偏置), 此前没传
            data={"model": _GATEWAY_ASR_MODEL, "language": os.environ.get("AGORA_ASR_LANGUAGE", "zh"), "prompt": _asr_prompt()},
            files={"file": (audio.name, audio.read_bytes(), "audio/wav")},
        )
        resp.raise_for_status()
        return (resp.json().get("text") or "").strip()


def transcribe(audio_path: str | Path, engine: str | None = None) -> dict[str, Any]:
    """Transcribe audio via the first available engine (or the forced one)."""
    audio = Path(audio_path)
    if not audio.is_file():
        return {"ok": False, "error_code": "audio_not_found", "audio": str(audio)}
    engines = [engine] if engine else _detect_engines() + ["gateway"]
    if not engines or engines == [None]:
        return {
            "ok": False,
            "error_code": "needs_asr_backend",
            "detail": "本机未检测到 whisper-cli/whisper-cpp/whisper/funasr — "
            "安装任一端侧引擎后重试（不伪造转录文本）",
            "install_hint": "brew install whisper-cpp  |  pip install funasr",
        }
    t0 = time.perf_counter()
    errors: list[
        str
    ] = []  # 各引擎失败原因 —— 此前全吞掉, 门面 413 被报成"模型权重或参数问题"
    for name in engines:
        if name == "gateway":
            try:
                txt = _transcribe_gateway(audio)
            except Exception as exc:
                errors.append(f"gateway: {exc}"[:200])
                continue
            if txt:
                return {
                    "ok": True,
                    "engine": "gateway",
                    "text": txt,
                    "elapsed_s": round(time.perf_counter() - t0, 3),
                }
            continue
        if name == "funasr":
            continue  # funasr 走 python API，下方统一处理
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            res = subprocess.run(
                [exe, "--file", str(audio), "--output-txt"],  # whisper.cpp 约定
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            txt = res.stdout.strip()
            txt_path = audio.with_suffix(".txt")
            if not txt and txt_path.exists():
                txt = txt_path.read_text(encoding="utf-8").strip()
            if txt:
                return {
                    "ok": True,
                    "engine": name,
                    "text": txt,
                    "elapsed_s": round(time.perf_counter() - t0, 3),
                }
        except Exception:
            continue
    return {
        "ok": False,
        "error_code": "needs_asr_backend",
        "detail": f"引擎 {engines} 存在但转录失败"
        + (f": {'; '.join(errors)}" if errors else "（模型权重或参数问题）"),
        "install_hint": "检查模型权重路径，或改用 brew install whisper-cpp / pip install funasr",
    }


_SENT_PUNCT = "。！？；!?;"
_PUNCT_RE = re.compile(r"[，。！？；、,.!?;:：\s]")
_PUNCT_MODEL = os.environ.get("AGORA_PUNCT_MODEL", "fast")


def restore_punctuation(text: str) -> tuple[str, bool]:
    """whisper 经门面常输出无标点长串 → polish 按行切句全失效(整场会议成一条任务)。
    交门面 LLM 只加标点; 去标点后与原文逐字不一致(改写/幻觉)则弃用, 保持原文。"""
    if len(text) < 20 or any(c in text for c in "。！？，"):
        return text, False
    import httpx

    base = (
        (os.environ.get("LLM_GATEWAY_URL") or "http://127.0.0.1:4000")
        .rstrip("/")
        .removesuffix("/v1")
    )
    key = _gateway_key()
    try:
        with httpx.Client(timeout=60, trust_env=False) as client:
            resp = client.post(
                f"{base}/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"} if key else {},
                json={
                    "model": _PUNCT_MODEL,
                    "temperature": 0,
                    "max_tokens": len(text) * 2 + 64,
                    "messages": [
                        {
                            "role": "system",
                            "content": "给语音转写文本加中文标点。只加标点，不增删改任何字，"
                            "只输出结果。",
                        },
                        {"role": "user", "content": text},
                    ],
                },
            )
            resp.raise_for_status()
            out = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return text, False
    if _PUNCT_RE.sub("", out) != _PUNCT_RE.sub("", text):
        return text, False
    return out, True


def _sentences(text: str) -> list[str]:
    parts = re.split(f"(?<=[{_SENT_PUNCT}])|\n", text)
    return [p.strip() for p in parts if p and p.strip()]


def polish(text: str) -> dict[str, Any]:
    """口语冗余清洗 + 结构化分拣（任务项/时间节点/责任人/随笔正文）。"""
    cleaned = _FILLER_RE.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    tasks, times, owners = [], [], []
    essay: list[str] = []
    for line in _sentences(cleaned):
        s = line.strip()
        if not s:
            continue
        if re.search(_TASK_VERB, s):
            tm = re.search(_TIME_HINT, s)
            rm = re.search(_RESPONSIBLE, s)
            tasks.append(
                {
                    "task": s[:80],
                    "deadline": tm.group(0) if tm else "待排期",
                    "owner": (rm.group(1) if rm else "待指定"),
                }
            )
            if tm:
                times.append(tm.group(0))
            if rm:
                owners.append(rm.group(1))
        else:
            essay.append(s)

    return {
        "polished": cleaned,
        "task_items": tasks,
        "time_mentions": sorted(set(times)),
        "owners": sorted(set(owners)),
        "essay": "\n".join(essay),
        "kind": "task_list" if tasks else "essay",
    }


def ingest_memo(audio_path: str | Path, engine: str | None = None) -> dict[str, Any]:
    """BOS 工具入口: bos://voice/memo/ingest。"""
    tr = transcribe(audio_path, engine=engine)
    if not tr.get("ok"):
        return {"schema": SCHEMA, "service": SERVICE_URI, **tr}
    corrected, n_fixed = apply_glossary(tr["text"])
    text, punctuated = restore_punctuation(corrected)
    polished = polish(text)
    return {
        "schema": SCHEMA,
        "service": SERVICE_URI,
        "ok": True,
        "engine": tr["engine"],
        "glossary_fixes": n_fixed,
        "elapsed_s": tr.get("elapsed_s"),
        "budget_s": TTFT_BUDGET_S,
        "within_budget": (tr.get("elapsed_s") or 99) <= TTFT_BUDGET_S,
        "punctuated": punctuated,
        **polished,
    }
