"""Unit tests for LLM-call persistence (src/llm/audit.py).

No DB / no network: the connection is faked (captures INSERT params) and the
Storage uploader is monkeypatched.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import src.llm.audit as audit_mod
from src.llm.audit import record_llm_call


class FakeConn:
    """Captures the args passed to ``fetchrow`` and returns a valid row dict."""

    def __init__(self) -> None:
        self.args: tuple[Any, ...] = ()

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        self.args = args
        return {
            "id": uuid4(),
            "call_type": args[0],
            "model": args[1],
            "chat_id": args[2],
            "message_ids": args[3],
            "prompt_hash": args[4],
            "prompt_storage_path": args[5],
            "response_summary": args[6],
            "response_storage_path": args[7],
            "tokens_in": args[8],
            "tokens_out": args[9],
            "cost_usd": args[10],
            "latency_ms": args[11],
            "disagreement_flag": args[12],
            "error": args[13],
            "created_at": datetime.now(UTC),
        }


async def test_record_hashes_prompt_and_nulls_paths_on_upload_failure(
    monkeypatch,
) -> None:
    monkeypatch.setattr(audit_mod, "upload_text", AsyncMock(return_value=False))
    conn = FakeConn()

    call = await record_llm_call(
        conn,  # type: ignore[arg-type]
        call_type="tier2_batch",
        model="anthropic/claude-haiku-4-5",
        chat_id=None,
        message_ids=None,
        prompt_text="PROMPT",
        response_text="RESPONSE",
    )

    assert conn.args[4] == hashlib.sha256(b"PROMPT").hexdigest()  # prompt_hash
    assert conn.args[5] is None  # prompt_storage_path (upload failed)
    assert conn.args[7] is None  # response_storage_path
    assert conn.args[6] == "RESPONSE"  # summary falls back to response text
    assert call.call_type == "tier2_batch"


async def test_record_keeps_paths_on_upload_success(monkeypatch) -> None:
    monkeypatch.setattr(audit_mod, "upload_text", AsyncMock(return_value=True))
    conn = FakeConn()

    await record_llm_call(
        conn,  # type: ignore[arg-type]
        call_type="priority",
        model="m",
        chat_id=None,
        message_ids=None,
        prompt_text="P",
        response_text="R",
        cost_usd=None,
    )

    assert conn.args[5] is not None and conn.args[5].endswith(".prompt.txt")
    assert conn.args[7] is not None and conn.args[7].endswith(".response.json")
