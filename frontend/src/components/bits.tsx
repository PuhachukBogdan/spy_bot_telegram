import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import type { UnitType, WorkHours } from '@/data'

/** A percentage, or an explicit dash — never 0, which would read as failure. */
export function Pct({ value }: { value: number | null }) {
  if (value === null) return <span className="italic text-muted-foreground">—</span>
  return <span className="num text-[19px] font-semibold">{value}%</span>
}

export function Meter({ value }: { value: number | null }) {
  if (value === null) return null
  return (
    <div className="mt-1.5 h-[6px] w-full min-w-[80px] overflow-hidden rounded-full bg-secondary">
      <div className="h-full rounded-full bg-primary" style={{ width: `${value}%` }} />
    </div>
  )
}

export function Stat({
  label,
  value,
  hint,
}: {
  label: string
  value: string
  hint?: string
}) {
  return (
    <Card className="shadow-card">
      <CardContent className="p-4">
        <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          {label}
        </div>
        <div className="num mt-1 text-[25px] font-bold leading-none">{value}</div>
        {hint ? (
          <div className="num mt-1 text-[11px] text-muted-foreground">{hint}</div>
        ) : null}
      </CardContent>
    </Card>
  )
}

/** Private units are visually loudest: groups run to the hundreds, private chats
 *  are a handful, and the rare one is the one worth noticing. */
const UNIT_STYLE: Record<UnitType, { label: string; className: string }> = {
  business: { label: 'private', className: 'border-crit-line bg-crit-bg text-crit' },
  topic: { label: 'topic', className: 'border-high-line bg-high-bg text-high' },
  group: { label: 'group', className: 'border-line bg-secondary text-muted-foreground' },
}

export function UnitBadge({ type }: { type: UnitType }) {
  const style = UNIT_STYLE[type] ?? UNIT_STYLE.group
  return (
    <Badge
      variant="outline"
      className={`rounded-sm px-1.5 py-0 font-mono text-[9.5px] font-semibold uppercase tracking-wider ${style.className}`}
    >
      {style.label}
    </Badge>
  )
}

export function HoursCell({ hours }: { hours: WorkHours | null }) {
  if (!hours) return <span className="italic text-muted-foreground">—</span>
  return (
    <div>
      <span className="num text-[13px]">
        {hours.start}–{hours.end}
      </span>{' '}
      <Badge
        variant="outline"
        className={`ml-1 rounded-sm px-1.5 py-0 font-mono text-[9.5px] uppercase tracking-wider ${
          hours.assumed ? 'border-high-line bg-high-bg text-high' : 'border-line text-ok'
        }`}
      >
        {hours.assumed ? 'assumed' : 'personal'}
      </Badge>
      <div className="num text-[11px] text-muted-foreground">{hours.timezone}</div>
    </div>
  )
}

const LEVEL_STYLE: Record<string, string> = {
  critical: 'border-crit-line bg-crit-bg text-crit',
  high: 'border-high-line bg-high-bg text-high',
  medium: 'border-med-line bg-med-bg text-med',
  low: 'border-low-line bg-low-bg text-low',
}

export function LevelBadge({ level, score }: { level: string; score: number }) {
  return (
    <Badge
      variant="outline"
      className={`rounded-sm px-1.5 py-0 font-mono text-[9.5px] font-semibold uppercase tracking-wider ${
        LEVEL_STYLE[level] ?? LEVEL_STYLE.low
      }`}
    >
      {level} {score}
    </Badge>
  )
}

export function riskLabel(type: string): string {
  return type.replace(/_/g, ' ')
}
