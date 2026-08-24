import { useMemo } from 'react'
import { Bar, BarChart, CartesianGrid, LabelList, XAxis, YAxis } from 'recharts'

import { Meter, Pct, Stat, riskLabel } from '@/components/bits'
import TrendBlock from '@/components/TrendBlock'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from '@/components/ui/chart'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import type { ReportData } from '@/data'
import {
  computeRange,
  inRange,
  periodLabel,
  type MetricSource,
  type PeriodRange,
  type PeriodState,
} from '@/lib/period'

/** Everything one overview row needs, already scoped to the selected period.
 * Falls back to the server's detail-window numbers when no trends exist. */
interface RowStats {
  slaPercent: number | null
  slaMet: number
  slaRated: number
  offline: number
  coveragePercent: number | null
  chatsActive: number
  chatsTotal: number
  proposals: number
  risksOwn: number
  risksContext: number
}

/** Trim the brand suffix so the axis reads as names, not repeated boilerplate. */
function shortName(name: string): string {
  return name.split('|')[0]?.trim() || name
}

/** Tooltip for percentage bars — the % sign is never implicit. */
function PctTip({
  active,
  payload,
}: {
  active?: boolean
  payload?: { value?: number | string; payload?: { name?: string } }[]
}) {
  const first = payload?.[0]
  if (!active || first?.value === undefined) return null
  return (
    <div className="num rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] shadow-lift">
      {first.payload?.name}: {first.value}%
    </div>
  )
}

/** One series → no legend; the title names it. Bars are direct-labelled instead
 *  of carrying an axis. Managers with nothing rated are omitted rather than drawn
 *  as a zero-length bar, which would read as total failure. */
function SlaChart({ rows }: { rows: { name: string; sla: number }[] }) {
  if (rows.length === 0) return null

  const config = {
    sla: { label: 'Replies within target', color: 'hsl(var(--chart-1))' },
  } satisfies ChartConfig

  return (
    <Card className="shadow-card">
      <CardHeader className="pb-1">
        <CardTitle className="font-display text-[12px] uppercase tracking-widest text-primary">
          SLA by manager
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ChartContainer config={config} className="h-[180px] w-full">
          <BarChart accessibilityLayer data={rows} layout="vertical" margin={{ right: 44 }}>
            <CartesianGrid horizontal={false} stroke="hsl(var(--border))" />
            <YAxis
              dataKey="name"
              type="category"
              tickLine={false}
              axisLine={false}
              width={96}
              tick={{ fontSize: 12 }}
            />
            <XAxis type="number" domain={[0, 100]} hide />
            <ChartTooltip cursor={false} content={<PctTip />} />
            <Bar dataKey="sla" fill="var(--color-sla)" radius={4} barSize={18}>
              <LabelList
                dataKey="sla"
                position="right"
                offset={8}
                className="fill-foreground"
                fontSize={12}
                formatter={(v: number) => `${v}%`}
              />
            </Bar>
          </BarChart>
        </ChartContainer>
      </CardContent>
    </Card>
  )
}

function CategoryChart({ categories }: { categories: { type: string; count: number }[] }) {
  const rows = categories.map((c) => ({
    name: riskLabel(c.type),
    count: c.count,
  }))
  if (rows.length === 0) {
    return (
      <Card className="shadow-card">
        <CardHeader className="pb-1">
          <CardTitle className="font-display text-[12px] uppercase tracking-widest text-primary">
            Risk by category
          </CardTitle>
        </CardHeader>
        <CardContent className="pb-6 text-[13px] text-muted-foreground">
          No risk cases in this period.
        </CardContent>
      </Card>
    )
  }

  const config = {
    count: { label: 'Cases', color: 'hsl(var(--chart-1))' },
  } satisfies ChartConfig

  return (
    <Card className="shadow-card">
      <CardHeader className="pb-1">
        <CardTitle className="font-display text-[12px] uppercase tracking-widest text-primary">
          Risk by category
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ChartContainer
          config={config}
          className="w-full"
          style={{ height: `${Math.max(120, rows.length * 26 + 24)}px` }}
        >
          <BarChart accessibilityLayer data={rows} layout="vertical" margin={{ right: 36 }}>
            <CartesianGrid horizontal={false} stroke="hsl(var(--border))" />
            <YAxis
              dataKey="name"
              type="category"
              tickLine={false}
              axisLine={false}
              width={130}
              tick={{ fontSize: 11 }}
            />
            <XAxis type="number" hide />
            <ChartTooltip cursor={false} content={<ChartTooltipContent />} />
            <Bar dataKey="count" fill="var(--color-count)" radius={4} barSize={14}>
              <LabelList
                dataKey="count"
                position="right"
                offset={7}
                className="fill-foreground"
                fontSize={11}
              />
            </Bar>
          </BarChart>
        </ChartContainer>
      </CardContent>
    </Card>
  )
}

