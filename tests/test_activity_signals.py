"""Unit tests for the activity signals layer (migration 0011).

Covers: LLM schema parsing, query persistence, and batch_processor integration.
No real DB or LLM — fake connection + monkeypatching.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from src.db.models import ActivitySignalRow
from src.db.queries.activity_signals import save_activity_signal
from src.llm.schemas import ActivitySignal, ActivitySignalType, RiskAnalysis
from src.pipeline import batch_processor as bp_mod
from src.pipeline.batch_processor import _persist

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _risk_analysis_json(signals: list[dict[str, Any]]) -> str:
    return json.dumps({"risk_events": [], "activity_signals": signals})


def _signal_dict(
    message_id: str,
    signal_type: str = "manager_proposal",
    description: str = "Offered 15% rev share",
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "signal_type": signal_type,
        "description": description,
    }


# ---------------------------------------------------------------------------
# Schema parsing
# ---------------------------------------------------------------------------


def test_risk_analysis_parses_activity_signals() -> None:
    mid = str(uuid4())
    raw = _risk_analysis_json([_signal_dict(mid)])
    analysis = RiskAnalysis.model_validate_json(raw)
    assert len(analysis.activity_signals) == 1
    sig = analysis.activity_signals[0]
    assert sig.message_id == mid
    assert sig.signal_type == ActivitySignalType.MANAGER_PROPOSAL
    assert sig.description == "Offered 15% rev share"


def test_risk_analysis_parses_deal_closed() -> None:
    mid = str(uuid4())
    raw = _risk_analysis_json(
        [_signal_dict(mid, signal_type="deal_closed", description="Agreed on terms")]
    )
    analysis = RiskAnalysis.model_validate_json(raw)
    assert analysis.activity_signals[0].signal_type == ActivitySignalType.DEAL_CLOSED


def test_risk_analysis_empty_signals_by_default() -> None:
    analysis = RiskAnalysis()
    assert analysis.activity_signals == []


def test_risk_analysis_both_lists_coexist() -> None:
    mid = str(uuid4())
    raw = json.dumps({
        "risk_events": [],
        "activity_signals": [_signal_dict(mid)],
    })
    analysis = RiskAnalysis.model_validate_json(raw)
    assert len(analysis.risk_events) == 0
    assert len(analysis.activity_signals) == 1


def test_activity_signal_type_values() -> None:
    assert ActivitySignalType.MANAGER_PROPOSAL == "manager_proposal"
    assert ActivitySignalType.DEAL_CLOSED == "deal_closed"


# ---------------------------------------------------------------------------
# Query: save_activity_signal
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self) -> None:
        self.inserts: list[tuple[Any, ...]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        self.inserts.append(args)
        chat_id, message_id, sender_id, signal_type, description = args
        return {
            "id": uuid4(),
            "chat_id": chat_id,
            "message_id": message_id,
            "sender_id": sender_id,
            "signal_type": signal_type,
            "description": description,
            "created_at": datetime.now(UTC),
        }


async def test_save_activity_signal_inserts_row() -> None:
    conn = _FakeConn()
    chat_id = uuid4()
    message_id = uuid4()
    row = await save_activity_signal(
        conn,  # type: ignore[arg-type]
        chat_id=chat_id,
        message_id=message_id,
        sender_id=12345,
        signal_type="manager_proposal",
        description="Proposed a new deal",
    )
    assert isinstance(row, ActivitySignalRow)
    assert row.chat_id == chat_id
    assert row.sender_id == 12345
    assert row.signal_type == "manager_proposal"
    assert conn.inserts[0][3] == "manager_proposal"


async def test_save_activity_signal_nullable_message_id() -> None:
    conn = _FakeConn()
    row = await save_activity_signal(
        conn,  # type: ignore[arg-type]
        chat_id=uuid4(),
        message_id=None,
        sender_id=None,
        signal_type="deal_closed",
        description=None,
    )
    assert row.message_id is None
    assert row.sender_id is None


# ---------------------------------------------------------------------------
# batch_processor integration: _persist saves activity signals
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_persist(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch _persist's I/O collaborators; return a recorder dict."""
    rec: dict[str, Any] = {"signals": [], "risks": []}

    async def fake_record_llm_call(conn: Any, **kw: Any) -> None:
        pass

    async def fake_record_llm_cost(conn: Any, cost: Any) -> None:
        pass

    async def fake_save_risk_event(conn: Any, **kw: Any) -> Any:
        return None

    async def fake_save_activity_signal(conn: Any, **kw: Any) -> Any:
        rec["signals"].append(kw)
        return None

    async def fake_update_watermark(conn: Any, chat_id: Any, ts: Any) -> None:
        pass

    class _NullAcquire:
        async def __aenter__(self) -> _NullAcquire:
            return self

        async def __aexit__(self, *_: Any) -> bool:
            return False

        def transaction(self) -> _NullAcquire:
            return self

    monkeypatch.setattr(bp_mod, "acquire_connection", lambda: _NullAcquire())
    monkeypatch.setattr(bp_mod, "record_llm_call", fake_record_llm_call)
    monkeypatch.setattr(bp_mod, "record_llm_cost", fake_record_llm_cost)
    monkeypatch.setattr(bp_mod, "save_risk_event", fake_save_risk_event)
    monkeypatch.setattr(bp_mod, "save_activity_signal", fake_save_activity_signal)
    monkeypatch.setattr(bp_mod, "update_chat_last_processed", fake_update_watermark)
    return rec


