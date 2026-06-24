"""Ops Alerts subsystem — proactive group notifications.

Independent of the monitoring pipeline. Two branches:
  - payment incidents: poll an external RSS feed, broadcast new incidents into
    active partner groups, edit prior messages on updates.
  - Argentina holidays: a daily reminder posted into active groups.

This is the ONLY subsystem that writes into partner groups (sanctioned
exception to CLAUDE.md §1). All sends target active ``unit_type='group'`` units.
"""
