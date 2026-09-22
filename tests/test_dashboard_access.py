"""Dashboard sign-in and per-viewer scoping (2026-09-11).

Two things are under test and they fail in opposite directions:

* **Sign-in** must refuse anything not exactly right — a tampered cookie, a
  replayed login link, a stale Telegram callback.
* **Scoping** must REMOVE data rather than hide it. A head's page is built
  without them in it, so the assertions here check absence in the payload, not
  absence on screen: the numbers a head reads have to be the team minus one
  person, which is a different number from the team.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from src.config import settings
from src.metrics.collect import ManagerMetrics
from src.metrics.preview import build_payload
from src.metrics.scope import (
    ADMIN_SCOPE,
    DASHBOARD_ROLES,
    PageScope,
    scope_for,
    visible_managers,
    visible_risk_rows,
    visible_rows,
    visible_tone,
)
from src.metrics.trends import build_scope_days
from src.metrics.window import resolve_metrics_window
from src.utils.session import (
    issue_login_token,
    login_url,
    sign_session,
    verify_login_token,
    verify_session,
    verify_telegram_login,
)

_KYIV = ZoneInfo("Europe/Kyiv")


class _User:
    """Just enough of InternalUser for the scope builder."""

    def __init__(self, *, role: str, name: str = "X", telegram: list[int] | None = None):
        self.id = uuid4()
        self.role = role
        self.full_name = name
        self.telegram_accounts = telegram or []


# ---------------------------------------------------------------------------
# Session cookie
# ---------------------------------------------------------------------------


def test_session_round_trip_carries_id_and_role() -> None:
    user_id = uuid4()
    claims = verify_session(sign_session(user_id, "head"))
    assert claims is not None
    assert claims.user_id == user_id
    assert claims.role == "head"


def test_session_rejects_tampered_role() -> None:
    """The whole point of signing: you cannot promote yourself by editing a cookie."""
    raw = sign_session(uuid4(), "head")
    forged = raw.replace(".head.", ".admin.", 1)
    assert forged != raw
    assert verify_session(forged) is None


@pytest.mark.parametrize(
    "raw",
    ["", "not-a-cookie", "v1.deadbeef.admin.99999999", "v9." + "a" * 32 + ".admin.1.2"],
)
def test_session_rejects_malformed(raw: str) -> None:
    assert verify_session(raw) is None


def test_session_expires() -> None:
    now = time.time()
    raw = sign_session(uuid4(), "admin", ttl_days=1, now=now)
    assert verify_session(raw, now=now + 3600) is not None
    assert verify_session(raw, now=now + 2 * 86400) is None


# ---------------------------------------------------------------------------
# Sign-in links
#
# Single-use and 15 minutes until 2026-09-22. The weekly report is now a PINNED
# message a reader comes back to all week, so a link that dies on first use made
# that message a one-shot.
# ---------------------------------------------------------------------------


def test_login_token_round_trip() -> None:
    user_id = uuid4()
    assert verify_login_token(issue_login_token(user_id)) == user_id


def test_login_token_survives_being_used_again() -> None:
    """The pinned weekly message is opened on a phone, then on a laptop."""
    user_id = uuid4()
    token = issue_login_token(user_id)
    assert verify_login_token(token) == user_id
    assert verify_login_token(token) == user_id


def test_login_token_expires_after_the_configured_days() -> None:
    now = 1_000_000.0
    token = issue_login_token(uuid4(), now=now)
    days = settings.DASHBOARD_LOGIN_LINK_DAYS
    assert verify_login_token(token, now=now + (days - 1) * 86400) is not None
    assert verify_login_token(token, now=now + (days + 1) * 86400) is None


def test_login_token_rejects_a_forged_user() -> None:
    """Swapping the id in the URL must not sign you in as somebody else."""
    token = issue_login_token(uuid4())
    version, _user_hex, expires, signature = token.split(".")
    forged = f"{version}.{uuid4().hex}.{expires}.{signature}"
    assert verify_login_token(forged) is None


def test_login_token_rejects_a_longer_life_than_it_was_given() -> None:
    now = 1_000_000.0
    token = issue_login_token(uuid4(), now=now)
    version, user_hex, expires, signature = token.split(".")
    stretched = f"{version}.{user_hex}.{int(expires) + 86400 * 365}.{signature}"
    assert verify_login_token(stretched, now=now) is None


def test_a_session_cookie_is_not_a_login_token() -> None:
    """Both are signed with the same key; the domain prefix keeps them apart."""
    assert verify_login_token(sign_session(uuid4(), "admin")) is None


@pytest.mark.parametrize("raw", ["", "never-issued", "v1.deadbeef.1.2.3"])
def test_login_token_rejects_malformed(raw: str) -> None:
    assert verify_login_token(raw) is None


def test_login_url_points_at_the_auth_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SERVER_BASE_URL", "https://example.test/")
    url = login_url(uuid4())
    assert url.startswith("https://example.test/auth/link/")
    assert verify_login_token(url.rsplit("/", 1)[1]) is not None


# ---------------------------------------------------------------------------
# Telegram Login Widget
# ---------------------------------------------------------------------------


def _signed_login(**fields: Any) -> dict[str, str]:
    params = {k: str(v) for k, v in fields.items()}
    check = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    key = hashlib.sha256(
        settings.TELEGRAM_BOT_TOKEN.get_secret_value().encode()
    ).digest()
    params["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return params


def test_telegram_login_accepts_a_valid_callback() -> None:
    now = time.time()
    params = _signed_login(id=1000000001, first_name="Kowalski", auth_date=int(now))
    assert verify_telegram_login(params, now=now) == 1000000001


def test_telegram_login_rejects_edited_id() -> None:
    now = time.time()
    params = _signed_login(id=1, first_name="A", auth_date=int(now))
    params["id"] = "999"
    assert verify_telegram_login(params, now=now) is None


def test_telegram_login_rejects_stale_callback() -> None:
    """A signed URL in someone's history must not stay a key forever."""
    now = time.time()
    params = _signed_login(id=7, first_name="A", auth_date=int(now) - 86400)
    assert verify_telegram_login(params, now=now) is None