async def test_persist_saves_activity_signals(
    patched_persist: dict[str, Any],
) -> None:
    from src.db.models import Chat, Message
    from src.llm.client import LlmResult

    msg_id = uuid4()
    chat = Chat(
        id=uuid4(), telegram_chat_id=-100, status="active", created_at=datetime.now(UTC)
    )
    msg = Message(
        id=msg_id, telegram_message_id=1, chat_id=chat.id,
        sender_id=99999, sender_role="internal", message_type="text",
        message_text="Let's do the deal", timestamp=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    analysis = RiskAnalysis(
        activity_signals=[
            ActivitySignal(
                message_id=str(msg_id),
                signal_type=ActivitySignalType.DEAL_CLOSED,
                description="Agreed on terms",
            )
        ]
    )
    result = LlmResult(
        analysis=analysis, model="test", raw_response="{}",
        tokens_in=10, tokens_out=5, cost_usd=None, latency_ms=100,
    )

    alertable = await _persist(chat, [msg], "prompt", result, [], datetime.now(UTC))

    assert alertable == []
    assert len(patched_persist["signals"]) == 1
    saved = patched_persist["signals"][0]
    assert saved["sender_id"] == 99999
    assert saved["signal_type"] == "deal_closed"
    assert saved["description"] == "Agreed on terms"
    assert saved["message_id"] == msg_id


async def test_persist_skips_signal_with_unknown_message_id(
    patched_persist: dict[str, Any],
) -> None:
    from src.db.models import Chat
    from src.llm.client import LlmResult

    chat = Chat(
        id=uuid4(), telegram_chat_id=-100, status="active", created_at=datetime.now(UTC)
    )
    analysis = RiskAnalysis(
        activity_signals=[
            ActivitySignal(
                message_id=str(uuid4()),  # not in new_messages
                signal_type=ActivitySignalType.MANAGER_PROPOSAL,
                description="proposal",
            )
        ]
    )
    result = LlmResult(
        analysis=analysis, model="test", raw_response="{}",
        tokens_in=1, tokens_out=1, cost_usd=None, latency_ms=1,
    )

    await _persist(chat, [], "prompt", result, [], datetime.now(UTC))

    assert patched_persist["signals"] == []  # skipped gracefully
