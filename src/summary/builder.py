"""HTML report builder for weekly/monthly manager-centric summaries. Phase 16.

Produces a single self-contained HTML page:
  - Header with period dates and generation time
  - Navigation TOC (jump links per manager)
  - Heat-map table: managers × 13 risk categories
  - Per-manager timeline sections with color-coded event cards

No external CSS or JS — the output is a standalone file safe to serve directly.
"""

from __future__ import annotations

import re as _re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from jinja2 import Environment
from markupsafe import Markup

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

    tg_username is stored without @; we add it here. Falls back to full_name and
    finally a literal placeholder so a manager missing BOTH aff_id and
    tg_username never renders as a bare "@" (the pilot bug).
    """
    aff = (mgr.get("aff_id") or "").strip()
    tg = (mgr.get("tg_username") or "").strip().lstrip("@")
    if aff and tg:
        return f"{aff} | @{tg}"
    if aff:
        return aff
    if tg:
        return f"@{tg}"
    name = (mgr.get("full_name") or "").strip()
    return name or "Unassigned"


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
<link href="https://fonts.googleapis.com/css2?family=Urbanist:wght@500;600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<title>{{ title }}</title>
<style>
/* ── Reset ───────────────────────────────────────── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

/* ── Tokens (light, businesslike) ────────────────── */
:root{
  --bg:        #f4f5f7;
  --surface:   #ffffff;
  --surface-2: #f1f5f9;
  --border:    #e2e8f0;
  --border-dim:#eceef1;
  --text-1:    #0f172a;
  --text-2:    #475569;
  --text-3:    #94a3b8;
  --accent:    #4f46e5;
  --accent-dim:#eef2ff;

  --shadow-sm: 0 1px 2px rgba(15,23,42,.05);
  --shadow-md: 0 1px 3px rgba(15,23,42,.08),0 1px 2px rgba(15,23,42,.04);

  --c-crit-fg:#b91c1c; --c-crit-bg:#fef2f2; --c-crit-bd:#fecaca; --c-crit-ac:#dc2626;
  --c-high-fg:#b45309; --c-high-bg:#fffbeb; --c-high-bd:#fde68a; --c-high-ac:#f59e0b;
  --c-med-fg: #475569; --c-med-bg: #f1f5f9; --c-med-bd: #cbd5e1; --c-med-ac:#94a3b8;
  --c-low-fg: #94a3b8; --c-low-bg: #f8fafc; --c-low-bd: #e2e8f0; --c-low-ac:#cbd5e1;
  --c-ok:     #16a34a;
}

/* ── Base ────────────────────────────────────────── */
html{scroll-behavior:smooth}
body{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:var(--bg);color:var(--text-1);
  font-size:14px;line-height:1.6;
  -webkit-font-smoothing:antialiased;
}
h1,h2,.section-label,.stat-num,.heatmap td.total-cell{
  font-family:'Urbanist','Inter',sans-serif;
}

/* ── Header ──────────────────────────────────────── */
.accent-bar{height:3px;background:var(--accent)}
.page-header{
  background:var(--surface);
  padding:26px 48px 0;
  border-bottom:1px solid var(--border);
}
.page-header h1{font-size:24px;font-weight:700;letter-spacing:-.02em;margin-bottom:4px}
.page-header .meta{color:var(--text-3);font-size:12.5px;margin-bottom:22px}
.page-header .meta strong{color:var(--text-2);font-weight:600}

/* ── Header stat strip ───────────────────────────── */
.stat-strip{display:flex;flex-wrap:wrap;gap:0;border-top:1px solid var(--border-dim)}
.stat-cell{
  padding:14px 26px 16px;border-right:1px solid var(--border-dim);
  display:flex;flex-direction:column;gap:2px;
}
.stat-cell:first-child{padding-left:0}
.stat-num{font-size:22px;font-weight:700;line-height:1;color:var(--text-1)}
.stat-num.crit{color:var(--c-crit-ac)}
.stat-num.high{color:var(--c-high-ac)}
.stat-lbl{font-size:11px;color:var(--text-3);text-transform:uppercase;letter-spacing:.06em}

/* ── Layout ──────────────────────────────────────── */
.layout{
  display:grid;
  grid-template-columns:236px 1fr;
  align-items:start;
}

/* ── Sidebar ─────────────────────────────────────── */
.sidebar{
  position:sticky;top:0;height:100vh;
  overflow-y:auto;background:var(--surface);
  border-right:1px solid var(--border);
  padding:24px 14px;
}
.sidebar-label{
  display:block;
  font-size:10px;font-weight:700;color:var(--text-3);
  text-transform:uppercase;letter-spacing:.1em;
  padding:0 8px;margin-bottom:12px;
}
.sb-item{
  display:flex;align-items:center;gap:8px;
  padding:7px 9px;border-radius:7px;
  text-decoration:none;color:var(--text-2);
  font-size:12.5px;font-weight:500;
  transition:background .1s,color .1s;
  margin-bottom:1px;
}
.sb-item:hover{background:var(--surface-2);color:var(--text-1)}
.sb-item .sb-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sb-crit{
  flex-shrink:0;font-size:10px;font-weight:700;
  color:#fff;background:var(--c-crit-ac);
  border-radius:999px;padding:1px 7px;line-height:16px;
}
.sb-count{flex-shrink:0;font-size:11px;font-weight:600;color:var(--text-3)}
.sb-clean{flex-shrink:0;font-size:13px;color:var(--c-ok)}

/* ── Main ────────────────────────────────────────── */
.main{padding:36px 48px;min-width:0;max-width:1180px}

/* ── Section label ───────────────────────────────── */
.section-label{
  font-size:11px;font-weight:700;color:var(--text-3);
  text-transform:uppercase;letter-spacing:.1em;
  margin-bottom:14px;
}

/* ── Heatmap ─────────────────────────────────────── */
.heatmap-wrap{
  overflow-x:auto;margin-bottom:52px;
  background:var(--surface);border:1px solid var(--border);
  border-radius:12px;box-shadow:var(--shadow-sm);
}
.heatmap{border-collapse:collapse;font-size:12px;white-space:nowrap;width:100%}
.heatmap th{
  padding:13px 12px;text-align:center;
  font-weight:600;color:var(--text-3);font-size:10.5px;
  text-transform:uppercase;letter-spacing:.04em;
  border-bottom:1px solid var(--border);
}
.heatmap th.mgr-col{text-align:left;min-width:170px;padding-left:20px}
.heatmap td{
  padding:11px 14px;text-align:center;
  border-bottom:1px solid var(--border-dim);
  font-size:12.5px;font-weight:600;
}
.heatmap tbody tr:last-child td{border-bottom:none}
.heatmap tbody tr:hover td{background:var(--surface-2)}
.heatmap td.mgr-cell{text-align:left;padding-left:20px}
.heatmap td.mgr-cell a{color:var(--text-1);text-decoration:none;font-weight:600}
.heatmap td.mgr-cell a:hover{color:var(--accent)}
.heatmap td.total-cell{
  font-weight:700;color:var(--text-1);
  border-left:1px solid var(--border);
}
.cell-zero{color:#cbd5e1}
.cell-warm{color:var(--c-high-fg);background:var(--c-high-bg)}
.cell-hot {color:var(--c-crit-fg);background:var(--c-crit-bg);font-weight:700}
.heatmap tbody tr:hover td.cell-warm{background:#fef6dd}
.heatmap tbody tr:hover td.cell-hot{background:#fde8e8}

/* ── Manager section ─────────────────────────────── */
.mgr-section{margin-bottom:48px;scroll-margin-top:18px}
.mgr-header{
  display:flex;align-items:center;flex-wrap:wrap;
  gap:10px;margin-bottom:16px;
  padding-bottom:13px;border-bottom:1px solid var(--border);
}
.mgr-header h2{font-size:18px;font-weight:700;color:var(--text-1);letter-spacing:-.01em}
.pill{
  font-size:11px;font-weight:600;
  background:var(--surface);border:1px solid var(--border);border-radius:999px;
  padding:3px 11px;color:var(--text-2);
}
.pill.crit{
  color:var(--c-crit-fg);background:var(--c-crit-bg);
  border-color:var(--c-crit-bd);
}

/* ── Event cards ─────────────────────────────────── */
.event{
  background:var(--surface);border:1px solid var(--border);
  border-left:4px solid var(--c-low-ac);
  border-radius:10px;padding:14px 18px;margin:10px 0;
  box-shadow:var(--shadow-sm);
  transition:box-shadow .15s,transform .15s;
}
.event:hover{box-shadow:var(--shadow-md);transform:translateY(-1px)}
.event.critical{border-left-color:var(--c-crit-ac)}
.event.high    {border-left-color:var(--c-high-ac)}
.event.medium  {border-left-color:var(--c-med-ac)}
.event.low     {border-left-color:var(--c-low-ac);opacity:.82}

/* ── Badges ──────────────────────────────────────── */
.event-header{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.badge{
  display:inline-flex;align-items:center;
  padding:3px 9px;border-radius:6px;
  font-size:10px;font-weight:700;
  text-transform:uppercase;letter-spacing:.06em;
  border:1px solid;
}
.badge.critical{color:var(--c-crit-fg);background:var(--c-crit-bg);border-color:var(--c-crit-bd)}
.badge.high    {color:var(--c-high-fg);background:var(--c-high-bg);border-color:var(--c-high-bd)}
.badge.medium  {color:var(--c-med-fg); background:var(--c-med-bg); border-color:var(--c-med-bd)}
.badge.low     {color:var(--c-low-fg); background:var(--c-low-bg); border-color:var(--c-low-bd)}

.ev-partner{font-weight:700;color:var(--text-1);font-size:14px}
.ev-type   {color:var(--text-2);font-size:13px}
.ev-score  {color:var(--text-3);font-size:12px;font-weight:600}
.ev-date   {color:var(--text-3);font-size:12px;margin-left:auto}

.ev-phrase{
  margin-top:11px;padding:9px 13px;
  background:var(--surface-2);border-radius:7px;
  font-style:italic;color:var(--text-2);font-size:13px;
  border-left:3px solid var(--border);
}
.ev-expl{
  margin-top:9px;color:var(--text-2);
  font-size:13px;line-height:1.65;
}

/* ── Empty state ─────────────────────────────────── */
.no-events{
  color:var(--text-3);font-style:italic;font-size:13px;
  padding:13px 16px;background:var(--surface);
  border:1px dashed var(--border);border-radius:10px;
}
.no-events::before{content:'✓  ';color:var(--c-ok);font-style:normal;font-weight:700}

/* ── Back to top ─────────────────────────────────── */
.back-top{
  display:inline-flex;align-items:center;gap:5px;
  margin-top:16px;color:var(--text-3);font-size:12px;font-weight:500;
  text-decoration:none;transition:color .1s;
}
.back-top:hover{color:var(--accent)}
</style>
</head>
<body>
<a id="top"></a>
<div class="accent-bar"></div>

<header class="page-header">
  <h1>{{ title }}</h1>
  <p class="meta">Period: <strong>{{ period_label }}</strong>&nbsp;&nbsp;·&nbsp;&nbsp;Generated: {{ generated_at }}</p>
  <div class="stat-strip">
    <div class="stat-cell"><span class="stat-num">{{ stats.total_events }}</span><span class="stat-lbl">Risk events</span></div>
    <div class="stat-cell"><span class="stat-num crit">{{ stats.critical }}</span><span class="stat-lbl">Critical</span></div>
    <div class="stat-cell"><span class="stat-num high">{{ stats.high }}</span><span class="stat-lbl">High</span></div>
    <div class="stat-cell"><span class="stat-num">{{ stats.flagged }}/{{ stats.managers }}</span><span class="stat-lbl">Managers flagged</span></div>
  </div>
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

# Shared mobile/responsive rules, appended into every template's <style> block.
# Injecting once (rather than duplicating in each literal) keeps the breakpoint
# behaviour identical across the standalone report and the dashboard.
_RESPONSIVE_CSS = """
/* ── Responsive (phones / small tablets) ─────────── */
@media (max-width:768px){
  body{font-size:13.5px}
  .page-header{padding:18px 16px 0}
  .page-header h1{font-size:20px}
  .page-header .meta{font-size:12px;margin-bottom:16px}
  .stat-strip{border-top:none}
  .stat-cell{padding:10px 16px 12px}
  .stat-cell:first-child{padding-left:0}
  .stat-num{font-size:18px}
  /* The manager jump-nav collapses on small screens; the heatmap (with its own
     manager links) and the header stat-strip carry navigation + overview. */
  .layout{grid-template-columns:1fr}
  .sidebar{display:none}
  .main{padding:22px 14px}
  .section-label{margin-bottom:10px}
  /* Wide heatmap scrolls horizontally with iOS momentum instead of squashing. */
  .heatmap-wrap{margin-bottom:34px;-webkit-overflow-scrolling:touch}
  .mgr-section{margin-bottom:34px}
  .mgr-header h2{font-size:16px}
  .event{padding:13px 14px}
  .event-header{gap:7px}
  .ev-partner{font-size:13.5px}
  .ev-date{margin-left:0}
  .dash-tabs{padding:0 10px}
  .tab-btn{padding:13px 14px;font-size:12.5px}
  .tab-empty{padding:48px 18px}
}
"""


def _with_responsive(tpl: str) -> str:
    """Append the shared responsive rules just before the template's </style>."""
    return tpl.replace("</style>", _RESPONSIVE_CSS + "</style>", 1)


