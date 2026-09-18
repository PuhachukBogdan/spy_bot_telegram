"""The daily tone-of-voice pass: one model call per chat-day, flags in, counters out.

Runs once a day (after local midnight, for the day just ended) from
:func:`src.pipeline.workers.tone_worker_loop`, and on demand from
``scripts/tone_backfill.py`` for calibration. Both go through :func:`run_tone_pass`.

Three properties, all deliberate:

* **Separate from Tier-2.** The Phase 2 spec first proposed folding tone into the
  risk prompt (§4.4 option A). A separate daily pass was chosen instead: the risk
  prompt is untouched (no detection regression to check for), the model sees the
  whole day rather than a 60-message window, and the cost is a known number per
  day instead of a tax on every analysis call.
* **Flags only.** The model reports clear cases; everything it does not mention
  is fine. Every returned flag is re-checked by :func:`src.metrics.tone.accept_flags`
  (confidence floor, assessable id, verbatim quote) before it counts.
* **Bounded and resumable.** ``TONE_DAILY_BUDGET_USD`` is checked against
  OpenRouter's *reported* spend before every call, and each finished chat-day is
  recorded, so an interrupted pass restarts where it stopped without paying twice.
  Spend is also written to ``cost_tracking`` so the global circuit breaker sees it.

Nothing here touches ``risk_events`` or Slack.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, tzinfo
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import asyncpg
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionToolChoiceOptionParam,
    ChatCompletionToolParam,
)

from src.config import settings
from src.db.models import Message
from src.db.queries.cost import record_llm_cost
from src.db.queries.etc import list_real_managers
from src.db.queries.tone import (
    ToneFlagRow,
    list_done_chat_days,
    list_tone_targets,
    load_chat_day_messages,
    persist_chat_day,
    tone_spend_since,
)
from src.llm.audit import record_llm_call
from src.llm.client import get_client
from src.llm.prompts import load_template
from src.llm.tone_schema import (
    TONE_TOOL_NAME,
    ToneAssessment,
    ToneFlag,
    build_tone_tool,
)
from src.metrics.attribution import build_manager_index
from src.metrics.tone import (
    TONE_METRICS,
    ToneGateStats,
    accept_flags,
    assessable_ids,
    local_day_bounds,
    message_text,
    plan_windows,
    render_tone_block,
)
from src.utils.logging import get_logger
from src.utils.retry import with_llm_retry

log = get_logger(__name__)

PROMPT_NAME = "tone_of_voice"
_RE_VERSION = re.compile(r"^PROMPT_VERSION:\s*(\S+)", re.MULTILINE)
_CALL_TYPE = "tone"


@dataclass
class ToneRunStats:
    """Accounting for one pass (one worker tick or one CLI run)."""

    days: list[date]
    chat_days_done: int = 0
    chat_days_skipped: int = 0
    calls: int = 0
    assessed: int = 0
    gate: ToneGateStats = field(default_factory=ToneGateStats)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal("0")
    errors: list[str] = field(default_factory=list)
    budget_exhausted: bool = False
    stopped_at_max_calls: bool = False

    @property
    def flags(self) -> int:
        return self.gate.accepted


async def load_tone_prompt(conn: asyncpg.Connection) -> tuple[str, str]:
    """``(prompt_text, version)`` — DB override if one is active, else the file."""
    text = await load_template(conn, PROMPT_NAME)
    match = _RE_VERSION.search(text)
    return text, match.group(1) if match else "unversioned"


def _parse_assessment(response: ChatCompletion) -> tuple[ToneAssessment, str]:
    """Validate the forced tool payload. No tool call = nothing flagged."""
    calls = response.choices[0].message.tool_calls
    if not calls:
        return ToneAssessment(), "{}"
    first = calls[0]
    if first.type != "function":
        return ToneAssessment(), "{}"
    raw = first.function.arguments
    return ToneAssessment.model_validate_json(raw), raw


def _parse_usage(response: ChatCompletion) -> tuple[int, int, Decimal]:
    usage = response.usage
    if usage is None:
        return 0, 0, Decimal("0")
    raw = usage.model_dump()
    cost_raw = raw.get("cost")
    cost = Decimal(str(cost_raw)) if cost_raw is not None else Decimal("0")
    return int(raw.get("prompt_tokens") or 0), int(raw.get("completion_tokens") or 0), cost


async def assess_tone(
    *, model: str, system_prompt: str, conversation_block: str
) -> tuple[ToneAssessment, int, int, Decimal, str]:
    """One forced-tool call over a window. Returns assessment + usage + raw JSON."""
    client = get_client()
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": conversation_block},
    ]
    tools = cast("list[ChatCompletionToolParam]", [build_tone_tool()])
    tool_choice = cast(
        "ChatCompletionToolChoiceOptionParam",
        {"type": "function", "function": {"name": TONE_TOOL_NAME}},
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
    assessment, raw = _parse_assessment(response)
    return assessment, tokens_in, tokens_out, cost, raw


async def _audit_call(
    pool_acquire: Any,
    *,
    model: str,
    chat_id: UUID,
    message_ids: list[UUID],
    prompt_text: str,
    raw: str,
    tokens_in: int,
    tokens_out: int,
    cost: Decimal,
    flags: int,
) -> None:
    """Best-effort: ``llm_calls`` row + today's ``cost_tracking``. Never raises."""
    try:
        async with pool_acquire() as conn:
            await record_llm_call(
                conn,
                call_type=_CALL_TYPE,
                model=model,
                chat_id=chat_id,
                message_ids=message_ids,
                prompt_text=prompt_text,
                response_text=raw,
                response_summary=json.dumps({"flags": flags}),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
            )
            await record_llm_cost(conn, cost)
    except Exception as exc:  # noqa: BLE001 - audit must not stop the pass
        log.warning("tone.audit_failed", chat_id=str(chat_id), error=str(exc))


