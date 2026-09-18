"""Self-service role grants, the report DM, and the command-cover invariant.

Three things here, all of them about people who are not going to be walked
through anything:

* **The grant** (``REGISTRATION_ROLE_GRANTS``) turns setup into "send /register,
  paste your Slack id, paste the code". It runs with no human in the loop, so
  the tests pin the two limits that make that safe — it fires only at the FIRST
  binding of a Slack account, and it never lowers a role.
* **The report DM** addresses whoever may currently read the page, read from the
  database at send time rather than from a list someone maintains.
* **The cover**: a command a manager may not use and a command that does not
  exist must be indistinguishable. That is one string compared in two places,
  which is exactly the kind of thing that silently drifts apart.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from src.bot.handlers.dm_commands import _help_for
from src.bot.handlers.registration import _granted_role
from src.config import Settings, settings
from src.db.queries.etc import list_report_recipients


class _User:
    """Just enough of InternalUser for the grant decision."""

    def __init__(self, *, role: str = "manager", slack: str | None = None) -> None:
        self.id = uuid4()
        self.role = role
        self.slack_user_id = slack
        self.full_name = "X"
        self.telegram_accounts = [1]


class _FakeConn:
    """Captures the SQL and returns canned rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.sql = ""

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.sql = " ".join(sql.split())
        return self.rows


# ---------------------------------------------------------------------------
# The grant
# ---------------------------------------------------------------------------


@pytest.fixture
def grants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "REGISTRATION_ROLE_GRANTS",
        {"UADMIN00001": "admin", "UHEAD00001": "head"},
    )


def test_grant_applies_to_a_brand_new_person(grants: None) -> None:
    # No row yet: /register is about to create one, and it should be created
    # with the granted role rather than the default manager.
    assert _granted_role("UADMIN00001", None) == "admin"
    assert _granted_role("UHEAD00001", None) == "head"


def test_grant_is_case_insensitive_on_the_slack_id(grants: None) -> None:
    # /register upper-cases what the person typed; the map is normalised the
    # same way at load. Both halves are checked so neither can drift.
    assert _granted_role("uadmin00001", None) == "admin"


def test_no_grant_for_an_unlisted_slack_account(grants: None) -> None:
    assert _granted_role("UNOBODY123", None) is None


def test_grant_skips_a_re_registration(grants: None) -> None:
    """The decisive limit: a grant is a way IN, not a standing override.

    Someone already linked has been through this once. If re-running /register
    re-applied the map, an admin's later /set_role could be undone by the
    subject of that decision simply repeating their own registration.
    """
    already_linked = _User(role="manager", slack="UADMIN00001")
    assert _granted_role("UADMIN00001", already_linked) is None


def test_grant_never_lowers_an_existing_role(grants: None) -> None:
    # The map says 'head'; the row says 'admin'. Nothing happens — a stale
    # entry in .env must not be able to demote anyone.
    admin_row = _User(role="admin", slack=None)
    assert _granted_role("UHEAD00001", admin_row) is None


def test_grant_upgrades_a_manager_row_that_predates_the_link(grants: None) -> None:
    # Whitelisted by /add_manager first, registering afterwards: the row exists
    # but has never been bound to Slack, so this is still a first binding.
    existing = _User(role="manager", slack=None)
    assert _granted_role("UADMIN00001", existing) == "admin"


def test_unknown_role_in_env_is_dropped_not_obeyed() -> None:
    """A typo must fail closed and must not take the process down either."""
    cleaned = Settings(
        REGISTRATION_ROLE_GRANTS={"uabc123": "Admin", "UDEF456": "superuser"}
    ).REGISTRATION_ROLE_GRANTS
    assert cleaned == {"UABC123": "admin"}


