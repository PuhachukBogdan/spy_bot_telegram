"""Unit tests for the Phase 9 batch analysis pieces (src/pipeline/batch_processor.py
+ src/db/queries/queue.enqueue_chat_analysis).

The pure ``prepare_scored_findings`` mapper and the queue dedup/bump are tested in
isolation — no real LLM or DB (a tiny fake connection records the SQL calls). The
end-to-end LLM/DB orchestration is covered by the live e2e smoke, not here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from src.config import settings
from src.db.queries.messages import get_chat_analysis_window
from src.db.queries.queue import enqueue_chat_analysis
from src.llm.schemas import RiskAnalysis, RiskFinding
from src.pipeline.batch_processor import prepare_scored_findings, should_defer_batch
from tests.conftest import Make

# --- prepare_scored_findings (pure) ------------------------------------------


def _finding(message_id: str, *, score: int, confidence: float) -> RiskFinding:
    return RiskFinding(
        message_id=message_id,
        risk_type="private_channel",  # type: ignore[arg-type]
        score=score,
        confidence=confidence,
        explanation="test",
    )


def test_rule_hint_never_inflates_llm_severity() -> None:
    # A flagged message (rule base 70) the LLM rates low-severity but confidently:
    # final must come from the LLM (40 * 1.2 = 48), not the rule score.
    mk = Make()
    msg = mk.message_row(base_score=70, text="между нами")
    analysis = RiskAnalysis(
        risk_events=[_finding(str(msg.id), score=40, confidence=0.85)]
    )
    scored = prepare_scored_findings(analysis, [msg])
    assert len(scored) == 1
    assert scored[0].score.base_score == 70  # recorded for reference
    assert scored[0].score.final_score == 48
    assert scored[0].score.risk_level == "medium"


def test_llm_only_finding_scores_without_dictionary() -> None:
    # The dictionary never fired (rule base 0) but the LLM finds a real risk.
    mk = Make()
    msg = mk.message_row(base_score=0, text="let's settle this off the books")
    analysis = RiskAnalysis(
        risk_events=[_finding(str(msg.id), score=80, confidence=0.9)]
    )
    scored = prepare_scored_findings(analysis, [msg])
    assert len(scored) == 1
    assert scored[0].score.final_score == 96  # 80 * 1.2 clamped to 100 -> 96
    assert scored[0].score.risk_level == "critical"


def test_finding_outside_window_is_dropped() -> None:
    # A finding pointing at a message not in the new window (e.g. an old context
    # message) is skipped, so we never create a duplicate risk on a reprocess.
    mk = Make()
    msg = mk.message_row(base_score=0, text="hi")
    stranger_id = str(uuid4())
    analysis = RiskAnalysis(
        risk_events=[_finding(stranger_id, score=90, confidence=0.9)]
    )
    scored = prepare_scored_findings(analysis, [msg])
    assert scored == []


def test_empty_analysis_yields_no_rows() -> None:
    mk = Make()
    msg = mk.message_row()
    assert prepare_scored_findings(RiskAnalysis(), [msg]) == []


# --- enqueue_chat_analysis (dedup / bump) ------------------------------------


class _FakeConn:
    """Records fetchval/execute calls; fetchval returns a preset value."""

    def __init__(self, existing_id: int | None) -> None:
        self._existing = existing_id
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, query: str, *args: Any) -> int | None:
        self.fetchval_calls.append((query, args))
        return self._existing

    async def execute(self, query: str, *args: Any) -> None:
        self.execute_calls.append((query, args))


async def test_enqueue_bumps_existing_pending_task() -> None:
    # A pending task already exists -> only the bump UPDATE runs, no INSERT.
    conn = _FakeConn(existing_id=42)
    chat_id = uuid4()
    run_at = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
    await enqueue_chat_analysis(conn, chat_id, run_at)  # type: ignore[arg-type]
    assert len(conn.fetchval_calls) == 1
    assert "UPDATE" in conn.fetchval_calls[0][0]
    assert conn.fetchval_calls[0][1] == (str(chat_id), run_at)
    assert conn.execute_calls == []  # no insert


async def test_enqueue_inserts_when_no_pending_task() -> None:
    # No pending task -> insert a fresh one.
    conn = _FakeConn(existing_id=None)
    chat_id = uuid4()
    run_at = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
    await enqueue_chat_analysis(conn, chat_id, run_at)  # type: ignore[arg-type]
    assert len(conn.execute_calls) == 1
    assert "INSERT" in conn.execute_calls[0][0]
    assert conn.execute_calls[0][1] == (str(chat_id), run_at)


# --- should_defer_batch (cost gate, pure) ------------------------------------


def test_defer_trickle_below_min_and_young() -> None:
    # One significant message, well under the batch minimum, just arrived -> wait.
    mk = Make()
    new = [mk.message_row(is_significant=True, text="hi")]
    assert should_defer_batch(new, now=datetime.now(UTC)) is True


def test_no_defer_when_priority_message_present() -> None:
    # A Tier-1 hit at/above the threshold forces the pass through regardless of size.
    mk = Make()
    new = [mk.message_row(is_significant=True, base_score=settings.PRIORITY_SCORE_THRESHOLD)]
    assert should_defer_batch(new, now=datetime.now(UTC)) is False


def test_no_defer_when_batch_full() -> None:
    # At least ANALYSIS_MIN_BATCH_MESSAGES significant messages -> analyse now.
    mk = Make()
    new = [mk.message_row(is_significant=True) for _ in range(settings.ANALYSIS_MIN_BATCH_MESSAGES)]
    assert should_defer_batch(new, now=datetime.now(UTC)) is False


def test_no_defer_when_oldest_aged_out() -> None:
    # A lone significant message older than the max wait is analysed, not starved.
    mk = Make()
    old = datetime.now(UTC) - timedelta(seconds=settings.ANALYSIS_MAX_WAIT_SECONDS + 60)
    new = [mk.message_row(is_significant=True, created_at=old)]
    assert should_defer_batch(new, now=datetime.now(UTC)) is False


def test_no_defer_when_nothing_significant() -> None:
    # Zero significant messages is the cost guard's job (advance watermark), not a defer.
    mk = Make()
    new = [mk.message_row(is_significant=False, text="")]
    assert should_defer_batch(new, now=datetime.now(UTC)) is False


# --- imported history must never reach the live analysis window ---------------


class _WindowConn:
    """Records the SQL of every fetch so the window's filters can be asserted."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append(query)
        return []


