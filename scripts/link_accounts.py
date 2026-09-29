"""Attach Telegram accounts to existing people — dry run unless --apply.

    python scripts/link_accounts.py --pair <PRIMARY>:<TELEGRAM_ID> [--pair ...] [--apply]

PRIMARY is a Telegram id of any account the person already has, or their
internal_users uuid. Each pair runs in its own transaction; without --apply the
transaction is rolled back after the report, so the "after" numbers are what the
write would really produce, not a prediction (spec 001, research R6).

Exit code: 0 all linked / noop · 1 something refused · 2 bad arguments or DB error.
Real ids never live in this file — they come in as arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.client import acquire_connection, close_pool  # noqa: E402
from src.db.models import InternalUser  # noqa: E402
from src.db.queries.account_links import (  # noqa: E402
    LinkPlan,
    PersonSnapshot,
    apply_account_link,
    person_snapshot,
    plan_account_link,
)
from src.db.queries.etc import (  # noqa: E402
    get_internal_user_by_id,
    get_internal_user_by_telegram_id_any,
)


class _DryRun(Exception):
    """Raised inside the transaction to roll a dry run back."""


def parse_pair(raw: str) -> tuple[str, int]:
    primary, sep, tg = raw.partition(":")
    if not sep or not primary.strip() or not tg.strip().isdigit():
        raise argparse.ArgumentTypeError(f"expected PRIMARY:TELEGRAM_ID, got {raw!r}")
    return primary.strip(), int(tg.strip())


async def resolve_primary(conn: asyncpg.Connection, primary: str) -> InternalUser | None:
    if primary.isdigit():
        return await get_internal_user_by_telegram_id_any(conn, int(primary))
    try:
        return await get_internal_user_by_id(conn, UUID(primary))
    except ValueError:
        return None


def _fmt_pair(values: tuple[int, int]) -> str:
    return f"{values[0]} / {values[1]}"


def render(
    plan: LinkPlan,
    before: PersonSnapshot | None,
    after: PersonSnapshot | None,
    moved: dict[str, int] | None,
) -> str:
    dup = plan.duplicate
    target = f"{plan.primary.full_name} ({str(plan.primary.id)[:8]})"
    dup_label = f"{dup.full_name} ({str(dup.id)[:8]})" if dup else "none"
    lines = [f"[{plan.status}] {plan.telegram_id} → {target}   duplicate: {dup_label}"]
    if plan.reason:
        lines.append(f"  reason: {plan.reason}")
    if plan.status != "link":
        return "\n".join(lines)
    slack: str = plan.slack_action
    if slack == "move" and dup is not None:
        slack = f"move {dup.slack_user_id}"
    elif slack == "conflict" and dup is not None:
        slack = f"conflict (primary {plan.primary.slack_user_id}, duplicate {dup.slack_user_id})"
    lines.append(f"  slack: {slack}")
    counts = moved if moved is not None else plan.references
    moved_text = " ".join(f"{k}={v}" for k, v in counts.items() if k != "relabelled_messages")
    lines.append(f"  moved: {moved_text or 'nothing'}")
    relabelled = (moved or {}).get("relabelled_messages", plan.relabel_messages)
    lines.append(f"  relabelled messages: {relabelled}")
    if before is not None and after is not None:
        lines.append(f"  {'':24}{'before (30d / 120d)':28}after (30d / 120d)")
        rows: list[tuple[str, str, str]] = [
            ("accounts", str(before.accounts), str(after.accounts)),
            ("owned active chats", str(before.owned_active_chats), str(after.owned_active_chats)),
        ]
        for label, attr in (
            ("messages internal", "messages_internal"),
            ("messages partner", "messages_partner"),
            ("waits closed", "waits_closed"),
            ("tone flagged", "tone_flagged"),
            ("tone assessed", "tone_assessed"),
            ("risk events authored", "risk_events_authored"),
        ):
            rows.append((label, _fmt_pair(getattr(before, attr)), _fmt_pair(getattr(after, attr))))
        lines.extend(f"  {label:24}{b:28}{a}" for label, b, a in rows)
    return "\n".join(lines)


async def run_pair(primary_ref: str, telegram_id: int, *, apply: bool) -> LinkPlan | None:
    async with acquire_connection() as conn:
        primary = await resolve_primary(conn, primary_ref)
        if primary is None:
            print(f"[refused] {telegram_id} → {primary_ref}: no such person")
            return None
        plan: LinkPlan | None = None
        try:
            async with conn.transaction():
                plan = await plan_account_link(conn, primary, telegram_id)
                if plan.status != "link":
                    print(render(plan, None, None, None))
                    return plan
                # "before" is the primary row alone — what the dashboard shows
                # for this person today; "after" includes everything merged in.
                before = await person_snapshot(conn, [primary.id])
                moved = await apply_account_link(conn, plan, via="script")
                after = await person_snapshot(conn, [primary.id])
                print(render(plan, before, after, moved))
                if not apply:
                    raise _DryRun
        except _DryRun:
            pass
        return plan


async def _main(args: argparse.Namespace) -> int:
    linked = noop = refused = 0
    try:
        for primary_ref, telegram_id in args.pair:
            plan = await run_pair(primary_ref, telegram_id, apply=args.apply)
            status = plan.status if plan is not None else "refused"
            linked += status == "link"
            noop += status == "noop"
            refused += status == "refused"
            print()
    except Exception as exc:  # noqa: BLE001 — report and exit 2, never a half-told story
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        await close_pool()
    mode = "APPLIED" if args.apply else "DRY RUN (nothing written)"
    print(f"{linked} linked · {noop} noop · {refused} refused · {mode}")
    return 1 if refused else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--pair", type=parse_pair, action="append", required=True,
                        help="PRIMARY:TELEGRAM_ID (repeatable)")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    return parser


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(_main(build_parser().parse_args())))
