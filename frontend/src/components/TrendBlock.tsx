import { useMemo, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ReferenceArea,
  XAxis,
  YAxis,
} from 'recharts'

import { Card, CardContent } from '@/components/ui/card'
import { ChartContainer, ChartTooltip, type ChartConfig } from '@/components/ui/chart'
import type {
  ChatDaysEntry,
  GranularityTrend,
  ScopeTrend,
  TrendGranularity,
  TrendPoint,
} from '@/data'
import {
  addDays,
  computeRange,
  daysBetweenInclusive,
  fmtShort,
  type MetricSource,
  type PeriodState,
} from '@/lib/period'

/* ── metric registry ─────────────────────────────────────────────────────────
 * One metric = one tile = one possible big chart. Form follows the data's job:
 * rates over time are lines (Y pinned 0–100), per-bucket counts are bars from
 * zero. `goodUp` drives delta colouring — more offline is bad, more SLA good. */
interface MetricDef {
  key: string
  label: string
  unit: '%' | 'n'
  goodUp: boolean
  value: (p: MetricSource) => number | null
  hint: (p: MetricSource) => string
}

const METRICS: MetricDef[] = [
  {
    key: 'sla',
    label: 'SLA',
    unit: '%',
    goodUp: true,
    value: (p) => p.slaPercent,
    hint: (p) => `${p.slaMet} / ${p.slaRated} rated`,
  },
  {
    key: 'coverage',
    label: 'Active chats',
    unit: '%',
    goodUp: true,
    value: (p) => p.coveragePercent,
    hint: (p) => `${p.coverageActive} / ${p.coverageTotal}`,
  },
  {
    key: 'proposals',
    label: 'Proposals',
    unit: 'n',
    goodUp: true,
    value: (p) => p.proposals,
    hint: () => 'counted as positive',
  },
  {
    key: 'offline',
    label: 'Offline',
    unit: 'n',
    goodUp: false,
    value: (p) => p.offline,
    hint: () => 'excluded from SLA %',
  },
  {
    key: 'risks',
    label: 'Risk cases',
    unit: 'n',
    goodUp: false,
    value: (p) => p.risksOwn,
    hint: () => "manager's own only",
  },
]

const GRANULARITIES: { key: TrendGranularity; label: string }[] = [
  { key: 'day', label: 'Days' },
  { key: 'week', label: 'Weeks' },
  { key: 'month', label: 'Months' },
  { key: 'quarter', label: 'Quarters' },
]

function bucketLabel(startIso: string, granularity: TrendGranularity): string {
  const d = new Date(`${startIso}T00:00:00`)
  if (granularity === 'day' || granularity === 'week') {
    return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
  }
  if (granularity === 'month') {
    return d.toLocaleDateString('en-GB', { month: 'short' })
  }
  return `Q${Math.floor(d.getMonth() / 3) + 1} ’${String(d.getFullYear()).slice(2)}`
}

/** Human title for a bucket's span: "Sat 16 Aug" / "10–16 Aug" / "Aug 2026" / "Q3 2026". */
function bucketTitle(b: TrendPoint, granularity: TrendGranularity): string {
  const d = new Date(`${b.start}T00:00:00`)
  if (granularity === 'day') {
    return d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' })
  }
  if (granularity === 'week') {
    return `${fmtShort(b.start)} – ${fmtShort(addDays(b.end, -1))}`
  }
  if (granularity === 'month') {
    return d.toLocaleDateString('en-GB', { month: 'long', year: 'numeric' })
  }
  return `Q${Math.floor(d.getMonth() / 3) + 1} ${d.getFullYear()}`
}

function isWeekend(iso: string): boolean {
  const dow = new Date(`${iso}T00:00:00`).getDay()
  return dow === 0 || dow === 6
}

/** A bucket with nothing measured in it at all — the empty pre-launch lead. */
function hasActivity(b: TrendPoint): boolean {
  return (
    b.slaRated > 0 || b.offline > 0 || b.proposals > 0 || b.risksOwn > 0 || b.coverageActive > 0
  )
}

