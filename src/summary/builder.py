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
<title>{{ title }}</title>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;margin:0;padding:24px 40px 60px;color:#1f2937;line-height:1.5}
h1{color:#111827;border-bottom:2px solid #e5e7eb;padding-bottom:12px;margin-bottom:6px}
h2{color:#374151;margin-top:0}
.meta{color:#6b7280;font-size:.9em;margin-bottom:28px}

/* TOC */
.toc{background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:14px 22px;margin-bottom:32px;display:inline-block;min-width:280px}
.toc ul{margin:6px 0 0;padding-left:20px;list-style:disc}
.toc li{margin:3px 0}
.toc a{color:#2563eb;text-decoration:none}
.toc a:hover{text-decoration:underline}
.crit-count{color:#dc2626;font-weight:600}
.clean{color:#16a34a}

/* Heatmap */
.heatmap-wrap{overflow-x:auto;margin-bottom:44px}
.heatmap{border-collapse:collapse;font-size:.82em;white-space:nowrap}
.heatmap th{background:#f3f4f6;padding:7px 10px;border:1px solid #e5e7eb;text-align:center;font-weight:600}
.heatmap th.mgr-col{text-align:left;min-width:140px}
.heatmap td{padding:6px 10px;border:1px solid #e5e7eb;text-align:center}
.heatmap td.mgr-cell{text-align:left;font-weight:500}
.heatmap td.mgr-cell a{color:#1f2937;text-decoration:none}
.heatmap td.mgr-cell a:hover{color:#2563eb}
.heatmap td.total-cell{font-weight:700;background:#f9fafb}
.cell-hot{background:#fef2f2;color:#b91c1c;font-weight:700}
.cell-warm{background:#fffbeb;color:#92400e;font-weight:600}
.cell-zero{color:#d1d5db}

/* Manager sections */
.mgr-section{margin-top:52px;padding-top:20px;border-top:2px solid #e5e7eb}
.mgr-header{display:flex;align-items:baseline;gap:12px;margin-bottom:14px}
.mgr-header h2{margin:0}
.event-count{color:#6b7280;font-size:.9em}

/* Event cards */
.event{border-left:4px solid #e5e7eb;padding:10px 16px;margin:10px 0;border-radius:0 6px 6px 0}
.event.critical{border-left-color:#dc2626;background:#fef2f2}
.event.high{border-left-color:#d97706;background:#fffbeb}
.event.medium{border-left-color:#9ca3af;background:#f9fafb}
.event.low{border-left-color:#e5e7eb}
.event-header{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.badge{font-size:.72em;font-weight:700;padding:2px 8px;border-radius:4px;text-transform:uppercase;letter-spacing:.05em}
.badge.critical{background:#dc2626;color:#fff}
.badge.high{background:#d97706;color:#fff}
.badge.medium{background:#9ca3af;color:#fff}
.badge.low{background:#d1d5db;color:#6b7280}
.partner{font-weight:600}
.risk-type{color:#4b5563;font-size:.9em}
.score{color:#9ca3af;font-size:.85em}
.date{color:#9ca3af;font-size:.8em;margin-left:auto}
.phrase{margin-top:6px;padding:3px 10px;background:rgba(0,0,0,.04);border-radius:4px;font-style:italic;color:#374151;font-size:.9em}
.explanation{margin-top:4px;color:#4b5563;font-size:.88em}
.no-events{color:#9ca3af;font-style:italic;padding:6px 0}
.back-top{display:inline-block;margin-top:10px;color:#2563eb;font-size:.85em;text-decoration:none}
.back-top:hover{text-decoration:underline}
</style>
</head>
<body>
<a id="top"></a>
<h1>{{ title }}</h1>
<p class="meta">Период: <strong>{{ period_label }}</strong>&nbsp;&nbsp;·&nbsp;&nbsp;Сгенерировано: {{ generated_at }}</p>

<nav class="toc">
  <strong>Менеджеры</strong>
  <ul>
  {% for m in managers %}
    <li>
      <a href="#mgr-{{ m.id }}">{{ m.name }}</a>
      {% if m.total > 0 %}
        — {{ m.total }} событий
        {% if m.critical_count %}, <span class="crit-count">{{ m.critical_count }} критич.{% if m.critical_count == 1 %}{% else %}{% endif %}</span>{% endif %}
      {% else %}
        — <span class="clean">чисто</span>
      {% endif %}
    </li>
  {% endfor %}
  </ul>
</nav>

<h2>Тепловая карта рисков</h2>
<div class="heatmap-wrap">
<table class="heatmap">
  <thead>
    <tr>
      <th class="mgr-col">Менеджер</th>
      {% for _, label in categories %}<th>{{ label }}</th>{% endfor %}
      <th>Итого</th>
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
    <span class="event-count">{{ m.total }} событий за период</span>
  </div>
  {% if m.events %}
    {% for ev in m.events %}
    <div class="event {{ ev.risk_level }}">
      <div class="event-header">
        <span class="badge {{ ev.risk_level }}">{{ ev.risk_level }}</span>
        <span class="partner">{{ ev.partner_name }}</span>
        <span class="risk-type">— {{ ev.risk_type_label }}</span>
        <span class="score">({{ ev.final_score }}/100)</span>
        <span class="date">{{ ev.date_str }}</span>
      </div>
      {% if ev.detected_phrase %}<div class="phrase">"{{ ev.detected_phrase }}"</div>{% endif %}
      {% if ev.llm_explanation %}<div class="explanation">{{ ev.llm_explanation }}</div>{% endif %}
    </div>
    {% endfor %}
  {% else %}
    <p class="no-events">Нет риск-событий за период — чистое портфолио.</p>
  {% endif %}
  <a class="back-top" href="#top">↑ Наверх</a>
</section>
{% endfor %}

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
                name=str(mgr["full_name"]),
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
