import { useMemo, useState } from 'react'

import { HoursCell, LevelBadge, Stat, UnitBadge, riskLabel } from '@/components/bits'
import TrendBlock from '@/components/TrendBlock'
import { Card, CardContent } from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import type { ManagerRow, RiskCase, Trends } from '@/data'
import {
  computeRange,
  coverageThreshold,
  daysBetweenInclusive,
  inRange,
  loadCollapsed,
  periodLabel,
  saveCollapsed,
  type PeriodRange,
  type PeriodState,
  type SectionId,
} from '@/lib/period'

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="mb-2 mt-6 font-display text-[12px] uppercase tracking-widest text-primary">
      {children}
    </h3>
  )
}

/** A section header that folds its body. The longest dossier blocks (risk cards,
 * the chat table) hide behind these so the page opens readable; the chosen state
 * persists across visits via localStorage (best-effort — a blocked store just
 * means defaults). The header keeps its count, so a folded section still says
 * how much it is hiding. */
function FoldableTitle({
  open,
  onToggle,
  children,
}: {
  open: boolean
  onToggle: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onToggle}
      aria-expanded={open}
      className="mb-2 mt-6 flex w-full items-center gap-1.5 text-left font-display text-[12px] uppercase tracking-widest text-primary transition-colors hover:text-foreground"
    >
      <span
        aria-hidden="true"
        className={`inline-block text-[10px] transition-transform ${open ? 'rotate-90' : ''}`}
      >
        ▶
      </span>
      <span>{children}</span>
      {!open ? (
        <span className="ml-1 font-mono text-[9px] normal-case tracking-wider text-muted-foreground">
          — click to expand
        </span>
      ) : null}
    </button>
  )
}

/** A risk card. Context cases — raised by someone else in this manager's chat —
 *  are deliberately recessive: no severity spine, muted type. They belong on the
 *  page because the chat is theirs, but the eye must not read them as conduct. */
function RiskCard({ risk }: { risk: RiskCase }) {
  return (
    <div
      className={`mb-2 rounded-md border bg-card p-3 ${
        risk.counts ? 'border-l-[3px] border-l-crit' : 'border-l-[3px] border-l-line'
      }`}
    >
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <LevelBadge level={risk.riskLevel} score={risk.score} />
        <span className="font-mono text-[11px] uppercase tracking-wider text-foreground">
          {riskLabel(risk.riskType)}
        </span>
        <span
          className={`rounded-sm px-1.5 py-0 font-mono text-[9.5px] uppercase tracking-wider ${
            risk.counts
              ? 'bg-crit-bg text-crit'
              : 'bg-secondary text-muted-foreground'
          }`}
        >
          {risk.counts ? "manager's action" : 'in their chat'}
        </span>
        <span className="num ml-auto text-[11px] text-muted-foreground">
          {risk.detectedAt.replace('T', ' ')}
        </span>
      </div>
      <div className="mb-1 flex items-center gap-2 text-[12px] text-muted-foreground">
        <UnitBadge type={risk.unitType} />
        <span>{risk.chatName}</span>
      </div>
      {risk.phrase ? (
        <div className="mb-1 border-l-2 border-line pl-2 text-[13px] italic">
          “{risk.phrase}”
        </div>
      ) : null}
      {risk.why ? <div className="text-[12.5px] text-muted-foreground">{risk.why}</div> : null}
      {!risk.counts ? (
        <div className="mt-1.5 text-[11px] italic text-muted-foreground">
          Raised by someone else — shown for context, excluded from every number.
        </div>
      ) : null}
    </div>
  )
}

