"""The one-off retrospective risk pass over imported archive history.

Deliberately a separate path from the live pipeline, in three ways that matter:

* **Its own prompt** (``prompts/archive_retro_analysis.txt``), whose gate is an
  explicit concealment/bypass marker rather than a topical judgement. The live
  prompt's precision — 14 confirmed against 24 human-rejected findings — is the
  reason this pass does not reuse it.
* **Its own table** (``archive_retro_findings``), so nothing here can reach the
  weekly report, the Slack dashboard, or the alert dispatcher.
* **Its own budget**, checked against OpenRouter's *reported* spend per call rather
  than an estimate, and enforced before each window so the run stops at the ceiling
  instead of discovering it afterwards.

Resumable: completed windows are recorded in ``archive_retro_progress``, so an
interrupted pass restarts where it stopped rather than re-paying for finished work.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import asyncpg
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionToolChoiceOptionParam,
    ChatCompletionToolParam,
)

from src.db.models import Message
from src.importer.retro_schema import (
    ARCHIVE_FINDINGS_TOOL_NAME,
    MIN_CONFIDENCE,
    ArchiveAnalysis,
    ArchiveFinding,
    build_archive_findings_tool,
)
from src.llm.client import get_client
from src.llm.prompts import build_conversation_block
from src.utils.logging import get_logger
from src.utils.retry import with_llm_retry

log = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_PROMPT_PATH = _PROMPTS_DIR / "archive_retro_analysis.txt"
_RE_VERSION = re.compile(r"^PROMPT_VERSION:\s*(\S+)", re.MULTILINE)

#: Messages per analysis window. Large enough that an episode spanning a few
#: messages stays intact, small enough that one window's context stays readable.
WINDOW_SIZE = 120

#: Messages of lead-in prepended to each window so an episode straddling a window
#: boundary is still judged in context. These are NOT re-analysed — only the
#: window's own messages can anchor a finding.
WINDOW_OVERLAP = 15


@dataclass
class RetroStats:
    """Accounting for one run."""

    run_id: UUID
    windows_done: int = 0
    windows_skipped: int = 0
    findings: int = 0
    dropped_low_confidence: int = 0
    dropped_unanchored: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)
    budget_exhausted: bool = False


def load_prompt() -> tuple[str, str]:
    """Return ``(prompt_text, version)`` for the retro system prompt."""
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    match = _RE_VERSION.search(text)
    return text, match.group(1) if match else "unversioned"


def _parse_analysis(response: ChatCompletion) -> ArchiveAnalysis:
    """Validate the forced tool payload. No tool call = nothing found."""
    calls = response.choices[0].message.tool_calls
    if not calls:
        return ArchiveAnalysis()
    first = calls[0]
    if first.type != "function":  # narrow off the custom-tool union member
        return ArchiveAnalysis()
    return ArchiveAnalysis.model_validate_json(first.function.arguments)


def _parse_usage(response: ChatCompletion) -> tuple[int, int, float]:
    """``(tokens_in, tokens_out, cost_usd)`` from OpenRouter's usage block."""
    usage = response.usage
    if usage is None:
        return 0, 0, 0.0
    cost = 0.0
    raw = getattr(usage, "cost", None)
    if isinstance(raw, (int, float)):
        cost = float(raw)
    return usage.prompt_tokens, usage.completion_tokens, cost


