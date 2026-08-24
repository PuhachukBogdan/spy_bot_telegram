"""Tests for the Telegram HTML-export parser and the staff/partner roster.

No real DB, Slack, or network. Fixtures are hand-written export fragments that
mirror the shapes actually observed in the archive (see the module docstrings in
``src.importer.parser`` / ``src.importer.roster`` for where each shape comes from).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from src.importer.matcher import EXCLUDED_AFF_IDS, DbChat, build_plan, summarise
from src.importer.parser import ParsedExport, parse_archive, parse_export_folder
from src.importer.roster import apply_roster, build_roster, has_brand_tag

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

_HEAD = """<html><body><div class="page_wrap"><div class="page_header">
<div class="content"><div class="text bold">{title}</div></div></div>
<div class="page_body chat_page"><div class="history">
"""
_TAIL = """</div></div></div></body></html>"""


def _msg(
    mid: int,
    date: str,
    name: str | None,
    text: str | None = None,
    *,
    joined: bool = False,
    extra: str = "",
) -> str:
    cls = "message default clearfix joined" if joined else "message default clearfix"
    name_div = f'<div class="from_name">\n{name}\n</div>' if name is not None else ""
    text_div = f'<div class="text">\n{text}\n</div>' if text is not None else ""
    return (
        f'<div class="{cls}" id="message{mid}">'
        f'<div class="pull_left userpic_wrap"><div class="userpic userpic4">'
        f'<div class="initials">X</div></div></div>'
        f'<div class="body">'
        f'<div class="pull_right date details" title="{date}">10:00</div>'
        f"{name_div}{extra}{text_div}"
        f"</div></div>"
    )


def _service(mid: int, body: str) -> str:
    return (
        f'<div class="message service" id="message{mid}">'
        f'<div class="body details">\n{body}\n</div></div>'
    )


def _write(folder: Path, title: str, blocks: str, filename: str = "messages.html") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_text(
        _HEAD.format(title=title) + blocks + _TAIL, encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parses_basic_message_fields(tmp_path: Path) -> None:
    _write(
        tmp_path / "58329",
        "LEGENDS | Betonwin | 58329 | 71862 | 74849",
        _msg(2, "07.02.2025 09:35:27 UTC+02:00", "Roman", "Привіт"),
    )
    export = parse_export_folder(tmp_path / "58329")

    assert export.aff_id == "58329"
    assert export.chat_title == "LEGENDS | Betonwin | 58329 | 71862 | 74849"
    # Every id in the title is captured, folder id first: one chat can serve
    # several aff_ids.
    assert export.aff_ids == ("58329", "71862", "74849")

    (message,) = export.messages
    assert message.telegram_message_id == 2
    assert message.sender_name == "Roman"
    assert message.text == "Привіт"
    assert message.message_type == "text"
    # Timestamps keep the export's UTC+02:00 offset rather than being floated.
    assert message.timestamp.isoformat() == "2025-02-07T09:35:27+02:00"


def test_date_divider_service_blocks_are_not_messages(tmp_path: Path) -> None:
    """Negative-id service blocks are presentation; positive ones are real events."""
    _write(
        tmp_path / "111",
        "Chat | 111",
        _service(-1, "19 February 2026")
        + _msg(5, "19.02.2026 10:00:00 UTC+02:00", "Anna", "hi")
        + _service(6, "Mirror | Betonwin invited Geralt | Betonwin"),
    )
    export = parse_export_folder(tmp_path / "111")

    assert [m.telegram_message_id for m in export.messages] == [5]
    assert [e.text for e in export.events] == ["Mirror | Betonwin invited Geralt | Betonwin"]


def test_joined_block_inherits_previous_sender(tmp_path: Path) -> None:
    _write(
        tmp_path / "111",
        "Chat | 111",
        _msg(1, "01.03.2026 10:00:00 UTC+02:00", "Christopher", "first")
        + _msg(2, "01.03.2026 10:01:00 UTC+02:00", None, "second", joined=True),
    )
    export = parse_export_folder(tmp_path / "111")

    assert [m.sender_name for m in export.messages] == ["Christopher", "Christopher"]


def test_reply_both_anchor_forms(tmp_path: Path) -> None:
    """Same-page replies use GoToMessage(N); cross-page ones only an anchor."""
    same_page = (
        '<div class="reply_to details">In reply to '
        '<a href="#go_to_message40" onclick="return GoToMessage(40)">this message</a></div>'
    )
    cross_page = (
        '<div class="reply_to details">In reply to '
        '<a href="messages.html#go_to_message1003">this message</a></div>'
    )
    _write(
        tmp_path / "111",
        "Chat | 111",
        _msg(41, "01.03.2026 10:00:00 UTC+02:00", "A", "a", extra=same_page)
        + _msg(42, "01.03.2026 10:01:00 UTC+02:00", "B", "b", extra=cross_page),
    )
    export = parse_export_folder(tmp_path / "111")

    assert [m.reply_to_message_id for m in export.messages] == [40, 1003]


def test_forward_without_origin_name_is_still_flagged(tmp_path: Path) -> None:
    """50 of the archive's forwards hide their origin, so flag and name differ."""
    named = (
        '<div class="forwarded body"><div class="from_name">Mirror | Betonwin'
        '<span class="date details" title="19.01.2026 17:50:58 UTC+02:00">'
        " 19.01.2026 17:50:58</span></div>"
    )
    anonymous = '<div class="forwarded body">'
    _write(
        tmp_path / "111",
        "Chat | 111",
        _msg(1, "01.03.2026 10:00:00 UTC+02:00", "A", "shared", extra=named)
        + _msg(2, "01.03.2026 10:01:00 UTC+02:00", "B", "shared too", extra=anonymous),
    )
    export = parse_export_folder(tmp_path / "111")

    assert [m.is_forward for m in export.messages] == [True, True]
    assert [m.forward_from_name for m in export.messages] == ["Mirror | Betonwin", None]


