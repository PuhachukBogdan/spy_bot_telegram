"""Tone-of-voice pass on demand: estimate, run, inspect. Requires migration 0025.

    python scripts/tone_backfill.py --estimate --days 14     # cost, writes nothing
    python scripts/tone_backfill.py --run --days 14          # judge finished days
    python scripts/tone_backfill.py --run --days 14 --max-calls 10   # a trial
    python scripts/tone_backfill.py --status --days 30       # rates per manager
    python scripts/tone_backfill.py --flags --days 30        # the flags, for review

``--estimate`` first: it counts the chat-days a run would send and prices them
from measured token rates, without a single model call.

``--run`` is the same code path the in-process worker uses every 15 minutes
(:func:`src.pipeline.tone.run_tone_pass`): finished LOCAL days only (never today),
one call per chat-day, a hard ceiling (``TONE_DAILY_BUDGET_USD`` unless
``--budget``), resumable — completed chat-days are skipped without a request.
Spend is recorded in ``cost_tracking`` so the live circuit breaker sees it.

``--flags`` is the calibration loop: read the quotes, decide which are wrong, and
tighten ``prompts/tone_of_voice.txt`` (bump ``PROMPT_VERSION``). Nothing here
touches ``risk_events`` or Slack.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings  # noqa: E402
from src.db.client import acquire_connection, close_pool  # noqa: E402
from src.db.queries.etc import list_real_managers  # noqa: E402
from src.db.queries.tone import (  # noqa: E402
    list_done_chat_days,
    list_tone_flags,
    list_tone_targets,
    tone_summary,
)
from src.metrics.tone import METRIC_BY_KEY, TonePolarity, local_day_bounds  # noqa: E402
from src.pipeline.tone import pending_days, run_tone_pass  # noqa: E402
from src.pipeline.workers import report_timezone  # noqa: E402

#: First-party list prices per million tokens, for the pre-flight estimate only.
#: Actual spend comes from OpenRouter's usage block, which is what the ceiling
#: enforces — these numbers never gate anything.
_PRICES: dict[str, tuple[float, float]] = {
    "anthropic/claude-haiku-4-5": (1.00, 5.00),
    "anthropic/claude-sonnet-4-6": (3.00, 15.00),
}
#: Measured on the archive retro pass: rendered XML per message, Cyrillic-heavy.
_TOKENS_PER_MESSAGE = 113.3
#: The system prompt (English, ~4.7k chars) at ~4 chars/token.
_PROMPT_TOKENS = 1200
_OUTPUT_TOKENS_PER_CALL = 80


def _days(n: int) -> list[date]:
    tz = report_timezone()
    today = datetime.now(UTC).astimezone(tz).date()
    return pending_days(today, backfill_days=n, epoch=settings.METRICS_EPOCH_DATE)


async def _estimate(days: list[date], model: str) -> None:
    tz = report_timezone()
    tz_name = getattr(tz, "key", None) or str(tz)
    async with acquire_connection() as conn:
        managers = await list_real_managers(conn)
        targets = await list_tone_targets(
            conn,
            local_day_bounds(days[0], tz)[0],
            local_day_bounds(days[-1], tz)[1],
            tz_name,
            [tid for m in managers for tid in m.telegram_accounts],
        )
        done = await list_done_chat_days(conn, days[0], days[-1])
    pending = [t for t in targets if (t["chat_id"], t["day"]) not in done]
    messages = sum(int(t["messages"]) for t in pending)
    manager_messages = sum(int(t["manager_messages"]) for t in pending)
    calls = sum(max(1, -(-int(t["messages"]) // settings.TONE_WINDOW_MESSAGES)) for t in pending)
    tokens_in = (
        calls * _PROMPT_TOKENS
        + (messages + calls * settings.TONE_CONTEXT_MESSAGES) * _TOKENS_PER_MESSAGE
    )
    tokens_out = calls * _OUTPUT_TOKENS_PER_CALL

    print(f"days              : {days[0]} .. {days[-1]} ({len(days)} finished days)")
    print(f"managers          : {len(managers)} real ({', '.join(m.full_name for m in managers)})")
    print(f"chat-days         : {len(pending)} pending, {len(targets) - len(pending)} already done")
    print(
        f"messages rendered : {messages:,} (of which {manager_messages:,} manager messages judged)"
    )
    print(f"model calls       : {calls}")
    print(f"ceiling per day   : ${settings.TONE_DAILY_BUDGET_USD} (TONE_DAILY_BUDGET_USD)")
    print(f"\n{'model':<32}{'in (k)':>10}{'out (k)':>9}{'est. cost':>12}")
    for name, (p_in, p_out) in _PRICES.items():
        cost = tokens_in / 1e6 * p_in + tokens_out / 1e6 * p_out
        marker = "  <- configured" if name == model else ""
        print(
            f"{name:<32}{tokens_in / 1000:>10.0f}{tokens_out / 1000:>9.0f}"
            f"{'$' + format(cost, '.2f'):>12}{marker}"
        )
    print(
        "\nEstimate only. The retro pass came in 1.8× over its estimate; the run bills "
        "OpenRouter's reported usage and stops at the ceiling."
    )


async def _run(days: list[date], model: str, budget: Decimal, max_calls: int | None) -> None:
    stats = await run_tone_pass(
        acquire_connection,
        days=days,
        model=model,
        budget_usd=budget,
        tz=report_timezone(),
        max_calls=max_calls,
    )
    print(f"days            : {stats.days[0]} .. {stats.days[-1]}")
    print(
        f"chat-days done  : {stats.chat_days_done}  "
        f"(skipped {stats.chat_days_skipped} already done)"
    )
    print(f"model calls     : {stats.calls}")
    print(f"messages judged : {stats.assessed}")
    print(f"flags kept      : {stats.flags}")
    print(
        f"dropped         : {stats.gate.dropped_low_confidence} low-confidence, "
        f"{stats.gate.dropped_quote_missing} quote not found, "
        f"{stats.gate.dropped_not_assessable} not assessable, "
        f"{stats.gate.dropped_duplicate} duplicate"
    )
    print(f"tokens          : {stats.tokens_in:,} in / {stats.tokens_out:,} out")
    print(f"spend           : ${stats.cost_usd:.4f}")
    if stats.budget_exhausted:
        print(
            f"\nSTOPPED AT BUDGET (${budget}). "
            "Re-run later or pass --budget to raise it for this run."
        )
    if stats.stopped_at_max_calls:
        print("\nStopped at --max-calls. Re-run to continue; finished chat-days are skipped.")
    if stats.errors:
        print(f"\nerrors ({len(stats.errors)}):")
        for error in stats.errors[:20]:
            print(f"  {error}")


async def _status(days: list[date]) -> None:
    async with acquire_connection() as conn:
        rows = await tone_summary(conn, days[0], days[-1])
    if not rows:
        print("no tone counters in this range — run --run first")
        return
    print(f"{days[0]} .. {days[-1]}\n")
    print(f"{'manager':<26}{'metric':<16}{'judged':>8}{'flagged':>9}{'value':>10}")
    for row in rows:
        d = METRIC_BY_KEY.get(str(row["metric"]))
        assessed = int(row["assessed"] or 0)
        flagged = int(row["flagged"] or 0)
        if d is None or assessed == 0:
            value = "—"
        else:
            rate = 100 * flagged / assessed
            if d.polarity is TonePolarity.NEGATIVE:
                value = f"{rate:.1f}%"
            elif d.polarity is TonePolarity.POSITIVE_GAP:
                value = f"{100 - rate:.1f}%"
            else:
                value = f"{flagged} ({rate:.1f}/100)"
            if assessed < settings.TONE_MIN_ASSESSED:
                value += " *"
        print(
            f"{str(row['full_name'])[:25]:<26}{str(row['metric']):<16}{assessed:>8}{flagged:>9}{value:>10}"
        )
    print(
        f"\n* fewer than TONE_MIN_ASSESSED={settings.TONE_MIN_ASSESSED} messages "
        "— the page hides RATES below this (counts always show)"
    )


async def _flags(days: list[date], limit: int) -> None:
    async with acquire_connection() as conn:
        rows = await list_tone_flags(conn, days[0], days[-1], limit=limit)
    if not rows:
        print("no flags in this range")
        return
    tz = report_timezone()
    for row in rows:
        at = row["occurred_at"].astimezone(tz).strftime("%Y-%m-%d %H:%M")
        print(f"[{row['metric']}] {at} · {row['chat_name']} · conf {float(row['confidence']):.2f}")
        print(f"  “{row['quote']}”")
        print(f"  {row['reason']}\n")
    print(f"{len(rows)} flags")


async def _main(args: argparse.Namespace) -> None:
    days = _days(args.days)
    if not days:
        print("no finished days in range (METRICS_EPOCH_DATE may floor the range)")
        return
    try:
        if args.estimate:
            await _estimate(days, args.model)
        elif args.run:
            budget = (
                Decimal(str(args.budget))
                if args.budget is not None
                else settings.TONE_DAILY_BUDGET_USD
            )
            await _run(days, args.model, budget, args.max_calls)
        elif args.status:
            await _status(days)
        elif args.flags:
            await _flags(days, args.limit)
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--estimate", action="store_true")
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--flags", action="store_true")
    parser.add_argument(
        "--days", type=int, default=14, help="finished local days to cover (default 14)"
    )
    parser.add_argument("--model", default=settings.LLM_MODEL_TONE)
    parser.add_argument("--budget", type=float, help="override TONE_DAILY_BUDGET_USD for this run")
    parser.add_argument("--max-calls", type=int, help="stop after this many model calls (trial)")
    parser.add_argument("--limit", type=int, default=200, help="--flags: max rows")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