/* ── sparkline: hand SVG, not a Recharts instance per tile ──────────────── */
function Sparkline({ values }: { values: (number | null)[] }) {
  const W = 104
  const H = 26
  const present = values.filter((v): v is number => v !== null)
  if (present.length < 2) return <div style={{ height: H }} />
  const min = Math.min(...present)
  const max = Math.max(...present)
  const span = max - min || 1
  const x = (i: number) => (values.length === 1 ? W / 2 : (i / (values.length - 1)) * W)
  const y = (v: number) => H - 3 - ((v - min) / span) * (H - 6)

  const segments: string[] = []
  let current: string[] = []
  values.forEach((v, i) => {
    if (v === null) {
      if (current.length > 1) segments.push(current.join(' '))
      current = []
    } else {
      current.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`)
    }
  })
  if (current.length > 1) segments.push(current.join(' '))
  const last = [...values].reverse().findIndex((v) => v !== null)
  const lastIdx = last === -1 ? -1 : values.length - 1 - last
  const lastValue = lastIdx >= 0 ? values[lastIdx] : null

  return (
    <svg width={W} height={H} className="mt-1 block" aria-hidden="true">
      {segments.map((points, i) => (
        <polyline
          key={i}
          points={points}
          fill="none"
          stroke="hsl(var(--primary))"
          strokeWidth="1.5"
          strokeLinejoin="round"
          strokeLinecap="round"
          opacity="0.75"
        />
      ))}
      {lastIdx >= 0 && lastValue !== null && lastValue !== undefined ? (
        <circle cx={x(lastIdx)} cy={y(lastValue)} r="2.4" fill="hsl(var(--primary))" />
      ) : null}
    </svg>
  )
}

/* ── delta chip ─────────────────────────────────────────────────────────── */
function Delta({
  current,
  base,
  metric,
}: {
  current: MetricSource | null
  base: MetricSource | null
  metric: MetricDef
}) {
  if (current?.truncated) {
    return (
      <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
        partial data
      </span>
    )
  }
  const now = current ? metric.value(current) : null
  const then = base ? metric.value(base) : null
  if (now === null || then === null) {
    return (
      <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
        no base
      </span>
    )
  }
  const diff = Math.round((now - then) * 10) / 10
  if (diff === 0) {
    return <span className="num text-[12px] text-muted-foreground">＝ 0</span>
  }
  const up = diff > 0
  const good = up === metric.goodUp
  return (
    <span className={`num text-[12px] font-semibold ${good ? 'text-ok' : 'text-crit'}`}>
      {up ? '▲' : '▼'} {up ? '+' : ''}
      {diff}
      {metric.unit === '%' ? ' pp' : ''}
    </span>
  )
}

/* ── the block ──────────────────────────────────────────────────────────── */
export default function TrendBlock({
  trend,
  chats,
  horizon,
  managerId,
  activeChatMin,
  period,
  onPeriodChange,
}: {
  trend: ScopeTrend
  chats: ChatDaysEntry[]
  horizon: { floor: string; today: string; testUntil: string | null }
  managerId: string | null
  activeChatMin: number
  /** Owned by App: the same selection drives every number on the page. */
  period: PeriodState
  onPeriodChange: (period: PeriodState) => void
}) {
  const { granularity, from, to } = period
  const [metricKey, setMetricKey] = useState('sla')
  const metric = METRICS.find((m) => m.key === metricKey) ?? METRICS[0]!
  const g: GranularityTrend = trend[granularity]

  // Leading buckets from before the bot had any activity carry no information —
  // they only squeeze the real data into the right edge. Trim them by default;
  // an explicit custom range can still reach back.
  const trimmed = useMemo(() => {
    const first = g.buckets.findIndex(hasActivity)
    return first <= 0 ? g.buckets : g.buckets.slice(first)
  }, [g.buckets])

  // A custom range applies on the Days view; the coarser views' buckets already
  // ARE ranges. Empty inputs = the full trimmed horizon.
  const rangeActive = granularity === 'day' && from !== '' && to !== '' && from <= to
  const buckets = useMemo(
    () =>
      rangeActive
        ? g.buckets.filter((b) => b.start >= from && b.start <= to)
        : trimmed,
    [g.buckets, trimmed, rangeActive, from, to],
  )
  const dayBuckets = trend.day.buckets

  // Tile source: the custom range when set, else the current (last) bucket.
  const rangeStats = useMemo(
    () =>
      rangeActive
        ? computeRange(dayBuckets, chats, managerId, from, to, activeChatMin)
        : null,
    [rangeActive, dayBuckets, chats, managerId, from, to, activeChatMin],
  )
  const baseStats = useMemo(() => {
    if (!rangeActive) return g.prevToDate
    const len = daysBetweenInclusive(from, to)
    const prevFrom = addDays(from, -len)
    const prevTo = addDays(from, -1)
    if (prevFrom < horizon.floor) return null // base outside the horizon: hide
    return computeRange(dayBuckets, chats, managerId, prevFrom, prevTo, activeChatMin)
  }, [rangeActive, g.prevToDate, from, to, dayBuckets, chats, managerId, activeChatMin, horizon.floor])

  const current: MetricSource | null =
    rangeStats ?? (buckets.length ? buckets[buckets.length - 1]! : null)

  interface ChartRow {
    name: string
    title: string
    value: number | null
    /** Interpolated stand-in across an empty stretch — drawn as a striped
     * white-on-dark dashed segment ON the line, never as real data. */
    bridge: number | null
    partial: boolean
    truncated: boolean
    test: boolean
    runLabel: string | null
  }

  const chartRows: ChartRow[] = buckets.map((b) => ({
    name: bucketLabel(b.start, granularity),
    title: bucketTitle(b, granularity),
    value: metric.value(b),
    bridge: null,
    partial: b.partial,
    truncated: b.truncated,
    test: b.test,
    runLabel: null,
  }))

  // Runs of consecutive empty buckets. The line bridges them (an interrupted
  // chart reads as broken data), so each run gets a marked cut-out zone and a
  // tooltip that says WHY it is empty — weekends first, with their dates.
  const nullRuns: { from: number; to: number }[] = []
  {
    let start = -1
    chartRows.forEach((row, i) => {
      if (row.value === null) {
        if (start === -1) start = i
      } else if (start !== -1) {
        nullRuns.push({ from: start, to: i - 1 })
        start = -1
      }
    })
    if (start !== -1) nullRuns.push({ from: start, to: chartRows.length - 1 })
  }
  for (const run of nullRuns) {
    const first = buckets[run.from]!
    const last = buckets[run.to]!
    const span =
      run.from === run.to
        ? bucketTitle(first, granularity)
        : `${fmtShort(first.start)} – ${fmtShort(last.start)}`
    const allWeekend =
      granularity === 'day' &&
      buckets.slice(run.from, run.to + 1).every((b) => isWeekend(b.start))
    const label = allWeekend
      ? `Weekend · ${span} — timers don't start`
      : `No rated waits · ${span}`
    for (let i = run.from; i <= run.to; i++) chartRows[i]!.runLabel = label

    // Interior runs get a bridge: a straight interpolation between the real
    // values on either side, anchored on both so the striped segment meets the
    // solid line exactly. Edge runs (nothing to connect to) stay open — a line
    // to nowhere would be an invention, and the tooltip still explains them.
    const prev = run.from - 1
    const next = run.to + 1
    const v0 = prev >= 0 ? chartRows[prev]!.value : null
    const v1 = next < chartRows.length ? chartRows[next]!.value : null
    if (v0 !== null && v1 !== null) {
      const steps = next - prev
      for (let k = prev; k <= next; k++) {
        chartRows[k]!.bridge = Math.round((v0 + ((v1 - v0) * (k - prev)) / steps) * 10) / 10
      }
    }
  }

  const config = {
    value: { label: metric.label, color: 'hsl(var(--chart-1))' },
  } satisfies ChartConfig
  const partialIdx = chartRows.findIndex((r) => r.partial)
  const testRows = chartRows.filter((r) => r.test)
  const hasTruncated = buckets.some((b) => b.truncated)
  const rowByName = new Map(chartRows.map((r) => [r.name, r]))

  /** Tooltip driven by the hovered x-label, so empty buckets explain themselves
   * instead of showing nothing. Values carry their unit — % is never implicit. */
  function TrendTip({ active, label }: { active?: boolean; label?: string | number }) {
    if (!active || label === undefined) return null
    const row = rowByName.get(String(label))
    if (!row) return null
    const flags = [
      row.test ? 'test period' : null,
      row.partial ? 'in progress' : null,
      row.truncated ? 'incomplete — before data horizon' : null,
    ].filter(Boolean)
    return (
      <div className="rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] shadow-lift">
        <div className="font-semibold">{row.title}</div>
        {row.value !== null ? (
          <div className="num">
            {metric.label}: {row.value}
            {metric.unit === '%' ? '%' : ''}
          </div>
        ) : (
          <div className="text-muted-foreground">{row.runLabel ?? 'No data'}</div>
        )}
        {flags.length ? (
          <div className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
            {flags.join(' · ')}
          </div>
        ) : null}
      </div>
    )
  }

  return (
    <Card className="shadow-card">
      <CardContent className="p-4">
        {/* header: title + granularity switch + (Days only) range inputs */}
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <span className="font-display text-[12px] uppercase tracking-widest text-primary">
            Analytics
          </span>
          {granularity === 'day' ? (
            <span className="ml-2 flex items-center gap-1.5">
              <input
                type="date"
                value={from}
                min={horizon.floor}
                max={to || horizon.today}
                onChange={(e) => onPeriodChange({ ...period, from: e.target.value })}
                className="rounded-md border border-border bg-card px-2 py-1 font-mono text-[11px] text-foreground outline-none focus:border-primary"
              />
              <span className="text-muted-foreground">→</span>
              <input
                type="date"
                value={to}
                min={from || horizon.floor}
                max={horizon.today}
                onChange={(e) => onPeriodChange({ ...period, to: e.target.value })}
                className="rounded-md border border-border bg-card px-2 py-1 font-mono text-[11px] text-foreground outline-none focus:border-primary"
              />
              {rangeActive ? (
                <button
                  onClick={() => onPeriodChange({ ...period, from: '', to: '' })}
                  className="rounded-md border border-border bg-card px-2 py-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground hover:bg-secondary"
                >
                  Reset
                </button>
              ) : null}
            </span>
          ) : null}
          <div className="ml-auto flex overflow-hidden rounded-md border border-border">
            {GRANULARITIES.map((item) => (
              <button
                key={item.key}
                onClick={() => onPeriodChange({ ...period, granularity: item.key })}
                className={`px-3 py-1 font-mono text-[11px] uppercase tracking-wider transition-colors ${
                  granularity === item.key
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-card text-muted-foreground hover:bg-secondary'
                }`}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>

        {/* What period the NUMBERS describe — not just these tiles: the whole
            page (table, charts, dossier cards and lists) follows the same
            selection. The chart alone spans the whole horizon, so on Days a
            Saturday's zeros would otherwise read as "the period is empty" when
            they only mean "today is empty". */}
        <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          {rangeActive
            ? `Page numbers: ${from} → ${to}`
            : granularity === 'day'
              ? `Page numbers: today · ${buckets.length ? bucketTitle(buckets[buckets.length - 1]!, 'day') : ''} — the chart spans the whole period`
              : `Page numbers: current ${granularity} to date`}
        </div>
        {/* tiles: clicking selects the big chart's metric */}
        <div className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
          {METRICS.map((m) => {
            const value = current ? m.value(current) : null
            const active = m.key === metric.key
            return (
              <button
                key={m.key}
                onClick={() => setMetricKey(m.key)}
                className={`rounded-md border p-3 text-left transition-colors ${
                  active
                    ? 'border-primary bg-secondary shadow-card'
                    : 'border-border bg-card hover:bg-secondary'
                }`}
              >
                <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                  {m.label}
                </div>
                <div className="num mt-1 text-[22px] font-bold leading-none">
                  {value === null ? '—' : `${value}${m.unit === '%' ? '%' : ''}`}
                </div>
                <div className="mt-1 flex items-center justify-between gap-1">
                  <Delta current={current} base={baseStats} metric={m} />
                </div>
                <Sparkline values={buckets.map((b) => m.value(b))} />
                {current ? (
                  <div className="num text-[10px] text-muted-foreground">
                    {m.hint(current)}
                  </div>
                ) : null}
              </button>
            )
          })}
        </div>

        {/* the big chart */}
        <ChartContainer config={config} className="h-[210px] w-full">
          {metric.unit === '%' ? (
            <LineChart accessibilityLayer data={chartRows} margin={{ top: 8, right: 12 }}>
              <CartesianGrid vertical={false} stroke="hsl(var(--border))" />
              <XAxis
                dataKey="name"
                tickLine={false}
                axisLine={false}
                tick={{ fontSize: 11 }}
                minTickGap={28}
              />
              <YAxis
                domain={[0, 100]}
                width={40}
                tickLine={false}
                axisLine={false}
                tick={{ fontSize: 11 }}
                tickFormatter={(v: number) => `${v}%`}
              />
              <ChartTooltip content={<TrendTip />} />
              {testRows.length > 0 ? (
                <ReferenceArea
                  x1={testRows[0]!.name}
                  x2={testRows[testRows.length - 1]!.name}
                  fill="#B25A0B"
                  fillOpacity={0.08}
                />
              ) : null}
              {partialIdx > 0 ? (
                <ReferenceArea
                  x1={chartRows[partialIdx]!.name}
                  x2={chartRows[chartRows.length - 1]!.name}
                  fill="hsl(var(--primary))"
                  fillOpacity={0.06}
                />
              ) : null}
              {/* Empty stretches are marked ON THE LINE, not on the canvas: a
                  striped white-on-dark dashed segment spans each gap (weekend
                  etc.), anchored to the real values on both sides. Outline
                  first, white core second — a line has no stroke-outline of its
                  own, so two layered dashes make the stripe. */}
              <Line
                dataKey="bridge"
                type="linear"
                stroke="hsl(var(--foreground))"
                strokeOpacity={0.7}
                strokeWidth={5}
                strokeDasharray="6 4"
                connectNulls={false}
                dot={false}
                activeDot={false}
                isAnimationActive={false}
              />
              <Line
                dataKey="bridge"
                type="linear"
                stroke="#FFFFFF"
                strokeWidth={2.5}
                strokeDasharray="6 4"
                connectNulls={false}
                dot={false}
                activeDot={false}
                isAnimationActive={false}
              />
              <Line
                dataKey="value"
                type="monotone"
                stroke="var(--color-value)"
                strokeWidth={2}
                connectNulls={false}
                dot={granularity === 'day' ? false : { r: 3 }}
                activeDot={{ r: 5 }}
              />
            </LineChart>
          ) : (
            <BarChart accessibilityLayer data={chartRows} margin={{ top: 8, right: 12 }}>
              <CartesianGrid vertical={false} stroke="hsl(var(--border))" />
              <XAxis
                dataKey="name"
                tickLine={false}
                axisLine={false}
                tick={{ fontSize: 11 }}
                minTickGap={28}
              />
              <YAxis
                width={34}
                allowDecimals={false}
                tickLine={false}
                axisLine={false}
                tick={{ fontSize: 11 }}
              />
              <ChartTooltip cursor={false} content={<TrendTip />} />
              <Bar dataKey="value" radius={4} maxBarSize={26}>
                {chartRows.map((row, i) => (
                  <Cell
                    key={i}
                    fill={row.test ? '#B25A0B' : 'var(--color-value)'}
                    fillOpacity={row.test ? 0.55 : row.partial || row.truncated ? 0.45 : 1}
                  />
                ))}
              </Bar>
            </BarChart>
          )}
        </ChartContainer>

        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
          {rangeActive ? (
            <span>
              Custom range {from} → {to}; compared against the {daysBetweenInclusive(from, to)}{' '}
              days immediately before it.
            </span>
          ) : current && 'start' in (current as TrendPoint) ? (
            <span>
              Latest {granularity} shown to date, compared against the same elapsed days of
              the previous {granularity}.
            </span>
          ) : null}
          {testRows.length > 0 ? (
            <span className="text-high">
              Amber span = the bot's first two weeks (test period
              {horizon.testUntil ? `, until ${horizon.testUntil}` : ''}).
            </span>
          ) : null}
          {partialIdx >= 0 && !rangeActive ? (
            <span>Lighter span = current period, in progress.</span>
          ) : null}
          {hasTruncated ? (
            <span>Earliest bucket starts before the data horizon — incomplete.</span>
          ) : null}
        </div>
      </CardContent>
    </Card>
  )
}