_env = Environment(autoescape=True)
_template = _env.from_string(_with_responsive(_TEMPLATE))


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

    all_events = [e for m in manager_list for e in m.events]
    stats = {
        "total_events": len(all_events),
        "critical": sum(1 for e in all_events if e.risk_level == "critical"),
        "high": sum(1 for e in all_events if e.risk_level == "high"),
        "managers": len(manager_list),
        "flagged": sum(1 for m in manager_list if m.total > 0),
    }

    return str(
        _template.render(
            title=title,
            period_label=period_label,
            generated_at=generated_at,
            managers=manager_list,
            categories=RISK_CATEGORIES,
            stats=stats,
        )
    )


# ---------------------------------------------------------------------------
# Dashboard builder (tabbed view of latest weekly + monthly reports)
# ---------------------------------------------------------------------------


def _extract_body(html: str) -> str:
    """Extract <body> content and neutralise back-to-top anchors for multi-tab use."""
    m = _re.search(r"<body>(.*)</body>", html, _re.DOTALL)
    body = m.group(1).strip() if m else html
    body = body.replace('<a id="top"></a>', "", 1)
    body = body.replace(
        'href="#top"',
        'href="javascript:void(0)" onclick="window.scrollTo(0,0)"',
    )
    return body


_DASHBOARD_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Urbanist:wght@500;600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<title>Risk Reports Dashboard</title>
<style>
/* ── Reset ───────────────────────────────────────── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

/* ── Tokens (light, businesslike) ────────────────── */
:root{
  --bg:        #f4f5f7;
  --surface:   #ffffff;
  --surface-2: #f1f5f9;
  --border:    #e2e8f0;
  --border-dim:#eceef1;
  --text-1:    #0f172a;
  --text-2:    #475569;
  --text-3:    #94a3b8;
  --accent:    #4f46e5;
  --accent-dim:#eef2ff;

  --shadow-sm: 0 1px 2px rgba(15,23,42,.05);
  --shadow-md: 0 1px 3px rgba(15,23,42,.08),0 1px 2px rgba(15,23,42,.04);

  --c-crit-fg:#b91c1c; --c-crit-bg:#fef2f2; --c-crit-bd:#fecaca; --c-crit-ac:#dc2626;
  --c-high-fg:#b45309; --c-high-bg:#fffbeb; --c-high-bd:#fde68a; --c-high-ac:#f59e0b;
  --c-med-fg: #475569; --c-med-bg: #f1f5f9; --c-med-bd: #cbd5e1; --c-med-ac:#94a3b8;
  --c-low-fg: #94a3b8; --c-low-bg: #f8fafc; --c-low-bd: #e2e8f0; --c-low-ac:#cbd5e1;
  --c-ok:     #16a34a;
}