def test_forward_header_name_does_not_become_the_sender(tmp_path: Path) -> None:
    """A forward's own from_name must not overwrite the forwarding sender."""
    forwarded = (
        '<div class="forwarded body"><div class="from_name">Someone Else</div>'
        '<div class="text">original</div>'
    )
    _write(
        tmp_path / "111",
        "Chat | 111",
        _msg(1, "01.03.2026 10:00:00 UTC+02:00", "Christopher", None, extra=forwarded),
    )
    (message,) = parse_export_folder(tmp_path / "111").messages

    assert message.sender_name == "Christopher"
    assert message.forward_from_name == "Someone Else"


def test_reaction_userpics_do_not_leak_into_text_or_sender(tmp_path: Path) -> None:
    reactions = (
        '<span class="reactions"><span class="reaction"><span class="emoji">🤝</span>'
        '<span class="userpics"><div class="userpic userpic6">'
        '<div class="initials" title="Mirror | Betonwin">M</div></div></span></span></span>'
    )
    _write(
        tmp_path / "111",
        "Chat | 111",
        _msg(1, "01.03.2026 10:00:00 UTC+02:00", "Anna", "Да, спасибо") + reactions,
    )
    (message,) = parse_export_folder(tmp_path / "111").messages

    assert message.sender_name == "Anna"
    assert message.text == "Да, спасибо"


def test_text_keeps_links_and_newlines(tmp_path: Path) -> None:
    body = 'Линк <br><br><a href="https://t.me/+abc">https://t.me/+abc</a> @some_user'
    _write(
        tmp_path / "111", "Chat | 111", _msg(1, "01.03.2026 10:00:00 UTC+02:00", "A", body)
    )
    (message,) = parse_export_folder(tmp_path / "111").messages

    assert message.text == "Линк \n\nhttps://t.me/+abc @some_user"
    assert message.links == ("https://t.me/+abc",)
    assert message.mentions == ("some_user",)