export default function Overview({
  data,
  onOpen,
  period,
  onPeriodChange,
  range,
}: {
  data: ReportData
  onOpen: (id: string) => void
  period: PeriodState
  onPeriodChange: (period: PeriodState) => void
  range: PeriodRange | null
}) {
  const trend = data.trends
  const rated = data.managers.filter((m) => m.slaPercent !== null)
  const avgSla = rated.length
    ? Math.round((rated.reduce((s, m) => s + (m.slaPercent ?? 0), 0) / rated.length) * 10) / 10
    : null
  const offline = data.managers.reduce((s, m) => s + m.slaOffline, 0)
  const chatsActive = data.managers.reduce((s, m) => s + m.chatsActive, 0)
  const chatsTotal = data.managers.reduce((s, m) => s + m.chatsTotal, 0)
  const risksOwn = data.managers.reduce((s, m) => s + m.risksOwn, 0)
  const risksContext = data.managers.reduce((s, m) => s + m.risksContext, 0)

  // Every row rescoped to the selected period: SLA/coverage/proposals summed
  // from day buckets, risk counts from the period-filtered case list. Without
  // trends (no horizon at all) the server's window numbers stand as-is.
  const rows: Map<string, RowStats> = useMemo(() => {
    const out = new Map<string, RowStats>()
    for (const m of data.managers) {
      const scoped = trend?.managers[m.id]
      if (trend && range && scoped) {
        const stats: MetricSource = computeRange(
          scoped.day.buckets,
          trend.chatDays.chats,
          m.id,
          range.from,
          range.to,
          data.thresholds.activeChatMinMessages,
        )
        const risks = m.risks.filter((r) => inRange(r.day, range))
        out.set(m.id, {
          slaPercent: stats.slaPercent,
          slaMet: stats.slaMet,
          slaRated: stats.slaRated,
          offline: stats.offline,
          coveragePercent: stats.coveragePercent,
          chatsActive: stats.coverageActive,
          chatsTotal: stats.coverageTotal,
          proposals: stats.proposals,
          risksOwn: risks.filter((r) => r.counts).length,
          risksContext: risks.filter((r) => !r.counts).length,
        })
      } else {
        out.set(m.id, {
          slaPercent: m.slaPercent,
          slaMet: m.slaMet,
          slaRated: m.slaRated,
          offline: m.slaOffline,
          coveragePercent: m.coveragePercent,
          chatsActive: m.chatsActive,
          chatsTotal: m.chatsTotal,
          proposals: m.proposals,
          risksOwn: m.risksOwn,
          risksContext: m.risksContext,
        })
      }
    }
    return out
  }, [data, trend, range])

  // Category totals follow the period too — context cases included, same as the
  // server's horizon-wide totals: the overview asks "what is happening across
  // the business", not "who did it".
  const categories = useMemo(() => {
    if (!trend || !range) return data.categories
    const totals = new Map<string, number>()
    for (const m of data.managers) {
      for (const r of m.risks) {
        if (inRange(r.day, range)) totals.set(r.riskType, (totals.get(r.riskType) ?? 0) + 1)
      }
    }
    return [...totals.entries()]
      .map(([type, count]) => ({ type, count }))
      .sort((a, b) => b.count - a.count || a.type.localeCompare(b.type))
  }, [data, trend, range])

  const slaRows = data.managers
    .map((m) => ({ name: shortName(m.name), sla: rows.get(m.id)?.slaPercent ?? null }))
    .filter((r): r is { name: string; sla: number } => r.sla !== null)

  return (
    <div>
      {/* The analytics block replaces the old hero tiles; the plain tiles stay
          only as the fallback when no trend horizon exists at all. */}
      {trend ? (
        <div className="mb-4">
          <TrendBlock
            trend={trend.team}
            chats={trend.chatDays.chats}
            horizon={trend.horizon}
            managerId={null}
            activeChatMin={data.thresholds.activeChatMinMessages}
            period={period}
            onPeriodChange={onPeriodChange}
          />
        </div>
      ) : (
        <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat
            label="Avg SLA"
            value={avgSla === null ? '—' : `${avgSla}%`}
            hint={`${rated.length} of ${data.managers.length} rated`}
          />
          <Stat label="Offline waits" value={String(offline)} hint="excluded from SLA %" />
          <Stat
            label="Active chats"
            value={chatsTotal ? `${Math.round((100 * chatsActive) / chatsTotal)}%` : '—'}
            hint={`${chatsActive} / ${chatsTotal}`}
          />
          <Stat
            label="Risk cases"
            value={String(risksOwn + risksContext)}
            hint={`${risksOwn} by managers · ${risksContext} context`}
          />
        </div>
      )}

      {range ? (
        <div className="mb-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          Charts and table below: {periodLabel(period, range)}
        </div>
      ) : null}
      <div className="mb-4 grid gap-3 md:grid-cols-2">
        <SlaChart rows={slaRows} />
        <CategoryChart categories={categories} />
      </div>

      {/* Few managers, four measures: a table with inline meters beats any chart —
          exact numbers, instant comparison, nothing truncated. */}
      <Card className="overflow-hidden shadow-card">
        <Table>
          <TableHeader>
            <TableRow className="bg-primary hover:bg-primary">
              {['Manager', 'SLA', 'Offline', 'Active chats', 'Proposals', 'Risk'].map(
                (h, i, all) => (
                  <TableHead
                    key={h}
                    className={`font-display text-[11px] uppercase tracking-wider text-primary-foreground ${
                      i === 0 ? 'pl-5' : 'text-right'
                    } ${i === all.length - 1 ? 'pr-6' : ''}`}
                  >
                    {h}
                  </TableHead>
                ),
              )}
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.managers.map((m) => {
              const r = rows.get(m.id)!
              return (
                <TableRow
                  key={m.id}
                  onClick={() => onOpen(m.id)}
                  className="cursor-pointer"
                  title="Open dossier"
                >
                  <TableCell className="pl-5 font-semibold underline decoration-dotted underline-offset-4">
                    {m.name}
                  </TableCell>
                  <TableCell className="text-right">
                    <Pct value={r.slaPercent} />
                    <Meter value={r.slaPercent} />
                    <div className="num text-[11px] text-muted-foreground">
                      {r.slaMet} / {r.slaRated}
                    </div>
                  </TableCell>
                  <TableCell
                    className={`num text-right ${
                      r.offline > 0 ? 'text-crit' : 'text-muted-foreground'
                    }`}
                  >
                    {r.offline}
                  </TableCell>
                  <TableCell className="text-right">
                    <Pct value={r.coveragePercent} />
                    <Meter value={r.coveragePercent} />
                    <div className="num text-[11px] text-muted-foreground">
                      {r.chatsActive} / {r.chatsTotal}
                    </div>
                  </TableCell>
                  <TableCell className="num text-right">{r.proposals}</TableCell>
                  <TableCell className="num pr-6 text-right">
                    {r.risksOwn}
                    {r.risksContext > 0 ? (
                      <span className="text-muted-foreground"> +{r.risksContext}</span>
                    ) : null}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </Card>
    </div>
  )
}