/* ── Base ────────────────────────────────────────── */
html{scroll-behavior:smooth}
body{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:var(--bg);color:var(--text-1);
  font-size:14px;line-height:1.6;
  -webkit-font-smoothing:antialiased;
}
h1,h2,.section-label,.stat-num,.heatmap td.total-cell{
  font-family:'Urbanist','Inter',sans-serif;
}

/* ── Accent bar ──────────────────────────────────── */
.accent-bar{height:3px;background:var(--accent)}

/* ── Tab bar ─────────────────────────────────────── */
.dash-tabs{
  display:flex;gap:0;
  background:var(--surface);
  border-bottom:1px solid var(--border);
  padding:0 36px;
  position:sticky;top:0;z-index:100;
}
.tab-btn{
  padding:14px 20px;
  font-size:13px;font-weight:600;color:var(--text-3);
  border:none;background:none;cursor:pointer;
  border-bottom:3px solid transparent;
  transition:color .15s,border-color .15s;
  font-family:'Inter',sans-serif;
}
.tab-btn:hover{color:var(--text-1)}
.tab-btn.active{color:var(--accent);border-bottom-color:var(--accent)}

/* ── Tab panels ──────────────────────────────────── */
.tab-panel{display:none}
.tab-panel.active{display:block}
.tab-empty{
  padding:80px 48px;color:var(--text-3);
  font-style:italic;font-size:14px;
}

