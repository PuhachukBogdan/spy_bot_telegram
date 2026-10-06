import { useMemo, useState } from 'react'

import { Card, CardContent } from '@/components/ui/card'
import type { AccountRow, ManagerRow, Trends } from '@/data'
import { addDays, fmtShort, inRange, type PeriodRange } from '@/lib/period'

/** Scope key of one Telegram account — the same key the server uses in
 * `trends.accounts` and in each chat's `ms` holder list. */
export function accountKey(id: string): string {
  return `acct:${id}`
}

/** Accounts in display order: old first, then new, then unlabelled. */
export function orderedAccounts(manager: ManagerRow): AccountRow[] {
  const rank = (a: AccountRow) => (a.label === 'old' ? 0 : a.label === 'new' ? 1 : 2)
  return [...manager.accounts].sort((a, b) => rank(a) - rank(b))
}

export function AccountTag({ account }: { account: AccountRow }) {
  const label = account.label ?? 'account'
  const tone =
    account.label === 'new' ? 'bg-primary/10 text-primary' : 'bg-secondary text-muted-foreground'
  return (
    <span
      className={`rounded-sm px-1 py-0 font-mono text-[9.5px] uppercase tracking-wider ${tone}`}
      title={`Telegram account ${account.id}`}
    >
      {label}
    </span>
  )
}

/** "deactivated · since 30 Sep" — a person who stopped working but is kept on
 *  the page with their history. Nothing after that date is attributed to them. */
export function DeactivatedBadge({
  manager,
  className = '',
}: {
  manager: ManagerRow
  className?: string
}) {
  if (!manager.deactivatedAt) return null
  const since = new Date(`${manager.deactivatedAt}T00:00:00`).toLocaleDateString('en-GB', {
    day: 'numeric',
    month: 'short',
  })
  return (
    <span
      className={`rounded-sm bg-secondary px-1 py-0 font-mono text-[9.5px] uppercase tracking-wider text-muted-foreground ${className}`}
      title={manager.deactivationNote ?? 'Marked deactivated — kept with history, nothing new attributed'}
    >
      deactivated · since {since}
    </span>
  )
}

/** All · Old · New. `value` is 'all' or an account id. */
export function AccountSwitch({
  manager,
  value,
  onChange,
}: {
  manager: ManagerRow
  value: string
  onChange: (value: string) => void
}) {
  const options = [
    { id: 'all', label: 'All accounts' },
    ...orderedAccounts(manager).map((a) => ({
      id: a.id,
      label: a.label ? `${a.label[0]!.toUpperCase()}${a.label.slice(1)} account` : a.id,
    })),
  ]
  return (
    <div className="mb-3 flex flex-wrap items-center gap-3">
      <div className="flex overflow-hidden rounded-md border border-border">
        {options.map((o) => (
          <button
            key={o.id}
            onClick={() => onChange(o.id)}
            className={`px-3 py-1.5 font-mono text-[11px] uppercase tracking-wider transition-colors ${
              value === o.id
                ? 'bg-primary text-primary-foreground'
                : 'bg-card text-muted-foreground hover:bg-secondary'
            }`}
            title={o.id === 'all' ? 'The person — every account together' : `Telegram account ${o.id}`}
          >
            {o.label}
          </button>
        ))}
      </div>
      {value !== 'all' ? (
        <span className="text-[11.5px] text-muted-foreground">
          Only what this account did: replies, proposals, own cases, chats it is in. Unanswered
          waits and tone stay on the person.
        </span>
      ) : null}
    </div>
  )
}

/** Sum a sparse day map over a range. */
function sumDays(map: Record<string, number> | undefined, range: PeriodRange | null): number {
  if (!map) return 0
  let total = 0
  for (const [day, count] of Object.entries(map)) {
    if (!range || inRange(day, range)) total += count
  }
  return total
}

const MAX_LISTED = 12

function ChatList({ title, hint, names }: { title: string; hint: string; names: string[] }) {
  const [open, setOpen] = useState(false)
  const shown = open ? names : names.slice(0, MAX_LISTED)
  return (
    <div>
      <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
        {title} · <span className="num text-foreground">{names.length}</span>
      </div>
      <div className="mb-1 text-[11.5px] text-muted-foreground">{hint}</div>
      {names.length === 0 ? (
        <div className="text-[12px] italic text-muted-foreground">none</div>
      ) : (
        <ul className="space-y-0.5 text-[12px]">
          {shown.map((n) => (
            <li key={n} className="truncate">
              {n}
            </li>
          ))}
          {names.length > MAX_LISTED ? (
            <li>
              <button className="text-[11.5px] underline" onClick={() => setOpen(!open)}>
                {open ? 'show less' : `+${names.length - MAX_LISTED} more`}
              </button>
            </li>
          ) : null}
        </ul>
      )}
    </div>
  )
}