def test_media_only_counted_when_present_on_disk(tmp_path: Path) -> None:
    """Videos are commonly referenced but not downloaded; only real files count."""
    folder = tmp_path / "111"
    photo = '<a class="photo_wrap clearfix pull_left" href="photos/photo_1@x.jpg">' "</a>"
    missing = (
        '<div class="media_wrap clearfix"><div class="media clearfix pull_left media_video">'
        '<div class="body"><div class="title bold">Animation</div>'
        '<div class="description">Not included, change data exporting settings to '
        "download.</div></div></div></div>"
    )
    _write(
        folder,
        "Chat | 111",
        _msg(1, "01.03.2026 10:00:00 UTC+02:00", "A", None, extra=photo)
        + _msg(2, "01.03.2026 10:01:00 UTC+02:00", "B", None, extra=missing),
    )
    (folder / "photos").mkdir()
    (folder / "photos" / "photo_1@x.jpg").write_bytes(b"jpeg")

    first, second = parse_export_folder(folder).messages
    assert first.media_paths == ("photos/photo_1@x.jpg",)
    assert first.message_type == "photo"
    assert second.media_paths == ()
    assert second.media_omitted is True


def test_thumbnails_are_not_imported_as_separate_media(tmp_path: Path) -> None:
    folder = tmp_path / "111"
    block = (
        '<a class="photo_wrap clearfix pull_left" href="photos/p@x.jpg">'
        '<img class="photo" src="photos/p@x_thumb.jpg"/></a>'
    )
    _write(folder, "Chat | 111", _msg(1, "01.03.2026 10:00:00 UTC+02:00", "A", None, extra=block))
    (folder / "photos").mkdir()
    (folder / "photos" / "p@x.jpg").write_bytes(b"full")
    (folder / "photos" / "p@x_thumb.jpg").write_bytes(b"thumb")

    (message,) = parse_export_folder(folder).messages
    assert message.media_paths == ("photos/p@x.jpg",)


def test_text_wins_over_media_for_message_type(tmp_path: Path) -> None:
    """Mirrors ``ingest._classify_type``: a captioned photo is stored as text."""
    folder = tmp_path / "111"
    block = '<a class="photo_wrap clearfix pull_left" href="photos/p.jpg"></a>'
    _write(
        folder,
        "Chat | 111",
        _msg(1, "01.03.2026 10:00:00 UTC+02:00", "A", "за 8 фтд закройте сверку", extra=block),
    )
    (folder / "photos").mkdir()
    (folder / "photos" / "p.jpg").write_bytes(b"x")

    (message,) = parse_export_folder(folder).messages
    assert message.message_type == "text"


def test_pages_are_ordered_numerically_not_lexicographically(tmp_path: Path) -> None:
    """messages10.html must follow messages2.html, not precede it."""
    folder = tmp_path / "111"
    _write(folder, "Chat | 111", _msg(1, "01.03.2026 10:00:00 UTC+02:00", "A", "one"))
    _write(folder, "Chat | 111", _msg(2, "02.03.2026 10:00:00 UTC+02:00", "A", "two"),
           filename="messages2.html")
    _write(folder, "Chat | 111", _msg(3, "03.03.2026 10:00:00 UTC+02:00", "A", "ten"),
           filename="messages10.html")

    export = parse_export_folder(folder)
    assert [m.text for m in export.messages] == ["one", "two", "ten"]


def test_identical_histories_share_a_content_hash(tmp_path: Path) -> None:
    """77106 and 78284 are the same chat exported twice; import must dedup them."""
    blocks = _msg(1, "01.03.2026 10:00:00 UTC+02:00", "A", "same")
    _write(tmp_path / "77106", "78284 | (77106) | MetaForge | Betonwin", blocks)
    _write(tmp_path / "78284", "78284 | (77106) | MetaForge | Betonwin", blocks)
    _write(tmp_path / "99999", "Other | 99999", _msg(1, "01.03.2026 10:00:00 UTC+02:00", "A", "x"))

    exports = {e.aff_id: e for e in parse_archive(tmp_path)}
    assert exports["77106"].content_hash == exports["78284"].content_hash
    assert exports["99999"].content_hash != exports["77106"].content_hash