/* ── Header ──────────────────────────────────────── */
.page-header{
  background:var(--surface);
  padding:26px 48px 0;
  border-bottom:1px solid var(--border);
}
.page-header h1{font-size:24px;font-weight:700;letter-spacing:-.02em;margin-bottom:4px}
.page-header .meta{color:var(--text-3);font-size:12.5px;margin-bottom:22px}
.page-header .meta strong{color:var(--text-2);font-weight:600}

/* ── Header stat strip ───────────────────────────── */
.stat-strip{display:flex;flex-wrap:wrap;gap:0;border-top:1px solid var(--border-dim)}
.stat-cell{
  padding:14px 26px 16px;border-right:1px solid var(--border-dim);
  display:flex;flex-direction:column;gap:2px;
}
.stat-cell:first-child{padding-left:0}
.stat-num{font-size:22px;font-weight:700;line-height:1;color:var(--text-1)}
.stat-num.crit{color:var(--c-crit-ac)}
.stat-num.high{color:var(--c-high-ac)}
.stat-lbl{font-size:11px;color:var(--text-3);text-transform:uppercase;letter-spacing:.06em}

/* ── Layout ──────────────────────────────────────── */
.layout{
  display:grid;
  grid-template-columns:236px 1fr;
  align-items:start;
}

