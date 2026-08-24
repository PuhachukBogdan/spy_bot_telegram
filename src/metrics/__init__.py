"""Phase 2 manager metrics: SLA, active-chat KPI, tone of voice, comparisons.

Deliberately separate from ``src/pipeline`` and ``src/summary``:

* it is NOT risk. Nothing here writes ``risk_events`` or can raise a Slack alert.
  Employee assessment and company-risk scoring are different scales with different
  consequences, and merging them would push a manager's blunt phrasing into the
  same channel as a traffic-diversion alert.
* it is NOT rendering. This package produces numbers; the report layer renders
  them. That split is what makes period comparison and arbitrary date ranges
  possible at all — see PHASE2_MANAGER_KPI.md §5.8.

Everything here counts forward from ``settings.METRICS_EPOCH_DATE`` (§0.2).
"""