def test_export_cruft_folders_are_skipped(tmp_path: Path) -> None:
    _write(tmp_path / "111", "Chat | 111", _msg(1, "01.03.2026 10:00:00 UTC+02:00", "A", "x"))
    (tmp_path / "__MACOSX").mkdir()
    (tmp_path / ".DS_Store").mkdir()

    assert [e.aff_id for e in parse_archive(tmp_path)] == ["111"]


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


def test_brand_tag_variants() -> None:
    assert has_brand_tag("Mirror | Betonwin")
    # Real archive spellings: missing space, and a Cyrillic 'С' homoglyph.
    assert has_brand_tag("Сhicco| Betonwin")
    assert has_brand_tag("76613 | DV | Beton.Win")
    assert not has_brand_tag("Igor Advertise.net")


def _portfolio(tmp_path: Path, staff: str, chats: int) -> Path:
    """*staff* posts in every chat; each chat also has its own local contact."""
    root = tmp_path / "arch"
    for index in range(chats):
        aff = str(60000 + index)
        _write(
            root / aff,
            f"Partner {aff} | Betonwin",
            _msg(1, "01.03.2026 10:00:00 UTC+02:00", staff, "internal msg")
            + _msg(2, "01.03.2026 10:01:00 UTC+02:00", f"Contact {aff}", "partner msg"),
        )
    return root


def test_portfolio_span_marks_staff_without_a_brand_tag(tmp_path: Path) -> None:
    """``Christopher`` and ``AffOps Helper`` carry no brand tag but span the book."""
    root = _portfolio(tmp_path, "Christopher", chats=10)
    roster = build_roster(parse_archive(root))

    assert roster["Christopher"].role == "internal"
    assert "present in 10 chats" in roster["Christopher"].reasons
    assert roster["Contact 60000"].role == "partner"


def test_single_chat_admin_activity_stays_partner(tmp_path: Path) -> None:
    """A partner runs their own group; raw invite counts must not promote them."""
    _write(
        tmp_path / "70589",
        "Alfaleads | BetonWin | 70589",
        _msg(1, "01.03.2026 10:00:00 UTC+02:00", "Менеджер Alfaleads", "hi")
        + _service(2, "Менеджер Alfaleads invited Olga")
        + _service(3, "Менеджер Alfaleads pinned this message")
        + _service(4, "Менеджер Alfaleads invited Kate"),
    )
    roster = build_roster(parse_archive(tmp_path))

    assert roster["Менеджер Alfaleads"].role == "partner"


def test_admin_activity_across_many_chats_marks_staff(tmp_path: Path) -> None:
    root = tmp_path / "arch"
    for index in range(4):
        aff = str(60000 + index)
        _write(
            root / aff,
            f"Partner {aff}",
            _msg(1, "01.03.2026 10:00:00 UTC+02:00", f"Contact {aff}", "hi")
            + _service(2, "Geralt Ops invited Someone"),
        )
    roster = build_roster(parse_archive(root))

    assert roster["Geralt Ops"].role == "internal"
    assert "invited people in 4 chats" in roster["Geralt Ops"].reasons


def test_group_posting_as_itself_is_anonymous_not_staff(tmp_path: Path) -> None:
    """The chat title as sender inherits the brand tag and must not read as staff."""
    title = "ParadoX Partners | BetonWin | 66599"
    _write(tmp_path / "66599", title, _msg(1, "01.03.2026 10:00:00 UTC+02:00", title, "hi"))
    roster = build_roster(parse_archive(tmp_path))

    assert roster[title].role == "anonymous_admin"


