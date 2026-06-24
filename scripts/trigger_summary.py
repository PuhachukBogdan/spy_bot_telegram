"""Manually generate one summary report and post its Slack link.

Runs the real ``generate_report`` against the configured DB + Slack (reads .env).
Use for on-demand reports or to verify delivery outside the scheduled cadence.

    python scripts/trigger_summary.py [weekly|monthly]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.client import close_pool  # noqa: E402
from src.summary.generator import generate_report  # noqa: E402


async def _run(period: str) -> None:
    try:
        result = await generate_report(period_type=period)  # type: ignore[arg-type]
        print(f"period:          {period}")
        print(f"url:             {result.url}")
        print(f"event_count:     {result.event_count}")
        print(f"slack_delivered: {result.slack_delivered}")
        if result.dashboard_password:
            print(f"dashboard_pw:    {result.dashboard_password}")
        if result.slack_error:
            print(f"slack_error:     {result.slack_error}")
    finally:
        await close_pool()


if __name__ == "__main__":
    period_arg = sys.argv[1] if len(sys.argv) > 1 else "weekly"
    if period_arg not in ("weekly", "monthly"):
        sys.exit("usage: python scripts/trigger_summary.py [weekly|monthly]")
    asyncio.run(_run(period_arg))
