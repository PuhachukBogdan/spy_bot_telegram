"""The Phase 2 preview stand: collect the metrics and render them standalone.

Deliberately isolated from ``src/summary``. The live weekly/monthly report keeps
running exactly as it does today — this module reads the same database and writes
a page of its own, sharing no code path, no table, and no token with it. Nothing
here can change what the production report shows.

The page is rendered live on every request and stored nowhere, so there is no
snapshot to go stale and no publish step: reload the link and you see current
numbers. It is a review surface, not a deliverable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from html import escape
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from src.config import settings
from src.db.client import acquire_connection
from src.db.queries.etc import list_real_managers
from src.db.queries.metrics import (
    count_messages_per_chat,
    count_messages_per_chat_day,
    count_proposals_by_manager,
    count_proposals_per_day,
    list_risk_days,
    list_risk_events,
    list_sla_messages,
)
from src.metrics.attribution import build_manager_index
from src.metrics.cache import preview_cache
from src.metrics.collect import (
    ManagerMetrics,
    assemble,
    chats_by_manager,
    coverage_by_manager,
    pair_waits_dated,
    risks_by_manager,
)
from src.metrics.shell import render_with_shell
from src.metrics.trends import build_scope_days, build_scope_trends
from src.metrics.window import MetricsWindow, resolve_metrics_window
from src.metrics.workhours import EffectiveWorkHours, resolve_effective_work_hours

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
    return {
        "trends": trends,
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
        },
        "categories": _category_totals(metrics),
        "managers": [
            {
                "id": str(m.manager_id),
                "name": m.name,
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
    """
    totals: dict[str, int] = {}
    for manager in metrics:
        for risk in manager.risks:
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
) -> dict[str, Any]:
    """Compact per-chat day counts for client-side custom-range coverage.

    One entry per owned chat: its id (``i`` — what lets the dossier's chat table
    recompute per-period message counts), its manager (``m``), local creation day
    (``c`` — bounds the denominator so a chat born mid-range doesn't drag earlier
    ranges down), and a sparse ``{iso day: messages}`` map (``d``).
    """
    per_chat: dict[Any, dict[str, int]] = {}
    for row in chat_day_rows:
        per_chat.setdefault(row["chat_id"], {})[row["day"].isoformat()] = row[
            "messages"
        ]
    return {
        "chats": [
            {
                "i": str(row["chat_id"]),
                "m": str(row["manager_id"]),
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

    asyncpg serialises queries per connection; running the stand's eight reads
    on one connection means eight sequential round trips to a pooler an ocean
    away. Fanning out across the pool turns that into roughly the latency of the
    slowest single query.
    """
    async with acquire_connection() as conn:
        return await query(conn, *args)


async def build_preview(days: int = 30, *, fresh: bool = False) -> str:
    """Collect current metrics over the trailing ``days`` and render the stand.

    Two windows on purpose: the DETAIL window (``days``, feeds the server-side
    manager numbers and the no-shell fallback table) and the TREND horizon (120
    days, feeds the bucket series, the risk-case list and the per-chat day maps —
    everything the client re-filters by the selected period). One SLA pairing
    pass over the horizon serves both — the detail tally is the dated outcomes
    filtered to the window, so the two surfaces can never disagree about an
    outcome.

    The rendered page is TTL-cached (see :mod:`src.metrics.cache`): mode
    switching on the stand re-requests this page, and a review surface must feel
    instant rather than second-fresh. ``fresh=True`` bypasses the cache.

    Prefers the built React shell. Falls back to the plain server-rendered table
    when the frontend has not been built — a container built without the Node
    stage still serves working numbers instead of an error page.
    """
    cache_key = f"summary:{days}"
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
    async with acquire_connection() as conn:
        managers = await list_real_managers(conn)
    hours: dict[UUID, EffectiveWorkHours] = {
        m.id: resolve_effective_work_hours(m) for m in managers
    }
    manager_index = build_manager_index(managers)
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
        )

        dated = pair_waits_dated(sla_rows, hours)
        window_outcomes = {
            manager_id: [o for started, o in pairs if started >= window.since]
            for manager_id, pairs in dated.items()
        }
        metrics = assemble(
            managers,
            coverage=coverage_by_manager(chat_rows),
            sla_outcomes=window_outcomes,
            proposals=proposals,
            hours=hours,
            chats=chats_by_manager(chat_rows),
            risks=risks_by_manager(risk_rows, manager_index),
        )

        scopes = build_scope_days(
            [m.id for m in managers],
            sla_dated=dated,
            proposal_days=proposal_days,
            risk_days=risk_days,
            manager_index=manager_index,
            chat_day_rows=chat_day_rows,
            chat_registry=registry_rows,
            tz=tz,
        )
        today = until.astimezone(tz).date()
        floor = horizon.since.astimezone(tz).date()
        test_until = settings.METRICS_TEST_PERIOD_UNTIL
        trends = {
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
            "chatDays": _chat_days_payload(registry_rows, chat_day_rows, tz),
            "horizon": {
                "floor": floor.isoformat(),
                "today": today.isoformat(),
                "testUntil": test_until.isoformat() if test_until else None,
            },
        }

    rendered = render_with_shell(build_payload(metrics, window, trends=trends, tz=tz))
    page = (
        rendered
        if rendered is not None
        else render_preview(metrics, window, epoch=settings.METRICS_EPOCH_DATE)
    )
    preview_cache.put(cache_key, page)
    return page
