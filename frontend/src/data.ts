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
  /** Telegram account that wrote the flagged message — the account view's filter. */
  senderAccount: string | null
}

/** One Telegram account of a person, over the server's detail window.
 * `label` is 'old' / 'new' when the person has more than one account (set by
 * an admin, or derived from which account was seen first), else null. */
export interface AccountRow {
  id: string
  label: 'old' | 'new' | null
  /** Active chats this account is present in right now. */
  chats: number
  messages: number
  activeDays: number
  lastActiveAt: string | null
  /** Rated waits this account closed, and how many of those were on time. */
  replies: number
  repliesOnTime: number
}

export interface ManagerRow {
  id: string
  name: string
  /** Local ISO day from which nothing is attributed to this person; null = working.
   * The row stays — with its history — on purpose. */
  deactivatedAt: string | null
  deactivationNote: string | null
  accounts: AccountRow[]
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
    /** A chat's crew on a day = managers present who wrote there within this
     * many days before. Unanswered waits are charged to the crew. */
    crewLookbackDays: number
  }
  /** Risk counts by category across everyone, biggest first. */
  categories: { type: string; count: number }[]
  managers: ManagerRow[]
  /** Bucket series for the analytics block. null when the horizon is empty. */
  trends: Trends | null
  /** Tone of voice (daily LLM pass): metric registry, per-manager-day counters
   * over the horizon, accepted flags per manager. null = tables unavailable. */
  tone: ToneData | null
  /** Who is signed in. The page is already filtered server-side for this person
   * (a head never receives their own row), so this only labels the page and
   * decides whether the risk-report switch is offered. */
  viewer: Viewer | null
}

export interface Viewer {
  name: string
  /** 'admin' sees the whole team · 'head' sees everyone except themselves. */
  role: string
  seesRiskReport: boolean
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

/** One chat: chat id, the managers it belongs to (everyone PRESENT in it —
 * one chat may sit in several portfolios; the team counts the entry once),
 * local creation day, sparse day→messages map. What lets custom ranges compute
 * coverage EXACTLY (same threshold formula as the server) instead of averaging
 * daily percentages, and lets the dossier's chat table recount messages for
 * any period. */
export interface ChatDaysEntry {
  i: string
  ms: string[]
  c: string
  d: Record<string, number>
}

/** One (manager, account): sparse per-day messages `d`, rated replies `r` and
 * on-time replies `o`, keyed by local day — the old/new split per period. */
export interface AccountDaysEntry {
  m: string
  a: string
  label: 'old' | 'new' | null
  d: Record<string, number>
  r: Record<string, number>
  o: Record<string, number>
  /** chat id -> {day: messages written by this account}. */
  cd: Record<string, Record<string, number>>
}

export interface Trends {
  team: ScopeTrend
  managers: Record<string, ScopeTrend>
  chatDays: { chats: ChatDaysEntry[] }
  accountDays: AccountDaysEntry[]
  /** One trend per Telegram account of a person with several, keyed `acct:<id>`.
   * Same bucket shape as a manager's; offline is always 0 (it stays on the person). */
  accounts: Record<string, ScopeTrend>
  horizon: { floor: string; today: string; testUntil: string | null }
}

/* ── Tone of voice ─────────────────────────────────────────────────────────── */

/** negative: a flag is bad, show the rate (ideal 0 %) · positive_gap: a flag is
 * a gap, show 100 − rate (ideal 100 %) · negative_event: a flag is a bad move,
 * show the count (ideal 0) · positive_event: a flag is a good move, show the
 * count. Mirrors TonePolarity in src/metrics/tone.py. */
export type TonePolarity = 'negative' | 'positive_gap' | 'negative_event' | 'positive_event'

export interface ToneMetricDef {
  key: string
  label: string
  polarity: TonePolarity
  description: string
}

/** One manager, one local day: `a` messages judged, `f` flags per metric key. */
export interface ToneDay {
  m: string
  d: string
  a: number
  f: Record<string, number>
}

export interface ToneFlag {
  id: string
  metric: string
  /** Local ISO day — the period filter's key, same calendar as risks. */
  day: string
  at: string
  chatName: string
  unitType: UnitType
  quote: string
  reason: string
  confidence: number
  /** Telegram account that wrote the judged message, when known. */
  account: string | null
}

export interface ToneData {
  enabled: boolean
  /** A rate shows only from this many judged messages in the period. */
  minAssessed: number
  metrics: ToneMetricDef[]
  days: ToneDay[]
  /** manager id -> accepted flags over the horizon, newest first. */
  flags: Record<string, ToneFlag[]>
}

/** Read the injected island. Throws a legible error rather than rendering blank. */
export function loadReportData(): ReportData {
  const node = document.getElementById('report-data')
  if (!node?.textContent) {
    throw new Error('report-data island missing — the shell was served unfilled')
  }
  return JSON.parse(node.textContent) as ReportData
}