def test_placeholder_names_stay_anonymous_despite_wide_span(tmp_path: Path) -> None:
    """"Deleted Account" spans 165 chats but collapses unknown people."""
    root = _portfolio(tmp_path, "Deleted Account", chats=10)
    roster = build_roster(parse_archive(root))

    assert roster["Deleted Account"].role == "anonymous_admin"


def test_manual_overrides_are_recorded_in_reasons(tmp_path: Path) -> None:
    root = _portfolio(tmp_path, "Christopher", chats=10)
    exports = parse_archive(root)

    forced = build_roster(exports, force_partner=frozenset({"Christopher"}))
    assert forced["Christopher"].role == "partner"
    assert forced["Christopher"].reasons[0] == "manual override → partner"

    promoted = build_roster(exports, force_internal=frozenset({"Contact 60000"}))
    assert promoted["Contact 60000"].role == "internal"


def test_db_roster_names_count_as_internal(tmp_path: Path) -> None:
    _write(
        tmp_path / "111",
        "Chat | 111",
        _msg(1, "01.03.2026 10:00:00 UTC+02:00", "Uncle Bogdan", "hi"),
    )
    roster = build_roster(
        parse_archive(tmp_path), db_internal_names=frozenset({"uncle bogdan"})
    )

    assert roster["Uncle Bogdan"].role == "internal"


def test_apply_roster_rewrites_message_roles(tmp_path: Path) -> None:
    root = _portfolio(tmp_path, "Christopher", chats=10)
    exports = parse_archive(root)
    # Name-only classification cannot know Christopher is staff.
    assert {m.sender_role for e in exports for m in e.messages} == {"partner"}

    updated = apply_roster(exports, build_roster(exports))
    roles = {
        m.sender_name: m.sender_role for e in updated for m in e.messages
    }
    assert roles["Christopher"] == "internal"
    assert roles["Contact 60000"] == "partner"


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------


def _db_chat(
    name: str,
    *,
    status: str = "active",
    messages: int = 0,
    aff: str | None = None,
    watermark: datetime | None = None,
) -> DbChat:
    return DbChat(
        id=uuid4(),
        telegram_chat_id=-1000000000000 - len(name),
        chat_name=name,
        status=status,
        unit_type="group",
        message_count=messages,
        last_processed_at=watermark,
        import_aff_id=aff,
    )


def _export(tmp_path: Path, aff: str, title: str, count: int = 3) -> ParsedExport:
    blocks = "".join(
        _msg(i, f"0{1 + i}.03.2026 10:00:00 UTC+02:00", "A", f"m{i}") for i in range(1, count + 1)
    )
    _write(tmp_path / aff, title, blocks)
    return parse_export_folder(tmp_path / aff)


def test_matches_on_aff_id_in_chat_name(tmp_path: Path) -> None:
    export = _export(tmp_path, "59743", "CGS | ID 59743 | BETONWIN")
    chats = [_db_chat("CGS | ID 59743 | BETONWIN"), _db_chat("Unrelated | 80000")]

    (plan,) = build_plan([export], chats)

    assert plan.action == "attach"
    assert plan.target is not None
    assert plan.target.chat_name == "CGS | ID 59743 | BETONWIN"
    assert plan.matched_on == ("59743",)


def test_matches_through_a_secondary_aff_id_from_the_title(tmp_path: Path) -> None:
    """A chat can be found by an id that is not the folder name."""
    export = _export(tmp_path, "58329", "LEGENDS | Betonwin | 58329 | 71862 | 74849")
    chats = [_db_chat("Legends renamed | 74849")]

    (plan,) = build_plan([export], chats)

    assert plan.action == "attach"
    assert "74849" in plan.matched_on


def test_unmatched_export_becomes_an_archived_unit(tmp_path: Path) -> None:
    export = _export(tmp_path, "66599", "ParadoX Partners | BetonWin | 66599")

    (plan,) = build_plan([export], [_db_chat("Something else | 12345")])

    assert plan.action == "create-archived"
    assert plan.target is None
    assert plan.new_messages == 3


