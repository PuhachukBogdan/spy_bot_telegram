/** Tone of voice — turning per-manager-day counters into the numbers on the page.
 *
 * The server ships raw counters (`assessed` messages and `flagged` per metric,
 * one entry per manager per local day). Everything here is summing: a period's
 * rate is flagged / assessed over the summed days — never an average of daily
 * rates — and the team is the sum of managers, exactly like the SLA counters
 * in period.ts. The polarity of a metric decides what the headline number is:
 *
 *   negative       → the flag RATE (ideal 0 %)                  e.g. toxicity 2.1 %
 *   positive_gap   → 100 − rate (ideal 100 %)                    e.g. completeness 96 %
 *   negative_event → the COUNT of misses, plus per 100 (ideal 0) e.g. handling complaints 1
 *   positive_event → the COUNT of good moves, plus per 100       e.g. initiative 7
 *
 * A RATE is only shown once `minAssessed` messages were judged in the period;
 * below that the reading is `enough: false` and the page says so instead of
 * printing 1-in-3 as 33 %. A COUNT is a count — one miss is one miss whatever
 * the volume — so the two event polarities show as soon as anything was judged;
 * the per-100 figure next to them carries the volume.
 */

import type { ToneDay, ToneMetricDef } from '@/data'
import { addDays, daysBetweenInclusive, type PeriodRange } from '@/lib/period'

export interface ToneTotals {
  assessed: number
  flagged: Record<string, number>
}

export interface ToneReading {
  def: ToneMetricDef
  assessed: number
  flagged: number
  /** Headline number per polarity (see module doc). null when not enough data. */
  value: number | null
  /** Flags per 100 judged messages, one decimal. null when nothing was judged. */
  per100: number | null
  enough: boolean
}

/** Count metrics (both event polarities) versus rate metrics. */
export function isCount(def: ToneMetricDef): boolean {
  return def.polarity === 'positive_event' || def.polarity === 'negative_event'
}

/** Is a smaller number better? True for the two negative polarities. */
function downIsGood(def: ToneMetricDef): boolean {
  return def.polarity === 'negative' || def.polarity === 'negative_event'
}

export function sumTone(
  days: ToneDay[],
  managerId: string | null,
  range: PeriodRange | null,
): ToneTotals {
  const totals: ToneTotals = { assessed: 0, flagged: {} }
  for (const day of days) {
    if (managerId !== null && day.m !== managerId) continue
    if (range && (day.d < range.from || day.d > range.to)) continue
    totals.assessed += day.a
    for (const [metric, n] of Object.entries(day.f)) {
      totals.flagged[metric] = (totals.flagged[metric] ?? 0) + n
    }
  }
  return totals
}

function round1(x: number): number {
  return Math.round(x * 10) / 10
}

export function readTone(
  totals: ToneTotals,
  defs: ToneMetricDef[],
  minAssessed: number,
): ToneReading[] {
  return defs.map((def) => {
    const flagged = totals.flagged[def.key] ?? 0
    const count = isCount(def)
    const enough = count ? totals.assessed > 0 : totals.assessed >= minAssessed
    const per100 = totals.assessed ? round1((100 * flagged) / totals.assessed) : null
    let value: number | null = null
    if (enough && per100 !== null) {
      if (count) value = flagged
      else if (def.polarity === 'negative') value = per100
      else value = round1(100 - per100)
    }
    return { def, assessed: totals.assessed, flagged, value, per100, enough }
  })
}

/** The equal-length range immediately before `range` — the delta base. */
export function previousRange(range: PeriodRange): PeriodRange {
  const len = daysBetweenInclusive(range.from, range.to)
  return { from: addDays(range.from, -len), to: addDays(range.from, -1), custom: range.custom }
}

/** Signed change of the headline value, or null when either side lacks data. */
export function toneDelta(current: ToneReading, previous: ToneReading): number | null {
  if (current.value === null || previous.value === null) return null
  return round1(current.value - previous.value)
}

/** Is this delta good news? Up is good for the positive polarities; for the
 * negative ones (toxicity, handling complaints) down is good. Zero is neutral. */
export function deltaIsGood(def: ToneMetricDef, delta: number): boolean | null {
  if (delta === 0) return null
  return downIsGood(def) ? delta < 0 : delta > 0
}

/** Meter fill for a reading, 0–100. Percent metrics fill by their value; count
 * metrics fill by their per-100 rate so a bar never overflows. */
export function toneFill(reading: ToneReading): number {
  if (reading.value === null) return 0
  if (isCount(reading.def)) {
    return Math.min(100, reading.per100 ?? 0)
  }
  return Math.max(0, Math.min(100, reading.value))
}

export function formatToneValue(reading: ToneReading): string {
  if (reading.value === null) return '—'
  if (isCount(reading.def)) return String(reading.value)
  return `${reading.value}%`
}

export function formatToneDelta(def: ToneMetricDef, delta: number): string {
  const sign = delta > 0 ? '+' : delta < 0 ? '−' : '±'
  const abs = Math.abs(delta)
  return isCount(def) ? `${sign}${abs}` : `${sign}${abs} pp`
}

export function toneLabel(key: string, defs: ToneMetricDef[]): string {
  return defs.find((d) => d.key === key)?.label ?? key
}
