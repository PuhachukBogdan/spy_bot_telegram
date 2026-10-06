"""The Team summary page: collect the manager metrics and render them standalone.

Born as the Phase 2 preview stand; released to production on 2026-08-25 — the
same page now fronts the live ``/dashboard`` (with the classic risk report as
its second mode). Deliberately isolated from ``src.summary``: it reads the same
database but shares no code path and no table with the stored weekly/monthly
reports, so it cannot alter what an issued ``/r/{token}`` snapshot shows.

The page is rendered live on every request and stored nowhere, so there is no
snapshot to go stale and no publish step: reload the link and you see current
numbers.

Since 2026-10 a chat belongs to every manager PRESENT in it (``chat_members``),
not to the one who added the bot; see :mod:`src.metrics.membership` for the
rule and :mod:`src.metrics.accounts` for the old/new account split.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from html import escape
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from src.config import settings
from src.db.client import acquire_connection
from src.db.queries.chat_members import first_seen_by_account, list_present_memberships
from src.db.queries.etc import list_real_managers
from src.db.queries.metrics import (
    count_messages_per_chat,
    count_messages_per_chat_day,
    count_messages_per_sender_day,
    count_proposals_by_manager,
    count_proposals_per_day,
    count_proposals_per_sender_day,
    list_risk_days,
    list_risk_events,
    list_sla_messages,
)
from src.db.queries.tone import list_tone_days, list_tone_flags
from src.metrics.accounts import (
    AccountStats,
    account_days_payload,
    account_stats,
    label_accounts,
)
from src.metrics.attribution import build_manager_index
from src.metrics.cache import preview_cache
from src.metrics.collect import (
    ManagerMetrics,
    WaitOutcome,
    assemble,
    by_manager,
    chats_by_manager,
    coverage_by_manager,
    pair_waits_all,
    risks_by_manager,
)
from src.metrics.membership import Crews, build_crews, fan_out_by_portfolio, local_day
from src.metrics.scope import (
    ADMIN_SCOPE,
    PageScope,
    visible_managers,
    visible_memberships,
    visible_risk_rows,
    visible_rows,
    visible_sender_rows,
    visible_tone,
)
from src.metrics.shell import render_with_shell
from src.metrics.tone import (
    metric_defs_payload,
    tone_days_payload,
    tone_flags_payload,
)
from src.metrics.trends import (
    account_key,
    build_account_scope_days,
    build_scope_days,
    build_scope_trends,
)
from src.metrics.window import MetricsWindow, resolve_metrics_window
from src.metrics.workhours import (
    EffectiveWorkHours,
    default_work_hours,
    resolve_effective_work_hours,
)
from src.utils.logging import get_logger

log = get_logger(__name__)

_CSS = """
:root{--ink:#1A1A1A;--muted:#5C5C5C;--rule:#C9C4BA;--surface:#ECE9E2;
--accent:#1E4D6B;--warn:#A33A2A;--ok:#2C6E4F;--paper:#F6F4EF}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:15px/1.5 "IBM Plex Sans","Segoe UI",system-ui,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:32px 24px 64px}
h1{font:600 26px/1.2 Archivo,"Segoe UI",sans-serif;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:8px}
.flag{display:inline-block;margin:14px 0 28px;padding:8px 12px;border-left:3px solid var(--accent);
background:var(--surface);font-size:13px}
h2{font:600 15px/1.2 Archivo,sans-serif;margin:34px 0 10px;
text-transform:uppercase;letter-spacing:.08em;color:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:14px;background:#fff}
th{background:var(--accent);color:#fff;font-weight:600;text-align:left;font-size:12px;
text-transform:uppercase;letter-spacing:.05em}
th,td{padding:9px 11px;border:1px solid var(--rule);vertical-align:top}
tr:nth-child(even) td{background:var(--surface)}
.mono{font-family:"IBM Plex Mono",Consolas,monospace}
.num{text-align:right}
.big{font-size:19px;font-weight:600}
.bar{position:relative;height:7px;background:var(--rule);border-radius:4px;
overflow:hidden;margin-top:5px;min-width:90px}
.bar>i{position:absolute;inset:0 auto 0 0;background:var(--accent);border-radius:4px}
.tag{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10.5px;
text-transform:uppercase;letter-spacing:.05em;font-family:"IBM Plex Mono",monospace}
.tag.assumed{background:#F0E2C8;color:#7A5A20}
.tag.personal{background:#DCEBE0;color:var(--ok)}
.none{color:var(--muted);font-style:italic}
.warn{color:var(--warn)}
footer{margin-top:44px;padding-top:14px;border-top:1px solid var(--rule);
color:var(--muted);font-size:12px}
"""


def _pct(value: float | None) -> str:
    """A percentage, or an explicit dash — never a misleading 0."""
    return f"{value:g}%" if value is not None else '<span class="none">—</span>'


def _bar(value: float | None) -> str:
    return f'<div class="bar"><i style="width:{value:g}%"></i></div>' if value else ""


def _hours_tag(hours: EffectiveWorkHours | None) -> str:
    if hours is None:
        return '<span class="none">—</span>'
    window = f"{hours.hours.start:%H:%M}–{hours.hours.end:%H:%M}"
    label = "assumed" if hours.is_assumed else "personal"
    return (
        f'<span class="mono">{escape(window)}</span> '
        f'<span class="tag {label}">{label}</span><br>'
        f'<span class="mono" style="font-size:11px;color:var(--muted)">'
        f"{escape(hours.hours.timezone)}</span>"
    )


def _row(m: ManagerMetrics) -> str:
    sla = m.sla
    return (
        "<tr>"
        f"<td><b>{escape(m.name)}</b></td>"
        f'<td class="num"><span class="big mono">{_pct(sla.percent)}</span>'
        f"{_bar(sla.percent)}"
        f'<div class="mono" style="font-size:11px;color:var(--muted)">'
        f"{sla.met}+{sla.met_substantive} / {sla.rated}</div></td>"
        f'<td class="num mono {"warn" if sla.offline else ""}">{sla.offline}</td>'
        f'<td class="num"><span class="big mono">{_pct(m.coverage.percent)}</span>'
        f"{_bar(m.coverage.percent)}"
        f'<div class="mono" style="font-size:11px;color:var(--muted)">'
        f"{m.coverage.active} / {m.coverage.total}</div></td>"
        f'<td class="num mono">{m.proposals}</td>'
        f"<td>{_hours_tag(m.work_hours)}</td>"
        "</tr>"
    )


def render_preview(
    metrics: list[ManagerMetrics], window: MetricsWindow, *, epoch: date | None
) -> str:
    """Render the stand. Pure — takes numbers, returns HTML."""
    rows = "".join(_row(m) for m in metrics) or (
        '<tr><td colspan="6" class="none">No managers resolved.</td></tr>'
    )
    epoch_note = (
        f"counting from {epoch:%Y-%m-%d}"
        if epoch is not None
        else '<span class="warn">METRICS_EPOCH_DATE is unset — no floor applied</span>'
    )
    comparison = (
        f"{window.previous[0]:%Y-%m-%d} → {window.previous[1]:%Y-%m-%d}"
        if window.previous is not None
        else '<span class="none">no comparable previous period</span>'
    )
    return f"""<!doctype html>
<html data-theme="light" lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phase 2 preview</title><style>{_CSS}</style></head><body><div class="wrap">
<h1>Phase 2 — manager metrics</h1>
<div class="sub mono">{window.since:%Y-%m-%d %H:%M} → {window.until:%Y-%m-%d %H:%M} UTC
 · {epoch_note}</div>
<div class="flag"><b>Preview stand.</b> Separate link, rendered live on each request.
Stores nothing, posts nothing to Slack, and shares no code or tables with the live
weekly/monthly report — that report is untouched and keeps running as before.</div>

<h2>Managers</h2>
<table><thead><tr>
<th>Manager</th><th class="num">SLA</th><th class="num">Offline</th>
<th class="num">Active chats</th><th class="num">Proposals</th><th>Work hours</th>
</tr></thead><tbody>{rows}</tbody></table>

<h2>How to read this</h2>
<table><tbody>
<tr><td><b>SLA</b></td><td>Replies inside {settings.SLA_RESPONSE_THRESHOLD_SECONDS // 60}
 min, plus substantial replies (&gt;{settings.SLA_SUBSTANTIVE_REPLY_CHARS} chars) inside
 {settings.SLA_SUBSTANTIVE_GRACE_SECONDS // 60} min. Timers only start during working
 hours — never at night, weekends or holidays. A dash means no partner messages waited
 in this window, which is not a failure.</td></tr>
<tr><td><b>Offline</b></td><td>Waits with no reply for
 {settings.SLA_OFFLINE_AFTER_SECONDS // 60} min. Counted separately and deliberately
 kept OUT of the SLA %: absence is not slowness, and averaging it in would hide it.</td></tr>
<tr><td><b>Active chats</b></td><td>Chats with at least
 {settings.ACTIVE_CHAT_MIN_MESSAGES} messages in the window, over the manager's whole
 portfolio. Silent chats stay in the denominator.</td></tr>
<tr><td><b>Work hours</b></td><td><span class="tag personal">personal</span> set by the
 manager via /set_hours · <span class="tag assumed">assumed</span> the configured default,
 so that row's SLA rests on a schedule nobody confirmed.</td></tr>
</tbody></table>

<footer>Comparison base: {comparison}. Imported archive messages are excluded
everywhere (<span class="mono">source &lt;&gt; 'imported'</span>).</footer>
</div></body></html>"""


def build_payload(
    metrics: list[ManagerMetrics],
    window: MetricsWindow,
    *,
    trends: dict[str, Any] | None = None,
    tz: ZoneInfo | None = None,
    tone: dict[str, Any] | None = None,
    scope: PageScope = ADMIN_SCOPE,
    accounts: dict[UUID, list[AccountStats]] | None = None,
    deactivated: dict[UUID, tuple[datetime, str | None]] | None = None,
) -> dict[str, Any]:
    """The metrics document handed to the React shell.

    Shape must match ``ReportData`` in ``frontend/src/data.ts``. Percentages stay
    nullable all the way through: ``None`` means "nothing was rated", which is not
    the same fact as zero and must not be flattened into one on the way out.

    ``tz`` keys each risk's ``day`` — the same local calendar the trend buckets
    live in, so the client's period filter and the bucket counters can never
    disagree about which day a case belongs to.
    """
    risk_tz = tz if tz is not None else UTC
    accounts = accounts or {}
    deactivated = deactivated or {}
    return {
        "trends": trends,
        # Who is reading. Drives the mode switch and the "signed in as" line; the
        # data itself was already filtered upstream, so this block only labels
        # the page — removing it would hide nothing extra.
        "viewer": scope.to_payload(),
        # Tone of voice (§18): metric registry + per-manager-day counters over the
        # horizon + the accepted flags. None when the tables are absent or the
        # read failed — the page then simply has no tone block.
        "tone": tone,
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "since": window.since.isoformat(timespec="minutes"),
        "until": window.until.isoformat(timespec="minutes"),
        "previous": (
            {
                "since": window.previous[0].isoformat(timespec="minutes"),
                "until": window.previous[1].isoformat(timespec="minutes"),
            }
            if window.previous is not None
            else None
        ),
        "epoch": (
            settings.METRICS_EPOCH_DATE.isoformat()
            if settings.METRICS_EPOCH_DATE is not None
            else None
        ),
        "thresholds": {
            "slaSeconds": settings.SLA_RESPONSE_THRESHOLD_SECONDS,
            "graceSeconds": settings.SLA_SUBSTANTIVE_GRACE_SECONDS,
            "substantiveChars": settings.SLA_SUBSTANTIVE_REPLY_CHARS,
            "offlineSeconds": settings.SLA_OFFLINE_AFTER_SECONDS,
            "activeChatMinMessages": settings.ACTIVE_CHAT_MIN_MESSAGES,
            "crewLookbackDays": settings.METRICS_CREW_LOOKBACK_DAYS,
        },
        "categories": _category_totals(metrics),
        "managers": [
            {
                "id": str(m.manager_id),
                "name": m.name,
                "deactivatedAt": (
                    deactivated[m.manager_id][0].astimezone(risk_tz).date().isoformat()
                    if m.manager_id in deactivated
                    else None
                ),
                "deactivationNote": (
                    deactivated[m.manager_id][1] if m.manager_id in deactivated else None
                ),
                "accounts": [a.to_payload() for a in accounts.get(m.manager_id, [])],
                "slaPercent": m.sla.percent,
                "slaMet": m.sla.met + m.sla.met_substantive,
                "slaRated": m.sla.rated,
                "slaOffline": m.sla.offline,
                "coveragePercent": m.coverage.percent,
                "chatsActive": m.coverage.active,
                "chatsTotal": m.coverage.total,
                "proposals": m.proposals,
                "workHours": (
                    {
                        "start": f"{m.work_hours.hours.start:%H:%M}",
                        "end": f"{m.work_hours.hours.end:%H:%M}",
                        "timezone": m.work_hours.hours.timezone,
                        "assumed": m.work_hours.is_assumed,
                    }
                    if m.work_hours is not None
                    else None
                ),
                "risksOwn": sum(1 for r in m.risks if r.counts),
                "risksContext": sum(1 for r in m.risks if not r.counts),
                "chats": [
                    {
                        "id": str(c.chat_id),
                        "name": c.name,
                        "unitType": c.unit_type,
                        "messages": c.messages,
                        "active": c.active,
                    }
                    for c in m.chats
                ],
                "risks": [
                    {
                        "id": str(r.risk_id),
                        "chatName": r.chat_name,
                        "unitType": r.unit_type,
                        "riskType": r.risk_type,
                        "riskLevel": r.risk_level,
                        "score": r.score,
                        "detectedAt": r.detected_at.isoformat(timespec="minutes"),
                        "day": r.detected_at.astimezone(risk_tz).date().isoformat(),
                        "phrase": r.phrase,
                        "why": r.why,
                        "attribution": r.attribution.value,
                        "counts": r.counts,
                        "senderAccount": (
                            str(r.sender_id) if r.sender_id is not None else None
                        ),
                    }
                    for r in m.risks
                ],
            }
            for m in metrics
        ],
    }


def _category_totals(metrics: list[ManagerMetrics]) -> list[dict[str, Any]]:
    """Risk counts by category across everyone, biggest first.

    Counts every case, context included — the overview answers "what is happening
    across the business", which is a different question from "what did this
    manager do". The per-manager split into own/context lives on the dossier.
    A case shown on several pages (context for a whole crew) is counted ONCE.
    """
    totals: dict[str, int] = {}
    seen: set[UUID] = set()
    for manager in metrics:
        for risk in manager.risks:
            if risk.risk_id in seen:
                continue
            seen.add(risk.risk_id)
            totals[risk.risk_type] = totals.get(risk.risk_type, 0) + 1
    return [
        {"type": risk_type, "count": count}
        for risk_type, count in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


#: How far back the trend series may reach. Bounded by retention: pg_cron purges
#: messages older than 120 days, so anything message-derived beyond that horizon
#: simply does not exist any more (spec §11.5 — the durable fix is the
#: metrics_daily rollup, phase G4).
TREND_HORIZON_DAYS = 120


def _report_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.REPORT_TIMEZONE)
    except (KeyError, ValueError):  # pragma: no cover — validated at deploy
        return ZoneInfo("UTC")


def _chat_days_payload(
    registry_rows: list[dict[str, Any]],
    chat_day_rows: list[dict[str, Any]],
    tz: ZoneInfo,
    crews: Crews | None = None,
) -> dict[str, Any]:
    """Compact per-chat day counts for client-side custom-range coverage.

    One entry per chat: its id (``i`` — what lets the dossier's chat table
    recompute per-period message counts), the managers it belongs to (``ms`` —
    everyone present, so one chat may sit in several portfolios; the team
    counts the entry once), local creation day (``c`` — bounds the denominator
    so a chat born mid-range doesn't drag earlier ranges down), and a sparse
    ``{iso day: messages}`` map (``d``).
    """
    per_chat: dict[Any, dict[str, int]] = {}
    for row in chat_day_rows:
        per_chat.setdefault(row["chat_id"], {})[row["day"].isoformat()] = row[
            "messages"
        ]

    def managers_of(row: dict[str, Any]) -> list[str]:
        if crews is not None:
            # Holders = the managers present AND their present accounts
            # (``acct:<id>``), so one filter serves the person view and the
            # Old / New account view alike.
            return sorted(str(m) for m in crews.members_of(row["chat_id"])) + sorted(
                account_key(a) for a in crews.present_accounts.get(row["chat_id"], ())
            )
        owner = row.get("owner_id", row.get("manager_id"))
        return [str(owner)] if owner is not None else []

    return {
        "chats": [
            {
                "i": str(row["chat_id"]),
                "ms": managers_of(row),
                "c": row["created_at"].astimezone(tz).date().isoformat(),
                "d": per_chat.get(row["chat_id"], {}),
            }
            for row in registry_rows
        ]
    }


async def _on_own_connection(
    query: Callable[..., Awaitable[Any]], *args: Any
) -> Any:
    """Run one query on its own pooled connection, so a batch can gather.

    asyncpg serialises queries per connection; running the stand's reads on one
    connection means that many sequential round trips to a pooler an ocean
    away. Fanning out across the pool turns that into roughly the latency of the
    slowest single query.
    """
    async with acquire_connection() as conn:
        return await query(conn, *args)


#: Flags shipped to the dossier's review list per page — the list is a folded
#: drill-down, not an archive; the counters carry the numbers.
_TONE_FLAGS_LIMIT = 400


async def _load_tone(floor: date, today: date, tz: ZoneInfo) -> dict[str, Any] | None:
    """Tone-of-voice block for the island, or ``None`` when it cannot be read.

    Failing soft is deliberate: the tables arrive with migration 0025, and a page
    served before it is applied (or during a partial deploy) must still render
    SLA, coverage and risk rather than 500 on a block that is additive.
    """

    async def _flags(conn: Any, since: date, until: date) -> Any:
        return await list_tone_flags(conn, since, until, limit=_TONE_FLAGS_LIMIT)

    try:
        day_rows, flag_rows = await asyncio.gather(
            _on_own_connection(list_tone_days, floor, today),
            _on_own_connection(_flags, floor, today),
        )
    except asyncpg.PostgresError as exc:
        log.warning("tone.payload_unavailable", error=str(exc))
        return None
    return {
        "enabled": settings.TONE_ANALYSIS_ENABLED,
        "minAssessed": settings.TONE_MIN_ASSESSED,
        "metrics": metric_defs_payload(),
        "days": tone_days_payload(day_rows),
        "flags": tone_flags_payload(flag_rows, tz),
    }


async def _load_memberships() -> list[dict[str, Any]]:
    """``chat_members`` presence, or an empty list before migration 0027 exists.

    Failing soft keeps the page up during the deploy window; with no membership
    the crew model falls back to chat owners for every chat, which is exactly
    the pre-0027 behaviour.
    """
    try:
        rows: list[dict[str, Any]] = await _on_own_connection(list_present_memberships)
        return rows
    except asyncpg.PostgresError as exc:
        log.warning("membership.payload_unavailable", error=str(exc))
        return []


async def _load_first_seen() -> dict[int, datetime]:
    try:
        seen: dict[int, datetime] = await _on_own_connection(first_seen_by_account)
        return seen
    except asyncpg.PostgresError as exc:
        log.warning("membership.first_seen_unavailable", error=str(exc))
        return {}


async def build_preview(
    days: int = 30,
    *,
    fresh: bool = False,
    include_tone: bool = False,
    scope: PageScope = ADMIN_SCOPE,
) -> str:
    """Collect current metrics over the trailing ``days`` and render the stand.

    Two windows on purpose: the DETAIL window (``days``, feeds the server-side
    manager numbers and the no-shell fallback table) and the TREND horizon (120
    days, feeds the bucket series, the risk-case list and the per-chat day maps —
    everything the client re-filters by the selected period). One SLA pairing
    pass over the horizon serves both — the detail tally is the dated outcomes
    filtered to the window, so the two surfaces can never disagree about an
    outcome.

    The rendered page is TTL-cached (see :mod:`src.metrics.cache`): mode
    switching re-requests this page, and a review surface must feel instant
    rather than second-fresh. ``fresh=True`` bypasses the cache.

    Prefers the built React shell. Falls back to the plain server-rendered table
    when the frontend has not been built — a container built without the Node
    stage still serves working numbers instead of an error page.

    ``include_tone`` adds the tone-of-voice block (section 18).

    ``scope`` decides WHOSE page this is (:mod:`src.metrics.scope`). A head's
    page is built without them in it — the exclusion happens on the raw rows,
    before any total is summed, so the team numbers a head reads are genuinely
    the team minus themselves rather than the full team with one row hidden.
    """
    # Cache slots are per surface AND per viewer: the tone block is route-gated,
    # and two roles see different pages from the same query set.
    cache_key = f"summary:{days}:{'tone' if include_tone else 'base'}:{scope.cache_key}"
    if not fresh:
        cached = preview_cache.get(cache_key)
        if cached is not None:
            return cached

    until = datetime.now(UTC)
    window = resolve_metrics_window(
        until - timedelta(days=days), until, epoch=settings.METRICS_EPOCH_DATE
    )
    horizon = resolve_metrics_window(
        until - timedelta(days=TREND_HORIZON_DAYS),
        until,
        epoch=settings.METRICS_EPOCH_DATE,
    )
    tz = _report_tz()

    metrics: list[ManagerMetrics] = []
    trends: dict[str, Any] | None = None
    tone: dict[str, Any] | None = None
    accounts: dict[UUID, list[AccountStats]] = {}
    async with acquire_connection() as conn:
        all_managers = await list_real_managers(conn)
    # The attribution index spans the WHOLE team on purpose: whether a case was
    # written by a manager at all is a fact about the case, not about who is
    # reading it. Scoping happens on the rows below, never on this index.
    manager_index = build_manager_index(all_managers)
    managers = visible_managers(all_managers, scope)
    roster_ids = [m.id for m in managers]
    staff_ids = sorted({tg for m in all_managers for tg in m.telegram_accounts})
    hours: dict[UUID, EffectiveWorkHours] = {
        m.id: resolve_effective_work_hours(m) for m in managers
    }
    deactivated_at = {
        m.id: (m.deactivated_at, m.deactivation_note)
        for m in managers
        if m.deactivated_at is not None
    }
    deactivated_days = {
        mid: local_day(when, tz) for mid, (when, _) in deactivated_at.items()
    }
    if not horizon.is_empty:
        tz_name = str(tz)
        (
            sla_rows,
            chat_rows,
            proposals,
            risk_rows,
            chat_day_rows,
            proposal_days,
            risk_days,
            # The chat registry (with created_at) must span the horizon too, so
            # past buckets know which chats already existed back then.
            registry_rows,
            membership_rows,
            sender_day_rows,
            first_seen,
            proposal_sender_days,
        ) = await asyncio.gather(
            _on_own_connection(list_sla_messages, horizon.since, horizon.until),
            _on_own_connection(count_messages_per_chat, window.since, window.until),
            _on_own_connection(
                count_proposals_by_manager, window.since, window.until
            ),
            # Risks span the HORIZON, not the detail window: the client filters
            # the list by the selected period (day/week/month/quarter/custom),
            # and a quarter reaches far past the 30-day window.
            _on_own_connection(list_risk_events, horizon.since, horizon.until),
            _on_own_connection(
                count_messages_per_chat_day, horizon.since, horizon.until, tz_name
            ),
            _on_own_connection(
                count_proposals_per_day, horizon.since, horizon.until, tz_name
            ),
            _on_own_connection(
                list_risk_days, horizon.since, horizon.until, tz_name
            ),
            _on_own_connection(
                count_messages_per_chat, horizon.since, horizon.until
            ),
            _load_memberships(),
            _on_own_connection(
                count_messages_per_sender_day,
                horizon.since,
                horizon.until,
                tz_name,
                staff_ids,
            ),
            _load_first_seen(),
            _on_own_connection(
                count_proposals_per_sender_day, horizon.since, horizon.until, tz_name
            ),
        )

        # Scope every person-keyed row set before a single number is derived
        # from it. The crews and the team series are built from these same
        # rows, so filtering afterwards would leave a hidden manager inside
        # every total.
        membership_rows = visible_memberships(membership_rows, scope)
        sender_day_rows = visible_sender_rows(sender_day_rows, scope)
        proposal_sender_days = visible_sender_rows(proposal_sender_days, scope)
        proposal_days = visible_rows(proposal_days, scope)
        risk_days = visible_risk_rows(risk_days, scope)
        risk_rows = visible_risk_rows(risk_rows, scope)
        proposals = {
            manager_id: count
            for manager_id, count in proposals.items()
            if manager_id not in scope.hidden_manager_ids
        }

        crews = build_crews(
            membership_rows,
            sender_day_rows,
            registry_rows,
            manager_index=manager_index,
            roster=roster_ids,
            deactivated=deactivated_days,
            lookback_days=settings.METRICS_CREW_LOOKBACK_DAYS,
        )
        waits: list[WaitOutcome] = pair_waits_all(
            sla_rows,
            hours,
            crews=crews,
            manager_index=manager_index,
            tz=tz,
            default_hours=default_work_hours(),
            hidden_accounts=scope.hidden_telegram_ids,
        )
        dated = by_manager(waits)
        window_outcomes = {
            manager_id: [o for started, o in pairs if started >= window.since]
            for manager_id, pairs in dated.items()
        }
        # The detail window's portfolio is "present now, still active": a person
        # deactivated before the window closed has no chats in it.
        today = until.astimezone(tz).date()
        detail_rows = [
            row
            for row in fan_out_by_portfolio(chat_rows, crews)
            if crews.is_active_on(row["manager_id"], today)
        ]
        metrics = assemble(
            managers,
            coverage=coverage_by_manager(detail_rows),
            sla_outcomes=window_outcomes,
            proposals=proposals,
            hours=hours,
            chats=chats_by_manager(detail_rows),
            risks=risks_by_manager(risk_rows, manager_index, crews=crews, tz=tz),
        )

        scopes = build_scope_days(
            roster_ids,
            waits=waits,
            proposal_days=proposal_days,
            risk_days=risk_days,
            manager_index=manager_index,
            chat_day_rows=chat_day_rows,
            chat_registry=registry_rows,
            tz=tz,
            crews=crews,
            deactivated=deactivated_days,
        )
        floor = horizon.since.astimezone(tz).date()
        test_until = settings.METRICS_TEST_PERIOD_UNTIL
        labels_by_manager = {
            m.id: label_accounts(m.telegram_accounts, m.account_labels, first_seen)
            for m in managers
        }
        accounts = {
            m.id: account_stats(
                m.telegram_accounts,
                labels=labels_by_manager[m.id],
                sender_day_rows=sender_day_rows,
                waits=waits,
                crews=crews,
                since=window.since,
                tz=tz,
            )
            for m in managers
        }
        # Old / New: one scope per account of every person who has more than
        # one. Same trend builder as the person, so the account view is the
        # same page with fewer facts in it, never a second formula.
        multi = {
            account: m.id
            for m in managers
            if len(m.telegram_accounts) > 1
            for account in m.telegram_accounts
        }
        account_scopes = build_account_scope_days(
            multi,
            waits=waits,
            proposal_sender_days=proposal_sender_days,
            risk_days=risk_days,
            chat_day_rows=chat_day_rows,
            chat_registry=registry_rows,
            crews=crews,
            tz=tz,
            deactivated=deactivated_days,
        )
        trends = {
            "accounts": {
                key: build_scope_trends(
                    scope_days, today=today, floor=floor, test_until=test_until
                )
                for key, scope_days in account_scopes.items()
            },
            "team": build_scope_trends(
                scopes[None], today=today, floor=floor, test_until=test_until
            ),
            "managers": {
                str(m.id): build_scope_trends(
                    scopes[m.id], today=today, floor=floor, test_until=test_until
                )
                for m in managers
            },
            # Per-chat day counts + registry: what lets the client compute an
            # ARBITRARY date range exactly — counters sum, and coverage re-runs
            # the same threshold formula the server uses, instead of averaging
            # daily percentages (which would lie).
            "chatDays": _chat_days_payload(registry_rows, chat_day_rows, tz, crews),
            # Per-account day maps: messages, replies and on-time replies per
            # local day, so the old/new split follows the selected period too.
            "accountDays": account_days_payload(
                managers,
                labels_by_manager=labels_by_manager,
                sender_day_rows=sender_day_rows,
                waits=waits,
                tz=tz,
            ),
            "horizon": {
                "floor": floor.isoformat(),
                "today": today.isoformat(),
                "testUntil": test_until.isoformat() if test_until else None,
            },
        }
        if include_tone:
            tone = visible_tone(await _load_tone(floor, today, tz), scope)

    rendered = render_with_shell(
        build_payload(
            metrics,
            window,
            trends=trends,
            tz=tz,
            tone=tone,
            scope=scope,
            accounts=accounts,
            deactivated=deactivated_at,
        )
    )
    page = (
        rendered
        if rendered is not None
        else render_preview(metrics, window, epoch=settings.METRICS_EPOCH_DATE)
    )
    preview_cache.put(cache_key, page)
    return page