export default function Dossier({
  manager,
  trends,
  activeChatMin,
  period,
  onPeriodChange,
  range,
}: {
  manager: ManagerRow
  trends: Trends | null
  activeChatMin: number
  period: PeriodState
  onPeriodChange: (period: PeriodState) => void
  range: PeriodRange | null
}) {
  const trend = trends?.managers[manager.id] ?? null

  const [collapsed, setCollapsed] = useState<Record<SectionId, boolean>>(loadCollapsed)
  const toggle = (id: SectionId) =>
    setCollapsed((prev) => {
      const next = { ...prev, [id]: !prev[id] }
      saveCollapsed(next)
      return next
    })

  // Everything below the analytics block follows the selected period: the risk
  // list filters by local detection day, proposals sum from the day buckets,
  // and the chat table recounts messages against the pro-rated threshold.
  const periodRisks = useMemo(
    () => (range ? manager.risks.filter((r) => inRange(r.day, range)) : manager.risks),
    [manager.risks, range],
  )
  const own = periodRisks.filter((r) => r.counts)
  const context = periodRisks.filter((r) => !r.counts)

  const proposals = useMemo(() => {
    if (!trends || !trend || !range) return manager.proposals
    return computeRange(
      trend.day.buckets,
      trends.chatDays.chats,
      manager.id,
      range.from,
      range.to,
      activeChatMin,
    ).proposals
  }, [trends, trend, range, manager, activeChatMin])

  const chatRows = useMemo(() => {
    if (!trends || !range) return manager.chats
    const byId = new Map(trends.chatDays.chats.map((c) => [c.i, c]))
    const threshold = coverageThreshold(
      activeChatMin,
      daysBetweenInclusive(range.from, range.to),
    )
    return manager.chats
      .map((chat) => {
        const entry = byId.get(chat.id)
        let messages = 0
        if (entry) {
          for (const [day, count] of Object.entries(entry.d)) {
            if (inRange(day, range)) messages += count
          }
        }
        return { ...chat, messages, active: messages >= threshold }
      })
      .sort((a, b) => b.messages - a.messages || a.name.localeCompare(b.name))
  }, [trends, range, manager.chats, activeChatMin])
  const activeCount = chatRows.filter((c) => c.active).length
  const quietCount = chatRows.length - activeCount
  const periodNote = range ? ` · ${periodLabel(period, range)}` : ''

  return (
    <div>
      {/* The manager-scoped analytics block — same component as the overview's,
          so the two scales can never drift apart visually or numerically. */}
      {trends && trend ? (
        <div className="mb-4">
          <TrendBlock
            trend={trend}
            chats={trends.chatDays.chats}
            horizon={trends.horizon}
            managerId={manager.id}
            activeChatMin={activeChatMin}
            period={period}
            onPeriodChange={onPeriodChange}
          />
        </div>
      ) : (
        <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat
            label="SLA"
            value={manager.slaPercent === null ? '—' : `${manager.slaPercent}%`}
            hint={`${manager.slaMet} / ${manager.slaRated} rated`}
          />
          <Stat
            label="Offline waits"
            value={String(manager.slaOffline)}
            hint="excluded from SLA %"
          />
          <Stat
            label="Active chats"
            value={
              manager.coveragePercent === null ? '—' : `${manager.coveragePercent}%`
            }
            hint={`${manager.chatsActive} / ${manager.chatsTotal}`}
          />
          <Stat label="Proposals" value={String(manager.proposals)} hint="counted as positive" />
        </div>
      )}

      <Card className="mb-2 shadow-card">
        <CardContent className="flex flex-wrap items-center gap-x-8 gap-y-2 p-3 text-[12px]">
          <div>
            <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
              Work hours
            </span>
            <div className="mt-0.5">
              <HoursCell hours={manager.workHours} />
            </div>
          </div>
          <div>
            <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
              Risk cases{periodNote}
            </span>
            <div className="num mt-0.5 text-[13px]">
              {own.length} own · {context.length} context
            </div>
          </div>
        </CardContent>
      </Card>

      <SectionTitle>Positive</SectionTitle>
      {proposals > 0 ? (
        <div className="rounded-md border bg-card p-3 text-[13px]">
          <b className="num text-[17px]">{proposals}</b> manager proposals in
          this period. Tone-of-voice signals (completeness, slang, toxicity) land here
          once that track ships.
        </div>
      ) : (
        <div className="rounded-md border border-dashed bg-card p-3 text-[13px] text-muted-foreground">
          No proposals recorded in this period.
        </div>
      )}

      <FoldableTitle open={!collapsed.risks} onToggle={() => toggle('risks')}>
        Negative — {periodRisks.length === 0 ? 'no cases' : `${own.length} own`}
        {context.length > 0 ? ` · ${context.length} context` : ''}
        {periodNote}
      </FoldableTitle>
      {collapsed.risks ? null : periodRisks.length === 0 ? (
        <div className="rounded-md border border-dashed bg-card p-3 text-[13px] text-muted-foreground">
          No risk cases in this period.
        </div>
      ) : (
        <>
          {own.map((r) => (
            <RiskCard key={r.id} risk={r} />
          ))}
          {context.length > 0 ? (
            <>
              <div className="mb-2 mt-4 text-[11px] uppercase tracking-widest text-muted-foreground">
                Context — not counted
              </div>
              {context.map((r) => (
                <RiskCard key={r.id} risk={r} />
              ))}
            </>
          ) : null}
        </>
      )}

      <FoldableTitle open={!collapsed.chats} onToggle={() => toggle('chats')}>
        Chats ({chatRows.length}) — {activeCount} active{periodNote}
      </FoldableTitle>
      {collapsed.chats ? null : (
        <>
          <Card className="overflow-hidden shadow-card">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Chat</TableHead>
                  <TableHead className="w-[90px]">Type</TableHead>
                  <TableHead className="w-[110px] text-right">Messages</TableHead>
                  <TableHead className="w-[90px] text-right">Active</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {chatRows.map((c) => (
                  <TableRow key={c.id} className={c.active ? '' : 'text-muted-foreground'}>
                    <TableCell className="max-w-[420px] truncate">{c.name}</TableCell>
                    <TableCell>
                      <UnitBadge type={c.unitType} />
                    </TableCell>
                    <TableCell className="num text-right">{c.messages}</TableCell>
                    <TableCell className="num text-right">{c.active ? 'yes' : '—'}</TableCell>
                  </TableRow>
                ))}
                {chatRows.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={4} className="italic text-muted-foreground">
                      No chats owned in this period.
                    </TableCell>
                  </TableRow>
                ) : null}
              </TableBody>
            </Table>
          </Card>
          {quietCount > 0 ? (
            <>
              <Separator className="mt-4" />
              <div className="pt-2 text-[12px] text-muted-foreground">
                {quietCount} of {chatRows.length} chats stayed below the activity
                threshold. They remain in the denominator — a silent chat is a fact about
                the portfolio, not a gap in the data.
              </div>
            </>
          ) : null}
        </>
      )}
    </div>
  )
}
