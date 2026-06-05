"""Unit tests for the OpenRouter client parsing (src/llm/client.py).

No network: the OpenAI client is replaced with a fake whose
``chat.completions.create`` returns a canned response.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import src.llm.client as client_mod
from src.llm.client import analyze_risk
from src.llm.schemas import RiskType


def _fake_usage(cost: float | None) -> object:
    data = {"prompt_tokens": 120, "completion_tokens": 30}
    if cost is not None:
        data["cost"] = cost
    return SimpleNamespace(model_dump=lambda: data)


def _fake_response(*, arguments: str | None, content: str | None = None) -> object:
    if arguments is None:
        message = SimpleNamespace(tool_calls=None, content=content)
    else:
        tool_call = SimpleNamespace(
            type="function", function=SimpleNamespace(arguments=arguments)
        )
        message = SimpleNamespace(tool_calls=[tool_call], content=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)], usage=_fake_usage(0.0012)
    )


def _install_fake_client(monkeypatch, response: object) -> AsyncMock:
    create = AsyncMock(return_value=response)
    fake = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(client_mod, "get_client", lambda: fake)
    return create


async def test_analyze_risk_parses_tool_call(monkeypatch) -> None:
    args = json.dumps(
        {
            "risk_events": [
                {
                    "message_id": "m1",
                    "risk_type": "private_channel",
                    "score": 75,
                    "confidence": 0.9,
                    "explanation": "moved private",
                    "context_message_ids": ["m1"],
                }
            ]
        }
    )
    create = _install_fake_client(monkeypatch, _fake_response(arguments=args))

    result = await analyze_risk(
        model="anthropic/claude-haiku-4-5",
        system_prompt="sys",
        conversation_block="<conversation></conversation>",
    )

    assert result.analysis.risk_events[0].risk_type is RiskType.PRIVATE_CHANNEL
    assert result.tokens_in == 120
    assert result.tokens_out == 30
    assert result.cost_usd == Decimal("0.0012")
    assert result.latency_ms >= 0
    # forced tool_choice was sent
    kwargs = create.call_args.kwargs
    assert kwargs["tool_choice"]["function"]["name"] == "report_risk_events"
    assert kwargs["temperature"] == 0


async def test_analyze_risk_no_tool_call_is_empty(monkeypatch) -> None:
    _install_fake_client(
        monkeypatch, _fake_response(arguments=None, content="nothing risky")
    )
    result = await analyze_risk(
        model="m", system_prompt="s", conversation_block="<conversation></conversation>"
    )
    assert result.analysis.risk_events == []
    assert result.raw_response == "nothing risky"


async def test_analyze_risk_cost_absent(monkeypatch) -> None:
    resp = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(tool_calls=None, content=""))
        ],
        usage=_fake_usage(None),  # OpenRouter didn't return a cost
    )
    _install_fake_client(monkeypatch, resp)
    result = await analyze_risk(model="m", system_prompt="s", conversation_block="x")
    assert result.cost_usd is None