async def test_analysis_window_selects_live_rows_only() -> None:
    """The archive import lockout, guarded structurally.

    ~57k imported messages live in ``messages``. The watermark does not hold them
    back: ``since`` is NULL for a never-analysed chat, which makes every row in it
    "new". If this filter is dropped, the whole archive goes to Tier-2 — daily
    budget gone, alerts raised about 2025 conversations. There is no cheap
    behavioural test for raw SQL under the no-real-DB rule, so the guard is pinned
    on the query text itself.

    The predicate must EXCLUDE the archive value, not allow-list live ones:
    ``source`` records the delivery path ('live_group' / 'live_topic' / 'business',
    plus bare 'live' on a few pre-0006 rows), so ``source = 'live'`` would hide
    3 421 of the 3 427 real messages and silently disable risk analysis outright.
    """
    conn = _WindowConn()
    await get_chat_analysis_window(
        conn,  # type: ignore[arg-type]
        uuid4(),
        since=None,
        limit=50,
        context_before=5,
    )

    assert conn.queries, "the window ran no query"
    assert "source <> 'imported'" in conn.queries[0], (
        "the 'new' selection must exclude imported history"
    )
    assert "source = 'live'" not in conn.queries[0], (
        "an allow-list on 'live' drops live_group/live_topic/business entirely"
    )


async def test_analysis_window_context_excludes_imported_rows() -> None:
    """Context must be live-only too, so a finding can never anchor on archive data."""
    class _CtxConn(_WindowConn):
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            self.queries.append(query)
            # Return one row for the first ('new') query so the context query runs.
            return [dict(Make().message_row().model_dump())] if len(self.queries) == 1 else []

    conn = _CtxConn()
    await get_chat_analysis_window(
        conn,  # type: ignore[arg-type]
        uuid4(),
        since=None,
        limit=50,
        context_before=5,
    )

    assert len(conn.queries) == 2, "context query did not run"
    assert "source <> 'imported'" in conn.queries[1]
