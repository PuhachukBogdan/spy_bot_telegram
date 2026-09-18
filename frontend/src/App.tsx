import { useState } from 'react'

import Dossier from '@/components/Dossier'
import HowToRead from '@/components/HowToRead'
import Overview from '@/components/Overview'
import { Separator } from '@/components/ui/separator'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import type { ReportData } from '@/data'
import {
  computeRange,
  resolveRange,
  type PeriodRange,
  type PeriodState,
} from '@/lib/period'

/** Managers tab: roster on the left, dossier on the right.
 *
 * Split by SCALE — overview answers "what happened across the business", the
 * dossier answers "how is this person working". Positive and negative live
 * together inside the dossier rather than behind a global toggle. */
function Managers({
  data,
  selected,
  onSelect,
  period,
  onPeriodChange,
  range,
}: {
  data: ReportData
  selected: string | null
  onSelect: (id: string) => void
  period: PeriodState
  onPeriodChange: (period: PeriodState) => void
  range: PeriodRange | null
}) {
  const active = data.managers.find((m) => m.id === selected) ?? data.managers[0]
  if (!active) {
    return <div className="italic text-muted-foreground">No managers resolved.</div>
  }
  const trends = data.trends
  return (
    <div className="grid gap-5 md:grid-cols-[210px_1fr]">
      <nav className="flex flex-col gap-1">
        {data.managers.map((m) => {
          // The roster's one-line summary follows the selected period too —
          // a nav that says "SLA 44%" while the dossier says 60% would read
          // as a bug, not as two windows.
          const scoped = trends?.managers[m.id]
          const stats =
            range && scoped
              ? computeRange(
                  scoped.day.buckets,
                  trends.chatDays.chats,
                  m.id,
                  range.from,
                  range.to,
                  data.thresholds.activeChatMinMessages,
                )
              : null
          const sla = stats ? stats.slaPercent : m.slaPercent
          const chatsTotal = stats ? stats.coverageTotal : m.chatsTotal
          return (
            <button
              key={m.id}
              onClick={() => onSelect(m.id)}
              className={`rounded-md border px-3 py-2 text-left text-[13px] transition-colors ${
                m.id === active.id
                  ? 'border-primary bg-primary text-primary-foreground'
                  : 'border-border bg-card hover:bg-secondary'
              }`}
            >
              <div className="font-semibold">{m.name}</div>
              <div
                className={`num text-[11px] ${
                  m.id === active.id ? 'opacity-80' : 'text-muted-foreground'
                }`}
              >
                {sla === null ? 'no data' : `SLA ${sla}%`} · {chatsTotal} chats
              </div>
            </button>
          )
        })}
      </nav>
      <Dossier
        manager={active}
        trends={trends}
        activeChatMin={data.thresholds.activeChatMinMessages}
        period={period}
        onPeriodChange={onPeriodChange}
        range={range}
        tone={data.tone}
      />
    </div>
  )
}

/** Mode switch between the two reports. The old risk report stays a full page
 * of its own (its filters and date-range live in its own JS); this is two links
 * styled as one control, not an embedding.
 *
 * Only an admin gets it. The risk report is rendered from stored HTML snapshots,
 * which cannot be re-scoped per viewer, so there is no version of that page a
 * head could read without reading their own cases too. */
function ModeSwitch() {
  const risk = `${window.location.pathname.replace(/\/$/, '')}/risk`
  return (
    <div className="flex overflow-hidden rounded-md border border-border">
      <a
        href={risk}
        className="bg-card px-3 py-1.5 font-mono text-[11px] uppercase tracking-wider text-muted-foreground no-underline transition-colors hover:bg-secondary"
      >
        Risk report
      </a>
      <span className="bg-primary px-3 py-1.5 font-mono text-[11px] uppercase tracking-wider text-primary-foreground">
        Team summary
      </span>
    </div>
  )
}

export default function App({ data }: { data: ReportData }) {
  const [selected, setSelected] = useState<string | null>(null)
  const [tab, setTab] = useState('overview')
  // ONE period selection for the whole page: the analytics block's switch is
  // the control, and overview + dossiers all recount under it. Kept here so
  // switching tabs (or managers) never resets the chosen period.
  const [period, setPeriod] = useState<PeriodState>({
    granularity: 'week',
    from: '',
    to: '',
  })
  const range = data.trends
    ? resolveRange(data.trends.team, data.trends.horizon, period)
    : null

  const openManager = (id: string) => {
    setSelected(id)
    setTab('managers')
  }

  return (
    <div className="mx-auto max-w-[1180px] px-6 pb-16 pt-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="font-display text-[27px] font-extrabold leading-tight tracking-tight">
            Team summary
          </h1>
          <div className="num mb-4 text-[12px] text-muted-foreground">
            {data.since} → {data.until}
            {data.epoch ? ` · counting from ${data.epoch}` : ' · no epoch floor set'}
          </div>
        </div>
        <div className="flex flex-col items-end gap-1.5">
          {data.viewer?.seesRiskReport ? <ModeSwitch /> : null}
          {data.viewer?.name ? (
            <div className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
              {data.viewer.name} · {data.viewer.role}
              {data.viewer.role === 'head' ? ' · your own numbers are not shown' : ''}
              {' · '}
              <a href="/logout" className="underline">
                sign out
              </a>
            </div>
          ) : null}
        </div>
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList className="mb-4">
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="managers">Managers</TabsTrigger>
        </TabsList>
        <TabsContent value="overview">
          <Overview
            data={data}
            onOpen={openManager}
            period={period}
            onPeriodChange={setPeriod}
            range={range}
          />
        </TabsContent>
        <TabsContent value="managers">
          <Managers
            data={data}
            selected={selected}
            onSelect={setSelected}
            period={period}
            onPeriodChange={setPeriod}
            range={range}
          />
        </TabsContent>
      </Tabs>

      <HowToRead data={data} />

      <Separator className="mt-8" />
      <footer className="pt-3 text-[12px] text-muted-foreground">
        {data.previous
          ? `Comparison base: ${data.previous.since} → ${data.previous.until}.`
          : 'No comparable previous period — deltas hidden.'}{' '}
        Imported archive messages are excluded everywhere.
      </footer>
    </div>
  )
}
