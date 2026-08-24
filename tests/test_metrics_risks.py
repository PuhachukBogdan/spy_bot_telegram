"""Risk attribution onto manager dossiers, chat lists, and category totals."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from src.metrics.attribution import RiskAttribution
from src.metrics.collect import (
    ManagerMetrics,
    chat_label,
    chats_by_manager,
    risks_by_manager,
)
from src.metrics.preview import _category_totals, build_payload
from src.metrics.window import resolve_metrics_window

MANAGER = uuid4()
MANAGER_TG = 8592696398
PARTNER_TG = 555000111
INDEX = {MANAGER_TG: MANAGER}


def _risk(sender_id: int | None, risk_type: str = "hidden_payment") -> dict[str, Any]:
    return {
        "id": uuid4(),
        "chat_id": uuid4(),
        "risk_type": risk_type,
        "risk_level": "high",
        "final_score": 72,
        "created_at": datetime(2026, 8, 12, 10, tzinfo=UTC),
        "detected_phrase": "давай мимо системы",
        "llm_explanation": "Proposes settling outside the official scheme.",
        "sender_id": sender_id,
        "status": "new",
        "chat_name": "78516 | Acme | Beton.Win",
        "topic_name": None,
        "unit_type": "group",
        "manager_id": MANAGER,
    }


def test_manager_authored_case_counts() -> None:
    cases = risks_by_manager([_risk(MANAGER_TG)], INDEX)[MANAGER]
    assert cases[0].attribution is RiskAttribution.MANAGER_ACTION
    assert cases[0].counts is True


def test_partner_raised_case_attaches_but_never_counts() -> None:
    # Ownership decides WHOSE page it lands on; authorship decides whether it counts.
    cases = risks_by_manager([_risk(PARTNER_TG)], INDEX)[MANAGER]
    assert cases[0].attribution is RiskAttribution.CHAT_CONTEXT
    assert cases[0].counts is False


def test_anonymous_sender_is_context() -> None:
    assert risks_by_manager([_risk(None)], INDEX)[MANAGER][0].counts is False


def test_own_and_context_counts_are_separate_in_the_payload() -> None:
    cases = risks_by_manager(
        [_risk(MANAGER_TG), _risk(PARTNER_TG), _risk(PARTNER_TG)], INDEX
    )
    metrics = [ManagerMetrics(manager_id=MANAGER, name="Mirror", risks=cases[MANAGER])]
    payload = build_payload(
        metrics,
        resolve_metrics_window(
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 8, 31, tzinfo=UTC),
            epoch=None,
        ),
    )
    row = payload["managers"][0]
    assert row["risksOwn"] == 1
    assert row["risksContext"] == 2
    assert [r["counts"] for r in row["risks"]] == [True, False, False]
    assert row["risks"][1]["attribution"] == "chat_context"


def test_risk_day_is_local_to_the_report_timezone() -> None:
    # 22:30 UTC is already the NEXT day in Kyiv (UTC+3 in summer). The client
    # filters risks by this day against bucket days, which are Kyiv-local too —
    # keying it on UTC would shift late-evening cases into the wrong period.
    late = _risk(MANAGER_TG)
    late["created_at"] = datetime(2026, 8, 12, 22, 30, tzinfo=UTC)
    cases = risks_by_manager([late], INDEX)
    metrics = [ManagerMetrics(manager_id=MANAGER, name="Mirror", risks=cases[MANAGER])]
    window = resolve_metrics_window(
        datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC), epoch=None
    )
    kyiv = build_payload(metrics, window, tz=ZoneInfo("Europe/Kyiv"))
    assert kyiv["managers"][0]["risks"][0]["day"] == "2026-08-13"
    # Without a timezone the day falls back to UTC rather than guessing.
    utc = build_payload(metrics, window)
    assert utc["managers"][0]["risks"][0]["day"] == "2026-08-12"


def test_category_totals_include_context_and_sort_by_size() -> None:
    # The overview answers "what is happening across the business", so context
    # cases belong there; the own/context split lives on the dossier.
    cases = risks_by_manager(
        [
            _risk(PARTNER_TG, "fraud_shave"),
            _risk(MANAGER_TG, "fraud_shave"),
            _risk(PARTNER_TG, "hidden_payment"),
        ],
        INDEX,
    )
    totals = _category_totals(
        [ManagerMetrics(manager_id=MANAGER, name="Mirror", risks=cases[MANAGER])]
    )
    assert totals == [
        {"type": "fraud_shave", "count": 2},
        {"type": "hidden_payment", "count": 1},
    ]


# ---------------------------------------------------------------------------
# chat lists
# ---------------------------------------------------------------------------


def _chat(messages: int, unit_type: str = "group", topic: str | None = None) -> dict[str, Any]:
    return {
        "manager_id": MANAGER,
        "chat_id": uuid4(),
        "chat_name": "78516 | Acme | Beton.Win",
        "topic_name": topic,
        "unit_type": unit_type,
        "messages": messages,
    }


def test_chats_sorted_busiest_first_and_flagged() -> None:
    chats = chats_by_manager([_chat(2), _chat(40), _chat(10)], min_messages=10)[MANAGER]
    assert [c.messages for c in chats] == [40, 10, 2]
    assert [c.active for c in chats] == [True, True, False]


def test_unit_type_survives_to_the_chat_row() -> None:
    chats = chats_by_manager([_chat(5, "business")], min_messages=10)[MANAGER]
    assert chats[0].unit_type == "business"


def test_topic_name_is_appended_to_the_label() -> None:
    assert chat_label(_chat(1, "topic", topic="Payouts")).endswith("· Payouts")
    assert "·" not in chat_label(_chat(1))


def test_missing_chat_name_degrades_gracefully() -> None:
    row = _chat(1)
    row["chat_name"] = None
    assert chat_label(row) == "—"


def test_chat_rows_reach_the_payload() -> None:
    metrics = [
        ManagerMetrics(
            manager_id=MANAGER,
            name="Mirror",
            chats=chats_by_manager([_chat(40, "business")], min_messages=10)[MANAGER],
        )
    ]
    payload = build_payload(
        metrics,
        resolve_metrics_window(
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 8, 2, tzinfo=UTC),
            epoch=None,
        ),
    )
    chat = payload["managers"][0]["chats"][0]
    assert chat["unitType"] == "business"
    assert chat["active"] is True
