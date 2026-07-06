"""HTML report builder for weekly/monthly manager-centric summaries. Phase 16.

Produces a single self-contained HTML page:
  - Header with period dates and generation time
  - Navigation TOC (jump links per manager)
  - Heat-map table: managers × 13 risk categories
  - Per-manager timeline sections with color-coded event cards

No external CSS or JS — the output is a standalone file safe to serve directly.

Visual system: "Signal Desk" — a risk-intelligence briefing. The report reads
as instrument readout: all data (scores, dates, counts, category cells, aff_ids)
is set in a monospace face; the heat-map is a "signal matrix"; severity is
encoded by a left "spine" on each card plus a badge. Two themes ship from one
token set: light "paper briefing" (warm ivory, default) and dark "control room".
The single shared ``_BASE_CSS`` block below styles BOTH the standalone report and
the tabbed dashboard, so the two never drift — edit styling there, once.
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
    author: str | None
    author_role: str | None
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
# Shared visual system ("Signal Desk")
# ---------------------------------------------------------------------------

# Webfonts (Cyrillic-safe): Archivo = the one display moment (the h1 hero);
# IBM Plex Sans = body + evidence quotes; IBM Plex Mono = the signature data
# treatment. All three carry Cyrillic, so Russian quotes/labels render evenly.
_FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    "family=Archivo:wght@700;800&"
    "family=IBM+Plex+Mono:wght@500;600;700&"
    "family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap"
    '">'
)

# Every color and type decision derives from these tokens. Light "paper
# briefing" (warm ivory) is the default :root; dark "control room" applies under
# the OS preference AND the viewer's explicit toggle (data-theme wins both ways).
_BASE_CSS = """
/* ── Reset ─────────────────────────────────────────── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

/* ── Tokens: light "paper briefing" (warm ivory) ───── */
:root{
  --paper:#ECE9E2; --surface:#F7F5F0; --surface-2:#EEEAE2;
  --line:#DED8CD; --line-2:#E7E2D9;
  --ink:#15171C; --ink-2:#565C69; --ink-3:#969BA6;
  --accent:#1E4D6B; --accent-soft:rgba(30,77,107,.09);

  --crit:#B42318; --crit-bg:#FBEEEC; --crit-line:#F0CFC9;
  --high:#B25A0B; --high-bg:#FBF3E8; --high-line:#EFDCC0;
  --med:#565C69;  --med-bg:#EEEAE2;  --med-line:#DCD6CB;
  --low:#969BA6;  --low-bg:#F3F1EA;  --low-line:#E7E2D9;
  --ok:#2E7D5B;

  --shadow:0 1px 2px rgba(21,23,28,.05);
  --shadow-lift:0 6px 20px -8px rgba(21,23,28,.18),0 1px 2px rgba(21,23,28,.06);

  --sans:'IBM Plex Sans',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,'SF Mono','Cascadia Code','Consolas',monospace;
  --display:'Archivo','IBM Plex Sans',system-ui,sans-serif;
}
@media (prefers-color-scheme:dark){
  :root{
    --paper:#0F1216; --surface:#161A20; --surface-2:#1C212A;
    --ink:#E7E9ED; --ink-2:#A2A9B5; --ink-3:#6D7480;
    --line:#252A33; --line-2:#1E232B;
    --accent:#6FB2D4; --accent-soft:rgba(111,178,212,.14);

    --crit:#F1897E; --crit-bg:rgba(180,35,24,.17); --crit-line:rgba(241,137,126,.32);
    --high:#E5A863; --high-bg:rgba(178,90,11,.18); --high-line:rgba(229,168,99,.32);
    --med:#A2A9B5;  --med-bg:#1C212A;  --med-line:#2C323C;
    --low:#6D7480;  --low-bg:#161A20;  --low-line:#242932;
    --ok:#5FBE93;

    --shadow:0 1px 2px rgba(0,0,0,.4);
    --shadow-lift:0 8px 24px -10px rgba(0,0,0,.6),0 1px 2px rgba(0,0,0,.4);
  }
}
/* Viewer's explicit theme toggle must win over the OS preference, both ways. */
:root[data-theme="light"]{
  --paper:#ECE9E2; --surface:#F7F5F0; --surface-2:#EEEAE2;
  --line:#DED8CD; --line-2:#E7E2D9;
  --ink:#15171C; --ink-2:#565C69; --ink-3:#969BA6;
  --accent:#1E4D6B; --accent-soft:rgba(30,77,107,.09);
  --crit:#B42318; --crit-bg:#FBEEEC; --crit-line:#F0CFC9;
  --high:#B25A0B; --high-bg:#FBF3E8; --high-line:#EFDCC0;
  --med:#565C69;  --med-bg:#EEEAE2;  --med-line:#DCD6CB;
  --low:#969BA6;  --low-bg:#F3F1EA;  --low-line:#E7E2D9;
  --ok:#2E7D5B;
  --shadow:0 1px 2px rgba(21,23,28,.05);
  --shadow-lift:0 6px 20px -8px rgba(21,23,28,.18),0 1px 2px rgba(21,23,28,.06);
}
:root[data-theme="dark"]{
  --paper:#0F1216; --surface:#161A20; --surface-2:#1C212A;
  --ink:#E7E9ED; --ink-2:#A2A9B5; --ink-3:#6D7480;
  --line:#252A33; --line-2:#1E232B;
  --accent:#6FB2D4; --accent-soft:rgba(111,178,212,.14);
  --crit:#F1897E; --crit-bg:rgba(180,35,24,.17); --crit-line:rgba(241,137,126,.32);
  --high:#E5A863; --high-bg:rgba(178,90,11,.18); --high-line:rgba(229,168,99,.32);
  --med:#A2A9B5;  --med-bg:#1C212A;  --med-line:#2C323C;
  --low:#6D7480;  --low-bg:#161A20;  --low-line:#242932;
  --ok:#5FBE93;
  --shadow:0 1px 2px rgba(0,0,0,.4);
  --shadow-lift:0 8px 24px -10px rgba(0,0,0,.6),0 1px 2px rgba(0,0,0,.4);
}

