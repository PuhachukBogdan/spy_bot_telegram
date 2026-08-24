/** The one period model the whole page shares.
 *
 * The analytics block's granularity switch (Days / Weeks / Months / Quarters +
 * the custom range on Days) is not a chart-local control any more: the selected
 * period drives EVERY number on the page — overview table, charts, dossier risk
 * cards, chat lists. This module owns what "the selected period" means and how
 * a range is recomputed client-side, with exactly the server's formulas:
 * counters sum, percentages derive from the sums, coverage re-applies the
 * pro-rated threshold. Averaging pre-computed percentages would lie.
 */

import type { ChatDaysEntry, ScopeTrend, TrendGranularity, TrendPoint } from '@/data'

export interface PeriodState {
  granularity: TrendGranularity
  /** Custom range bounds (ISO days, inclusive). Only honored on 'day'. */
  from: string
  to: string
}

export interface PeriodRange {
  from: string
  to: string
  custom: boolean
}

/** The fields both a server bucket and a client-computed range provide. */
export interface MetricSource {
  slaPercent: number | null
  slaMet: number
  slaRated: number
  offline: number
  proposals: number
  risksOwn: number
  coveragePercent: number | null
  coverageActive: number
  coverageTotal: number
  truncated: boolean
}

/* ── ISO-date helpers (dates compare lexicographically, so strings suffice) ── */
export function addDays(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

export function daysBetweenInclusive(from: string, to: string): number {
  const a = new Date(`${from}T00:00:00Z`).getTime()
  const b = new Date(`${to}T00:00:00Z`).getTime()
  return Math.round((b - a) / 86_400_000) + 1
}

export function fmtShort(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString('en-GB', {
    day: 'numeric',
    month: 'short',
  })
}

/** The active-chat threshold pro-rated to the range length — the server's
 * ceil(ACTIVE_CHAT_MIN_MESSAGES × days / 30), never below 1. */
export function coverageThreshold(activeChatMin: number, rangeDays: number): number {
  return Math.max(1, Math.ceil((activeChatMin * rangeDays) / 30))
}

/** What days the selection means right now.
 *
 * A custom range is itself the answer. Otherwise the period is the CURRENT
 * bucket to date — today, this week, this month, this quarter — matching the
 * tiles' long-standing semantics, so tiles and page can never disagree. */
export function resolveRange(
  team: ScopeTrend,
  horizon: { today: string },
  period: PeriodState,
): PeriodRange {
  if (
    period.granularity === 'day' &&
    period.from !== '' &&
    period.to !== '' &&
    period.from <= period.to
  ) {
    return { from: period.from, to: period.to, custom: true }
  }
  const buckets = team[period.granularity].buckets
  const last = buckets.length ? buckets[buckets.length - 1]! : null
  return {
    from: last ? last.start : horizon.today,
    to: horizon.today,
    custom: false,
  }
}

const PERIOD_NAMES: Record<TrendGranularity, string> = {
  day: 'today',
  week: 'this week to date',
  month: 'this month to date',
  quarter: 'this quarter to date',
}

/** Human label for the caption lines: what period the numbers describe. */
export function periodLabel(period: PeriodState, range: PeriodRange): string {
  if (range.custom) return `${fmtShort(range.from)} → ${fmtShort(range.to)}`
  if (period.granularity === 'day') return `today · ${fmtShort(range.to)}`
  return `${PERIOD_NAMES[period.granularity]} · ${fmtShort(range.from)} → ${fmtShort(range.to)}`
}

export function inRange(day: string, range: PeriodRange): boolean {
  return day >= range.from && day <= range.to
}

/** Sum a scope's day buckets over [from, to] — EXACTLY the server's formulas,
 * run client-side. Counters sum; SLA % derives from the summed counters;
 * coverage re-applies the pro-rated threshold over per-chat day maps. */
export function computeRange(
  dayBuckets: TrendPoint[],
  chats: ChatDaysEntry[],
  managerId: string | null,
  from: string,
  to: string,
  activeChatMin: number,
): MetricSource {
  let slaMet = 0
  let slaRated = 0
  let offline = 0
  let proposals = 0
  let risksOwn = 0
  for (const b of dayBuckets) {
    if (b.start >= from && b.start <= to) {
      slaMet += b.slaMet
      slaRated += b.slaRated
      offline += b.offline
      proposals += b.proposals
      risksOwn += b.risksOwn
    }
  }
  const threshold = coverageThreshold(activeChatMin, daysBetweenInclusive(from, to))
  let total = 0
  let active = 0
  for (const chat of chats) {
    if (managerId !== null && chat.m !== managerId) continue
    if (chat.c > to) continue // born after the range — not in the denominator
    total += 1
    let messages = 0
    for (const [day, count] of Object.entries(chat.d)) {
      if (day >= from && day <= to) messages += count
    }
    if (messages >= threshold) active += 1
  }
  return {
    slaMet,
    slaRated,
    slaPercent: slaRated ? Math.round((1000 * slaMet) / slaRated) / 10 : null,
    offline,
    proposals,
    risksOwn,
    coverageActive: active,
    coverageTotal: total,
    coveragePercent: total ? Math.round((1000 * active) / total) / 10 : null,
    truncated: false,
  }
}

/* ── collapsed-section persistence ──────────────────────────────────────────
 * localStorage can throw (private windows, blocked site data), and the page
 * must render correctly with no stored value — every access is wrapped. */
const COLLAPSE_KEY = 'preview.sections.v1'

export type SectionId = 'risks' | 'chats'

/** Sections closed by default on first visit: the chat list is the longest
 * block on the dossier, so it starts collapsed until the viewer opens it. */
const DEFAULT_COLLAPSED: Record<SectionId, boolean> = {
  risks: false,
  chats: true,
}

export function loadCollapsed(): Record<SectionId, boolean> {
  try {
    const raw = localStorage.getItem(COLLAPSE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<Record<SectionId, boolean>>
      return { ...DEFAULT_COLLAPSED, ...parsed }
    }
  } catch {
    /* storage unavailable — fall through to defaults */
  }
  return { ...DEFAULT_COLLAPSED }
}

export function saveCollapsed(state: Record<SectionId, boolean>): void {
  try {
    localStorage.setItem(COLLAPSE_KEY, JSON.stringify(state))
  } catch {
    /* storage unavailable — the toggle still works for this visit */
  }
}
