import { useMemo } from 'react'

import { UnitBadge } from '@/components/bits'
import { Card, CardContent } from '@/components/ui/card'
import type { ToneData, ToneFlag, ToneMetricDef } from '@/data'
import { inRange, type PeriodRange } from '@/lib/period'
import {
  deltaIsGood,
  formatToneDelta,
  formatToneValue,
  isCount,
  previousRange,
  readTone,
  sumTone,
  toneDelta,
  toneFill,
  toneLabel,
  type ToneReading,
} from '@/lib/tone'

/** Bar colour by what a full bar MEANS: red for anything bad (a flag rate, a
 * count of misses), house blue for a "100 % = clean" gap metric, green for the
 * count of good moves. The number keeps the metric's natural direction —
 * toxicity reads "2 %", not "98 % non-toxic" — because that is how people talk
 * about it. */
const FILL: Record<ToneMetricDef['polarity'], string> = {
  negative: 'bg-crit',
  positive_gap: 'bg-primary',
  negative_event: 'bg-crit',
  positive_event: 'bg-ok',
}

function Gauge({
  reading,
  previous,
  minAssessed,
}: {
  reading: ToneReading
  previous: ToneReading | null
  minAssessed: number
}) {
  const delta = previous ? toneDelta(reading, previous) : null
  const good = delta !== null ? deltaIsGood(reading.def, delta) : null
  // The sub-line is the raw material behind the headline. "judged" is spelled
  // out because the denominator is ALL judged manager messages — a reader who
  // sees "1 of 255" under a gauge must not take 255 for complaints or questions.
  const sub = !reading.assessed
    ? 'no messages judged'
    : isCount(reading.def)
      ? `${reading.flagged} in ${reading.assessed} messages · ${reading.per100 ?? 0} per 100`
      : `${reading.flagged} flagged · ${reading.assessed} judged`
  return (
    <div className="min-w-0" title={reading.def.description}>
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          {reading.def.label}
        </span>
        {delta !== null ? (
          <span
            className={`num text-[11px] ${
              good === null ? 'text-muted-foreground' : good ? 'text-ok' : 'text-crit'
            }`}
            title="vs the equal-length period just before"
          >
            {formatToneDelta(reading.def, delta)}
          </span>
        ) : null}
      </div>
      <div className="num mt-0.5 text-[22px] font-bold leading-none">
        {reading.enough ? (
          formatToneValue(reading)
        ) : (
          <span
            className="text-[13px] font-normal italic text-muted-foreground"
            title={`A rate needs at least ${minAssessed} judged messages in the period. Widen the period.`}
          >
            too few messages{' '}
            <span className="num not-italic">
              ({reading.assessed} of {minAssessed})
            </span>
          </span>
        )}
      </div>
      <div className="mt-1.5 h-[6px] w-full overflow-hidden rounded-full bg-secondary">
        <div
          className={`h-full rounded-full ${FILL[reading.def.polarity]}`}
          style={{ width: `${toneFill(reading)}%` }}
        />
      </div>
      <div className="num mt-1 text-[11px] text-muted-foreground">{sub}</div>
    </div>
  )
}

/** The tone-of-voice gauges for one scope (team, or one manager), rescoped to
 * the selected period like every other number on the page. */
export default function ToneBlock({
  tone,
  managerId,
  range,
  periodNote,
}: {
  tone: ToneData
  managerId: string | null
  range: PeriodRange | null
  periodNote: string
}) {
  const readings = useMemo(
    () => readTone(sumTone(tone.days, managerId, range), tone.metrics, tone.minAssessed),
    [tone, managerId, range],
  )
  const previous = useMemo(() => {
    if (!range) return null
    return readTone(
      sumTone(tone.days, managerId, previousRange(range)),
      tone.metrics,
      tone.minAssessed,
    )
  }, [tone, managerId, range])

  const judged = readings[0]?.assessed ?? 0
  return (
    <Card className="shadow-card">
      <CardContent className="p-4">
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="font-display text-[12px] uppercase tracking-widest text-primary">
            Tone of voice{periodNote}
          </h3>
          <span className="num text-[11px] text-muted-foreground">
            {judged ? `${judged} manager messages judged` : 'nothing judged in this period'}
            {!tone.enabled ? ' · daily review is OFF' : ''}
          </span>
        </div>
        {judged === 0 ? (
          <div className="text-[13px] italic text-muted-foreground">
            {tone.enabled
              ? 'The daily pass runs after local midnight for the day just ended — numbers appear once a full day has been judged.'
              : 'Tone review is switched off (TONE_ANALYSIS_ENABLED). Historic numbers, if any, still show when a period contains them.'}
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            {readings.map((r, i) => (
              <Gauge
                key={r.def.key}
                reading={r}
                previous={previous ? previous[i] ?? null : null}
                minAssessed={tone.minAssessed}
              />
            ))}
          </div>
        )}
        {judged > 0 && judged < tone.minAssessed ? (
          <div className="mt-3 text-[11px] italic text-muted-foreground">
            Rates are shown from {tone.minAssessed} judged messages — fewer than that turns one
            flag into a headline. Counts (Handling complaints, Initiative) always show.
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}

/** The dossier's folded review list: every accepted flag in the period, newest
 * first, with the verbatim quote. It exists so a number can be traced to the
 * messages behind it — and so the prompt can be calibrated against real cases. */
export function ToneFlagList({
  flags,
  defs,
  range,
}: {
  flags: ToneFlag[]
  defs: ToneMetricDef[]
  range: PeriodRange | null
}) {
  const rows = useMemo(
    () => (range ? flags.filter((f) => inRange(f.day, range)) : flags),
    [flags, range],
  )
  if (rows.length === 0) {
    return (
      <div className="rounded-md border border-dashed bg-card p-3 text-[13px] text-muted-foreground">
        No tone flags in this period.
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-2">
      {rows.map((f) => {
        const def = defs.find((d) => d.key === f.metric)
        const good = def?.polarity === 'positive_event'
        return (
          <div
            key={f.id}
            className={`rounded-md border bg-card p-3 ${
              good ? 'border-l-[3px] border-l-ok' : 'border-l-[3px] border-l-high'
            }`}
          >
            <div className="mb-1 flex flex-wrap items-center gap-2">
              <span
                className={`rounded-sm px-1.5 py-0 font-mono text-[9.5px] uppercase tracking-wider ${
                  good ? 'bg-secondary text-ok' : 'bg-high-bg text-high'
                }`}
              >
                {toneLabel(f.metric, defs)}
              </span>
              <UnitBadge type={f.unitType} />
              <span className="text-[12px] text-muted-foreground">{f.chatName}</span>
              <span className="num ml-auto text-[11px] text-muted-foreground">
                {f.at.replace('T', ' ')} · conf {Math.round(f.confidence * 100)}%
              </span>
            </div>
            <div className="mb-1 border-l-2 border-line pl-2 text-[13px] italic">“{f.quote}”</div>
            <div className="text-[12.5px] text-muted-foreground">{f.reason}</div>
          </div>
        )
      })}
    </div>
  )
}