/* ── Base ──────────────────────────────────────────── */
html{scroll-behavior:smooth}
body{
  font-family:var(--sans);
  background:var(--paper);color:var(--ink);
  font-size:14px;line-height:1.6;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
}
a{color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.01em}

.accent-bar{height:2px;background:var(--accent)}

/* ── Header ────────────────────────────────────────── */
.page-header{background:var(--surface);border-bottom:1px solid var(--line);padding:30px 48px 0}
.page-header h1{font-family:var(--display);font-size:29px;font-weight:800;letter-spacing:-.025em;line-height:1.1;text-wrap:balance;margin-bottom:8px}
.page-header .meta{font-family:var(--mono);color:var(--ink-3);font-size:12px;margin-bottom:26px}
.page-header .meta strong{color:var(--ink-2);font-weight:600}

/* ── Header stat readouts ──────────────────────────── */
.stat-strip{display:flex;flex-wrap:wrap}
.stat-cell{padding:15px 30px 18px;border-left:1px solid var(--line-2);display:flex;flex-direction:column;gap:3px}
.stat-cell:first-child{padding-left:0;border-left:none}
.stat-num{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:25px;font-weight:700;line-height:1;color:var(--ink);letter-spacing:-.02em}
.stat-num.crit{color:var(--crit)}
.stat-num.high{color:var(--high)}
.stat-lbl{font-size:11px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.09em}

/* ── Layout ────────────────────────────────────────── */
.layout{display:grid;grid-template-columns:250px 1fr;align-items:start}

