/** The contract between Python and this shell.
 *
 * Python renders NO markup for the report any more — it produces a metrics
 * document and injects it as <script id="report-data" type="application/json">.
 * This module is the only place that touches that island, so the shape lives in
 * exactly one file on the TypeScript side.
 *
 * Keep in sync with src/metrics/shell.py (which writes the island) and
 * src/metrics/collect.py (which produces the numbers).
 */

export interface WorkHours {
  start: string
  end: string
  timezone: string
  /** True when nobody set hours and the configured default was assumed. */
  assumed: boolean
}

/** `group` | `topic` | `business`. Business is the private (личка) unit. */
export type UnitType = 'group' | 'topic' | 'business'

export interface ChatRow {
  id: string
  name: string
  unitType: UnitType
  messages: number
  active: boolean
}

export interface RiskCase {
  id: string
  chatName: string
  unitType: UnitType
  riskType: string
  riskLevel: string
  score: number
  detectedAt: string
  /** Local (report-timezone) ISO day of detection — the key the period filter
   * compares against bucket days, so the list and the counters always agree. */
  day: string
  phrase: string | null
  why: string | null
  /** 'manager_action' = they wrote it · 'chat_context' = raised in their chat. */
  attribution: 'manager_action' | 'chat_context'
  /** Context cases are shown but never counted. Mirrors RiskAttribution.counts. */
  counts: boolean
}

export interface ManagerRow {
  id: string
  name: string
  /** null = nothing was rated this period. NOT zero — zero would read as failure. */
  slaPercent: number | null
  slaMet: number
  slaRated: number
  /** Waits nobody answered inside the offline window. Never folded into slaPercent. */
  slaOffline: number
  coveragePercent: number | null
  chatsActive: number
  chatsTotal: number
  proposals: number
  workHours: WorkHours | null
  risksOwn: number
  risksContext: number
  chats: ChatRow[]
  /** Spans the whole trend horizon (120d), NOT the detail window — the client
   * filters this list by the selected period before showing or counting it. */
  risks: RiskCase[]
}

export interface ReportData {
  generatedAt: string
  since: string
  until: string
  /** null when there is no comparable preceding period (see METRICS_EPOCH_DATE). */
  previous: { since: string; until: string } | null
  epoch: string | null
  thresholds: {
    slaSeconds: number
    graceSeconds: number
    substantiveChars: number
    offlineSeconds: number
    activeChatMinMessages: number
  }
  /** Risk counts by category across everyone, biggest first. */
  categories: { type: string; count: number }[]
  managers: ManagerRow[]
  /** Bucket series for the analytics block. null when the horizon is empty. */
  trends: Trends | null
}

export type TrendGranularity = 'day' | 'week' | 'month' | 'quarter'

/** One bucket (or one to-date base window) of a scope's trend.
 * Counters, with percentages derived server-side from the SUMS — never from
 * averaging smaller percentages. */
export interface TrendPoint {
  start: string
  end: string
  /** The bucket containing today — still accumulating, drawn distinctly. */
  partial: boolean
  /** Bucket starts before the data horizon — value incomplete, marked in UI. */
  truncated: boolean
  /** Falls inside the bot's onboarding fortnight — muted colour + label. */
  test: boolean
  slaPercent: number | null
  slaMet: number
  slaRated: number
  offline: number
  proposals: number
  risksOwn: number
  coveragePercent: number | null
  coverageActive: number
  coverageTotal: number
}

export interface GranularityTrend {
  buckets: TrendPoint[]
  /** Same-elapsed-days window at the start of the previous bucket, or null when
   * that base would reach past the horizon — deltas are hidden, not zeroed. */
  prevToDate: TrendPoint | null
}

export type ScopeTrend = Record<TrendGranularity, GranularityTrend>

/** One owned chat: chat id, manager id, local creation day, sparse
 * day→messages map. What lets custom ranges compute coverage EXACTLY (same
 * threshold formula as the server) instead of averaging daily percentages,
 * and lets the dossier's chat table recount messages for any period. */
export interface ChatDaysEntry {
  i: string
  m: string
  c: string
  d: Record<string, number>
}

export interface Trends {
  team: ScopeTrend
  managers: Record<string, ScopeTrend>
  chatDays: { chats: ChatDaysEntry[] }
  horizon: { floor: string; today: string; testUntil: string | null }
}

/** Read the injected island. Throws a legible error rather than rendering blank. */
export function loadReportData(): ReportData {
  const node = document.getElementById('report-data')
  if (!node?.textContent) {
    throw new Error('report-data island missing — the shell was served unfilled')
  }
  return JSON.parse(node.textContent) as ReportData
}