def test_active_row_wins_over_pending_and_abandoned(tmp_path: Path) -> None:
    """Attaching to the wrong duplicate would strand history in an unread unit."""
    export = _export(tmp_path, "80958", "80958| A22 | Betonwin | Gods Partners")
    chats = [
        _db_chat("80958| A22 | Betonwin | Gods Partners", status="abandoned"),
        _db_chat("80958| A22 | Betonwin | Gods Partners", status="active"),
        _db_chat("80958| A22 | Betonwin | Gods Partners", status="pending"),
    ]

    (plan,) = build_plan([export], chats)

    assert plan.target is not None
    assert plan.target.status == "active"
    assert plan.ambiguous is True
    assert len(plan.candidates) == 3


def test_busiest_row_wins_within_the_same_status(tmp_path: Path) -> None:
    export = _export(tmp_path, "76812", "76812 | Roiheads | Betwin.partners")
    chats = [
        _db_chat("76812 | Roiheads | Betwin.partners", messages=0),
        _db_chat("76812 | Roiheads | Betwin.partners", messages=57),
    ]

    (plan,) = build_plan([export], chats)

    assert plan.target is not None
    assert plan.target.message_count == 57


def test_duplicate_folders_import_once(tmp_path: Path) -> None:
    """77106 and 78284 are the same conversation; the title names 78284 first."""
    title = "78284 | (77106) | MetaForge | Betonwin"
    first = _export(tmp_path, "77106", title)
    second = _export(tmp_path, "78284", title)

    plans = {p.export.aff_id: p for p in build_plan([first, second], [])}

    assert plans["77106"].action == "skip-duplicate"
    assert plans["77106"].duplicate_of == "78284"
    assert plans["77106"].new_messages == 0
    assert plans["78284"].action == "create-archived"
    assert plans["78284"].new_messages == 3


def test_duplicate_keeper_is_stable_when_the_title_settles_nothing(tmp_path: Path) -> None:
    first = _export(tmp_path, "70000", "No ids in this title")
    second = _export(tmp_path, "60000", "No ids in this title")

    plans = {p.export.aff_id: p for p in build_plan([first, second], [])}

    # Lowest aff_id kept, so a re-run makes the same choice.
    assert plans["60000"].action == "create-archived"
    assert plans["70000"].action == "skip-duplicate"


def test_empty_folders_are_not_treated_as_duplicates(tmp_path: Path) -> None:
    """Two message-less exports hash alike but are different chats."""
    _write(tmp_path / "111", "Chat A | 111", "")
    _write(tmp_path / "222", "Chat B | 222", "")
    exports = parse_archive(tmp_path)

    plans = build_plan(exports, [])

    assert [p.action for p in plans] == ["create-archived", "create-archived"]


def test_collisions_are_counted_not_reinserted(tmp_path: Path) -> None:
    """The live window overlaps the archive, so a matched chat may already hold rows."""
    export = _export(tmp_path, "79732", "79732 | Q3 | Betonwin", count=5)
    chat = _db_chat("79732 | Q3 | Betonwin")

    (plan,) = build_plan([export], [chat], {chat.id: frozenset({1, 2})})

    assert plan.colliding == 2
    assert plan.new_messages == 3


def test_previously_imported_unit_is_reused_on_a_rerun(tmp_path: Path) -> None:
    """A second run must update the archived unit, not create a twin."""
    export = _export(tmp_path, "66599", "ParadoX Partners | BetonWin | 66599")
    existing = _db_chat("ParadoX Partners | BetonWin | 66599", status="archived", aff="66599")

    (plan,) = build_plan([export], [existing])

    assert plan.action == "attach"
    assert plan.target is not None
    assert plan.target.import_aff_id == "66599"


