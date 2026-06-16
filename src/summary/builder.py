"""HTML report builder for weekly/monthly manager-centric summaries. Phase 16.

Produces a single self-contained HTML page:
  - Header with period dates and generation time
  - Navigation TOC (jump links per manager)
  - Heat-map table: managers × 13 risk categories
  - Per-manager timeline sections with color-coded event cards

No external CSS or JS — the output is a standalone file safe to serve directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from jinja2 import Environment

# ---------------------------------------------------------------------------
# Risk category registry (canonical order for heat-map columns)
# ---------------------------------------------------------------------------

RISK_CATEGORIES: list[tuple[str, str]] = [
    ("shadow_deal", "Shadow Deal"),
    ("private_channel", "Private Channel"),
    ("hidden_payment", "Hidden Payment"),
    ("traffic_leakage", "Traffic Leakage"),
    ("commercial_terms", "Commercial Terms"),
    ("fraud_shave", "Fraud / Shave"),
    ("access_risk", "Access Risk"),
    ("partner_churn", "Partner Churn"),
    ("payment_conflict", "Payment Conflict"),
    ("reputation_risk", "Reputation Risk"),
    ("operational_sla", "Operational SLA"),
    ("employee_behavior", "Employee Behavior"),
    ("data_leak", "Data Leak"),
]

_RISK_TYPE_LABELS: dict[str, str] = dict(RISK_CATEGORIES)


def _risk_type_label(risk_type: str) -> str:
    return _RISK_TYPE_LABELS.get(risk_type, risk_type.replace("_", " ").title())


def _manager_label(mgr: dict[str, Any]) -> str:
    """Format manager display name: "{aff_id} | @{tg_username}" or either alone.

    tg_username is stored without @; we add it here.
    """
    aff = (mgr.get("aff_id") or "").strip()
    tg = (mgr.get("tg_username") or "").strip().lstrip("@")
    if aff and tg:
        return f"{aff} | @{tg}"
    if aff:
        return aff
    return f"@{tg}"


# ---------------------------------------------------------------------------
# Builder data structures (passed directly into the Jinja2 template context)
# ---------------------------------------------------------------------------


@dataclass
class EventData:
    risk_level: str
    partner_name: str
    risk_type: str
    risk_type_label: str
    final_score: int
    detected_phrase: str | None
    llm_explanation: str | None
    status: str
    date_str: str


@dataclass
class ManagerData:
    id: str          # UUID as string — used in HTML id="mgr-{id}" anchors
    name: str
    events: list[EventData]
    heatmap: dict[str, int]   # risk_type → count
    total: int
    critical_count: int


# ---------------------------------------------------------------------------
# HTML template (inline; autoescape=True for XSS safety)
# ---------------------------------------------------------------------------

_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<title>{{ title }}</title>
<style>
/* ── Reset ───────────────────────────────────────── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

/* ── Tokens ──────────────────────────────────────── */
:root{
  --bg:        #09090b;
  --surface:   #18181b;
  --surface-2: #27272a;
  --border:    #3f3f46;
  --border-dim:#27272a;
  --text-1:    #fafafa;
  --text-2:    #a1a1aa;
  --text-3:    #71717a;

  --c-crit-fg:#fb7185; --c-crit-bg:#1f0812; --c-crit-bd:#9f1239;
  --c-high-fg:#fbbf24; --c-high-bg:#1c1007; --c-high-bd:#92400e;
  --c-med-fg: #94a3b8; --c-med-bg: #0f172a; --c-med-bd: #334155;
  --c-low-fg: #52525b; --c-low-bg: #09090b; --c-low-bd: #27272a;
}

/* ── Base ────────────────────────────────────────── */
html{scroll-behavior:smooth}
body{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:var(--bg);color:var(--text-1);
  font-size:14px;line-height:1.6;
  -webkit-font-smoothing:antialiased;
}

/* ── Header ──────────────────────────────────────── */
.page-header{
  padding:28px 48px 22px;
  border-bottom:1px solid var(--border-dim);
}
.page-header h1{font-size:20px;font-weight:600;margin-bottom:3px}
.page-header .meta{color:var(--text-3);font-size:12px}
.page-header .meta strong{color:var(--text-2);font-weight:500}

/* ── Layout ──────────────────────────────────────── */
.layout{
  display:grid;
  grid-template-columns:220px 1fr;
  align-items:start;
}

/* ── Sidebar ─────────────────────────────────────── */
.sidebar{
  position:sticky;top:0;height:100vh;
  overflow-y:auto;
  border-right:1px solid var(--border-dim);
  padding:24px 12px;
}
.sidebar-label{
  display:block;
  font-size:10px;font-weight:600;color:var(--text-3);
  text-transform:uppercase;letter-spacing:.09em;
  padding:0 8px;margin-bottom:10px;
}
.sb-item{
  display:flex;align-items:center;gap:8px;
  padding:6px 8px;border-radius:6px;
  text-decoration:none;color:var(--text-2);
  font-size:12.5px;
  transition:background .1s,color .1s;
  margin-bottom:1px;
}
.sb-item:hover{background:var(--surface-2);color:var(--text-1)}
.sb-item .sb-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sb-crit{
  flex-shrink:0;font-size:10px;font-weight:700;
  color:var(--c-crit-fg);background:var(--c-crit-bg);
  border:1px solid var(--c-crit-bd);
  border-radius:999px;padding:0 7px;line-height:18px;
}
.sb-count{flex-shrink:0;font-size:11px;color:var(--text-3)}
.sb-clean{flex-shrink:0;font-size:13px;color:#4ade80}

/* ── Main ────────────────────────────────────────── */
.main{padding:36px 48px;min-width:0}

/* ── Section label ───────────────────────────────── */
.section-label{
  font-size:10px;font-weight:600;color:var(--text-3);
  text-transform:uppercase;letter-spacing:.09em;
  margin-bottom:14px;
}

/* ── Heatmap ─────────────────────────────────────── */
.heatmap-wrap{overflow-x:auto;margin-bottom:52px}
.heatmap{border-collapse:collapse;font-size:12px;white-space:nowrap}
.heatmap th{
  padding:7px 12px;text-align:center;
  font-weight:500;color:var(--text-3);font-size:11px;
  border-bottom:1px solid var(--border-dim);
}
.heatmap th.mgr-col{text-align:left;min-width:160px}
.heatmap td{
  padding:7px 14px;text-align:center;
  border-bottom:1px solid var(--border-dim);
  font-size:12px;font-weight:500;
}
.heatmap td.mgr-cell{text-align:left}
.heatmap td.mgr-cell a{color:var(--text-1);text-decoration:none;font-weight:500}
.heatmap td.mgr-cell a:hover{color:#818cf8}
.heatmap td.total-cell{
  font-weight:700;color:var(--text-1);
  border-left:1px solid var(--border-dim);
}
.cell-zero{color:var(--text-3)}
.cell-warm{color:#ca8a04;background:#1c1a07}
.cell-hot {color:#f87171;background:#1f0812;font-weight:700}

/* ── Manager section ─────────────────────────────── */
.mgr-section{margin-bottom:52px}
.mgr-header{
  display:flex;align-items:center;flex-wrap:wrap;
  gap:10px;margin-bottom:18px;
  padding-bottom:14px;border-bottom:1px solid var(--border-dim);
}
.mgr-header h2{font-size:17px;font-weight:600;color:var(--text-1)}
.pill{
  font-size:11px;font-weight:500;
  border:1px solid var(--border-dim);border-radius:999px;
  padding:2px 10px;color:var(--text-3);
}
.pill.crit{
  color:var(--c-crit-fg);background:var(--c-crit-bg);
  border-color:var(--c-crit-bd);
}

/* ── Event cards ─────────────────────────────────── */
.event{
  background:var(--surface);border:1px solid var(--border-dim);
  border-radius:10px;padding:14px 18px;margin:8px 0;
  transition:border-color .15s;
}
.event:hover{border-color:var(--border)}
.event.critical{
  border-color:var(--c-crit-bd);
  background:linear-gradient(135deg,var(--c-crit-bg) 0%,var(--surface) 55%);
}
.event.high{
  border-color:var(--c-high-bd);
  background:linear-gradient(135deg,var(--c-high-bg) 0%,var(--surface) 55%);
}
.event.medium{border-color:var(--c-med-bd)}
.event.low{opacity:.65}

/* ── Badges ──────────────────────────────────────── */
.event-header{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.badge{
  display:inline-flex;align-items:center;
  padding:2px 9px;border-radius:999px;
  font-size:10px;font-weight:700;
  text-transform:uppercase;letter-spacing:.07em;
  border:1px solid;
}
.badge.critical{color:var(--c-crit-fg);background:var(--c-crit-bg);border-color:var(--c-crit-bd)}
.badge.high    {color:var(--c-high-fg);background:var(--c-high-bg);border-color:var(--c-high-bd)}
.badge.medium  {color:var(--c-med-fg); background:var(--c-med-bg); border-color:var(--c-med-bd)}
.badge.low     {color:var(--c-low-fg); background:var(--c-low-bg); border-color:var(--c-low-bd)}

.ev-partner{font-weight:600;color:var(--text-1);font-size:14px}
.ev-type   {color:var(--text-2);font-size:13px}
.ev-score  {color:var(--text-3);font-size:12px}
.ev-date   {color:var(--text-3);font-size:12px;margin-left:auto}

.ev-phrase{
  margin-top:10px;padding:8px 12px;
  background:rgba(255,255,255,.04);border-radius:6px;
  font-style:italic;color:var(--text-2);font-size:13px;
  border-left:2px solid var(--border);
}
.ev-expl{
  margin-top:8px;color:var(--text-2);
  font-size:13px;line-height:1.65;
}

/* ── Empty state ─────────────────────────────────── */
.no-events{
  color:var(--text-3);font-style:italic;font-size:13px;
  padding:14px 4px;
}
.no-events::before{content:'✓  ';color:#4ade80;font-style:normal;font-weight:600}

/* ── Back to top ─────────────────────────────────── */
.back-top{
  display:inline-flex;align-items:center;gap:5px;
  margin-top:16px;color:var(--text-3);font-size:12px;
  text-decoration:none;transition:color .1s;
}
.back-top:hover{color:var(--text-2)}
</style>
</head>
<body>
<a id="top"></a>

<header class="page-header">
  <h1>{{ title }}</h1>
  <p class="meta">Period: <strong>{{ period_label }}</strong>&nbsp;&nbsp;·&nbsp;&nbsp;Generated: {{ generated_at }}</p>
</header>

<div class="layout">

<aside class="sidebar">
  <span class="sidebar-label">Managers</span>
  {% for m in managers %}
  <a class="sb-item" href="#mgr-{{ m.id }}">
    <span class="sb-name">{{ m.name }}</span>
    {% if m.total == 0 %}
      <span class="sb-clean">✓</span>
    {% else %}
      {% if m.critical_count %}<span class="sb-crit">{{ m.critical_count }}!</span>{% endif %}
      <span class="sb-count">{{ m.total }}</span>
    {% endif %}
  </a>
  {% endfor %}
</aside>

<main class="main">

  <p class="section-label">Risk Heatmap</p>
  <div class="heatmap-wrap">
  <table class="heatmap">
    <thead>
      <tr>
        <th class="mgr-col">Manager</th>
        {% for _, label in categories %}<th>{{ label }}</th>{% endfor %}
        <th>Total</th>
      </tr>
    </thead>
    <tbody>
    {% for m in managers %}
      <tr>
        <td class="mgr-cell"><a href="#mgr-{{ m.id }}">{{ m.name }}</a></td>
        {% for key, _ in categories %}
          {% set cnt = m.heatmap.get(key, 0) %}
          <td class="{{ 'cell-hot' if cnt >= 3 else ('cell-warm' if cnt >= 1 else 'cell-zero') }}">
            {{- cnt if cnt else '·' -}}
          </td>
        {% endfor %}
        <td class="total-cell">{{ m.total }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>

  {% for m in managers %}
  <section class="mgr-section" id="mgr-{{ m.id }}">
    <div class="mgr-header">
      <h2>{{ m.name }}</h2>
      <span class="pill">{{ m.total }} event{{ 's' if m.total != 1 else '' }}</span>
      {% if m.critical_count %}<span class="pill crit">{{ m.critical_count }} critical</span>{% endif %}
    </div>
    {% if m.events %}
      {% for ev in m.events %}
      <div class="event {{ ev.risk_level }}">
        <div class="event-header">
          <span class="badge {{ ev.risk_level }}">{{ ev.risk_level }}</span>
          <span class="ev-partner">{{ ev.partner_name }}</span>
          <span class="ev-type">— {{ ev.risk_type_label }}</span>
          <span class="ev-score">{{ ev.final_score }}/100</span>
          <span class="ev-date">{{ ev.date_str }}</span>
        </div>
        {% if ev.detected_phrase %}<div class="ev-phrase">"{{ ev.detected_phrase }}"</div>{% endif %}
        {% if ev.llm_explanation %}<div class="ev-expl">{{ ev.llm_explanation }}</div>{% endif %}
      </div>
      {% endfor %}
    {% else %}
      <p class="no-events">No risk events in this period — clean portfolio.</p>
    {% endif %}
    <a class="back-top" href="#top">↑ Back to top</a>
  </section>
  {% endfor %}

</main>
</div>
</body>
</html>
"""