# ---------------------------------------------------------------------------
# Who the report reaches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_recipients_are_exactly_the_dashboard_roles() -> None:
    conn = _FakeConn()
    await list_report_recipients(conn)  # type: ignore[arg-type]
    sql = conn.sql
    assert "role IN ('admin', 'head')" in sql
    assert "enabled = true" in sql
    # No Telegram account means nowhere to deliver; including such a row would
    # only produce a logged failure on every release.
    assert "jsonb_array_length(COALESCE(telegram_accounts, '[]'::jsonb)) > 0" in sql


@pytest.mark.asyncio
async def test_release_dms_every_reader_with_the_plain_dashboard_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The weekly DM must carry the permanent link, never a one-time token.

    Login tokens live 15 minutes in process memory: a weekly message carrying
    one would be dead by morning, and dead again after any restart.
    """
    from src.summary import generator as g

    readers = [_User(role="admin"), _User(role="head")]
    sent: list[tuple[Any, str]] = []

    async def fake_recipients(conn: Any) -> list[Any]:
        return readers

    async def fake_notify(bot: Any, user: Any, text: str) -> bool:
        sent.append((user, text))
        return True

    class _NullConn:
        async def __aenter__(self) -> Any:
            return None

        async def __aexit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(g, "list_report_recipients", fake_recipients)
    monkeypatch.setattr(g, "notify_internal_user", fake_notify)
    monkeypatch.setattr(g, "acquire_connection", lambda: _NullConn())

    await g._announce_to_readers(
        object(), "weekly", datetime(2026, 9, 7, tzinfo=UTC), datetime(2026, 9, 14, tzinfo=UTC), 3
    )

    assert [u for u, _ in sent] == readers
    body = sent[0][1]
    assert "/dashboard" in body
    assert "/auth/link/" not in body


def test_generate_report_can_still_run_without_a_bot() -> None:
    """scripts/trigger_summary and POST /summary/generate pass no bot."""
    from src.summary.generator import generate_report

    assert inspect.signature(generate_report).parameters["bot"].default is None


# ---------------------------------------------------------------------------
# The cover
# ---------------------------------------------------------------------------


def test_head_help_offers_the_dashboard_and_nothing_about_monitoring() -> None:
    body = _help_for("head")
    assert "/dashboard" in body
    for leak in ("/risks", "/partners", "/chats", "/admin"):
        assert leak not in body


def test_manager_and_viewer_help_never_mentions_the_dashboard() -> None:
    # A manager may be the SUBJECT of what the report measures. The command is
    # already refused for them; it must not be advertised either.
    for role in ("manager", "viewer", None):
        assert "/dashboard" not in _help_for(role)


def test_admin_help_is_short_by_default_and_complete_on_request() -> None:
    short = _help_for("admin")
    full = _help_for("admin", full=True)
    assert "/dashboard" in short
    assert "/admin" not in short  # the 35-command wall is one command away
    assert "/admin" in full and "/risks" in full
    assert len(full) > len(short)


def test_unknown_command_reply_matches_the_refusal_word_for_word() -> None:
    """The whole cover rests on these two strings being identical.

    ``require_role`` answers a manager who types /dashboard; the fallback router
    answers anyone who types a command that does not exist. If they ever differ,
    the difference itself reveals which hidden commands are real.
    """
    from src.bot.handlers import unknown
    from src.bot.middleware import roles

    refusal = inspect.getsource(roles)
    fallback = inspect.getsource(unknown)
    assert 'await message.answer("Command not found.")' in refusal
    assert 'await message.answer("Command not found.")' in fallback


def test_fallback_router_is_registered_after_the_command_routers() -> None:
    """Order is the whole correctness argument: a real command must win first."""
    from src.bot.instance import dp

    names = [r.name for r in dp.sub_routers]
    assert "unknown_commands" in names
    for earlier in ("dm_commands", "admin_panel", "registration"):
        assert names.index(earlier) < names.index("unknown_commands")
    # ...and ahead of the group ingestion catch-all, which would otherwise
    # swallow the update in silence.
    assert names.index("unknown_commands") < names.index("messages")