/* ── Sidebar ─────────────────────────────────────── */
.sidebar{
  position:sticky;top:47px;height:calc(100vh - 47px);
  overflow-y:auto;background:var(--surface);
  border-right:1px solid var(--border);
  padding:24px 14px;
}
.sidebar-label{
  display:block;
  font-size:10px;font-weight:700;color:var(--text-3);
  text-transform:uppercase;letter-spacing:.1em;
  padding:0 8px;margin-bottom:12px;
}
.sb-item{
  display:flex;align-items:center;gap:8px;
  padding:7px 9px;border-radius:7px;
  text-decoration:none;color:var(--text-2);
  font-size:12.5px;font-weight:500;
  transition:background .1s,color .1s;
  margin-bottom:1px;
}
.sb-item:hover{background:var(--surface-2);color:var(--text-1)}
.sb-item .sb-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sb-crit{
  flex-shrink:0;font-size:10px;font-weight:700;
  color:#fff;background:var(--c-crit-ac);
  border-radius:999px;padding:1px 7px;line-height:16px;
}
.sb-count{flex-shrink:0;font-size:11px;font-weight:600;color:var(--text-3)}
.sb-clean{flex-shrink:0;font-size:13px;color:var(--c-ok)}

/* ── Main ────────────────────────────────────────── */
.main{padding:36px 48px;min-width:0;max-width:1180px}