/** The cross-account view: how far the person has moved to the new account.
 *
 * Weekly share of messages written from the new account(s), the chats where
 * the new account is not present yet, and the chats where both are present
 * but only the old one still writes in the selected period. */
export function MovingBlock({
  manager,
  trends,
  range,
  periodNote,
}: {
  manager: ManagerRow
  trends: Trends
  range: PeriodRange | null
  periodNote: string
}) {
  const accounts = orderedAccounts(manager)
  const olds = accounts.filter((a) => a.label === 'old')
  const news = accounts.filter((a) => a.label !== 'old')

  const data = useMemo(() => {
    const entries = trends.accountDays.filter((e) => e.m === manager.id)
    const entryOf = (id: string) => entries.find((e) => e.a === id)
    const names = new Map(manager.chats.map((c) => [c.id, c.name]))

    // Weekly share of new-account messages, last 8 weeks (Monday-based).
    const today = trends.horizon.today
    const dow = (new Date(`${today}T00:00:00Z`).getUTCDay() + 6) % 7
    const thisMonday = addDays(today, -dow)
    const weeks = Array.from({ length: 8 }, (_, i) => addDays(thisMonday, -7 * (7 - i)))
    const weekly = weeks.map((start) => {
      const r: PeriodRange = { from: start, to: addDays(start, 6), custom: true }
      const fromNew = news.reduce((s, a) => s + sumDays(entryOf(a.id)?.d, r), 0)
      const fromOld = olds.reduce((s, a) => s + sumDays(entryOf(a.id)?.d, r), 0)
      const total = fromNew + fromOld
      return { start, total, share: total ? Math.round((100 * fromNew) / total) : null }
    })

    const oldKeys = olds.map((a) => accountKey(a.id))
    const newKeys = news.map((a) => accountKey(a.id))
    const onlyOld: string[] = []
    const stillOld: string[] = []
    for (const chat of trends.chatDays.chats) {
      if (!chat.ms.includes(manager.id)) continue
      const hasOld = oldKeys.some((k) => chat.ms.includes(k))
      const hasNew = newKeys.some((k) => chat.ms.includes(k))
      const name = names.get(chat.i) ?? chat.i
      if (hasOld && !hasNew) {
        onlyOld.push(name)
        continue
      }
      if (hasOld && hasNew) {
        const oldWrote = olds.some((a) => sumDays(entryOf(a.id)?.cd[chat.i], range) > 0)
        const newWrote = news.some((a) => sumDays(entryOf(a.id)?.cd[chat.i], range) > 0)
        if (oldWrote && !newWrote) stillOld.push(name)
      }
    }
    onlyOld.sort()
    stillOld.sort()
    return { weekly, onlyOld, stillOld }
  }, [trends, manager, olds, news, range])

  if (olds.length === 0 || news.length === 0) return null
  const lastOld = olds
    .map((a) => a.lastActiveAt)
    .filter((x): x is string => !!x)
    .sort()
    .pop()

  return (
    <Card className="mb-2 shadow-card">
      <CardContent className="p-3 text-[12px]">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
            Moving to the new account
          </span>
          <span className="text-[11.5px] text-muted-foreground">
            old account last wrote: {lastOld ? lastOld.slice(0, 16).replace('T', ' ') : 'not in 30 days'}
          </span>
        </div>
        <div className="mb-3 flex items-end gap-1.5" title="Share of messages written from the new account, per week">
          {data.weekly.map((w) => (
            <div key={w.start} className="flex w-[52px] flex-col items-center gap-1">
              <div className="num text-[11px] text-foreground">{w.share === null ? '—' : `${w.share}%`}</div>
              <div className="relative h-[34px] w-[22px] overflow-hidden rounded-sm bg-secondary">
                <div
                  className="absolute bottom-0 left-0 right-0 bg-primary"
                  style={{ height: `${w.share ?? 0}%` }}
                />
              </div>
              <div className="num text-[10px] text-muted-foreground">{fmtShort(w.start)}</div>
            </div>
          ))}
          <div className="ml-2 self-center text-[11px] text-muted-foreground">
            share of messages
            <br />
            from the new account
          </div>
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <ChatList
            title="New account not in the chat yet"
            hint="Only the old account is a member — the partner still sees the old one."
            names={data.onlyOld}
          />
          <ChatList
            title={`Both present, only old writes${periodNote}`}
            hint="The new account was added but the old one still does the talking."
            names={data.stillOld}
          />
        </div>
      </CardContent>
    </Card>
  )
}