/* ── Sidebar roster ────────────────────────────────── */
.sidebar{position:sticky;top:0;height:100vh;overflow-y:auto;background:var(--surface);border-right:1px solid var(--line);padding:20px 14px}
.sb-search-wrap{padding:0 6px 12px}
.sb-search{width:100%;font-family:var(--mono);font-size:12px;color:var(--ink);background:var(--surface-2);border:1px solid var(--line);border-radius:6px;padding:8px 11px;outline:none;transition:border-color .12s}
.sb-search::placeholder{color:var(--ink-3)}
.sb-search:focus{border-color:var(--accent)}
.sb-empty{display:none;padding:8px 12px;font-family:var(--mono);font-size:11px;color:var(--ink-3);font-style:italic}
.sidebar-label{display:block;font-family:var(--mono);font-size:10px;font-weight:600;color:var(--ink-3);text-transform:uppercase;letter-spacing:.16em;padding:0 10px;margin:12px 0 8px}
.sb-item{width:100%;display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:6px;border:0;background:none;cursor:pointer;text-align:left;text-decoration:none;color:var(--ink-2);font-family:inherit;font-size:12.5px;font-weight:500;transition:background .12s,color .12s;margin-bottom:1px}
.sb-item:hover{background:var(--surface-2);color:var(--ink)}
.sb-item.active{background:var(--accent-soft);color:var(--ink);font-weight:600}
.sb-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono);font-size:12px;letter-spacing:-.01em}
.sb-crit{flex-shrink:0;font-family:var(--mono);font-size:10px;font-weight:700;color:#fff;background:var(--crit);border-radius:3px;padding:1px 6px;line-height:16px}
:root[data-theme="dark"] .sb-crit{color:#1a0d0b}
.sb-count{flex-shrink:0;font-family:var(--mono);font-size:11px;font-weight:600;color:var(--ink-3)}
.sb-clean{flex-shrink:0;font-size:12px;color:var(--ok)}

/* ── Main ──────────────────────────────────────────── */
.main{padding:38px 48px 72px;min-width:0;max-width:1200px}
.section-label{font-family:var(--mono);font-size:11px;font-weight:600;color:var(--ink-3);text-transform:uppercase;letter-spacing:.16em;margin-bottom:16px;display:flex;align-items:center;gap:12px}
.section-label::after{content:"";flex:1;height:1px;background:var(--line)}

/* ── Signal matrix (heat-map) ──────────────────────── */
.heatmap-wrap{overflow-x:auto;margin-bottom:56px;background:var(--surface);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow)}
.heatmap{border-collapse:collapse;width:100%;white-space:nowrap}
.heatmap th{padding:14px 10px;text-align:center;font-family:var(--mono);font-weight:600;color:var(--ink-3);font-size:10px;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid var(--line)}
.heatmap th.mgr-col{text-align:left;min-width:184px;padding-left:22px}
.heatmap td{padding:12px;text-align:center;border-bottom:1px solid var(--line-2);font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:13px;font-weight:600}
.heatmap tbody tr:last-child td{border-bottom:none}
.heatmap tbody tr:hover td{background:var(--surface-2)}
.heatmap td.mgr-cell{text-align:left;padding-left:22px}
.heatmap td.mgr-cell a{color:var(--ink);text-decoration:none;font-weight:600;font-family:var(--mono);font-size:12px}
.heatmap td.mgr-cell a:hover{color:var(--accent)}
.heatmap td.total-cell{font-weight:700;color:var(--ink);border-left:1px solid var(--line)}
.cell-zero{color:var(--ink-3);opacity:.45}
.cell-warm{color:var(--high);background:var(--high-bg)}
.cell-hot{color:var(--crit);background:var(--crit-bg);font-weight:700;position:relative}
.cell-hot::after{content:"";position:absolute;top:5px;right:5px;width:4px;height:4px;background:var(--crit);border-radius:50%}

/* ── Manager dossier ───────────────────────────────── */
.mgr-section{margin-bottom:52px;scroll-margin-top:20px}
.mgr-header{display:flex;align-items:baseline;flex-wrap:wrap;gap:12px;margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid var(--line)}
.mgr-header h2{font-family:var(--mono);font-size:19px;font-weight:700;letter-spacing:-.015em;color:var(--ink)}
.pill{font-family:var(--mono);font-size:11px;font-weight:600;background:var(--surface-2);border:1px solid var(--line);border-radius:5px;padding:3px 10px;color:var(--ink-2)}
.pill.crit{color:var(--crit);background:var(--crit-bg);border-color:var(--crit-line)}
.pill.clean{color:var(--ok)}

/* ── Navigation modes: All (default) vs single-manager view ─ */
.mode-single .view-all-only{display:none}
.mgr-back{font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--ink-3);background:none;border:0;cursor:pointer;padding:0;margin-right:2px}
.mgr-back:hover{color:var(--accent)}
.mode-all .mgr-back{display:none}