def _to_rows(
    flags: Sequence[ToneFlag],
    *,
    by_id: dict[str, Message],
    owners: dict[str, UUID],
    chat_id: UUID,
    day: date,
) -> list[ToneFlagRow]:
    rows: list[ToneFlagRow] = []
    for f in flags:
        m = by_id[f.message_id]
        rows.append(
            ToneFlagRow(
                manager_id=owners[f.message_id],
                chat_id=chat_id,
                message_id=m.id,
                metric=f.metric.value,
                confidence=f.confidence,
                quote=f.quote,
                reason=f.reason,
                occurred_at=m.timestamp,
                day=day,
                sender_name=m.sender_name,
            )
        )
    return rows


async def run_tone_pass(
    pool_acquire: Any,
    *,
    days: Sequence[date],
    model: str,
    budget_usd: Decimal,
    tz: tzinfo,
    max_calls: int | None = None,
    min_confidence: float | None = None,
) -> ToneRunStats:
    """Assess every not-yet-processed chat-day in ``days`` under a spend ceiling.

    ``budget_usd`` is compared against the pass's reported spend since 00:00 UTC
    today (``manager_tone_progress.cost_usd``) plus this run's own, before each
    call — the run stops at the ceiling rather than crossing it. Completed
    chat-days are skipped without a request, so calling this every 15 minutes is
    free once the day is done.
    """
    stats = ToneRunStats(days=sorted(days))
    if not days:
        return stats
    floor = min_confidence if min_confidence is not None else settings.TONE_MIN_CONFIDENCE
    tz_name = getattr(tz, "key", None) or str(tz)

    async with pool_acquire() as conn:
        system_prompt, prompt_version = await load_tone_prompt(conn)
        managers = await list_real_managers(conn)
        since_utc = local_day_bounds(stats.days[0], tz)[0]
        until_utc = local_day_bounds(stats.days[-1], tz)[1]
        targets = await list_tone_targets(
            conn,
            since_utc,
            until_utc,
            tz_name,
            [tid for m in managers for tid in m.telegram_accounts],
        )
        done = await list_done_chat_days(conn, stats.days[0], stats.days[-1])
        spent_today = await tone_spend_since(
            conn, datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        )

    manager_index = build_manager_index(managers)
    wanted = set(stats.days)
    pending = [t for t in targets if t["day"] in wanted and (t["chat_id"], t["day"]) not in done]
    stats.chat_days_skipped = sum(1 for t in targets if (t["chat_id"], t["day"]) in done)

    log.info(
        "tone.start",
        days=[d.isoformat() for d in stats.days],
        chat_days=len(pending),
        skipped=stats.chat_days_skipped,
        model=model,
        prompt_version=prompt_version,
        spent_today_usd=str(spent_today),
        budget_usd=str(budget_usd),
    )
    metric_keys = [m.key.value for m in TONE_METRICS]

    for target in pending:
        chat_id: UUID = target["chat_id"]
        day: date = target["day"]
        if max_calls is not None and stats.calls >= max_calls:
            stats.stopped_at_max_calls = True
            break
        if spent_today + stats.cost_usd >= budget_usd:
            stats.budget_exhausted = True
            log.warning(
                "tone.budget_exhausted",
                spent_usd=str(spent_today + stats.cost_usd),
                budget_usd=str(budget_usd),
            )
            break

        start, end = local_day_bounds(day, tz)
        async with pool_acquire() as conn:
            context, day_messages = await load_chat_day_messages(
                conn, chat_id, start, end, context_limit=settings.TONE_CONTEXT_MESSAGES
            )
        owners = assessable_ids(day_messages, manager_index, start=start)
        if not owners:
            # The target query said a manager wrote here; if nothing judgeable
            # survives (e.g. text stripped to empty) record the day as done so it
            # is not re-fetched every tick.
            async with pool_acquire() as conn:
                await persist_chat_day(
                    conn,
                    chat_id=chat_id,
                    day=day,
                    messages=len(day_messages),
                    assessed_by_manager={},
                    flagged={},
                    metric_keys=metric_keys,
                    flags=[],
                    model=model,
                    prompt_version=prompt_version,
                    tokens_in=0,
                    tokens_out=0,
                    cost_usd=Decimal("0"),
                )
            stats.chat_days_done += 1
            continue

        by_id = {str(m.id): m for m in day_messages}
        gate = ToneGateStats()
        accepted: list[ToneFlag] = []
        tokens_in = tokens_out = 0
        cost = Decimal("0")
        failed = False
        for window, ids in plan_windows(
            day_messages,
            context,
            owners,
            window_size=settings.TONE_WINDOW_MESSAGES,
            overlap=settings.TONE_WINDOW_OVERLAP,
        ):
            if not ids:
                continue
            if max_calls is not None and stats.calls >= max_calls:
                stats.stopped_at_max_calls = True
                failed = True
                break
            if spent_today + stats.cost_usd + cost >= budget_usd:
                stats.budget_exhausted = True
                failed = True
                break
            block = render_tone_block(window, ids, tz)
            try:
                assessment, w_in, w_out, w_cost, raw = await assess_tone(
                    model=model, system_prompt=system_prompt, conversation_block=block
                )
            except Exception as exc:  # noqa: BLE001 - one bad window must not end the pass
                stats.errors.append(f"{chat_id} {day}: {exc}")
                log.warning(
                    "tone.window_failed", chat_id=str(chat_id), day=day.isoformat(), error=str(exc)
                )
                failed = True
                break
            stats.calls += 1
            tokens_in += w_in
            tokens_out += w_out
            cost += w_cost
            texts = {mid: message_text(by_id[mid]) for mid in ids}
            kept = accept_flags(assessment.flags, texts, min_confidence=floor, stats=gate)
            accepted.extend(kept)
            await _audit_call(
                pool_acquire,
                model=model,
                chat_id=chat_id,
                message_ids=[UUID(mid) for mid in sorted(ids)],
                prompt_text=system_prompt + "\n\n" + block,
                raw=raw,
                tokens_in=w_in,
                tokens_out=w_out,
                cost=w_cost,
                flags=len(kept),
            )

        stats.tokens_in += tokens_in
        stats.tokens_out += tokens_out
        stats.cost_usd += cost
        if failed:
            # A partially judged day is not recorded: the next tick redoes it whole,
            # so the counters never carry a half-day's denominator.
            if stats.budget_exhausted or stats.stopped_at_max_calls:
                break
            continue

        assessed_by_manager: dict[UUID, int] = {}
        for manager_id in owners.values():
            assessed_by_manager[manager_id] = assessed_by_manager.get(manager_id, 0) + 1
        flagged: dict[tuple[UUID, str], int] = {}
        for f in accepted:
            key = (owners[f.message_id], f.metric.value)
            flagged[key] = flagged.get(key, 0) + 1

        async with pool_acquire() as conn:
            await persist_chat_day(
                conn,
                chat_id=chat_id,
                day=day,
                messages=len(day_messages),
                assessed_by_manager=assessed_by_manager,
                flagged=flagged,
                metric_keys=metric_keys,
                flags=_to_rows(accepted, by_id=by_id, owners=owners, chat_id=chat_id, day=day),
                model=model,
                prompt_version=prompt_version,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
            )
        stats.chat_days_done += 1
        stats.assessed += len(owners)
        stats.gate.add(gate)

    log.info(
        "tone.done",
        chat_days=stats.chat_days_done,
        calls=stats.calls,
        assessed=stats.assessed,
        flags=stats.flags,
        dropped_low_confidence=stats.gate.dropped_low_confidence,
        dropped_quote_missing=stats.gate.dropped_quote_missing,
        dropped_not_assessable=stats.gate.dropped_not_assessable,
        spent_usd=str(stats.cost_usd),
        budget_exhausted=stats.budget_exhausted,
        errors=len(stats.errors),
    )
    return stats


def pending_days(today_local: date, *, backfill_days: int, epoch: date | None) -> list[date]:
    """The local days a tick may process: the last ``backfill_days`` before today.

    Today is never included — the day must have ended to be judged whole. The
    metrics epoch floors the range like every other Phase 2 window.
    """
    if backfill_days <= 0:
        return []
    first = today_local - date.resolution * backfill_days
    if epoch is not None and epoch > first:
        first = epoch
    out: list[date] = []
    d = first
    while d < today_local:
        out.append(d)
        d += date.resolution
    return out