def test_summarise_accounts_for_every_parsed_message(tmp_path: Path) -> None:
    title = "78284 | (77106) | MetaForge | Betonwin"
    dup_a = _export(tmp_path, "77106", title)
    dup_b = _export(tmp_path, "78284", title)
    other = _export(tmp_path, "59743", "CGS | ID 59743 | BETONWIN", count=4)
    chat = _db_chat("CGS | ID 59743 | BETONWIN")

    plans = build_plan([dup_a, dup_b, other], [chat], {chat.id: frozenset({1})})
    totals = summarise(plans)

    parsed = sum(len(p.export.messages) for p in plans)
    skipped = sum(len(p.export.messages) for p in plans if p.action == "skip-duplicate")
    assert totals["messages_new"] + totals["messages_colliding"] + skipped == parsed
    assert totals["skip_duplicate"] == 1
    assert totals["attach"] == 1
    assert totals["create_archived"] == 1


def test_excluded_folders_are_skipped_by_name_not_by_size(tmp_path: Path) -> None:
    """Junk exports are named individually; there is deliberately no size threshold.

    A "fewer than N messages" rule would drop legitimately quiet partner chats
    silently, and these two would still have needed naming. The explicit list also
    surfaces in the dry-run report instead of vanishing into a cutoff.
    """
    # Distinct bodies, so neither is the other's content-hash duplicate.
    _write(tmp_path / "66570", "Advertiv | Betonwin | 66570",
           _msg(1, "01.03.2026 10:00:00 UTC+02:00", "A", "junk one")
           + _msg(2, "01.03.2026 10:01:00 UTC+02:00", "A", "junk two"))
    _write(tmp_path / "82370", "82370 | INCITE PARTNERS | BETONWIN",
           _msg(1, "01.03.2026 10:00:00 UTC+02:00", "B", "real one")
           + _msg(2, "01.03.2026 10:01:00 UTC+02:00", "B", "real two"))
    junk = parse_export_folder(tmp_path / "66570")
    small_but_real = parse_export_folder(tmp_path / "82370")

    plans = {p.export.aff_id: p for p in build_plan([junk, small_but_real], [])}

    assert plans["66570"].action == "skip-excluded"
    assert plans["66570"].new_messages == 0
    assert "no usable history" in (plans["66570"].excluded_reason or "")
    # Same message count, not on the list -> imported.
    assert plans["82370"].action == "create-archived"
    assert plans["82370"].new_messages == 2


def test_excluded_folders_are_named_in_the_registry() -> None:
    assert set(EXCLUDED_AFF_IDS) == {"66570", "80511"}
    assert all(reason.strip() for reason in EXCLUDED_AFF_IDS.values())


def test_summarise_keeps_excluded_messages_out_of_every_total(tmp_path: Path) -> None:
    junk = _export(tmp_path, "66570", "Advertiv | Betonwin | 66570", count=2)
    real = _export(tmp_path, "59743", "CGS | ID 59743 | BETONWIN", count=4)

    totals = summarise(build_plan([junk, real], []))

    assert totals["skip_excluded"] == 1
    assert totals["messages_new"] == 4
    assert totals["events"] == 0


def test_excluded_folder_never_becomes_a_duplicate_keeper(tmp_path: Path) -> None:
    """Otherwise both copies vanish — one skipped as a duplicate, one as excluded."""
    title = "Advertiv | Betonwin | 66570 | 82370"
    blocks = _msg(1, "01.03.2026 10:00:00 UTC+02:00", "A", "same content")
    _write(tmp_path / "66570", title, blocks)   # on the exclusion list
    _write(tmp_path / "82370", title, blocks)   # identical history, not excluded

    plans = {p.export.aff_id: p for p in build_plan(parse_archive(tmp_path), [])}

    assert plans["66570"].action == "skip-excluded"
    # The real folder still imports rather than being skipped against a dropped twin.
    assert plans["82370"].action == "create-archived"
    assert plans["82370"].new_messages == 1