/* ── Event cards (severity spine) ──────────────────── */
.event{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--low);border-radius:8px;padding:16px 20px;margin:10px 0;box-shadow:var(--shadow);transition:box-shadow .16s ease,transform .16s ease}
.event:hover{box-shadow:var(--shadow-lift);transform:translateY(-1px)}
.event.critical{border-left-color:var(--crit)}
.event.high{border-left-color:var(--high)}
.event.medium{border-left-color:var(--med)}
.event.low{border-left-color:var(--low)}
.event-header{display:flex;align-items:center;gap:11px;flex-wrap:wrap}
.badge{font-family:var(--mono);display:inline-flex;align-items:center;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;border:1px solid}
.badge.critical{color:var(--crit);background:var(--crit-bg);border-color:var(--crit-line)}
.badge.high{color:var(--high);background:var(--high-bg);border-color:var(--high-line)}
.badge.medium{color:var(--med);background:var(--med-bg);border-color:var(--med-line)}
.badge.low{color:var(--low);background:var(--low-bg);border-color:var(--low-line)}
.ev-partner{font-weight:700;color:var(--ink);font-size:14px}
.ev-type{color:var(--ink-2);font-size:13px}
.ev-score{font-family:var(--mono);color:var(--ink-3);font-size:12px;font-weight:600;font-variant-numeric:tabular-nums}
.ev-date{font-family:var(--mono);color:var(--ink-3);font-size:11.5px;margin-left:auto}
.ev-author{margin-top:11px;color:var(--ink-2);font-size:12.5px;font-weight:600;display:flex;align-items:center;gap:8px}
.ev-role{font-family:var(--mono);font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.05em;padding:2px 7px;border-radius:4px;background:var(--surface-2);color:var(--ink-3);border:1px solid var(--line)}
.ev-role.internal{background:var(--accent-soft);color:var(--accent);border-color:transparent}
.ev-phrase{margin-top:12px;padding:10px 14px;background:var(--surface-2);border-radius:6px;border-left:2px solid var(--line);color:var(--ink-2);font-size:13px;font-style:italic}
.ev-expl{margin-top:11px;color:var(--ink-2);font-size:13px;line-height:1.66;max-width:74ch}

/* ── Empty state ───────────────────────────────────── */
.no-events{color:var(--ink-3);font-size:13px;padding:14px 18px;background:var(--surface);border:1px dashed var(--line);border-radius:8px}
.no-events::before{content:'\\2713  ';color:var(--ok);font-weight:700}

/* ── Back to top ───────────────────────────────────── */
.back-top{display:inline-flex;align-items:center;gap:6px;margin-top:18px;color:var(--ink-3);font-family:var(--mono);font-size:11px;text-decoration:none;text-transform:uppercase;letter-spacing:.08em;transition:color .12s}
.back-top:hover{color:var(--accent)}

