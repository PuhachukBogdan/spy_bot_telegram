"""Load the approved Tier-1 red-flag dictionary into ``red_flag_patterns``. Phase 6.

The dictionary is DATA, not code: the matcher (``src/pipeline/tier1.py``) ships
with an empty table and hot-reloads from it every 5 minutes. This one-off loader
takes the leadership-approved JSON (gitignored) and writes it into the live table.

By default it REPLACES the table contents atomically (the file is the source of
truth), so re-running is idempotent. Use ``--append`` to add without clearing.

Usage::

    python -m scripts.load_patterns                      # default file, replace
    python -m scripts.load_patterns path/to/dict.json    # explicit path
    python -m scripts.load_patterns --append             # add, keep existing
    python -m scripts.load_patterns --dry-run            # validate only, no write

The JSON is a list of objects with keys ``pattern``, ``pattern_type``,
``language``, ``risk_category``, ``base_score`` and (optional) ``examples`` —
matching the ``red_flag_patterns`` columns 1:1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from src.db.client import acquire_connection, close_pool
from src.utils.logging import get_logger

log = get_logger(__name__)

DEFAULT_FILE = "red_flag_patterns_dictionary.txt"

# The 12 risk categories from CLAUDE.md section 7.6 / Table 6.
RISK_CATEGORIES = frozenset(
    {
        "shadow_deal",
        "private_channel",
        "hidden_payment",
        "traffic_leakage",
        "commercial_terms",
        "fraud_shave",
        "access_risk",
        "partner_churn",
        "payment_conflict",
        "reputation_risk",
        "operational_sla",
        "employee_behavior",
    }
)
PATTERN_TYPES = frozenset({"literal", "regex"})
REQUIRED_KEYS = frozenset(
    {"pattern", "pattern_type", "language", "risk_category", "base_score"}
)


def validate(data: object) -> list[dict[str, Any]]:
    """Validate the parsed dictionary, raising ``ValueError`` on any bad row."""
    if not isinstance(data, list):
        raise ValueError("top-level JSON must be a list of pattern objects")
    errors: list[str] = []
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            errors.append(f"[{i}] not an object")
            continue
        missing = REQUIRED_KEYS - row.keys()
        if missing:
            errors.append(f"[{i}] missing keys: {sorted(missing)}")
            continue
        if row["pattern_type"] not in PATTERN_TYPES:
            errors.append(f"[{i}] bad pattern_type {row['pattern_type']!r}")
        if row["risk_category"] not in RISK_CATEGORIES:
            errors.append(f"[{i}] unknown risk_category {row['risk_category']!r}")
        if not isinstance(row["pattern"], str) or not row["pattern"].strip():
            errors.append(f"[{i}] empty/invalid pattern")
        score = row["base_score"]
        if not isinstance(score, int) or not 0 <= score <= 100:
            errors.append(f"[{i}] base_score out of range: {score!r}")
        if row["pattern_type"] == "regex":
            try:
                re.compile(row["pattern"])
            except re.error as exc:
                errors.append(f"[{i}] bad regex {row['pattern']!r}: {exc}")
    if errors:
        raise ValueError(
            f"{len(errors)} invalid row(s):\n  " + "\n  ".join(errors[:25])
        )
    # mypy: every element is a validated dict at this point.
    return [row for row in data if isinstance(row, dict)]


async def load(rows: list[dict[str, Any]], *, replace: bool) -> int:
    """Insert ``rows`` into ``red_flag_patterns``. Replace clears the table first.

    Done in a single transaction so the live matcher's hot-reload never observes a
    half-empty dictionary: it sees the old set, then atomically the new one.
    """
    params = [
        (
            row["pattern"],
            row["pattern_type"],
            row["language"],
            row["risk_category"],
            int(row["base_score"]),
            row.get("examples"),
        )
        for row in rows
    ]
    async with acquire_connection() as conn:
        async with conn.transaction():
            before = await conn.fetchval("SELECT count(*) FROM red_flag_patterns")
            if replace:
                await conn.execute("DELETE FROM red_flag_patterns")
            await conn.executemany(
                """
                INSERT INTO red_flag_patterns
                    (pattern, pattern_type, language, risk_category,
                     base_score, examples)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                params,
            )
            after = await conn.fetchval("SELECT count(*) FROM red_flag_patterns")
    log.info(
        "patterns.loaded",
        inserted=len(params),
        rows_before=int(before),
        rows_after=int(after),
        replaced=replace,
    )
    return int(after)


async def _load_and_close(rows: list[dict[str, Any]], *, replace: bool) -> int:
    """Run the DB write and always close the pool (the async entry point)."""
    try:
        return await load(rows, replace=replace)
    finally:
        await close_pool()


def main() -> int:
    """Parse args, read + validate the file synchronously, then write to the DB."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "file",
        nargs="?",
        default=DEFAULT_FILE,
        help=f"path to the dictionary JSON (default: {DEFAULT_FILE})",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="add patterns without clearing the table first",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the file and report stats; do not touch the DB",
    )
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2

    try:
        rows = validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    langs = sorted({row["language"] for row in rows})
    cats = len({row["risk_category"] for row in rows})
    print(
        f"validated {len(rows)} patterns "
        f"({len(langs)} languages: {','.join(langs)}; {cats} categories)"
    )

    if args.dry_run:
        print("dry-run: no changes written")
        return 0

    total = asyncio.run(_load_and_close(rows, replace=not args.append))
    mode = "appended" if args.append else "replaced"
    print(f"done: {mode}; red_flag_patterns now has {total} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
