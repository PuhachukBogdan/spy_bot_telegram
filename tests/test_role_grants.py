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
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramAPIError

from src.bot.handlers.dm_commands import _help_for
from src.bot.handlers.registration import _granted_role
from src.bot.notify import pin_replacing_previous
from src.config import Settings, settings
from src.db.queries.etc import list_report_recipients, list_slack_report_recipients
from src.utils.session import verify_login_token


class _User:
    """Just enough of InternalUser for the grant decision."""

    def __init__(
        self,
        *,
        role: str = "manager",
        slack: str | None = None,
        accounts: list[int] | None = None,
    ) -> None:
        self.id = uuid4()
        self.role = role
        self.slack_user_id = slack
        self.full_name = "X"
        self.telegram_accounts = [1] if accounts is None else accounts


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
async def test_release_dms_every_reader_with_their_own_sign_in_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The weekly DM carries a link that still works when it is opened.

    It is pinned and read all week, so it cannot depend on a 90-day cookie
    happening to be alive, and it cannot be the 15-minute single-use token this
    message used to avoid — which is why those became week-long and reusable.
    The token must resolve to the reader it was addressed to, not just to
    somebody.
    """
    from src.summary import generator as g

    readers = [_User(role="admin"), _User(role="head")]
    sent: list[tuple[Any, str]] = []
    pins: list[bool] = []

    async def fake_recipients(conn: Any) -> list[Any]:
        return readers

    async def fake_notify(
        bot: Any, user: Any, text: str, *, pin: bool = False
    ) -> bool:
        sent.append((user, text))
        pins.append(pin)
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
    assert pins == [True, True]  # each release replaces the last pinned one
    for user, body in sent:
        assert "/auth/link/" in body
        token = body.split("/auth/link/")[1].split('"')[0]
        assert verify_login_token(token) == user.id


def test_generate_report_can_still_run_without_a_bot() -> None:
    """scripts/trigger_summary and POST /summary/generate pass no bot."""
    from src.summary.generator import generate_report

    assert inspect.signature(generate_report).parameters["bot"].default is None


# ---------------------------------------------------------------------------
# The Slack copy of the report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_report_recipients_need_a_linked_slack_account() -> None:
    conn = _FakeConn()
    await list_slack_report_recipients(conn)  # type: ignore[arg-type]
    sql = conn.sql
    assert "role IN ('admin', 'head')" in sql
    assert "enabled = true" in sql
    # The Telegram list drops rows with no Telegram account; this one drops rows
    # with no Slack account, for the same reason — nowhere to deliver.
    assert "slack_user_id IS NOT NULL" in sql


class _SlackUser:
    def __init__(self, slack: str, role: str = "head") -> None:
        self.slack_user_id = slack
        self.role = role


@pytest.fixture
def slack_dm_env(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, Any]]:
    """Wire _announce_to_slack_dms to fakes; return the captured sends."""
    from src.summary import generator as g

    sent: list[tuple[str, str, Any]] = []

    async def fake_dm(uid: str, text: str, *, blocks: Any = None) -> None:
        if uid == "UBROKEN0001":
            from src.alerts.slack import SlackDeliveryError

            raise SlackDeliveryError("channel_not_found")
        sent.append((uid, text, blocks))

    async def fake_rows(conn: Any) -> list[Any]:
        return [_SlackUser("UHEAD00001")]

    class _NullConn:
        async def __aenter__(self) -> Any:
            return None

        async def __aexit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(g, "send_dm_to_user", fake_dm)
    monkeypatch.setattr(g, "list_slack_report_recipients", fake_rows)
    monkeypatch.setattr(g, "acquire_connection", lambda: _NullConn())
    return sent


async def _run_slack_dms() -> None:
    from src.summary import generator as g

    await g._announce_to_slack_dms(
        "weekly",
        datetime(2026, 9, 14, tzinfo=UTC),
        datetime(2026, 9, 21, tzinfo=UTC),
        3,
        "https://example.test/dashboard",
    )


@pytest.mark.asyncio
async def test_slack_dm_addresses_env_ids_and_role_holders_exactly_once(
    slack_dm_env: list[tuple[str, str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The .env seed reaches people with no row yet; roles cover everyone else.

    The overlap is the point of the de-duplication: once the seeded person
    registers, they are in BOTH lists and must still get one DM.
    """
    monkeypatch.setattr(
        settings, "REPORT_SLACK_DM_IDS", ["uceo000001", "UHEAD00001"], raising=False
    )
    await _run_slack_dms()
    assert [uid for uid, _, _ in slack_dm_env] == ["UCEO000001", "UHEAD00001"]