/* ── Page-load reveal ──────────────────────────────── */
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.mgr-section,.heatmap-wrap{animation:rise .5s ease both}
.heatmap-wrap{animation-delay:.05s}
"""

# Dashboard-only additions: the sticky segmented tab bar + panels. Appended to
# _BASE_CSS for the dashboard shell only.
_DASH_EXTRA_CSS = """
/* ── Tab bar (segmented control) ───────────────────── */
.dash-tabs{display:flex;align-items:center;gap:6px;height:52px;background:var(--surface);border-bottom:1px solid var(--line);padding:0 36px;position:sticky;top:0;z-index:100}
.tab-btn{font-family:var(--mono);padding:7px 16px;font-size:12px;font-weight:600;color:var(--ink-2);border:none;background:none;cursor:pointer;border-radius:6px;text-transform:uppercase;letter-spacing:.06em;transition:background .15s,color .15s}
.tab-btn:hover{background:var(--surface-2);color:var(--ink)}
.tab-btn.active{color:var(--accent);background:var(--accent-soft)}
.tab-panel{display:none}
.tab-panel.active{display:block}
.tab-empty{padding:80px 48px;color:var(--ink-3);font-style:italic;font-size:14px}
/* Nested report bodies keep their own sticky sidebar below the tab bar. */
.tab-panel .sidebar{top:52px;height:calc(100vh - 52px)}
"""

# Responsive rules shared by both shells. References only classes that exist in
# both bodies; injected after _BASE_CSS (+ any extra) so the breakpoint wins.
_RESPONSIVE_CSS = """
/* ── Responsive (phones / small tablets) ───────────── */
@media (max-width:820px){
  body{font-size:13.5px}
  .page-header{padding:20px 16px 0}
  .page-header h1{font-size:23px}
  .page-header .meta{margin-bottom:18px}
  .stat-cell{padding:11px 18px 13px}
  .stat-cell:first-child{padding-left:0}
  .stat-num{font-size:20px}
  .layout{grid-template-columns:1fr}
  .sidebar{position:static;height:auto;max-height:320px;overflow-y:auto;border-right:none;border-bottom:1px solid var(--line);padding:14px 14px;display:block}
  .tab-panel .sidebar{top:0;height:auto}
  .sb-search-wrap{padding:0 0 10px}
  .sidebar-label{margin:10px 0 6px;padding:0 4px}
  .sb-item{margin-bottom:2px}
  .main{padding:24px 14px 56px}
  .section-label{margin-bottom:12px}
  .heatmap-wrap{margin-bottom:36px;-webkit-overflow-scrolling:touch}
  .mgr-section{margin-bottom:36px}
  .mgr-header h2{font-size:16px}
  .event{padding:14px 15px}
  .event-header{gap:8px}
  .ev-partner{font-size:13.5px}
  .ev-date{margin-left:0}
  .dash-tabs{padding:0 12px;height:48px}
  .tab-btn{padding:7px 12px}
  .tab-empty{padding:48px 18px}
}
@media (prefers-reduced-motion:reduce){
  *{animation:none!important;transition:none!important;scroll-behavior:auto!important}
}
"""

# Client-side navigation. Scoped per ``[data-report]`` root so the dashboard's
# two embedded report bodies (weekly + monthly) never cross-wire. Adds: the
# ``All`` view (default — full report), a per-manager single view (heat-map
# hidden, only that manager's dossier shown), a "← All" back control, and a
# search box that filters ONLY the roster list without touching the open view.
# Pure presentation: no data, no routing, degrades to the full page if JS is off.
_NAV_SCRIPT = """
<script>
(function(){
  function initReport(root){
    var search = root.querySelector('[data-search]');
    var empty = root.querySelector('[data-empty]');
    function show(view){
      root.classList.toggle('mode-all', view === 'all');
      root.classList.toggle('mode-single', view !== 'all');
      root.querySelectorAll('.mgr-section').forEach(function(s){
        s.style.display = (view === 'all' || s.getAttribute('data-mgr') === view) ? '' : 'none';
      });
      root.querySelectorAll('.sidebar [data-view]').forEach(function(b){
        b.classList.toggle('active', b.getAttribute('data-view') === view);
      });
      if (view !== 'all') { window.scrollTo(0, 0); }
    }
    root.querySelectorAll('[data-view]').forEach(function(el){
      el.addEventListener('click', function(e){ e.preventDefault(); show(el.getAttribute('data-view')); });
    });
    if (search){
      search.addEventListener('input', function(){
        var q = search.value.trim().toLowerCase(), shown = 0;
        root.querySelectorAll('.sidebar .sb-item[data-view]').forEach(function(b){
          if (b.getAttribute('data-view') === 'all') { return; }
          var name = (b.getAttribute('data-name') || b.textContent).toLowerCase();
          var hit = name.indexOf(q) >= 0;
          b.style.display = hit ? '' : 'none';
          if (hit) { shown++; }
        });
        if (empty) { empty.style.display = shown ? 'none' : 'block'; }
      });
    }
    show('all');
  }
  document.querySelectorAll('[data-report]').forEach(initReport);
})();
</script>
"""


def _page(title_html: str, body: str, *, extra_css: str = "") -> str:
    """Assemble a full HTML document from the shared head + given body/CSS."""
    css = _BASE_CSS + extra_css + _RESPONSIVE_CSS
    # Hard-set the light "paper briefing" theme: this is a light-theme product,
    # so the page must NOT follow a viewer's dark OS (prefers-color-scheme). The
    # dark "control room" tokens stay in _BASE_CSS but dormant — reachable only
    # if a future build flips data-theme (e.g. a user toggle).
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ru" data-theme="light">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{_FONT_LINK}\n"
        f"<title>{title_html}</title>\n"
        f"<style>{css}</style>\n"
        "</head>\n<body>\n"
        f"{body}\n"
        f"{_NAV_SCRIPT}\n"
        "</body>\n</html>\n"
    )


# ---------------------------------------------------------------------------
# Report body (Jinja markup; autoescape=True for XSS safety)
# ---------------------------------------------------------------------------

_REPORT_BODY = """\
<a id="top"></a>
<div class="accent-bar"></div>
<div class="report mode-all" data-report>

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
  <div class="sb-search-wrap">
    <input type="search" class="sb-search" data-search placeholder="Search AffID / manager…" aria-label="Search AffID or manager">
  </div>
  <button type="button" class="sb-item sb-all active" data-view="all" data-name="all">
    <span class="sb-name">All</span>
    {% if stats.critical %}<span class="sb-crit">{{ stats.critical }}!</span>{% endif %}
    <span class="sb-count">{{ stats.total_events }}</span>
  </button>
  <span class="sidebar-label">Managers</span>
  {% for m in managers %}
  <button type="button" class="sb-item" data-view="{{ m.id }}" data-name="{{ m.name }}">
    <span class="sb-name">{{ m.name }}</span>
    {% if m.total == 0 %}
      <span class="sb-clean">✓</span>
    {% else %}
      {% if m.critical_count %}<span class="sb-crit">{{ m.critical_count }}!</span>{% endif %}
      <span class="sb-count">{{ m.total }}</span>
    {% endif %}
  </button>
  {% endfor %}
  <p class="sb-empty" data-empty>No match.</p>
