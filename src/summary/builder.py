"""HTML report builder for weekly/monthly chat-centric summaries. Phase 16.

The roster is one-per-monitored-chat (``chats`` where status='active'); each chat
shows the human who authorised it (``manager_name``). It is NOT keyed on the
per-affiliate ``internal_users`` role=manager stubs.

Produces a single self-contained HTML page:
  - Header with period dates and generation time
  - Navigation TOC (jump links per chat)
  - Risk-by-category summary + per-chat cards
  - Per-chat timeline sections with color-coded event cards

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
from html import escape as _hesc
from typing import Any
from uuid import UUID

from jinja2 import Environment
from markupsafe import Markup

from src.utils.text import short_why

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


def _chat_name(chat: dict[str, Any]) -> str:
    """Display name for a monitored chat unit.

    The chat title (``chat_name``, e.g. ``78516 | Acme Media | Beton.Win``) is the
    primary label; a forum topic appends ``/ {topic_name}`` so the two topics of
    one group don't collide. Falls back to a short id when a title is missing.
    """
    base = (chat.get("chat_name") or "").strip()
    topic = (chat.get("topic_name") or "").strip()
    if base and topic:
        return f"{base} / {topic}"
    if base:
        return base
    return f"chat {str(chat.get('id') or '')[:8]}" if chat.get("id") else "Unnamed chat"


def _chat_slug(chat: dict[str, Any], taken: set[str]) -> str:
    """URL-safe handle for a chat's #hash deep-link.

    Derived from the chat title (+ topic), sanitised to ``[a-z0-9-]`` and
    de-duplicated so two chats never collide on one hash.
    """
    raw = _chat_name(chat)
    base = _re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-") or "chat"
    slug = base
    i = 2
    while slug in taken:
        slug = f"{base}-{i}"
        i += 1
    taken.add(slug)
    return slug


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
    date_iso: str    # YYYY-MM-DD — client-side date-range filter key (monthly)


@dataclass
class ChatUnit:
    id: str            # chat UUID as string — the data-view / data-mgr nav key
    slug: str          # URL-safe handle (chat title) for the #hash deep-link
    name: str          # chat display title (partner_name — derived from chat_name)
    manager_name: str  # human who authorised the chat, or "unassigned"
    is_test: bool          # test chat — hidden by default, toggled in the filter panel
    manager_is_test: bool  # authorising manager is a test account
    proposals: int         # manager_proposal signals in the period (0 = not shown)
    events: list[EventData]
    total: int
    critical_count: int
    high_count: int
    medium_count: int
    low_count: int
    cats: list[str]        # distinct risk_types present (for the category filter)


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

/* ── Date-range picker (monthly report only) ───────── */
.daterange{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:13px 48px;background:var(--surface);border-bottom:1px solid var(--line)}
.dr-label{font-family:var(--mono);font-size:10px;font-weight:600;color:var(--ink-3);text-transform:uppercase;letter-spacing:.14em}
.dr-input{font-family:var(--mono);font-size:12px;color:var(--ink);background:var(--surface-2);border:1px solid var(--line);border-radius:6px;padding:6px 10px;outline:none;transition:border-color .12s;color-scheme:light}
.dr-input:focus{border-color:var(--accent)}
.dr-sep{color:var(--ink-3)}
.dr-reset{font-family:var(--mono);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-2);background:var(--surface-2);border:1px solid var(--line);border-radius:6px;padding:6px 12px;cursor:pointer;transition:background .12s,color .12s}
.dr-reset:hover{background:var(--accent-soft);color:var(--accent)}

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
.sb-prop{flex-shrink:0;font-family:var(--mono);font-size:10px;font-weight:700;color:var(--ok);background:rgba(46,125,91,.12);border-radius:3px;padding:1px 5px;line-height:16px}

/* ── Filter panel ──────────────────────────────────── */
.sb-search-wrap{display:flex;gap:6px;align-items:center;padding:0 6px 12px}
.sb-search-wrap .sb-search{flex:1}
.sb-filter-btn{flex-shrink:0;width:34px;height:34px;display:flex;align-items:center;justify-content:center;font-size:14px;color:var(--ink-2);background:var(--surface-2);border:1px solid var(--line);border-radius:6px;cursor:pointer;transition:background .12s,color .12s,border-color .12s}
.sb-filter-btn:hover,.sb-filter-btn.active{background:var(--accent-soft);color:var(--accent);border-color:var(--accent)}
.filter-panel{margin:0 6px 12px;padding:12px 12px 10px;background:var(--surface-2);border:1px solid var(--line);border-radius:8px;box-shadow:var(--shadow)}
.filter-panel[hidden]{display:none}
.fp-sec{margin-bottom:12px}
.fp-label{font-family:var(--mono);font-size:9.5px;font-weight:600;color:var(--ink-3);text-transform:uppercase;letter-spacing:.14em;margin-bottom:6px}
.fp-check{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--ink-2);padding:3px 2px;cursor:pointer;line-height:1.3}
.fp-check span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fp-check input{flex-shrink:0;accent-color:var(--accent);cursor:pointer}
.fp-reset{width:100%;font-family:var(--mono);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-2);background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:7px;cursor:pointer;transition:background .12s,color .12s}
.fp-reset:hover{background:var(--accent-soft);color:var(--accent)}
.mc-prop{color:var(--ok);font-weight:600}
.test-tag{background:var(--surface-2);border-color:var(--line);color:var(--ink-3);letter-spacing:.08em}
.prop-tag{background:rgba(46,125,91,.10);border-color:transparent;color:var(--ok)}

/* ── Daily: active-chats list (two columns) ────────── */
.dc-list{display:grid;grid-template-columns:repeat(2,1fr);gap:2px 28px;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 20px 16px;box-shadow:var(--shadow)}
.dc-row{display:flex;align-items:center;gap:10px;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--line-2)}
.dc-name{font-family:var(--mono);font-size:12.5px;color:var(--ink-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dc-count{flex-shrink:0;font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:12px;font-weight:700;color:var(--ink)}
.dc-head .dc-name,.dc-head .dc-count{font-size:10px;font-weight:600;color:var(--ink-3);text-transform:uppercase;letter-spacing:.1em}
.dc-head{border-bottom:1px solid var(--line)}
@media (max-width:820px){.dc-list{grid-template-columns:1fr}}

/* ── Main ──────────────────────────────────────────── */
.main{padding:38px 48px 72px;min-width:0;max-width:1200px}
.section-label{font-family:var(--mono);font-size:11px;font-weight:600;color:var(--ink-3);text-transform:uppercase;letter-spacing:.16em;margin-bottom:16px;display:flex;align-items:center;gap:12px}
.section-label::after{content:"";flex:1;height:1px;background:var(--line)}

/* ── Portfolio summary: clean banner (All view, zero risk) ── */
.portfolio-clean{display:flex;align-items:center;gap:18px;background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--ok);border-radius:10px;padding:22px 26px;margin-bottom:44px;box-shadow:var(--shadow)}
.pc-check{flex-shrink:0;width:38px;height:38px;display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:700;color:var(--ok);background:rgba(46,125,91,.12);border-radius:50%}
.pc-title{font-family:var(--display);font-size:18px;font-weight:800;color:var(--ink);letter-spacing:-.02em}
.pc-sub{color:var(--ink-2);font-size:13px;margin-top:3px}

/* ── Portfolio summary: risk-by-category bars (All view) ──── */
.cat-list{display:flex;flex-direction:column;gap:9px;margin-bottom:52px;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:22px 26px;box-shadow:var(--shadow)}
.cat-row{display:grid;grid-template-columns:150px 1fr 34px;align-items:center;gap:16px}
.cat-label{font-size:12.5px;color:var(--ink-2);font-weight:500;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cat-track{height:14px;border-radius:4px;background:var(--surface-2);overflow:hidden}
.cat-fill{display:flex;height:100%;min-width:4px;border-radius:4px;overflow:hidden}
.seg{display:block;min-width:0}
.cat-fill .seg.hi{background:var(--crit)}
.cat-fill .seg.lo{background:var(--med)}
.cat-count{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:13px;font-weight:700;color:var(--ink)}
.cat-legend{display:flex;gap:18px;margin:-38px 0 52px;font-family:var(--mono);font-size:10.5px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.06em}
.cat-legend span{display:inline-flex;align-items:center;gap:6px}
.cat-legend i{width:9px;height:9px;border-radius:2px;display:inline-block}
.cat-legend i.hi{background:var(--crit)}
.cat-legend i.lo{background:var(--med)}

/* ── Portfolio summary: manager cards (All view) ──────────── */
.mgr-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(232px,1fr));gap:14px;margin-bottom:44px}
.mgr-card{display:flex;flex-direction:column;gap:11px;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px 18px;text-decoration:none;color:inherit;box-shadow:var(--shadow);transition:box-shadow .16s ease,transform .16s ease,border-color .16s ease}
.mgr-card:hover{box-shadow:var(--shadow-lift);transform:translateY(-1px);border-color:var(--accent)}
.mgr-card.is-clean{opacity:.66}
.mgr-card.is-clean:hover{opacity:1}
.mc-top{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.mc-name{font-family:var(--mono);font-size:12.5px;font-weight:600;color:var(--ink);letter-spacing:-.01em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mc-total{flex-shrink:0;font-family:var(--mono);font-size:22px;font-weight:700;line-height:1;color:var(--ink);font-variant-numeric:tabular-nums}
.mc-clean{flex-shrink:0;font-size:12px;color:var(--ok);font-weight:600}
.mc-pills{display:flex;flex-wrap:wrap;gap:5px}
.mc-pill{font-family:var(--mono);font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;border:1px solid;text-transform:uppercase;letter-spacing:.04em}
.mc-pill.crit{color:var(--crit);background:var(--crit-bg);border-color:var(--crit-line)}
.mc-pill.high{color:var(--high);background:var(--high-bg);border-color:var(--high-line)}
.mc-pill.med{color:var(--med);background:var(--med-bg);border-color:var(--med-line)}
.mc-bar{display:flex;height:6px;border-radius:3px;overflow:hidden;background:var(--surface-2)}
.mc-bar .seg.crit{background:var(--crit)}
.mc-bar .seg.high{background:var(--high)}
.mc-bar .seg.med{background:var(--med)}
.mc-bar .seg.low{background:var(--low)}
.mc-mgr{font-family:var(--mono);font-size:10.5px;color:var(--ink-3);letter-spacing:-.01em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:-4px}

/* ── Signal matrix (heat-map) — retained for transitional stored
      bodies embedded in the dashboard during the report expiry window;
      new reports no longer emit the matrix (superseded by the summary). ── */
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
.mgr-sub{font-family:var(--mono);font-size:12px;font-weight:500;color:var(--ink-2);background:var(--surface-2);border:1px solid var(--line);border-radius:5px;padding:3px 10px}
.mgr-pills{display:contents}
.pill{font-family:var(--mono);font-size:11px;font-weight:600;background:var(--surface-2);border:1px solid var(--line);border-radius:5px;padding:3px 10px;color:var(--ink-2)}
.pill.crit{color:var(--crit);background:var(--crit-bg);border-color:var(--crit-line)}
.pill.high{color:var(--high);background:var(--high-bg);border-color:var(--high-line)}
.pill.med{color:var(--med);background:var(--med-bg);border-color:var(--med-line)}
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
.mgr-section,.heatmap-wrap,.cat-list,.mgr-cards,.portfolio-clean{animation:rise .5s ease both}
.mgr-cards{animation-delay:.05s}
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
  .daterange{padding:11px 16px;gap:8px}
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
  .cat-list{padding:16px 16px;margin-bottom:36px}
  .cat-row{grid-template-columns:96px 1fr 28px;gap:10px}
  .cat-label{font-size:11.5px}
  .cat-legend{margin:-28px 0 36px}
  .mgr-cards{grid-template-columns:1fr 1fr;gap:10px;margin-bottom:32px}
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
# two embedded report bodies (weekly + monthly) never cross-wire. Two views:
#   • ``All`` (default) — the portfolio *summary* (category bars + manager cards);
#     every per-manager dossier is hidden here.
#   • a per-AffID *detail* view — the summary hides, only that manager's dossier
#     shows, with a "← All" control and the roster item highlighted.
# When the page holds exactly ONE report (the standalone /r/{token} page), the
# selected AffID is mirrored to the URL as ``#{slug}`` so a view is shareable and
# deep-linkable; the dashboard (two roots) skips the hash to stay unambiguous.
# The roster search filters ONLY the sidebar list, never the open view. Pure
# presentation — degrades to the full stacked report if JS is off.
_NAV_SCRIPT = """
<script>
(function(){
  function initReport(root, useHash){
    if (root.hasAttribute('data-nav-ready')) { return; }
    root.setAttribute('data-nav-ready', '1');
    var search = root.querySelector('[data-search]');
    var empty = root.querySelector('[data-empty]');
    var slugToView = {}, viewToSlug = {};
    root.querySelectorAll('[data-slug]').forEach(function(el){
      var v = el.getAttribute('data-view') || el.getAttribute('data-mgr');
      var s = el.getAttribute('data-slug');
      if (v && s){ slugToView[s] = v; viewToSlug[v] = s; }
    });
    function apply(view){
      root.classList.toggle('mode-all', view === 'all');
      root.classList.toggle('mode-single', view !== 'all');
      // Dossiers show ONLY in a specific manager's detail view; All is summary-only.
      root.querySelectorAll('.mgr-section').forEach(function(s){
        s.style.display = (view !== 'all' && s.getAttribute('data-mgr') === view) ? '' : 'none';
      });
      root.querySelectorAll('.sidebar [data-view]').forEach(function(b){
        b.classList.toggle('active', b.getAttribute('data-view') === view);
      });
      if (view !== 'all') { window.scrollTo(0, 0); }
    }
    function show(view){
      apply(view);
      if (useHash){
        var s = viewToSlug[view];
        if (view !== 'all' && s){
          if (location.hash !== '#' + s) { history.replaceState(null, '', '#' + s); }
        } else if (location.hash){
          history.replaceState(null, '', location.pathname + location.search);
        }
      }
    }
    // Delegated so dynamically re-rendered cards (monthly date filter) still work.
    root.addEventListener('click', function(e){
      var el = e.target.closest ? e.target.closest('[data-view]') : null;
      if (el && root.contains(el)) { e.preventDefault(); show(el.getAttribute('data-view')); }
    });
    // Search + faceted filtering are owned by the filter script (initFilters),
    // so both write sidebar/card visibility through one code path.
    function fromHash(){
      var h = decodeURIComponent((location.hash || '').replace(/^#/, ''));
      return (h && slugToView[h]) ? slugToView[h] : 'all';
    }
    if (useHash){
      window.addEventListener('hashchange', function(){ apply(fromHash()); });
      apply(fromHash());
    } else {
      apply('all');
    }
  }
  var roots = document.querySelectorAll('[data-report]');
  var useHash = roots.length === 1;
  roots.forEach(function(r){ initReport(r, useHash); });
})();
</script>
"""

# Faceted filter panel (search + managers + risk categories + test toggles).
# Single authority over sidebar-item and All-view card visibility (the nav
# script deliberately no longer touches search). Test chats/managers are hidden
# on first apply() (their toggles start unchecked). Exposes ``root.__applyFilters``
# so the monthly date-range recompute can re-apply after it rebuilds the cards.
_FILTER_SCRIPT = """
<script>
(function(){
  function initFilters(root){
    if(root.hasAttribute('data-flt-ready'))return;
    root.setAttribute('data-flt-ready','1');
    var search=root.querySelector('[data-search]');
    var empty=root.querySelector('[data-empty]');
    var panel=root.querySelector('[data-filter-panel]');
    var toggle=root.querySelector('[data-filter-toggle]');
    if(toggle&&panel){
      toggle.addEventListener('click',function(){
        if(panel.hasAttribute('hidden')){panel.removeAttribute('hidden');toggle.classList.add('active');}
        else{panel.setAttribute('hidden','');toggle.classList.remove('active');}
      });
    }
    function checkedVals(sel){var o={};root.querySelectorAll(sel).forEach(function(c){if(c.checked)o[c.value]=1;});return o;}
    function anyUnchecked(sel){var u=false;root.querySelectorAll(sel).forEach(function(c){if(!c.checked)u=true;});return u;}
    function passes(el){
      var q=(search&&search.value.trim().toLowerCase())||'';
      if(q){var hay=(el.getAttribute('data-filter')||el.getAttribute('data-name')||'').toLowerCase();if(hay.indexOf(q)<0)return false;}
      var stc=root.querySelector('[data-show-test-chats]'),stm=root.querySelector('[data-show-test-mgr]');
      if(el.getAttribute('data-test')==='1' && !(stc&&stc.checked))return false;
      if(el.getAttribute('data-mgrtest')==='1' && !(stm&&stm.checked))return false;
      if(el.getAttribute('data-mgrtest')!=='1'){
        var boxes=root.querySelectorAll('[data-mgr-filter]');
        if(boxes.length){var mc=checkedVals('[data-mgr-filter]');if(!mc[el.getAttribute('data-manager')||''])return false;}
      }
      if(anyUnchecked('[data-cat-filter]')){
        var cc=checkedVals('[data-cat-filter]'),cats=(el.getAttribute('data-cats')||'').split(' ').filter(Boolean),hit=false;
        for(var i=0;i<cats.length;i++){if(cc[cats[i]]){hit=true;break;}}
        if(!hit)return false;
      }
      return true;
    }
    function apply(){
      var shown=0;
      root.querySelectorAll('.sidebar .sb-item[data-view]').forEach(function(b){
        if(b.getAttribute('data-view')==='all')return;
        var ok=passes(b);b.style.display=ok?'':'none';if(ok)shown++;
      });
      if(empty)empty.style.display=shown?'none':'block';
      root.querySelectorAll('.mgr-cards .mgr-card[data-view]').forEach(function(c){c.style.display=passes(c)?'':'none';});
    }
    root.__applyFilters=apply;
    if(search)search.addEventListener('input',apply);
    root.querySelectorAll('[data-mgr-filter],[data-cat-filter],[data-show-test-chats],[data-show-test-mgr]').forEach(function(c){c.addEventListener('change',apply);});
    var reset=root.querySelector('[data-filter-reset]');
    if(reset)reset.addEventListener('click',function(){
      root.querySelectorAll('[data-mgr-filter],[data-cat-filter]').forEach(function(c){c.checked=true;});
      root.querySelectorAll('[data-show-test-chats],[data-show-test-mgr]').forEach(function(c){c.checked=false;});
      if(search)search.value='';
      apply();
    });
    apply();
  }
  document.querySelectorAll('[data-report]').forEach(initFilters);
})();
</script>
"""

# Monthly-only client-side date-range filter. Emitted (with the data island +
# picker) only when period_type == "monthly". It recomputes every aggregate from
# the embedded per-event dates for the chosen sub-range — stat strip, sidebar
# counts, category bars, manager cards, per-manager dossiers (event cards +
# pills), proposals count, and the period label. It does NOT render on load
# (the server HTML already shows the full month); it engages only when the user
# picks a range. Scoped to the root that carries [data-month-data]; the weekly
# root (dashboard) has none and is skipped. Events without a date are dropped
# from a sub-range (their bucket is unknown) rather than mis-placed.
_MONTH_FILTER_SCRIPT = """
<script>
(function(){
  var MONTHS=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function fmt(iso){var p=(iso||'').split('-');if(p.length!==3)return iso||'';return p[2]+' '+(MONTHS[parseInt(p[1],10)-1]||p[1])+' '+p[0];}
  function initMonth(root){
    if(root.hasAttribute('data-month-ready'))return;
    var dataEl=root.querySelector('[data-month-data]');
    if(!dataEl)return;
    root.setAttribute('data-month-ready','1');
    var D;try{D=JSON.parse(dataEl.textContent);}catch(e){return;}
    var fromI=root.querySelector('[data-range-from]'),toI=root.querySelector('[data-range-to]'),
        resetB=root.querySelector('[data-range-reset]'),periodEl=root.querySelector('[data-period]');
    if(!fromI||!toI)return;
    var catOrder={},catLabel={};
    (D.categories||[]).forEach(function(c,i){catOrder[c[0]]=i;catLabel[c[0]]=c[1];});
    var lo0=D.periodStart,hi0=D.periodEnd;
    fromI.min=lo0;fromI.max=hi0;fromI.value=lo0;
    toI.min=lo0;toI.max=hi0;toI.value=hi0;

    function inRange(d,lo,hi){return d>=lo&&d<=hi;}
    function badges(total,crit,isAll){
      if(total===0&&!isAll)return '<span class="sb-clean">\\u2713</span>';
      var s='';if(crit)s+='<span class="sb-crit">'+crit+'!</span>';s+='<span class="sb-count">'+total+'</span>';return s;
    }
    function catRows(cat){
      var arr=[],max=0,k;
      for(k in cat){arr.push({key:k,total:cat[k].total,hi:cat[k].hi,lo:cat[k].lo});if(cat[k].total>max)max=cat[k].total;}
      arr.sort(function(a,b){var o1=catOrder[a.key]==null?99:catOrder[a.key],o2=catOrder[b.key]==null?99:catOrder[b.key];return (b.total-a.total)||(o1-o2);});
      return arr.map(function(c){
        var pct=max?Math.max(8,Math.round(c.total/max*100)):0;
        return '<div class="cat-row"><span class="cat-label">'+esc(catLabel[c.key]||c.key)+'</span>'
          +'<span class="cat-track"><span class="cat-fill" style="width:'+pct+'%">'
          +'<span class="seg hi" style="flex:'+c.hi+'"></span><span class="seg lo" style="flex:'+c.lo+'"></span>'
          +'</span></span><span class="cat-count">'+c.total+'</span></div>';
      }).join('');
    }
    function cards(perMgr){
      // Only chats with risk signals in the selected range (matches server render).
      return (D.managers||[]).map(function(m){
        var pm=perMgr[m.id]||{crit:0,high:0,med:0,low:0,total:0};
        if(pm.total===0)return '';
        var head='<div class="mc-top"><span class="mc-name">'+esc(m.name)+'</span>'
          +'<span class="mc-total">'+pm.total+'</span></div>'
          +'<span class="mc-mgr">Manager: '+esc(m.managerName||'unassigned')+'</span>';
        var pills='';if(pm.crit)pills+='<span class="mc-pill crit">'+pm.crit+' crit</span>';
        if(pm.high)pills+='<span class="mc-pill high">'+pm.high+' high</span>';
        if(pm.med)pills+='<span class="mc-pill med">'+pm.med+' med</span>';
        var bar='';if(pm.crit)bar+='<span class="seg crit" style="flex:'+pm.crit+'"></span>';
        if(pm.high)bar+='<span class="seg high" style="flex:'+pm.high+'"></span>';
        if(pm.med)bar+='<span class="seg med" style="flex:'+pm.med+'"></span>';
        if(pm.low)bar+='<span class="seg low" style="flex:'+pm.low+'"></span>';
        var body='<div class="mc-pills">'+pills+'</div><div class="mc-bar">'+bar+'</div>';
        return '<a href="#" class="mgr-card" data-view="'+esc(m.id)+'" data-slug="'+esc(m.slug)+'" data-name="'+esc(m.name)+'" data-manager="'+esc(m.managerName||'')+'" data-cats="'+esc((m.cats||[]).join(' '))+'" data-test="0" data-mgrtest="0">'+head+body+'</a>';
      }).join('');
    }
    function setStat(n,v){var el=root.querySelector('[data-stat="'+n+'"]');if(el)el.textContent=v;}

    function render(lo,hi){
      var evs=(D.events||[]).filter(function(e){return e.d&&inRange(e.d,lo,hi);});
      var stats={total:evs.length,critical:0,high:0,medium:0,low:0},perMgr={},cat={};
      evs.forEach(function(e){
        if(e.lvl==='critical')stats.critical++;else if(e.lvl==='high')stats.high++;else if(e.lvl==='medium')stats.medium++;else stats.low++;
        var pm=perMgr[e.m]||(perMgr[e.m]={crit:0,high:0,med:0,low:0,total:0});pm.total++;
        if(e.lvl==='critical')pm.crit++;else if(e.lvl==='high')pm.high++;else if(e.lvl==='medium')pm.med++;else pm.low++;
        var c=cat[e.type]||(cat[e.type]={hi:0,lo:0,total:0});c.total++;
        if(e.lvl==='critical'||e.lvl==='high')c.hi++;else c.lo++;
      });
      var proposals=(D.proposalDates||[]).filter(function(d){return d&&inRange(d,lo,hi);}).length;
      var chatsAdded=(D.chatsAddedDates||[]).filter(function(d){return d&&inRange(d,lo,hi);}).length;
      var mgrs=D.managers||[],flagged=0;mgrs.forEach(function(m){if(perMgr[m.id]&&perMgr[m.id].total>0)flagged++;});
      setStat('total',stats.total);setStat('critical',stats.critical);setStat('high',stats.high);
      setStat('medium',stats.medium);setStat('proposals',proposals);setStat('chatsadded',chatsAdded);setStat('flagged',flagged+'/'+mgrs.length);
      if(periodEl)periodEl.textContent=fmt(lo)+' \\u2013 '+fmt(hi);
      var allItem=root.querySelector('.sidebar [data-view="all"]');
      if(allItem)allItem.innerHTML='<span class="sb-name">All</span>'+badges(stats.total,stats.critical,true);
      root.querySelectorAll('.sidebar .sb-item[data-view]').forEach(function(b){
        var v=b.getAttribute('data-view');if(v==='all')return;
        var pm=perMgr[v]||{total:0,crit:0};
        b.innerHTML='<span class="sb-name">'+esc(b.getAttribute('data-name'))+'</span>'+badges(pm.total,pm.crit,false);
      });
      var summary=root.querySelector('[data-summary]');
      if(summary){
        if(stats.total===0){
          summary.innerHTML='<div class="portfolio-clean"><span class="pc-check">\\u2713</span><div>'
            +'<p class="pc-title">No risk signals in selected range</p>'
            +'<p class="pc-sub">All '+mgrs.length+' monitored chat'+(mgrs.length!==1?'s':'')+' clean. '
            +proposals+' manager proposal'+(proposals!==1?'s':'')+' logged.</p></div></div>';
        }else{
          summary.innerHTML='<p class="section-label">Risk by Category</p><div class="cat-list">'+catRows(cat)+'</div>'
            +'<div class="cat-legend"><span><i class="hi"></i>Critical / High</span><span><i class="lo"></i>Medium / Low</span></div>'
            +'<p class="section-label">Chats</p><div class="mgr-cards">'+cards(perMgr)+'</div>';
        }
      }
      mgrs.forEach(function(m){
        var sec=root.querySelector('.mgr-section[data-mgr="'+m.id+'"]');if(!sec)return;
        var pm=perMgr[m.id]||{total:0,crit:0,high:0,med:0,low:0};
        var evcards=sec.querySelectorAll('.event'),vis=0;
        evcards.forEach(function(c){var d=c.getAttribute('data-date');var sh=d&&inRange(d,lo,hi);c.style.display=sh?'':'none';if(sh)vis++;});
        var pillsEl=sec.querySelector('[data-mgr-pills]');
        if(pillsEl){var h='<span class="pill">'+pm.total+' event'+(pm.total!==1?'s':'')+'</span>';
          if(pm.crit)h+='<span class="pill crit">'+pm.crit+' critical</span>';
          if(pm.high)h+='<span class="pill high">'+pm.high+' high</span>';
          if(pm.med)h+='<span class="pill med">'+pm.med+' medium</span>';pillsEl.innerHTML=h;}
        var note=sec.querySelector('[data-no-range]');
        if(note)note.style.display=(evcards.length>0&&vis===0)?'':'none';
      });
      // Re-apply the faceted filters to the freshly-rebuilt cards/sidebar.
      if(root.__applyFilters)root.__applyFilters();
    }
    function apply(){
      var lo=fromI.value||lo0,hi=toI.value||hi0;
      if(lo>hi){var t=lo;lo=hi;hi=t;}
      if(lo<lo0)lo=lo0;if(hi>hi0)hi=hi0;
      render(lo,hi);
    }
    fromI.addEventListener('change',apply);
    toI.addEventListener('change',apply);
    if(resetB)resetB.addEventListener('click',function(){fromI.value=lo0;toI.value=hi0;apply();});
  }
  document.querySelectorAll('[data-report]').forEach(initMonth);
})();
</script>
"""


# A floating "save as PDF" affordance (top-right). Uses the browser's own
# print-to-PDF — no server-side rendering. Hidden when actually printing, and the
# interactive chrome (sidebar/search/date picker/back button) is stripped so the
# printed/PDF page is clean content only. Cards avoid mid-card page breaks.
_PRINT_CSS = """
.print-btn{position:fixed;top:14px;right:16px;z-index:60;cursor:pointer;
  font:600 12px/1 var(--mono);letter-spacing:.05em;color:var(--ink);
  background:var(--surface);border:1px solid var(--line);border-radius:8px;
  padding:9px 13px;box-shadow:var(--shadow-lift)}
.print-btn:hover{background:var(--surface-2)}
@media print{
  .print-btn{display:none!important}
  .sidebar,.daterange,.mgr-back,[data-search],.accent-bar{display:none!important}
  body{background:#fff}
  .event,.mgr-section,.mgr-card,.cat-row{break-inside:avoid;page-break-inside:avoid}
}
"""
_PRINT_BTN = (
    '<button type="button" class="print-btn" onclick="window.print()" '
    'aria-label="Сохранить как PDF">⭳ PDF</button>'
)


def _page(title_html: str, body: str, *, extra_css: str = "") -> str:
    """Assemble a full HTML document from the shared head + given body/CSS."""
    css = _BASE_CSS + extra_css + _RESPONSIVE_CSS + _PRINT_CSS
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
        f"{_PRINT_BTN}\n"
        f"{body}\n"
        f"{_NAV_SCRIPT}\n"
        f"{_FILTER_SCRIPT}\n"
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
  <p class="meta">Period: <strong data-period>{{ period_label }}</strong>&nbsp;&nbsp;·&nbsp;&nbsp;Generated: {{ generated_at }}</p>
  <div class="stat-strip">
    <div class="stat-cell"><span class="stat-num" data-stat="total">{{ stats.total_events }}</span><span class="stat-lbl">Risk events</span></div>
    <div class="stat-cell"><span class="stat-num crit" data-stat="critical">{{ stats.critical }}</span><span class="stat-lbl">Critical</span></div>
    <div class="stat-cell"><span class="stat-num high" data-stat="high">{{ stats.high }}</span><span class="stat-lbl">High</span></div>
    <div class="stat-cell"><span class="stat-num" data-stat="medium">{{ stats.medium }}</span><span class="stat-lbl">Medium</span></div>
    <div class="stat-cell"><span class="stat-num" data-stat="proposals">{{ stats.proposals }}</span><span class="stat-lbl">Mgr proposals</span></div>
    <div class="stat-cell"><span class="stat-num" data-stat="chatsadded">{{ stats.chats_added }}</span><span class="stat-lbl">New chats</span></div>
    <div class="stat-cell"><span class="stat-num" data-stat="flagged">{{ stats.flagged }}/{{ stats.chats }}</span><span class="stat-lbl">Chats flagged</span></div>
  </div>
</header>
{% if is_monthly %}
<div class="daterange" data-daterange>
  <span class="dr-label">Date range</span>
  <input type="date" class="dr-input" data-range-from aria-label="Range start date">
  <span class="dr-sep">–</span>
  <input type="date" class="dr-input" data-range-to aria-label="Range end date">
  <button type="button" class="dr-reset" data-range-reset>Reset</button>
</div>
{% endif %}

<div class="layout">

<aside class="sidebar">
  <div class="sb-search-wrap">
    <input type="search" class="sb-search" data-search placeholder="Search chat / manager…" aria-label="Search chat or manager">
    <button type="button" class="sb-filter-btn" data-filter-toggle aria-label="Filter" title="Filter">⚙</button>
  </div>
  <div class="filter-panel" data-filter-panel hidden>
    {% if managers_filter %}
    <div class="fp-sec">
      <p class="fp-label">Managers</p>
      {% for mg in managers_filter %}{% if not mg.is_test %}
      <label class="fp-check"><input type="checkbox" data-mgr-filter value="{{ mg.name }}" checked><span>{{ mg.name }}</span></label>
      {% endif %}{% endfor %}
    </div>
    {% endif %}
    {% if present_cats %}
    <div class="fp-sec">
      <p class="fp-label">Risk categories</p>
      {% for c in present_cats %}
      <label class="fp-check"><input type="checkbox" data-cat-filter value="{{ c.key }}" checked><span>{{ c.label }}</span></label>
      {% endfor %}
    </div>
    {% endif %}
    <div class="fp-sec">
      <p class="fp-label">Visibility</p>
      <label class="fp-check"><input type="checkbox" data-show-test-chats><span>Show test chats</span></label>
      <label class="fp-check"><input type="checkbox" data-show-test-mgr><span>Show test managers</span></label>
    </div>
    <button type="button" class="fp-reset" data-filter-reset>Reset filters</button>
  </div>
  <button type="button" class="sb-item sb-all active" data-view="all" data-name="all">
    <span class="sb-name">All</span>
    {% if stats.critical %}<span class="sb-crit">{{ stats.critical }}!</span>{% endif %}
    <span class="sb-count">{{ stats.total_events }}</span>
  </button>
  <span class="sidebar-label">Chats · {{ stats.chats }}</span>
  {% for m in chats %}
  <button type="button" class="sb-item" data-view="{{ m.id }}" data-slug="{{ m.slug }}" data-name="{{ m.name }}" data-filter="{{ m.name }} {{ m.manager_name }}" data-manager="{{ m.manager_name }}" data-cats="{{ m.cats|join(' ') }}" data-test="{{ '1' if m.is_test else '0' }}" data-mgrtest="{{ '1' if m.manager_is_test else '0' }}">
    <span class="sb-name">{{ m.name }}</span>
    {% if m.proposals %}<span class="sb-prop" title="{{ m.proposals }} manager proposal(s)">◆{{ m.proposals }}</span>{% endif %}
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

  {# ── All view: portfolio summary (no per-event list) ── #}
  <div class="view-all-only"><div data-summary>
  {% if stats.total_events == 0 %}
    <div class="portfolio-clean">
      <span class="pc-check">✓</span>
      <div>
        <p class="pc-title">No risk signals this period</p>
        <p class="pc-sub">All {{ stats.chats }} monitored chat{{ 's' if stats.chats != 1 else '' }} clean. {{ stats.proposals }} manager proposal{{ 's' if stats.proposals != 1 else '' }} logged.</p>
      </div>
    </div>
  {% else %}
    <p class="section-label">Risk by Category</p>
    <div class="cat-list">
    {% for c in categories_summary %}
      <div class="cat-row">
        <span class="cat-label">{{ c.label }}</span>
        <span class="cat-track"><span class="cat-fill" style="width:{{ c.pct }}%"><span class="seg hi" style="flex:{{ c.hi }}"></span><span class="seg lo" style="flex:{{ c.lo }}"></span></span></span>
        <span class="cat-count">{{ c.total }}</span>
      </div>
    {% endfor %}
    </div>
    <div class="cat-legend"><span><i class="hi"></i>Critical / High</span><span><i class="lo"></i>Medium / Low</span></div>

    <p class="section-label">Chats with risk signals</p>
    <div class="mgr-cards">
    {% for m in chats %}{% if m.total > 0 %}
      <a href="#" class="mgr-card" data-view="{{ m.id }}" data-slug="{{ m.slug }}" data-name="{{ m.name }}" data-manager="{{ m.manager_name }}" data-cats="{{ m.cats|join(' ') }}" data-test="{{ '1' if m.is_test else '0' }}" data-mgrtest="{{ '1' if m.manager_is_test else '0' }}">
        <div class="mc-top">
          <span class="mc-name">{{ m.name }}</span>
          <span class="mc-total">{{ m.total }}</span>
        </div>
        <span class="mc-mgr">Manager: {{ m.manager_name }}{% if m.proposals %} · <span class="mc-prop">◆ {{ m.proposals }} proposal{{ 's' if m.proposals != 1 else '' }}</span>{% endif %}</span>
        <div class="mc-pills">
          {% if m.critical_count %}<span class="mc-pill crit">{{ m.critical_count }} crit</span>{% endif %}
          {% if m.high_count %}<span class="mc-pill high">{{ m.high_count }} high</span>{% endif %}
          {% if m.medium_count %}<span class="mc-pill med">{{ m.medium_count }} med</span>{% endif %}
        </div>
        <div class="mc-bar">
          {% if m.critical_count %}<span class="seg crit" style="flex:{{ m.critical_count }}"></span>{% endif %}
          {% if m.high_count %}<span class="seg high" style="flex:{{ m.high_count }}"></span>{% endif %}
          {% if m.medium_count %}<span class="seg med" style="flex:{{ m.medium_count }}"></span>{% endif %}
          {% if m.low_count %}<span class="seg low" style="flex:{{ m.low_count }}"></span>{% endif %}
        </div>
      </a>
    {% endif %}{% endfor %}
    </div>
  {% endif %}
  </div></div>

  {# ── Per-chat detail dossiers (shown one at a time in single view) ── #}
  {% for m in chats %}
  <section class="mgr-section" data-mgr="{{ m.id }}" data-slug="{{ m.slug }}" data-test="{{ '1' if m.is_test else '0' }}" data-mgrtest="{{ '1' if m.manager_is_test else '0' }}">
    <div class="mgr-header">
      <button type="button" class="mgr-back" data-view="all">← All</button>
      <h2>{{ m.name }}</h2>
      <span class="mgr-sub">Manager: {{ m.manager_name }}</span>
      {% if m.is_test or m.manager_is_test %}<span class="mgr-sub test-tag">TEST</span>{% endif %}
      {% if m.proposals %}<span class="mgr-sub prop-tag">◆ {{ m.proposals }} proposal{{ 's' if m.proposals != 1 else '' }}</span>{% endif %}
      <span class="mgr-pills" data-mgr-pills>
        <span class="pill">{{ m.total }} event{{ 's' if m.total != 1 else '' }}</span>
        {% if m.critical_count %}<span class="pill crit">{{ m.critical_count }} critical</span>{% endif %}
        {% if m.high_count %}<span class="pill high">{{ m.high_count }} high</span>{% endif %}
        {% if m.medium_count %}<span class="pill med">{{ m.medium_count }} medium</span>{% endif %}
      </span>
    </div>
    {% if m.events %}
      {% for ev in m.events %}
      <div class="event {{ ev.risk_level }}" data-date="{{ ev.date_iso }}">
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
      <p class="no-events">No risk events in this period — clean chat.</p>
    {% endif %}
    {% if is_monthly %}<p class="no-events" data-no-range style="display:none">No risk events in the selected range.</p>{% endif %}
  </section>
  {% endfor %}

</main>
</div>
{% if is_monthly %}
<script type="application/json" data-month-data>{{ month_data|tojson }}</script>
{{ month_script }}
{% endif %}
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
    chats: list[dict[str, Any]],
    event_rows: list[dict[str, Any]],
    proposals_count: int = 0,
    proposal_dates: list[datetime] | None = None,
    chats_added: int = 0,
    chats_added_dates: list[datetime] | None = None,
    proposals_by_chat: dict[str, int] | None = None,
) -> str:
    """Render a complete HTML report page and return the HTML string.

    The report is CHAT-centric: the roster (left sidebar), the summary cards, and
    the detail dossiers are all one-per-monitored-chat. Each chat carries the human
    who authorised it (``manager_name``); events attach to their chat directly.

    Args:
        period_type: "weekly" or "monthly".
        since: UTC start of the period (inclusive).
        until: UTC end of the period (exclusive).
        chats: rows from list_active_chats() — id, chat_name, topic_name,
            manager_name. One row per active monitored chat unit.
        event_rows: rows from list_events_by_chat() — event fields + chat_id +
            partner_name + author attribution.
        proposals_count: portfolio-wide count of manager proposals in the period
            (activity_signals) — a top-line summary metric, defaults to 0.
        proposal_dates: proposal timestamps for the period. Supplied for the
            MONTHLY report only, so the client-side date-range filter can
            recompute the proposals count for a sub-range. None → weekly (no
            filter, count stays the full-period figure).
        chats_added: count of chats onboarded within the period (weekly=7d,
            monthly=30d) — the "New chats" stat.
        chats_added_dates: authorized_at dates for chats added in the period.
            Supplied for the MONTHLY report only so the date-range filter can
            recompute "New chats" for a sub-range.
    """
    _HI = ("critical", "high")

    # Build event index: chat_id → list[EventData]
    events_index: dict[UUID, list[EventData]] = {}
    for row in event_rows:
        cid = UUID(str(row["chat_id"]))
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
            llm_explanation=short_why(str(row["llm_explanation"])) or None
            if row.get("llm_explanation")
            else None,
            author=str(row["author_name"]) if row.get("author_name") else None,
            author_role=str(row["author_role"]) if row.get("author_role") else None,
            status=str(row.get("status") or ""),
            date_str=ts.strftime("%Y-%m-%d %H:%M"),
            date_iso=ts.strftime("%Y-%m-%d"),
        )
        events_index.setdefault(cid, []).append(ev)

    # Assemble one ChatUnit per active chat
    prop_by_chat = proposals_by_chat or {}
    chat_list: list[ChatUnit] = []
    taken_slugs: set[str] = set()
    for chat in chats:
        cid = UUID(str(chat["id"]))
        cid_str = str(cid)
        evs = events_index.get(cid, [])
        chat_list.append(
            ChatUnit(
                id=cid_str,
                slug=_chat_slug(chat, taken_slugs),
                name=_chat_name(chat),
                manager_name=str(chat.get("manager_name") or "unassigned"),
                is_test=bool(chat.get("is_test")),
                manager_is_test=bool(chat.get("manager_is_test")),
                proposals=int(prop_by_chat.get(cid_str, 0)),
                events=evs,
                total=len(evs),
                critical_count=sum(1 for e in evs if e.risk_level == "critical"),
                high_count=sum(1 for e in evs if e.risk_level == "high"),
                medium_count=sum(1 for e in evs if e.risk_level == "medium"),
                low_count=sum(1 for e in evs if e.risk_level == "low"),
                cats=sorted({e.risk_type for e in evs}),
            )
        )

    label = "Weekly" if period_type == "weekly" else "Monthly"
    title = f"{label} Risk Report"
    period_label = f"{since.strftime('%d %b %Y')} – {until.strftime('%d %b %Y')}"
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    # Headline stats + category summary exclude TEST chats (hidden by default);
    # the filter panel can reveal test chats in the roster, but the top-line
    # numbers always describe the real portfolio.
    real_chats = [m for m in chat_list if not (m.is_test or m.manager_is_test)]
    all_events = [e for m in real_chats for e in m.events]
    stats = {
        "total_events": len(all_events),
        "critical": sum(1 for e in all_events if e.risk_level == "critical"),
        "high": sum(1 for e in all_events if e.risk_level == "high"),
        "medium": sum(1 for e in all_events if e.risk_level == "medium"),
        "low": sum(1 for e in all_events if e.risk_level == "low"),
        "proposals": proposals_count,
        "chats": len(real_chats),
        "flagged": sum(1 for m in real_chats if m.total > 0),
        "chats_added": chats_added,
    }

    # Portfolio risk-by-category breakdown (replaces the wide manager×category
    # matrix): per category a hi (critical+high) / lo (medium+low) split, sorted
    # by volume, only non-zero categories, bar width relative to the busiest.
    cat_acc: dict[str, dict[str, int]] = {}
    for e in all_events:
        d = cat_acc.setdefault(e.risk_type, {"hi": 0, "lo": 0, "total": 0})
        if e.risk_level in _HI:
            d["hi"] += 1
        else:
            d["lo"] += 1
        d["total"] += 1
    max_total = max((d["total"] for d in cat_acc.values()), default=0)
    order = {k: i for i, (k, _) in enumerate(RISK_CATEGORIES)}
    categories_summary: list[dict[str, Any]] = []
    for key, d in cat_acc.items():
        pct = max(8, round(d["total"] / max_total * 100)) if max_total else 0
        categories_summary.append(
            {
                "key": key,
                "label": _risk_type_label(key),
                "total": d["total"],
                "hi": d["hi"],
                "lo": d["lo"],
                "pct": pct,
            }
        )
    categories_summary.sort(key=lambda c: (-c["total"], order.get(c["key"], 99)))

    # Monthly-only: data island for the client-side date-range filter. The
    # picker is bounded to the report's actual window [since, until]; events and
    # proposals carry ISO dates so the browser can recompute every aggregate for
    # any sub-range without a server round-trip.
    is_monthly = period_type == "monthly"
    month_data: dict[str, Any] = {}
    month_script: Markup | str = ""
    if is_monthly:
        month_data = {
            "periodStart": since.strftime("%Y-%m-%d"),
            "periodEnd": until.strftime("%Y-%m-%d"),
            "categories": [[k, v] for k, v in RISK_CATEGORIES],
            # "managers" is the client-side key for the per-unit list; here each
            # unit is a chat, carrying its authorising manager's name for the card.
            # Test chats are excluded (headline numbers describe the real portfolio).
            "managers": [
                {
                    "id": m.id,
                    "slug": m.slug,
                    "name": m.name,
                    "managerName": m.manager_name,
                    "cats": m.cats,
                }
                for m in real_chats
            ],
            "events": [
                {"m": m.id, "d": ev.date_iso, "lvl": ev.risk_level, "type": ev.risk_type}
                for m in real_chats
                for ev in m.events
            ],
            "proposalDates": [d.strftime("%Y-%m-%d") for d in (proposal_dates or [])],
            "chatsAddedDates": [
                d.strftime("%Y-%m-%d") for d in (chats_added_dates or [])
            ],
        }
        month_script = Markup(_MONTH_FILTER_SCRIPT)

    # Distinct managers for the filter panel (test managers included so the
    # "show test managers" toggle can reveal them), sorted, test last.
    mgr_seen: dict[str, bool] = {}
    for m in chat_list:
        mgr_seen[m.manager_name] = mgr_seen.get(m.manager_name, False) or m.manager_is_test
    managers_filter = [
        {"name": name, "is_test": is_test}
        for name, is_test in sorted(mgr_seen.items(), key=lambda kv: (kv[1], kv[0].lower()))
    ]
    # Categories actually present in this report (for the category filter list).
    present_cats = [
        {"key": c["key"], "label": c["label"]} for c in categories_summary
    ]

    return str(
        _template.render(
            title=title,
            period_label=period_label,
            generated_at=generated_at,
            chats=chat_list,
            categories=RISK_CATEGORIES,
            categories_summary=categories_summary,
            stats=stats,
            is_monthly=is_monthly,
            month_data=month_data,
            month_script=month_script,
            managers_filter=managers_filter,
            present_cats=present_cats,
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
  <button class="tab-btn active" data-tab="daily" onclick="showTab('daily')">Daily</button>
  <button class="tab-btn" data-tab="weekly" onclick="showTab('weekly')">Weekly</button>
  <button class="tab-btn" data-tab="monthly" onclick="showTab('monthly')">Monthly</button>
</div>

<div id="panel-daily" class="tab-panel active">
  {% if daily_body %}{{ daily_body }}{% else %}<p class="tab-empty">No daily data available.</p>{% endif %}
</div>

<div id="panel-weekly" class="tab-panel">
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


def build_daily_card(
    *,
    day: str,
    digest: Any,
    min_day: str,
    max_day: str,
    generated_at: str,
) -> str:
    """Render the Daily-digest panel body (embedded as the first dashboard tab).

    ``digest`` is a ``db.queries.daily.DailyDigest`` (duck-typed). ``day`` /
    ``min_day`` / ``max_day`` are ISO dates; picking a date auto-submits ``?day=``
    to the dashboard route (no button), which re-renders this card for that day.
    """
    pct = round(digest.significant / digest.messages_total * 100) if digest.messages_total else 0
    header = (
        '<div class="accent-bar"></div>'
        '<header class="page-header">'
        "<h1>Daily Digest</h1>"
        f'<p class="meta">Day: <strong>{day}</strong>&nbsp;&nbsp;·&nbsp;&nbsp;'
        f"Generated: {generated_at}</p>"
        '<form class="daterange" method="get">'
        '<span class="dr-label">Pick a day</span>'
        f'<input type="date" class="dr-input" name="day" value="{day}" '
        f'min="{min_day}" max="{max_day}" onchange="this.form.submit()">'
        "</form>"
    )
    if not digest.has_activity:
        return (
            header + "</header><main class=\"main\">"
            '<div class="portfolio-clean"><span class="pc-check">✓</span><div>'
            f'<p class="pc-title">No activity on {day}</p>'
            '<p class="pc-sub">No messages, risk events, or onboarding recorded '
            "for this day.</p></div></div></main>"
        )

    def cell(num: str, lbl: str, cls: str = "") -> str:
        return (
            f'<div class="stat-cell"><span class="stat-num {cls}">{num}</span>'
            f'<span class="stat-lbl">{lbl}</span></div>'
        )

    strip = (
        '<div class="stat-strip">'
        + cell(f"{digest.messages_total:,}", "Messages")
        + cell(f"{digest.significant:,} ({pct}%)", "Significant")
        + cell(f"{digest.active_chats}/{digest.total_active_chats}", "Active chats")
        + cell(str(digest.active_managers), "Active managers")
        + cell(str(digest.new_chats), "New chats")
        + cell(str(digest.new_partners), "New partners")
        + "</div></header>"
    )

    risk = (
        '<main class="main"><p class="section-label">Risk events</p>'
        '<div class="mgr-header" style="border:none;margin-bottom:8px">'
        f'<span class="pill crit">{digest.risk_critical} critical</span>'
        f'<span class="pill high">{digest.risk_high} high</span>'
        f'<span class="pill med">{digest.risk_medium} medium</span>'
        f'<span class="pill">{digest.risk_low} low</span>'
        "</div>"
    )
    rows = getattr(digest, "active_chat_rows", []) or []
    if rows:
        # One header cell per column so "Chat / Message count" labels both columns.
        head = (
            '<div class="dc-row dc-head"><span class="dc-name">Chat</span>'
            '<span class="dc-count">Message count</span></div>'
        )
        items = "".join(
            '<div class="dc-row"><span class="dc-name">'
            f"{_hesc(name)}</span><span class=\"dc-count\">{count}</span></div>"
            for name, count in rows
        )
        risk += (
            f'<p class="section-label">Active chats · {len(rows)}</p>'
            f'<div class="dc-list">{head}{head}{items}</div>'
        )
    risk += "</main>"
    return header + strip + risk


def build_dashboard_html(
    *,
    weekly_html: str | None,
    monthly_html: str | None,
    daily_html: str | None = None,
) -> str:
    """Render a tabbed dashboard: Daily (first) + latest weekly and monthly."""
    wb = Markup(_extract_body(weekly_html)) if weekly_html else None
    mb = Markup(_extract_body(monthly_html)) if monthly_html else None
    db = Markup(daily_html) if daily_html else None
    return str(_dash_template.render(weekly_body=wb, monthly_body=mb, daily_body=db))