def test_telegram_login_rejects_missing_hash() -> None:
    assert verify_telegram_login({"id": "7", "auth_date": "1"}) is None


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_admin_scope_hides_nobody_and_keeps_the_risk_report() -> None:
    scope = scope_for(_User(role="admin", name="CEO"))
    assert scope.hidden_manager_ids == frozenset()
    assert scope.sees_risk_report is True
    assert ADMIN_SCOPE.sees_risk_report is True


def test_head_scope_hides_self_and_drops_the_risk_report() -> None:
    head = _User(role="head", name="Kowalski", telegram=[1000000001])
    scope = scope_for(head)
    assert scope.hidden_manager_ids == frozenset({head.id})
    assert scope.hidden_telegram_ids == frozenset({1000000001})
    assert scope.sees_risk_report is False
    assert "head" in DASHBOARD_ROLES and "manager" not in DASHBOARD_ROLES


def test_scope_cache_keys_never_collide_between_roles() -> None:
    head = _User(role="head", telegram=[1])
    other = _User(role="head", telegram=[2])
    keys = {
        ADMIN_SCOPE.cache_key,
        scope_for(head).cache_key,
        scope_for(other).cache_key,
    }
    assert len(keys) == 3


def test_visible_managers_and_rows_drop_the_hidden_one() -> None:
    head = _User(role="head", telegram=[10])
    peer = _User(role="manager")
    scope = scope_for(head)
    assert [m.id for m in visible_managers([head, peer], scope)] == [peer.id]
    rows = [{"manager_id": head.id, "n": 1}, {"manager_id": peer.id, "n": 2}]
    assert visible_rows(rows, scope) == [{"manager_id": peer.id, "n": 2}]
    # An admin's filter is a no-op, not a copy-with-surprises.
    assert visible_rows(rows, ADMIN_SCOPE) == rows