async def analyze_window(
    *, model: str, system_prompt: str, conversation_block: str
) -> tuple[ArchiveAnalysis, int, int, float]:
    """One forced-tool call over a window. Returns analysis + usage."""
    client = get_client()
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": conversation_block},
    ]
    tools = cast("list[ChatCompletionToolParam]", [build_archive_findings_tool()])
    tool_choice = cast(
        "ChatCompletionToolChoiceOptionParam",
        {"type": "function", "function": {"name": ARCHIVE_FINDINGS_TOOL_NAME}},
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

    response = await _call()
    tokens_in, tokens_out, cost = _parse_usage(response)
    return _parse_analysis(response), tokens_in, tokens_out, cost


def _accept(
    finding: ArchiveFinding, window_ids: set[str], stats: RetroStats
) -> bool:
    """Client-side enforcement of the two rules the prompt states.

    The prompt asking for a quote and confidence ≥ 0.7 is not the same as getting
    them: a model under pressure to find something will hedge. Both are re-checked
    here, and an anchor outside the window is rejected because it would attach a
    finding to a message the model only saw as lead-in context.
    """
    if finding.confidence < MIN_CONFIDENCE:
        stats.dropped_low_confidence += 1
        return False
    if not finding.quote.strip() or not finding.marker.strip():
        stats.dropped_unanchored += 1
        return False
    if finding.message_id not in window_ids:
        stats.dropped_unanchored += 1
        return False
    return True


async def _load_imported_messages(
    conn: asyncpg.Connection, chat_id: UUID
) -> list[Message]:
    """Every imported message for a chat, oldest first."""
    rows = await conn.fetch(
        """
        SELECT * FROM messages
        WHERE chat_id = $1 AND source = 'imported' AND deleted_at IS NULL
        ORDER BY timestamp ASC, telegram_message_id ASC
        """,
        chat_id,
    )
    return [Message.from_record(dict(row)) for row in rows]


async def _completed_windows(
    conn: asyncpg.Connection, run_id: UUID, chat_id: UUID
) -> set[int]:
    rows = await conn.fetch(
        "SELECT window_index FROM archive_retro_progress WHERE run_id = $1 AND chat_id = $2",
        run_id,
        chat_id,
    )
    return {row["window_index"] for row in rows}


async def _persist(
    conn: asyncpg.Connection,
    *,
    run_id: UUID,
    chat_id: UUID,
    aff_id: str | None,
    window_index: int,
    findings: list[ArchiveFinding],
    by_id: dict[str, Message],
    analysed: int,
    model: str,
    prompt_version: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
) -> None:
    """Write a window's findings and mark it done, in one transaction."""
    async with conn.transaction():
        for finding in findings:
            message = by_id[finding.message_id]
            await conn.execute(
                """
                INSERT INTO archive_retro_findings (
                    message_id, chat_id, aff_id, risk_type, score, confidence,
                    quote, marker, explanation, context_message_ids,
                    sender_name, sender_role, occurred_at,
                    run_id, model, prompt_version
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                ON CONFLICT (run_id, message_id, risk_type) DO NOTHING
                """,
                message.id,
                chat_id,
                aff_id,
                str(finding.risk_type),
                finding.score,
                finding.confidence,
                finding.quote,
                finding.marker,
                finding.explanation,
                [UUID(i) for i in finding.context_message_ids if i in by_id],
                message.sender_name,
                message.sender_role,
                message.timestamp,
                run_id,
                model,
                prompt_version,
            )
        await conn.execute(
            """
            INSERT INTO archive_retro_progress (
                run_id, chat_id, window_index, messages, input_tokens, output_tokens,
                cost_usd, findings
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (run_id, chat_id, window_index) DO NOTHING
            """,
            run_id,
            chat_id,
            window_index,
            analysed,
            tokens_in,
            tokens_out,
            cost_usd,
            len(findings),
        )


@dataclass(frozen=True)
class RetroTarget:
    """One chat to analyse."""

    chat_id: UUID
    chat_name: str | None
    aff_id: str | None
    message_count: int


async def load_targets(conn: asyncpg.Connection) -> list[RetroTarget]:
    """Chats holding imported history, busiest first.

    Excludes ``status = 'merged'`` placeholders. When the bot joins an archived
    group, ``attach_archived_history`` moves that history to the real chat and
    retires the placeholder as ``merged`` — but leaves behind any message the real
    chat already held, because deleting those could trip an FK from a finding or an
    edit. Analysing a merged placeholder would therefore re-read content that now
    lives in the real chat: paid for twice, and reported twice.

    Tolerates the pre-0023 schema so ``--estimate`` can run before the migration —
    the estimate exists to inform the decision to migrate and import, so requiring
    the migration first would defeat it.
    """
    from src.importer.load import has_import_columns

    aff = "c.import_aff_id" if await has_import_columns(conn) else "NULL::text"
    rows = await conn.fetch(
        f"""
        SELECT c.id, c.chat_name, {aff} AS import_aff_id,
               count(m.id) AS n
        FROM chats c
        JOIN messages m ON m.chat_id = c.id AND m.source = 'imported'
        WHERE c.status <> 'merged'
        GROUP BY c.id, c.chat_name, {aff}
        HAVING count(m.id) > 0
        ORDER BY n DESC
        """  # noqa: S608 - interpolation is a fixed column name, not user input
    )
    return [
        RetroTarget(
            chat_id=row["id"],
            chat_name=row["chat_name"],
            aff_id=row["import_aff_id"],
            message_count=row["n"],
        )
        for row in rows
    ]


def plan_windows(messages: list[Message]) -> list[tuple[int, list[Message], set[str]]]:
    """Split into ``(index, messages_including_overlap, anchorable_ids)`` windows."""
    windows: list[tuple[int, list[Message], set[str]]] = []
    for index, start in enumerate(range(0, len(messages), WINDOW_SIZE)):
        own = messages[start : start + WINDOW_SIZE]
        if not own:
            continue
        lead_in = messages[max(0, start - WINDOW_OVERLAP) : start]
        windows.append((index, lead_in + own, {str(m.id) for m in own}))
    return windows


async def run_retro(
    pool_acquire: Any,
    *,
    model: str,
    run_id: UUID | None = None,
    budget_usd: float,
    only_chat: UUID | None = None,
    max_windows: int | None = None,
) -> RetroStats:
    """Analyse every imported chat under a hard spend ceiling.

    *budget_usd* is checked against reported spend before each call, so the run
    stops at the ceiling rather than crossing it. Pass an existing *run_id* to
    resume; completed windows are skipped without a request.
    """
    system_prompt, prompt_version = load_prompt()
    stats = RetroStats(run_id=run_id or uuid4())

    async with pool_acquire() as conn:
        targets = await load_targets(conn)
    if only_chat is not None:
        targets = [t for t in targets if t.chat_id == only_chat]

    log.info(
        "retro.start",
        run_id=str(stats.run_id),
        model=model,
        chats=len(targets),
        budget_usd=budget_usd,
    )

    for target in targets:
        async with pool_acquire() as conn:
            messages = await _load_imported_messages(conn, target.chat_id)
            done = await _completed_windows(conn, stats.run_id, target.chat_id)

        for index, window, anchorable in plan_windows(messages):
            if index in done:
                stats.windows_skipped += 1
                continue
            if max_windows is not None and stats.windows_done >= max_windows:
                return stats
            if stats.cost_usd >= budget_usd:
                stats.budget_exhausted = True
                log.warning(
                    "retro.budget_exhausted",
                    run_id=str(stats.run_id),
                    spent_usd=round(stats.cost_usd, 4),
                    budget_usd=budget_usd,
                )
                return stats

            block = build_conversation_block(window, flagged_ids=())
            try:
                analysis, tokens_in, tokens_out, cost = await analyze_window(
                    model=model, system_prompt=system_prompt, conversation_block=block
                )
            except Exception as exc:  # noqa: BLE001 - one bad window must not end the run
                stats.errors.append(f"{target.aff_id or target.chat_id} w{index}: {exc}")
                log.warning(
                    "retro.window_failed",
                    chat_id=str(target.chat_id),
                    window=index,
                    error=str(exc),
                )
                continue

            stats.tokens_in += tokens_in
            stats.tokens_out += tokens_out
            stats.cost_usd += cost
            stats.windows_done += 1

            by_id = {str(m.id): m for m in window}
            accepted = [f for f in analysis.findings if _accept(f, anchorable, stats)]
            stats.findings += len(accepted)

            async with pool_acquire() as conn:
                await _persist(
                    conn,
                    run_id=stats.run_id,
                    chat_id=target.chat_id,
                    aff_id=target.aff_id,
                    window_index=index,
                    findings=accepted,
                    by_id=by_id,
                    analysed=len(anchorable),
                    model=model,
                    prompt_version=prompt_version,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost,
                )

            if stats.windows_done % 25 == 0:
                log.info(
                    "retro.progress",
                    windows=stats.windows_done,
                    findings=stats.findings,
                    spent_usd=round(stats.cost_usd, 4),
                )

    log.info(
        "retro.done",
        run_id=str(stats.run_id),
        windows=stats.windows_done,
        findings=stats.findings,
        spent_usd=round(stats.cost_usd, 4),
    )
    return stats


#: Measured over the whole archive: 3 823 162 characters across 57 372 messages.
#: Note this is *characters*, not UTF-8 bytes — the same corpus is 6 118 966 bytes
#: (1.60 bytes/char for this Cyrillic-Latin mix), and using the byte figure here
#: would overstate the token count by 60%.
AVG_CHARS_PER_MESSAGE = 67

#: Characters per token for Cyrillic-dominant text. English runs nearer 4, so an
#: English-calibrated estimate would understate this archive roughly twofold.
CHARS_PER_TOKEN = 2.0

#: **Measured** input tokens per message, end to end, over the completed full run:
#: 6 381 659 tokens across 56 329 messages, 589 windows.
#:
#: Far above the ~34 that the message *text* alone accounts for, because the
#: dominant cost is not the text — it is the per-message envelope. Every message is
#: rendered as ``<message id="<uuid>" sender_role="…" flagged="false">…</message>``,
#: and the UUID alone outweighs a typical 67-character message. Add the system
#: prompt re-sent per window and the lead-in overlap re-read, and a bottom-up
#: estimate from character counts lands ~1.6× low. Calibrating on the measurement
#: instead is what makes the projected spend trustworthy.
MEASURED_INPUT_TOKENS_PER_MESSAGE = 113.3

#: Output tokens per window, measured on the same run (44 257 over 589 windows).
#: Small because most windows correctly return an empty findings list.
OUTPUT_TOKENS_PER_WINDOW = 75.0


def count_windows(messages_per_chat: Sequence[int]) -> int:
    """Total windows the run will send, given each chat's message count.

    Windows never span chats, so this is the sum of per-chat ceilings — not
    ``total / WINDOW_SIZE``. That shortcut assumes perfect packing and undercounts
    badly on a long tail of small chats: 56 329 messages across 238 chats packs to
    470 in theory but actually cost 589 windows, because a 5-message chat still
    consumes one. Each partial window still re-sends the whole system prompt, so the
    difference is real money.
    """
    return sum(max(1, -(-n // WINDOW_SIZE)) for n in messages_per_chat if n > 0)


def estimate_cost(
    messages_per_chat: Sequence[int],
    *,
    input_per_mtok: float,
    output_per_mtok: float,
    tokens_per_message: float = MEASURED_INPUT_TOKENS_PER_MESSAGE,
) -> dict[str, float]:
    """Pre-flight cost estimate, before spending anything.

    Takes the per-chat message counts rather than a total, so the window count
    reflects chat boundaries (see :func:`count_windows`). Calibrated on a real trial
    rather than derived from character counts — see
    :data:`MEASURED_INPUT_TOKENS_PER_MESSAGE`. Still only an estimate: the run bills
    OpenRouter's reported usage, which is also what the budget gate enforces. Its job
    is to make the spend a decision rather than a discovery.
    """
    total_messages = sum(messages_per_chat)
    windows = max(1, count_windows(messages_per_chat))
    tokens_in = total_messages * tokens_per_message
    tokens_out = windows * OUTPUT_TOKENS_PER_WINDOW
    return {
        "windows": float(windows),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": tokens_in / 1e6 * input_per_mtok + tokens_out / 1e6 * output_per_mtok,
    }