/* ── Section label ───────────────────────────────── */
.section-label{
  font-size:11px;font-weight:700;color:var(--text-3);
  text-transform:uppercase;letter-spacing:.1em;
  margin-bottom:14px;
}

/* ── Heatmap ─────────────────────────────────────── */
.heatmap-wrap{
  overflow-x:auto;margin-bottom:52px;
  background:var(--surface);border:1px solid var(--border);
  border-radius:12px;box-shadow:var(--shadow-sm);
}
.heatmap{border-collapse:collapse;font-size:12px;white-space:nowrap;width:100%}
.heatmap th{
  padding:13px 12px;text-align:center;
  font-weight:600;color:var(--text-3);font-size:10.5px;
  text-transform:uppercase;letter-spacing:.04em;
  border-bottom:1px solid var(--border);
}
.heatmap th.mgr-col{text-align:left;min-width:170px;padding-left:20px}
.heatmap td{
  padding:11px 14px;text-align:center;
  border-bottom:1px solid var(--border-dim);
  font-size:12.5px;font-weight:600;
}
.heatmap tbody tr:last-child td{border-bottom:none}
.heatmap tbody tr:hover td{background:var(--surface-2)}
.heatmap td.mgr-cell{text-align:left;padding-left:20px}
.heatmap td.mgr-cell a{color:var(--text-1);text-decoration:none;font-weight:600}
.heatmap td.mgr-cell a:hover{color:var(--accent)}
.heatmap td.total-cell{
  font-weight:700;color:var(--text-1);
  border-left:1px solid var(--border);
}
.cell-zero{color:#cbd5e1}
.cell-warm{color:var(--c-high-fg);background:var(--c-high-bg)}
.cell-hot {color:var(--c-crit-fg);background:var(--c-crit-bg);font-weight:700}
.heatmap tbody tr:hover td.cell-warm{background:#fef6dd}
.heatmap tbody tr:hover td.cell-hot{background:#fde8e8}

/* ── Manager section ─────────────────────────────── */
.mgr-section{margin-bottom:48px;scroll-margin-top:18px}
.mgr-header{
  display:flex;align-items:center;flex-wrap:wrap;
  gap:10px;margin-bottom:16px;
  padding-bottom:13px;border-bottom:1px solid var(--border);
}
.mgr-header h2{font-size:18px;font-weight:700;color:var(--text-1);letter-spacing:-.01em}
.pill{
  font-size:11px;font-weight:600;
  background:var(--surface);border:1px solid var(--border);border-radius:999px;
  padding:3px 11px;color:var(--text-2);
}
.pill.crit{
  color:var(--c-crit-fg);background:var(--c-crit-bg);
  border-color:var(--c-crit-bd);
}

/* ── Event cards ─────────────────────────────────── */
.event{
  background:var(--surface);border:1px solid var(--border);
  border-left:4px solid var(--c-low-ac);
  border-radius:10px;padding:14px 18px;margin:10px 0;
  box-shadow:var(--shadow-sm);
  transition:box-shadow .15s,transform .15s;
}
.event:hover{box-shadow:var(--shadow-md);transform:translateY(-1px)}
.event.critical{border-left-color:var(--c-crit-ac)}
.event.high    {border-left-color:var(--c-high-ac)}
.event.medium  {border-left-color:var(--c-med-ac)}
.event.low     {border-left-color:var(--c-low-ac);opacity:.82}

/* ── Badges ──────────────────────────────────────── */
.event-header{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.badge{
  display:inline-flex;align-items:center;
  padding:3px 9px;border-radius:6px;
  font-size:10px;font-weight:700;
  text-transform:uppercase;letter-spacing:.06em;
  border:1px solid;
}
.badge.critical{color:var(--c-crit-fg);background:var(--c-crit-bg);border-color:var(--c-crit-bd)}
.badge.high    {color:var(--c-high-fg);background:var(--c-high-bg);border-color:var(--c-high-bd)}
.badge.medium  {color:var(--c-med-fg); background:var(--c-med-bg); border-color:var(--c-med-bd)}
.badge.low     {color:var(--c-low-fg); background:var(--c-low-bg); border-color:var(--c-low-bd)}

.ev-partner{font-weight:700;color:var(--text-1);font-size:14px}
.ev-type   {color:var(--text-2);font-size:13px}
.ev-score  {color:var(--text-3);font-size:12px;font-weight:600}
.ev-date   {color:var(--text-3);font-size:12px;margin-left:auto}

.ev-phrase{
  margin-top:11px;padding:9px 13px;
  background:var(--surface-2);border-radius:7px;
  font-style:italic;color:var(--text-2);font-size:13px;
  border-left:3px solid var(--border);
}
.ev-expl{
  margin-top:9px;color:var(--text-2);
  font-size:13px;line-height:1.65;
}

/* ── Empty state ─────────────────────────────────── */
.no-events{
  color:var(--text-3);font-style:italic;font-size:13px;
  padding:13px 16px;background:var(--surface);
  border:1px dashed var(--border);border-radius:10px;
}
.no-events::before{content:'✓  ';color:var(--c-ok);font-style:normal;font-weight:700}

/* ── Back to top ─────────────────────────────────── */
.back-top{
  display:inline-flex;align-items:center;gap:5px;
  margin-top:16px;color:var(--text-3);font-size:12px;font-weight:500;
  text-decoration:none;transition:color .1s;
}
.back-top:hover{color:var(--accent)}
</style>
</head>
<body>
<div class="accent-bar"></div>
<div class="dash-tabs">
  <button class="tab-btn active" data-tab="weekly" onclick="showTab('weekly')">📊 Weekly</button>
  <button class="tab-btn" data-tab="monthly" onclick="showTab('monthly')">📅 Monthly</button>
</div>

<div id="panel-weekly" class="tab-panel active">
  {% if weekly_body %}{{ weekly_body }}{% else %}<p class="tab-empty">No weekly report available yet.</p>{% endif %}
</div>

<div id="panel-monthly" class="tab-panel">
  {% if monthly_body %}{{ monthly_body }}{% else %}<p class="tab-empty">No monthly report available yet.</p>{% endif %}
</div>

<script>
function showTab(name) {
  document.querySelectorAll('.tab-panel').forEach(function(p) { p.classList.remove('active'); });
  document.getElementById('panel-' + name).classList.add('active');
  document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
  document.querySelector('[data-tab="' + name + '"]').classList.add('active');
}
</script>
</body>
</html>
"""

_dash_template = _env.from_string(_with_responsive(_DASHBOARD_TEMPLATE))


def build_dashboard_html(
    *, weekly_html: str | None, monthly_html: str | None
) -> str:
    """Render a tabbed dashboard combining the latest weekly and monthly reports."""
    wb = Markup(_extract_body(weekly_html)) if weekly_html else None
    mb = Markup(_extract_body(monthly_html)) if monthly_html else None
    return str(_dash_template.render(weekly_body=wb, monthly_body=mb))