@pytest.mark.asyncio
async def test_slack_dm_carries_the_same_card_and_the_permanent_link(
    slack_dm_env: list[tuple[str, str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.summary import generator as g

    monkeypatch.setattr(settings, "REPORT_SLACK_DM_IDS", ["UCEO000001"], raising=False)
    monkeypatch.setattr(
        settings, "REGISTRATION_ROLE_GRANTS", {"UCEO000001": "admin"}, raising=False
    )
    await _run_slack_dms()

    _, text, blocks = slack_dm_env[0]
    expected_text, expected_blocks = g._report_message(
        "weekly",
        datetime(2026, 9, 14, tzinfo=UTC),
        datetime(2026, 9, 21, tzinfo=UTC),
        3,
        "https://example.test/dashboard",
    )
    # The private copy IS the channel card — not a reworded second version.
    assert (text, blocks) == (expected_text, expected_blocks)
    urls = [
        el["url"]
        for b in blocks
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert urls == ["https://example.test/dashboard"]
    assert "/auth/link/" not in str(blocks)


@pytest.mark.asyncio
async def test_one_unreachable_slack_account_does_not_stop_the_others(
    slack_dm_env: list[tuple[str, str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "REPORT_SLACK_DM_IDS", ["UBROKEN0001", "UCEO000001"], raising=False
    )
    await _run_slack_dms()
    # UBROKEN0001 raises; the .env id after it and the role holder from the
    # database are still delivered to.
    assert [uid for uid, _, _ in slack_dm_env] == ["UCEO000001", "UHEAD00001"]


@pytest.mark.asyncio
async def test_no_slack_readers_configured_sends_nothing(
    slack_dm_env: list[tuple[str, str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.summary import generator as g

    async def no_rows(conn: Any) -> list[Any]:
        return []

    monkeypatch.setattr(g, "list_slack_report_recipients", no_rows)
    monkeypatch.setattr(settings, "REPORT_SLACK_DM_IDS", [], raising=False)
    await _run_slack_dms()
    assert slack_dm_env == []


@pytest.mark.asyncio
async def test_a_head_never_gets_the_company_wide_signal_count(
    slack_dm_env: list[tuple[str, str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The head's page excludes his own rows (§21).

    A total he cannot reconcile against that page is exactly the leak the
    scoping exists to prevent, so the count is an admin-only line. A seeded id
    with no grant entry is treated as scoped — fail-closed, one line less.
    """
    monkeypatch.setattr(
        settings,
        "REPORT_SLACK_DM_IDS",
        ["UCEO000001", "USEEDHEAD1", "UMYSTERY01"],
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "REGISTRATION_ROLE_GRANTS",
        {"UCEO000001": "admin", "USEEDHEAD1": "head"},
        raising=False,
    )
    await _run_slack_dms()
    cards = {uid: (text, blocks) for uid, text, blocks in slack_dm_env}

    assert "3 risk events recorded" in cards["UCEO000001"][0]
    assert "Risk events recorded" in str(cards["UCEO000001"][1])
    for scoped in ("USEEDHEAD1", "UMYSTERY01", "UHEAD00001"):
        text, blocks = cards[scoped]
        assert "recorded" not in text
        assert "Risk events recorded" not in str(blocks)
        # Everything else is the same message: period, and a way in.
        assert "Partner Risk Report" in str(blocks)
        assert "/dashboard" in str(blocks)


@pytest.mark.asyncio
async def test_telegram_dm_follows_the_same_count_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.summary import generator as g

    readers = [_User(role="admin"), _User(role="head")]
    sent: list[tuple[Any, str]] = []
    pins: list[bool] = []

    async def fake_recipients(conn: Any) -> list[Any]:
        return readers

    async def fake_notify(
        bot: Any, user: Any, text: str, *, pin: bool = False
    ) -> bool:
        sent.append((user, text))
        pins.append(pin)
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
        object(),
        "weekly",
        datetime(2026, 9, 14, tzinfo=UTC),
        datetime(2026, 9, 21, tzinfo=UTC),
        3,
    )

    admin_text, head_text = sent[0][1], sent[1][1]
    assert "3 risk signals" in admin_text
    assert "risk signals" not in head_text
    assert "14 Sep" in head_text and "/auth/link/" in head_text


# ---------------------------------------------------------------------------
# Telegram readers seeded in .env
#
# The CEO reads the report in Telegram and has no internal_users row, so no role
# query can address him. REPORT_TELEGRAM_DM_IDS carries him until a /register
# does; these tests pin that it stays a seed and never becomes a second address
# book that can drift from the roles table.
# ---------------------------------------------------------------------------

UNREACHABLE = 999  # a seeded id that never pressed Start in the bot


@pytest.fixture
def telegram_dm_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Wire _announce_to_readers to fakes; return what each sender was handed."""
    from src.summary import generator as g

    captured: dict[str, Any] = {
        "readers": [_User(role="admin")],
        "users": [],
        "ids": [],
        "pins": [],
    }

    async def fake_recipients(conn: Any) -> list[Any]:
        return list(captured["readers"])

    async def fake_notify(
        bot: Any, user: Any, text: str, *, pin: bool = False
    ) -> bool:
        captured["users"].append((user, text))
        return True

    async def fake_notify_id(
        bot: Any, chat_id: int, text: str, *, pin: bool = False
    ) -> bool:
        captured["pins"].append((chat_id, pin))
        # The real helper swallows a Telegram error and reports the failure in
        # its return value, so an unreachable reader looks like this, not like
        # an exception.
        if chat_id == UNREACHABLE:
            return False
        captured["ids"].append((chat_id, text))
        return True

    class _NullConn:
        async def __aenter__(self) -> Any:
            return None

        async def __aexit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(g, "list_report_recipients", fake_recipients)
    monkeypatch.setattr(g, "notify_internal_user", fake_notify)
    monkeypatch.setattr(g, "notify_telegram_id", fake_notify_id)
    monkeypatch.setattr(g, "acquire_connection", lambda: _NullConn())
    monkeypatch.setattr(settings, "REPORT_TELEGRAM_DM_IDS", {}, raising=False)
    return captured


async def _run_telegram_dms() -> None:
    from src.summary import generator as g

    await g._announce_to_readers(
        object(),
        "weekly",
        datetime(2026, 9, 14, tzinfo=UTC),
        datetime(2026, 9, 21, tzinfo=UTC),
        3,
    )


@pytest.mark.asyncio
async def test_a_seeded_id_is_dmd_although_it_holds_no_role(
    telegram_dm_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "REPORT_TELEGRAM_DM_IDS", {"6000000001": "admin"}, raising=False
    )
    await _run_telegram_dms()

    chat_id, text = telegram_dm_env["ids"][0]
    assert chat_id == 6000000001
    assert "/dashboard" in text
    assert "/auth/link/" not in text
    # The role holder from the database is still addressed as well.
    assert len(telegram_dm_env["users"]) == 1


@pytest.mark.asyncio
async def test_a_seeded_id_that_is_already_a_role_holder_is_dmd_once(
    telegram_dm_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overlap is the point: registering must not double the message."""
    telegram_dm_env["readers"] = [_User(role="admin", accounts=[6000000001])]
    monkeypatch.setattr(
        settings, "REPORT_TELEGRAM_DM_IDS", {"6000000001": "admin"}, raising=False
    )
    await _run_telegram_dms()

    assert telegram_dm_env["ids"] == []
    assert len(telegram_dm_env["users"]) == 1


@pytest.mark.asyncio
async def test_a_seeded_reader_follows_the_same_count_rule(
    telegram_dm_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an admin reads an unscoped page, so only an admin gets the total."""
    telegram_dm_env["readers"] = []
    monkeypatch.setattr(
        settings,
        "REPORT_TELEGRAM_DM_IDS",
        {"11": "admin", "22": "head"},
        raising=False,
    )
    await _run_telegram_dms()

    cards = dict(telegram_dm_env["ids"])
    assert "3 risk signals" in cards[11]
    assert "risk signals" not in cards[22]
    assert "14 Sep" in cards[22] and "/dashboard" in cards[22]


@pytest.mark.asyncio
async def test_one_unreachable_seeded_id_does_not_stop_the_others(
    telegram_dm_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings,
        "REPORT_TELEGRAM_DM_IDS",
        {str(UNREACHABLE): "admin", "11": "admin"},
        raising=False,
    )
    await _run_telegram_dms()
    assert [cid for cid, _ in telegram_dm_env["ids"]] == [11]


@pytest.mark.asyncio
async def test_the_seed_alone_is_enough_to_send_a_release(
    telegram_dm_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody holds a role with a Telegram account yet — the seed still delivers."""
    telegram_dm_env["readers"] = []
    monkeypatch.setattr(
        settings, "REPORT_TELEGRAM_DM_IDS", {"11": "admin"}, raising=False
    )
    await _run_telegram_dms()
    assert [cid for cid, _ in telegram_dm_env["ids"]] == [11]


@pytest.mark.asyncio
async def test_a_seeded_reader_gets_the_plain_dashboard_link(
    telegram_dm_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no row to sign a token for, so there is no personal link to send.

    The bot cannot mint a session for someone it cannot identify; the seeded id
    is an address, not an account. They get the page URL and sign in once they
    have a row (which /register creates).
    """
    telegram_dm_env["readers"] = []
    monkeypatch.setattr(
        settings, "REPORT_TELEGRAM_DM_IDS", {"11": "admin"}, raising=False
    )
    await _run_telegram_dms()

    _, text = telegram_dm_env["ids"][0]
    assert "/dashboard" in text
    assert "/auth/link/" not in text


@pytest.mark.asyncio
async def test_every_release_dm_is_pinned(
    telegram_dm_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "REPORT_TELEGRAM_DM_IDS", {"11": "admin"}, raising=False
    )
    await _run_telegram_dms()
    assert telegram_dm_env["pins"] == [(11, True)]


# ---------------------------------------------------------------------------
# Pinning: one live report at the top of the chat
# ---------------------------------------------------------------------------

_BOT_ID = 777


class _PinBot:
    """Enough of aiogram's Bot for :func:`pin_replacing_previous`."""

    def __init__(self, pinned: Any = None, *, pin_fails: bool = False) -> None:
        self.pinned = pinned
        self.pin_fails = pin_fails
        self.calls: list[tuple[str, int]] = []

    async def me(self) -> Any:
        return SimpleNamespace(id=_BOT_ID)

    async def get_chat(self, chat_id: int) -> Any:
        return SimpleNamespace(pinned_message=self.pinned)

    async def unpin_chat_message(self, *, chat_id: int, message_id: int) -> None:
        self.calls.append(("unpin", message_id))

    async def pin_chat_message(
        self, *, chat_id: int, message_id: int, disable_notification: bool
    ) -> None:
        if self.pin_fails:
            raise TelegramAPIError(method=SimpleNamespace(), message="not allowed")
        assert disable_notification is True  # the message itself already pinged
        self.calls.append(("pin", message_id))


def _pinned(message_id: int, author_id: int) -> Any:
    return SimpleNamespace(
        message_id=message_id, from_user=SimpleNamespace(id=author_id)
    )


@pytest.mark.asyncio
async def test_pinning_replaces_the_bots_previous_pin() -> None:
    bot = _PinBot(pinned=_pinned(10, _BOT_ID))
    assert await pin_replacing_previous(bot, 42, 20) is True  # type: ignore[arg-type]
    assert bot.calls == [("unpin", 10), ("pin", 20)]


@pytest.mark.asyncio
async def test_pinning_leaves_a_message_the_reader_pinned_themselves() -> None:
    """Their chat, their pin — we only ever take down our own last report."""
    bot = _PinBot(pinned=_pinned(10, 12345))
    assert await pin_replacing_previous(bot, 42, 20) is True  # type: ignore[arg-type]
    assert bot.calls == [("pin", 20)]


@pytest.mark.asyncio
async def test_pinning_an_empty_chat_pins_without_unpinning() -> None:
    bot = _PinBot(pinned=None)
    assert await pin_replacing_previous(bot, 42, 20) is True  # type: ignore[arg-type]
    assert bot.calls == [("pin", 20)]


@pytest.mark.asyncio
async def test_a_refused_pin_costs_a_log_line_not_the_report() -> None:
    """The message is already delivered; the pin is a convenience on top."""
    bot = _PinBot(pinned=None, pin_fails=True)
    assert await pin_replacing_previous(bot, 42, 20) is False  # type: ignore[arg-type]


def test_telegram_dm_ids_drop_non_ids_and_scope_an_unknown_role() -> None:
    """A key that cannot be a chat id is unusable; an unknown role is not.

    Dropping the entry would silently withhold the report from someone who was
    deliberately listed, so a typo in the ROLE only costs the count line.
    """
    cleaned = Settings(
        REPORT_TELEGRAM_DM_IDS={
            " 6000000001 ": "Admin",
            "@yoda": "admin",
            "42": "boss",
        }
    ).REPORT_TELEGRAM_DM_IDS
    assert cleaned == {"6000000001": "admin", "42": "head"}


def test_slack_dm_ids_are_normalised_and_deduplicated() -> None:
    """Slack member IDs are upper-case; a lower-case paste would address nobody."""
    cleaned = Settings(
        REPORT_SLACK_DM_IDS=[" uabc123 ", "UABC123", "", "UDEF456"]
    ).REPORT_SLACK_DM_IDS
    assert cleaned == ["UABC123", "UDEF456"]


def test_the_slack_copy_does_not_depend_on_the_telegram_bot() -> None:
    """The person who asked for this does not use the bot at all.

    ``_announce_to_readers`` is skipped when a caller passes no bot (the HTTP
    route, trigger_summary.py); the Slack DM must not sit behind that gate.
    """
    from src.summary import generator as g

    assert "bot" not in inspect.signature(g._announce_to_slack_dms).parameters
    release = inspect.getsource(g.generate_report)
    dm_call = release.index("_announce_to_slack_dms")
    bot_gate = release.index("if bot is not None:")
    assert dm_call < bot_gate


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