</aside>

<main class="main">

  <div class="view-all-only">
  <p class="section-label">Signal Matrix</p>
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
        <td class="mgr-cell"><a href="#" data-view="{{ m.id }}">{{ m.name }}</a></td>
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
  </div>

  {% for m in managers %}
  <section class="mgr-section" data-mgr="{{ m.id }}">
    <div class="mgr-header">
      <button type="button" class="mgr-back" data-view="all">← All</button>
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
        {% if ev.author %}<div class="ev-author">✍ {{ ev.author }}{% if ev.author_role %} <span class="ev-role {{ ev.author_role }}">{{ {'internal':'сотрудник','partner':'партнёр','anonymous_admin':'анон-админ'}.get(ev.author_role, ev.author_role) }}</span>{% endif %}</div>{% endif %}
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
</div>
"""

_env = Environment(autoescape=True)
_template = _env.from_string(_page("{{ title }}", _REPORT_BODY))


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
            author=str(row["author_name"]) if row.get("author_name") else None,
            author_role=str(row["author_role"]) if row.get("author_role") else None,
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


_DASH_BODY = """\
<div class="accent-bar"></div>
<div class="dash-tabs">
  <button class="tab-btn active" data-tab="weekly" onclick="showTab('weekly')">Weekly</button>
  <button class="tab-btn" data-tab="monthly" onclick="showTab('monthly')">Monthly</button>
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
"""

_dash_template = _env.from_string(
    _page("Risk Reports Dashboard", _DASH_BODY, extra_css=_DASH_EXTRA_CSS)
)


def build_dashboard_html(
    *, weekly_html: str | None, monthly_html: str | None
) -> str:
    """Render a tabbed dashboard combining the latest weekly and monthly reports."""
    wb = Markup(_extract_body(weekly_html)) if weekly_html else None
    mb = Markup(_extract_body(monthly_html)) if monthly_html else None
    return str(_dash_template.render(weekly_body=wb, monthly_body=mb))
