"""The one-off archive report.

A standalone HTML file rather than a row in ``summaries``. Writing it through the
normal report machinery would rotate the dashboard token and supersede the live
Slack report — the archive review is a separate artefact with its own audience, and
it must not disturb the weekly cadence. So: self-contained markup, no shared token,
nothing revoked.

Visual language follows the Signal Desk system (mono telemetry, severity spine,
ivory/control-room pair) but the CSS is inlined here, so the file survives being
emailed or dropped in Slack with no server behind it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from typing import Any
from uuid import UUID

import asyncpg

#: One finding row. Typed as a plain mapping rather than ``asyncpg.Record`` so the
#: renderer is a pure function testable without a database.
FindingRow = Mapping[str, Any]

#: Severity bands for presentation only — the retro contract has no risk_level.
_BANDS = ((80, "critical"), (60, "high"), (30, "medium"), (0, "low"))

_CSS = """
:root{--bg:#f7f5ef;--panel:#fffdf8;--ink:#16150f;--dim:#6b675a;--line:#dcd7c8;
--crit:#a8261f;--high:#b8621b;--med:#8a7520;--low:#5d6b52;--accent:#1f4f4a}
@media (prefers-color-scheme:dark){:root{--bg:#0f1113;--panel:#171a1d;--ink:#e8e6df;
--dim:#8f8d83;--line:#2b2f34;--crit:#e0574d;--high:#e0913f;--med:#cbb44e;
--low:#8fb07a;--accent:#5fd0c2}}
:root[data-theme=dark]{--bg:#0f1113;--panel:#171a1d;--ink:#e8e6df;--dim:#8f8d83;
--line:#2b2f34;--crit:#e0574d;--high:#e0913f;--med:#cbb44e;--low:#8fb07a;--accent:#5fd0c2}
:root[data-theme=light]{--bg:#f7f5ef;--panel:#fffdf8;--ink:#16150f;--dim:#6b675a;
--line:#dcd7c8;--crit:#a8261f;--high:#b8621b;--med:#8a7520;--low:#5d6b52;--accent:#1f4f4a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1060px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:26px;letter-spacing:-.02em;margin:0 0 4px}
.sub{color:var(--dim);font-size:13px;margin-bottom:28px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
font-variant-numeric:tabular-nums}
.strip{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:28px}
.tile{flex:1 1 150px;background:var(--panel);border:1px solid var(--line);
border-radius:8px;padding:12px 14px}
.tile .n{font-size:24px;font-weight:600}
.tile .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.1em;color:var(--dim);
margin:32px 0 12px;font-weight:600}
.cat{display:flex;align-items:center;gap:10px;margin:5px 0}
.cat .lbl{width:150px;font-size:13px}
.cat .bar{height:9px;border-radius:5px;background:var(--accent);min-width:3px}
.cat .v{font-size:12px;color:var(--dim)}
.f{background:var(--panel);border:1px solid var(--line);border-left-width:4px;
border-radius:8px;padding:14px 16px;margin-bottom:12px}
.f.critical{border-left-color:var(--crit)}.f.high{border-left-color:var(--high)}
.f.medium{border-left-color:var(--med)}.f.low{border-left-color:var(--low)}
.f .hd{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline;margin-bottom:8px}
.badge{font-size:10px;text-transform:uppercase;letter-spacing:.09em;padding:2px 7px;
border-radius:4px;border:1px solid currentColor;font-weight:600}
.badge.critical{color:var(--crit)}.badge.high{color:var(--high)}
.badge.medium{color:var(--med)}.badge.low{color:var(--low)}
.f .type{font-weight:600;font-size:14px}
.f .meta{color:var(--dim);font-size:12px;margin-left:auto}
.q{border-left:2px solid var(--line);padding:6px 0 6px 12px;margin:8px 0;
font-size:14px;white-space:pre-wrap;word-break:break-word}
.mk{font-size:12px;color:var(--dim)}
.mk b{color:var(--ink);font-weight:600}
.ex{font-size:13px;margin-top:8px}
.chat{font-size:12px;color:var(--dim);margin-top:8px;
border-top:1px dashed var(--line);padding-top:7px}
.empty{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:22px;color:var(--dim)}
.note{font-size:12px;color:var(--dim);margin-top:36px;border-top:1px solid var(--line);
padding-top:14px}
table{width:100%;border-collapse:collapse;font-size:13px}
.scroll{overflow-x:auto}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.07em}
"""


def _band(score: int) -> str:
    for threshold, name in _BANDS:
        if score >= threshold:
            return name
    return "low"


@dataclass(frozen=True)
class RunSummary:
    """Accounting for the run the report describes."""

    run_id: UUID
    model: str
    prompt_version: str
    windows: int
    messages_analysed: int
    chats: int
    tokens_in: int
    tokens_out: int
    cost_usd: float


async def load_findings(
    conn: asyncpg.Connection, run_id: UUID
) -> list[FindingRow]:
    """Findings for one run, most severe first, with chat and message context."""
    rows = await conn.fetch(
        """
        SELECT f.*, c.chat_name, m.message_text, m.telegram_message_id
        FROM archive_retro_findings f
        LEFT JOIN chats c    ON c.id = f.chat_id
        LEFT JOIN messages m ON m.id = f.message_id
        WHERE f.run_id = $1
        ORDER BY f.score DESC, f.occurred_at ASC
        """,
        run_id,
    )
    return [dict(row) for row in rows]


async def load_latest_run_id(conn: asyncpg.Connection) -> UUID | None:
    """The most recent retro run that completed at least one window.

    Drives the permanent link: it always shows the newest review rather than a run
    id baked into a URL. Keyed on ``archive_retro_progress`` rather than on findings,
    so a run that legitimately found nothing is still the current answer instead of
    falling back to an older, noisier one.
    """
    run_id: UUID | None = await conn.fetchval(
        """
        SELECT run_id FROM archive_retro_progress
        GROUP BY run_id
        ORDER BY max(created_at) DESC
        LIMIT 1
        """
    )
    return run_id


async def load_run_summary(
    conn: asyncpg.Connection, run_id: UUID
) -> RunSummary | None:
    """Aggregate the progress rows into the run's headline numbers."""
    row = await conn.fetchrow(
        """
        SELECT count(*) AS windows,
               count(DISTINCT chat_id) AS chats,
               COALESCE(sum(messages), 0)      AS messages_analysed,
               COALESCE(sum(input_tokens), 0)  AS tokens_in,
               COALESCE(sum(output_tokens), 0) AS tokens_out,
               COALESCE(sum(cost_usd), 0)      AS cost_usd
        FROM archive_retro_progress WHERE run_id = $1
        """,
        run_id,
    )
    if row is None or row["windows"] == 0:
        return None
    meta = await conn.fetchrow(
        "SELECT model, prompt_version FROM archive_retro_findings WHERE run_id = $1 LIMIT 1",
        run_id,
    )
    return RunSummary(
        run_id=run_id,
        model=str(meta["model"]) if meta else "—",
        prompt_version=str(meta["prompt_version"]) if meta else "—",
        windows=row["windows"],
        # Summed from the completed windows, not from the chats' totals: a run that
        # stopped at its budget must not claim coverage it never paid for.
        messages_analysed=int(row["messages_analysed"]),
        chats=row["chats"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        cost_usd=float(row["cost_usd"]),
    )


def _finding_html(row: FindingRow) -> str:
    band = _band(int(row["score"]))
    chat = escape(str(row["chat_name"] or row["aff_id"] or "—"))
    when = row["occurred_at"].strftime("%Y-%m-%d %H:%M")
    sender = escape(str(row["sender_name"] or "—"))
    role = escape(str(row["sender_role"] or "unknown"))
    return f"""
<div class="f {band}">
  <div class="hd">
    <span class="badge {band}">{band}</span>
    <span class="type">{escape(str(row['risk_type']))}</span>
    <span class="meta mono">score {row['score']} · conf {row['confidence']:.2f} · {when}</span>
  </div>
  <div class="q">{escape(str(row['quote']))}</div>
  <div class="mk">marker: <b>{escape(str(row['marker']))}</b></div>
  <div class="ex">{escape(str(row['explanation']))}</div>
  <div class="chat">{chat} · {sender} <span class="mono">[{role}]</span>
    · msg <span class="mono">{row['telegram_message_id'] or '—'}</span></div>
</div>"""


def render_report(summary: RunSummary | None, findings: Sequence[FindingRow]) -> str:
    """Self-contained HTML for one retro run."""
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    by_type: dict[str, int] = defaultdict(int)
    by_band: dict[str, int] = defaultdict(int)
    by_chat: dict[str, int] = defaultdict(int)
    for row in findings:
        by_type[str(row["risk_type"])] += 1
        by_band[_band(int(row["score"]))] += 1
        by_chat[str(row["chat_name"] or row["aff_id"] or "—")] += 1

    peak = max(by_type.values(), default=1)
    cats = "".join(
        f'<div class="cat"><span class="lbl">{escape(t)}</span>'
        f'<span class="bar" style="width:{max(3, round(n / peak * 420))}px"></span>'
        f'<span class="v mono">{n}</span></div>'
        for t, n in sorted(by_type.items(), key=lambda kv: -kv[1])
    )

    chat_rows = "".join(
        f"<tr><td>{escape(c)}</td><td class='mono'>{n}</td></tr>"
        for c, n in sorted(by_chat.items(), key=lambda kv: -kv[1])[:30]
    )

    if findings:
        body = "".join(_finding_html(row) for row in findings)
    else:
        body = (
            '<div class="empty">No findings met the bar. Every candidate either '
            "lacked an explicit concealment or bypass marker, or fell below the "
            "confidence floor. For a retrospective review that is a real result, "
            "not an error — the pass is built to stay silent rather than guess.</div>"
        )

    scope = (
        f"{summary.messages_analysed:,} imported messages across {summary.chats} chats · "
        f"{summary.windows} windows · {summary.model} · prompt {summary.prompt_version}"
        if summary
        else "no completed windows recorded for this run"
    )
    spend = (
        f'<div class="tile"><div class="n mono">${summary.cost_usd:.2f}</div>'
        f'<div class="k">analysis cost</div></div>'
        f'<div class="tile"><div class="n mono">{summary.tokens_in / 1000:.0f}k</div>'
        f'<div class="k">input tokens</div></div>'
        if summary
        else ""
    )

    # A full document shell, not a fragment: this is served over HTTP on the
    # permanent link, and without the doctype browsers fall into quirks mode, where
    # `box-sizing: border-box` is ignored and the layout collapses.
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Archive risk review</title>
<style>{_CSS}</style></head><body>
<div class="wrap">
  <h1>Archive risk review</h1>
  <div class="sub">One-off retrospective pass over imported partner-chat history.
    Generated {generated}.<br>{escape(scope)}</div>

  <div class="strip">
    <div class="tile"><div class="n mono">{len(findings)}</div>
      <div class="k">findings</div></div>
    <div class="tile"><div class="n mono">{by_band['critical']}</div>
      <div class="k">critical</div></div>
    <div class="tile"><div class="n mono">{by_band['high']}</div>
      <div class="k">high</div></div>
    <div class="tile"><div class="n mono">{len(by_chat)}</div>
      <div class="k">chats affected</div></div>
    {spend}
  </div>

  {'<h2>By category</h2>' + cats if cats else ''}

  <h2>Findings</h2>
  {body}

  {'<h2>Chats by finding count</h2><div class="scroll"><table><tr><th>Chat</th>'
   '<th>Findings</th></tr>' + chat_rows + '</table></div>' if chat_rows else ''}

  <div class="note">
    Every finding above quotes the concealment, bypass, or off-record language it
    rests on; anything that could not quote such a marker was not reported. The pass
    is calibrated against the live system's reviewed alerts — 14 human-confirmed and
    24 human-rejected — with the rejected patterns (bare payout hashes, a staff
    farewell read as partner churn, ordinary rate negotiation, routine traffic talk,
    a chat move with no concealment reason) excluded by rule.
    <br><br>
    These findings live in <span class="mono">archive_retro_findings</span>, separate
    from <span class="mono">risk_events</span>: they never enter the weekly report,
    the Slack dashboard, or the alert path. This page is served on its own permanent
    link and always reflects the newest completed review.
  </div>
</div>
</body></html>"""