_env = Environment(autoescape=True)
_template = _env.from_string(_TEMPLATE)


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_report_html(
    *,
    period_type: str,
    since: datetime,
    until: datetime,
    managers: list[dict[str, Any]],
    heatmap_rows: list[dict[str, Any]],
    event_rows: list[dict[str, Any]],
) -> str:
    """Render a complete HTML report page and return the HTML string.

    Args:
        period_type: "weekly" or "monthly".
        since: UTC start of the period (inclusive).
        until: UTC end of the period (exclusive).
        managers: rows from list_active_managers() — id, full_name.
        heatmap_rows: rows from risk_heatmap() — manager_id, risk_type, cnt.
        event_rows: rows from list_events_for_report() — full event + attribution.
    """
    # Build heatmap index: manager_id → {risk_type: count}
    heatmap_index: dict[UUID, dict[str, int]] = {}
    for row in heatmap_rows:
        mid = UUID(str(row["manager_id"]))
        heatmap_index.setdefault(mid, {})[str(row["risk_type"])] = int(row["cnt"])

    # Build event index: manager_id → list[EventData]
    events_index: dict[UUID, list[EventData]] = {}
    for row in event_rows:
        mid = UUID(str(row["manager_id"]))
        ts: datetime = row["created_at"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        ev = EventData(
            risk_level=str(row["risk_level"]),
            partner_name=str(row["partner_name"]),
            risk_type=str(row["risk_type"]),
            risk_type_label=_risk_type_label(str(row["risk_type"])),
            final_score=int(row["final_score"]),
            detected_phrase=str(row["detected_phrase"]) if row.get("detected_phrase") else None,
            llm_explanation=str(row["llm_explanation"]) if row.get("llm_explanation") else None,
            status=str(row.get("status") or ""),
            date_str=ts.strftime("%Y-%m-%d %H:%M"),
        )
        events_index.setdefault(mid, []).append(ev)

    # Assemble ManagerData for each manager
    manager_list: list[ManagerData] = []
    for mgr in managers:
        mid = UUID(str(mgr["id"]))
        mid_str = str(mid)
        evs = events_index.get(mid, [])
        manager_list.append(
            ManagerData(
                id=mid_str,
                name=_manager_label(mgr),
                events=evs,
                heatmap=heatmap_index.get(mid, {}),
                total=len(evs),
                critical_count=sum(1 for e in evs if e.risk_level == "critical"),
            )
        )

    label = "Weekly" if period_type == "weekly" else "Monthly"
    title = f"{label} Risk Report"
    period_label = f"{since.strftime('%d %b %Y')} – {until.strftime('%d %b %Y')}"
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    return str(
        _template.render(
            title=title,
            period_label=period_label,
            generated_at=generated_at,
            managers=manager_list,
            categories=RISK_CATEGORIES,
        )
    )
