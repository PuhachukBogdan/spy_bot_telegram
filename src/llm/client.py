"""OpenRouter LLM client: forced tool-use risk analysis with retries. Phase 8.

A thin async wrapper over the OpenAI SDK pointed at OpenRouter. The one risk-
analysis call forces the model to answer through the ``report_risk_events`` tool
(``tool_choice``), so the result is always validated JSON (:class:`RiskAnalysis`)
rather than free-form text. Transient API errors are retried
(:func:`src.utils.retry.with_llm_retry`); everything deterministic propagates.

This module performs the call and parses the result. Persistence (``llm_calls`` +
Storage + cost) lives in :mod:`src.llm.audit` / :mod:`src.db.queries.cost`; the
budget circuit breaker is a later phase. Cost, when OpenRouter returns it in the
usage block, is surfaced on :class:`LlmResult` for the caller to record.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionToolChoiceOptionParam,
    ChatCompletionToolParam,
)

from src.config import settings
from src.llm.file_schemas import (
    FILE_RISK_TOOL_NAME,
    FileRiskAnalysis,
    build_file_risk_tool,
)
from src.llm.schemas import (
    RISK_ANALYSIS_TOOL_NAME,
    RiskAnalysis,
    build_risk_analysis_tool,
)
from src.utils.logging import get_logger
from src.utils.retry import with_llm_retry

log = get_logger(__name__)

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """Lazily build the shared OpenRouter client (OpenAI-compatible API)."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=_OPENROUTER_BASE_URL,
            api_key=settings.OPENROUTER_API_KEY.get_secret_value(),
        )
    return _client


async def close_client() -> None:
    """Close the shared client (call on shutdown)."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


@dataclass(slots=True)
class LlmFileResult:
    """Parsed outcome of one file-risk analysis call."""

    analysis: FileRiskAnalysis
    model: str
    raw_response: str
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: Decimal | None
    latency_ms: int


@dataclass(slots=True)
class LlmResult:
    """Parsed outcome of one risk-analysis call.

    ``raw_response`` is the tool-call arguments JSON (or the message content if the
    model declined the tool) — archived verbatim by the audit layer.
    """

    analysis: RiskAnalysis
    model: str
    raw_response: str
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: Decimal | None
    latency_ms: int


async def analyze_risk(
    *, model: str, system_prompt: str, conversation_block: str
) -> LlmResult:
    """Run one forced-tool risk-analysis call and return the parsed result.

    Raises on non-transient API errors and on a tool payload that fails
    :class:`RiskAnalysis` validation (a malformed model response is a real error,
    not a silent empty result). A response with no tool call is treated as
    "nothing risky" (empty analysis).
    """
    client = get_client()
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": conversation_block},
    ]
    tools = cast("list[ChatCompletionToolParam]", [build_risk_analysis_tool()])
    tool_choice = cast(
        "ChatCompletionToolChoiceOptionParam",
        {"type": "function", "function": {"name": RISK_ANALYSIS_TOOL_NAME}},
    )

    @with_llm_retry()
    async def _call() -> ChatCompletion:
        return await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=0,
            extra_body={"usage": {"include": True}},
        )

    start = time.perf_counter()
    response = await _call()
    latency_ms = int((time.perf_counter() - start) * 1000)

    analysis, raw = _parse_analysis(response)
    tokens_in, tokens_out, cost_usd = _parse_usage(response)
    log.info(
        "llm.analyzed",
        model=model,
        events=len(analysis.risk_events),
        signals=len(analysis.activity_signals),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
    )
    return LlmResult(
        analysis=analysis,
        model=model,
        raw_response=raw,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )


async def analyze_file_risk(
    *, model: str, system_prompt: str, file_name: str, file_content: str
) -> LlmFileResult:
    """Run a forced-tool file-risk analysis call and return the parsed result.

    File content is wrapped in a ``<file>`` envelope (HTML-escaped) to isolate
    untrusted data from the system prompt (prompt-injection defence).
    """
    from html import escape

    client = get_client()
    file_block = (
        f'<file name="{escape(file_name, quote=True)}">\n'
        f"{escape(file_content)}\n"
        f"</file>"
    )
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": file_block},
    ]
    tools = cast("list[ChatCompletionToolParam]", [build_file_risk_tool()])
    tool_choice = cast(
        "ChatCompletionToolChoiceOptionParam",
        {"type": "function", "function": {"name": FILE_RISK_TOOL_NAME}},
    )

    @with_llm_retry()
    async def _call() -> ChatCompletion:
        return await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=0,
            extra_body={"usage": {"include": True}},
        )

    start = time.perf_counter()
    response = await _call()
    latency_ms = int((time.perf_counter() - start) * 1000)

    analysis, raw = _parse_file_analysis(response)
    tokens_in, tokens_out, cost_usd = _parse_usage(response)
    log.info(
        "llm.file_analyzed",
        model=model,
        findings=len(analysis.findings),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
    )
    return LlmFileResult(
        analysis=analysis,
        model=model,
        raw_response=raw,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )


def _parse_file_analysis(response: ChatCompletion) -> tuple[FileRiskAnalysis, str]:
    """Extract and validate the file-risk tool payload; empty analysis if no call."""
    message = response.choices[0].message
    tool_calls = message.tool_calls
    if not tool_calls:
        return FileRiskAnalysis(), message.content or ""
    first = tool_calls[0]
    if first.type != "function":
        return FileRiskAnalysis(), message.content or ""
    arguments = first.function.arguments
    return FileRiskAnalysis.model_validate_json(arguments), arguments


def _parse_analysis(response: ChatCompletion) -> tuple[RiskAnalysis, str]:
    """Extract and validate the tool payload; empty analysis if no tool call."""
    message = response.choices[0].message
    tool_calls = message.tool_calls
    if not tool_calls:
        return RiskAnalysis(), message.content or ""
    first = tool_calls[0]
    if first.type != "function":  # narrow off the custom-tool union member
        return RiskAnalysis(), message.content or ""
    arguments = first.function.arguments
    return RiskAnalysis.model_validate_json(arguments), arguments


def _parse_usage(
    response: ChatCompletion,
) -> tuple[int | None, int | None, Decimal | None]:
    """Pull token counts and (OpenRouter) cost from the usage block."""
    usage = response.usage
    if usage is None:
        return None, None, None
    raw = usage.model_dump()
    cost_raw = raw.get("cost")
    cost = Decimal(str(cost_raw)) if cost_raw is not None else None
    return raw.get("prompt_tokens"), raw.get("completion_tokens"), cost