def test_head_never_sees_a_case_they_wrote_in_someone_elses_chat() -> None:
    """The subtle leak: attribution puts the case on the CHAT OWNER's page, so
    filtering by manager_id alone would still show a head their own words."""
    head = _User(role="head", telegram=[10])
    peer = _User(role="manager")
    scope = scope_for(head)
    rows = [
        {"manager_id": peer.id, "sender_id": 10, "id": "written-by-head"},
        {"manager_id": peer.id, "sender_id": 99, "id": "written-by-peer"},
        {"manager_id": head.id, "sender_id": 99, "id": "in-heads-chat"},
    ]
    assert [r["id"] for r in visible_risk_rows(rows, scope)] == ["written-by-peer"]


def test_visible_tone_drops_the_hidden_managers_days_and_flags() -> None:
    head = _User(role="head", telegram=[10])
    peer = _User(role="manager")
    scope = scope_for(head)
    tone = {
        "enabled": True,
        "minAssessed": 20,
        "metrics": [],
        "days": [
            {"m": str(head.id), "d": "2026-09-01", "a": 5, "f": {}},
            {"m": str(peer.id), "d": "2026-09-01", "a": 7, "f": {}},
        ],
        "flags": {str(head.id): [{"id": "x"}], str(peer.id): [{"id": "y"}]},
    }
    scoped = visible_tone(tone, scope)
    assert scoped is not None
    assert [d["m"] for d in scoped["days"]] == [str(peer.id)]
    assert set(scoped["flags"]) == {str(peer.id)}
    assert visible_tone(tone, ADMIN_SCOPE) == tone
    assert visible_tone(None, scope) is None


def test_team_totals_are_summed_without_the_hidden_manager() -> None:
    """Not a display concern: the team series is folded from the same rows, so a
    head's page must be built from rows that never contained them."""
    head = _User(role="head", telegram=[10])
    peer = _User(role="manager")
    scope = scope_for(head)
    day = datetime(2026, 9, 1, 12, tzinfo=UTC)
    proposal_days = [
        {"manager_id": head.id, "day": day.date(), "proposals": 4},
        {"manager_id": peer.id, "day": day.date(), "proposals": 3},
    ]
    scopes = build_scope_days(
        [m.id for m in visible_managers([head, peer], scope)],
        sla_dated={},
        proposal_days=visible_rows(proposal_days, scope),
        risk_days=[],
        manager_index={},
        chat_day_rows=[],
        chat_registry=[],
        tz=_KYIV,
    )
    team = scopes[None].day_counters(day.astimezone(_KYIV).date())
    assert team.proposals == 3  # the head's 4 never entered the sum
    assert head.id not in scopes


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def _window() -> Any:
    return resolve_metrics_window(
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC), epoch=None
    )


def test_payload_labels_the_viewer() -> None:
    head = _User(role="head", name="Kowalski | BetonWin", telegram=[10])
    payload = build_payload([], _window(), scope=scope_for(head))
    assert payload["viewer"] == {
        "name": "Kowalski | BetonWin",
        "role": "head",
        "seesRiskReport": False,
    }


def test_payload_managers_follow_the_scope_it_was_built_with() -> None:
    """build_payload renders what it is handed — the filtering happened upstream,
    and this pins that the viewer block cannot disagree with the roster."""
    peer_id: UUID = uuid4()
    payload = build_payload(
        [ManagerMetrics(manager_id=peer_id, name="Peer")],
        _window(),
        scope=PageScope(role="head", viewer_id=uuid4(), viewer_name="Head"),
    )
    assert [m["id"] for m in payload["managers"]] == [str(peer_id)]
    assert payload["viewer"]["seesRiskReport"] is False
