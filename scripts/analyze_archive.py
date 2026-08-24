"""One-off retrospective risk analysis over imported archive history.

Three phases, each safe to run on its own:

    python scripts/analyze_archive.py --estimate          # cost, writes nothing
    python scripts/analyze_archive.py --run               # analyse (resumable)
    python scripts/analyze_archive.py --link              # the permanent URL
    python scripts/analyze_archive.py --report out.html   # local copy of the findings

``--estimate`` first. It reports the projected spend without making a single model
call, so the run is a decision rather than a surprise.

The review is published on ONE permanent link, ``/archive/{ARCHIVE_REPORT_TOKEN}``,
deliberately outside the weekly/monthly report machinery — that rotates its token on
every generation and revokes the previous one, which is the opposite of what is
wanted here. The route renders the newest run live, so there is no publish step after
a run: finish the analysis and the existing link shows it. ``--report`` is only for a
local copy to keep or attach somewhere.

The run is bounded by ``RETRO_BUDGET_USD`` and checks OpenRouter's *reported* spend
before each call, so it stops at the ceiling instead of crossing it. It is also
resumable: pass the printed ``--run-id`` back to continue where it stopped, and
completed windows are skipped without a request.

Findings land in ``archive_retro_findings`` — never ``risk_events`` — so nothing
here can reach the weekly report, the Slack dashboard, or the alert path.
Requires migrations 0023 and 0024.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings  # noqa: E402
from src.db.client import acquire_connection, close_pool  # noqa: E402
from src.importer.retro import (  # noqa: E402
    WINDOW_SIZE,
    count_windows,
    estimate_cost,
    load_targets,
    run_retro,
)
from src.importer.retro_report import (  # noqa: E402
    load_findings,
    load_run_summary,
    render_report,
)

#: First-party Anthropic list prices per million tokens, for the pre-flight
#: estimate only. Actual spend comes from OpenRouter's usage block, which is what
#: the budget gate enforces — these numbers never gate anything.
_PRICES: dict[str, tuple[float, float]] = {
    "anthropic/claude-haiku-4-5": (1.00, 5.00),
    "anthropic/claude-sonnet-4-6": (3.00, 15.00),
    "anthropic/claude-sonnet-5": (3.00, 15.00),
    "anthropic/claude-opus-5": (5.00, 25.00),
    "anthropic/claude-opus-4-8": (5.00, 25.00),
}


async def _estimate(model: str) -> None:
    async with acquire_connection() as conn:
        targets = await load_targets(conn)
    per_chat = [t.message_count for t in targets]
    total = sum(per_chat)
    if not total:
        print("no imported messages found — run scripts/import_archive.py --apply first")
        return

    print(f"imported messages : {total:,} across {len(targets)} chats")
    print(f"window size       : {WINDOW_SIZE} messages -> {count_windows(per_chat)} windows")
    print(
        "                    windows never span chats, so a long tail of small chats\n"
        "                    costs more than total/window_size would suggest"
    )
    print(f"budget ceiling    : ${settings.RETRO_BUDGET_USD:.2f} (RETRO_BUDGET_USD)")
    print(f"\n{'model':<32}{'windows':>9}{'in (k)':>10}{'out (k)':>9}{'est. cost':>12}")
    for name, (price_in, price_out) in _PRICES.items():
        est = estimate_cost(per_chat, input_per_mtok=price_in, output_per_mtok=price_out)
        marker = "  <- configured" if name == model else ""
        print(
            f"{name:<32}{est['windows']:>9.0f}{est['tokens_in'] / 1000:>10.0f}"
            f"{est['tokens_out'] / 1000:>9.0f}{'$' + format(est['cost_usd'], '.2f'):>12}"
            f"{marker}"
        )
    print(
        "\nEstimate only — the run bills OpenRouter's reported usage, which is also "
        "what the budget gate enforces.\nCyrillic runs ~2 chars/token, so an "
        "English-calibrated estimate would understate this archive by about half."
    )


async def _run(model: str, run_id: UUID | None, max_windows: int | None) -> None:
    stats = await run_retro(
        acquire_connection,
        model=model,
        run_id=run_id,
        budget_usd=settings.RETRO_BUDGET_USD,
        max_windows=max_windows,
    )
    print(f"run id          : {stats.run_id}")
    print(f"windows done    : {stats.windows_done}  (skipped {stats.windows_skipped})")
    print(f"findings kept   : {stats.findings}")
    print(f"dropped         : {stats.dropped_low_confidence} low-confidence, "
          f"{stats.dropped_unanchored} unanchored")
    print(f"tokens          : {stats.tokens_in:,} in / {stats.tokens_out:,} out")
    print(f"spend           : ${stats.cost_usd:.4f}")
    if stats.budget_exhausted:
        print(
            f"\nSTOPPED AT BUDGET (${settings.RETRO_BUDGET_USD:.2f}). Resume with:\n"
            f"  python scripts/analyze_archive.py --run --run-id {stats.run_id}"
        )
    if stats.errors:
        print(f"\nerrors ({len(stats.errors)}):")
        for error in stats.errors[:20]:
            print(f"  {error}")
    print(f"\n{_permanent_link_status()}")
    print(
        f"\nAlso available as a local file:\n"
        f"  python scripts/analyze_archive.py --report archive_review.html "
        f"--run-id {stats.run_id}"
    )


def _permanent_link_status() -> str:
    """Describe the permanent link, or say exactly what is missing to enable it.

    The link is rendered live from the newest run, so it needs no regeneration step
    after a run — but it stays 404 until both secrets are set (fail-closed, because
    nothing ever rotates or expires this URL).
    """
    token = settings.ARCHIVE_REPORT_TOKEN
    password = settings.ARCHIVE_REPORT_PASSWORD
    base = settings.SERVER_BASE_URL.rstrip("/")
    if token and token.get_secret_value() and password and password.get_secret_value():
        return (
            "Permanent link (already live — it renders the newest run, no publish step):\n"
            f"  {base}/archive/{token.get_secret_value()}"
        )
    missing = [
        name
        for name, value in (
            ("ARCHIVE_REPORT_TOKEN", token),
            ("ARCHIVE_REPORT_PASSWORD", password),
        )
        if value is None or not value.get_secret_value()
    ]
    return (
        "Permanent link is DISABLED — /archive/{token} returns 404.\n"
        f"  Set {' and '.join(missing)} in .env, then restart the app.\n"
        "  Suggested token: python -c \"import secrets; print(secrets.token_hex(32))\"\n"
        "  Both are required: this URL never rotates and never expires, unlike the\n"
        "  weekly dashboard's, so it is not opened without a password."
    )


async def _report(run_id: UUID, out: Path) -> str:
    """Return the rendered HTML; the caller writes it outside the event loop."""
    async with acquire_connection() as conn:
        summary = await load_run_summary(conn, run_id)
        findings = await load_findings(conn, run_id)
    print(f"{len(findings)} findings")
    if summary is not None:
        print(f"  {summary.windows} windows, {summary.chats} chats, "
              f"${summary.cost_usd:.2f} spent, model {summary.model}")
    return render_report(summary, findings)


async def _main(args: argparse.Namespace) -> str | None:
    try:
        if args.link:
            print(_permanent_link_status())
        elif args.estimate:
            await _estimate(args.model)
        elif args.run:
            await _run(args.model, args.run_id, args.max_windows)
        else:
            if args.run_id is None:
                sys.exit("--report needs --run-id (printed by --run)")
            return await _report(args.run_id, Path(args.report))
    finally:
        await close_pool()
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--estimate", action="store_true", help="project cost, write nothing")
    mode.add_argument("--run", action="store_true", help="run the analysis")
    mode.add_argument("--link", action="store_true", help="show the permanent URL")
    mode.add_argument("--report", metavar="OUT.html", help="render findings to HTML")
    parser.add_argument("--model", default=settings.LLM_MODEL_RETRO)
    parser.add_argument("--run-id", type=UUID, help="resume, or select a run to report")
    parser.add_argument(
        "--max-windows", type=int, help="stop after N windows (for a trial run)"
    )
    parsed = parser.parse_args()

    import asyncio

    html = asyncio.run(_main(parsed))
    # Written here rather than inside the coroutine: filesystem calls in async
    # context trip ASYNC240, and the report is a plain artefact, not I/O the event
    # loop needs to await.
    if html is not None:
        destination = Path(parsed.report)
        destination.write_text(html, encoding="utf-8")
        print(f"wrote {destination}")
