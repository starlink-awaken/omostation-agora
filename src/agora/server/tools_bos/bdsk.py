"""B.D.S.K. virtual-board evaluation through the canonical BOS compute route.

The persona endpoint is orchestration, not an inference engine. Every proved
evaluation therefore comes from ``bos://compute/aetherforge/infer``. There is
no rules-based success fallback and this endpoint never writes an ADR.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from agora.mcp.resolver.api import resolve_bos_uri as _resolve_bos_uri
from agora.server._response import FORMAT_VERSION, _error, _ok

_COMPUTE_URI = "bos://compute/aetherforge/infer"
_ROLES = ("builder", "devil", "sage", "keeper")
_SAFE_VERDICTS = {
    "REVIEW_REQUIRED",
    "PROCEED_WITH_GUARDRAILS",
    "DO_NOT_PROCEED",
}


def _persist_bdsk_adr(*_args: Any, **_kwargs: Any) -> str:
    """Retained import shim; automatic ADR persistence is intentionally disabled."""
    raise RuntimeError("automatic_adr_persistence_disabled")


def _not_proven(error_code: str) -> dict[str, Any]:
    result = _error("BDSK evaluation not proven")
    result.update(
        {
            "proof_state": "not_proven",
            "verdict": "NOT_PROVEN",
            "compute_uri": _COMPUTE_URI,
            "error_code": error_code,
        }
    )
    return result


def _extract_openai_content(result: Any) -> str | None:
    """Extract bounded OpenAI-compatible content from resolver envelopes."""
    current = result
    for _depth in range(4):
        if not isinstance(current, Mapping):
            return None
        choices = current.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if not isinstance(first, Mapping):
                return None
            message = first.get("message")
            if not isinstance(message, Mapping):
                return None
            content = message.get("content")
            if isinstance(content, str) and len(content) <= 32_000:
                return content
            return None
        nested = current.get("result")
        if nested is current:
            return None
        current = nested
    return None


def _validated_board_result(content: str) -> dict[str, Any] | None:
    """Accept only the small decision schema; discard all untrusted extra output."""
    try:
        raw = json.loads(content)
    except (json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(raw, Mapping):
        return None

    verdict = raw.get("verdict")
    risk_score = raw.get("risk_score")
    recommendation = raw.get("recommendation")
    reviews = raw.get("board_reviews")
    if verdict not in _SAFE_VERDICTS:
        return None
    if (
        isinstance(risk_score, bool)
        or not isinstance(risk_score, int)
        or not 0 <= risk_score <= 100
    ):
        return None
    if not isinstance(recommendation, str) or not recommendation.strip():
        return None
    if not isinstance(reviews, Mapping):
        return None

    safe_reviews: dict[str, dict[str, str]] = {}
    for role in _ROLES:
        review = reviews.get(role)
        if not isinstance(review, Mapping):
            return None
        opinion = review.get("opinion")
        if not isinstance(opinion, str) or not opinion.strip():
            return None
        safe_review = {"opinion": opinion[:4_000]}
        for optional in ("role", "focus"):
            value = review.get(optional)
            if isinstance(value, str) and value.strip():
                safe_review[optional] = value[:500]
        safe_reviews[role] = safe_review

    return {
        "verdict": verdict,
        "risk_score": risk_score,
        "recommendation": recommendation[:4_000],
        "board_reviews": safe_reviews,
    }


def _prompt(topic: str, mode: str, context: str) -> str:
    return f"""Evaluate this proposal as the B.D.S.K. four-corner board.
Mode: {mode}
Topic: {topic}
Context: {context or '[not provided]'}

Return JSON only with exactly these decision fields:
{{
  "verdict": "REVIEW_REQUIRED|PROCEED_WITH_GUARDRAILS|DO_NOT_PROCEED",
  "risk_score": 0,
  "recommendation": "bounded human-review recommendation",
  "board_reviews": {{
    "builder": {{"opinion": "..."}},
    "devil": {{"opinion": "..."}},
    "sage": {{"opinion": "..."}},
    "keeper": {{"opinion": "..."}}
  }}
}}
Never claim APPROVED or ACCEPTED. This output is advisory and cannot authorize
a commit, merge, deployment, ADR, or external side effect.
"""


async def persona_bdsk_evaluate(
    topic: str,
    mode: str = "deep",
    context: str = "",
    persist_adr: bool = False,
    adr_dir: str = "docs/decisions",
) -> dict[str, Any]:
    """Evaluate via AetherForge; unavailable or malformed compute is not proven."""
    del adr_dir  # legacy input retained only to fail closed without touching disk
    if persist_adr:
        return _not_proven("automatic_adr_persistence_disabled")
    if mode not in {"deep", "fast"}:
        return _not_proven("unsupported_mode")
    if not isinstance(topic, str) or not topic.strip():
        return _not_proven("topic_required")

    try:
        compute_result = await _resolve_bos_uri(
            _COMPUTE_URI,
            prompt=_prompt(topic, mode, context),
            model="coding-fast",
            routing_mode="local",
            stream=False,
            timeout=120,
        )
    except Exception:
        return _not_proven("compute_unavailable")
    if not isinstance(compute_result, Mapping) or compute_result.get("status") != "ok":
        return _not_proven("compute_unavailable")

    content = _extract_openai_content(compute_result)
    board_result = _validated_board_result(content) if content is not None else None
    if board_result is None:
        return _not_proven("invalid_compute_response")

    return _ok(
        {
            "format_version": FORMAT_VERSION,
            "topic": topic,
            "mode": mode,
            "proof_state": "proven",
            "compute_uri": _COMPUTE_URI,
            **board_result,
        }
    )


# ── route helper ──────────────────────────────────────────────
