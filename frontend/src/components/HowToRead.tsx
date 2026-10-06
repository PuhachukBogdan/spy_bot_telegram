import { useState, type ReactNode } from 'react'

import { FoldableTitle } from '@/components/bits'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import type { ReportData } from '@/data'
import { loadCollapsed, persistCollapsed } from '@/lib/period'

/** The in-page reading guide. Long on purpose: it is the reference a viewer
 * opens when a number surprises them, so every label the page can show is
 * explained here, with the thresholds read from the same island the numbers
 * come from — the guide can never quote a limit the page does not use. Folds
 * like the dossier sections and remembers the choice. */

const min = (seconds: number) => Math.round(seconds / 60)

function H({ children }: { children: ReactNode }) {
  return (
    <h3 className="mb-1.5 mt-5 font-display text-[11px] uppercase tracking-widest text-primary first:mt-0">
      {children}
    </h3>
  )
}

function B({ children }: { children: ReactNode }) {
  return <b className="text-foreground">{children}</b>
}

function K({ children }: { children: ReactNode }) {
  return (
    <code className="rounded-sm bg-secondary px-1 py-0.5 font-mono text-[11px] text-foreground">
      {children}
    </code>
  )
}

function Ul({ children }: { children: ReactNode }) {
  return <ul className="list-disc space-y-1.5 pl-5">{children}</ul>
}

const TONE_ROWS: { key: string; label: string; flagged: string; number: string; ideal: string }[] = [
  {
    key: 'toxicity',
    label: 'Toxicity',
    flagged:
      'Rudeness, contempt, mockery, threats, blaming the partner as a person. A dry style, a firm "no" or citing the rules is NOT a case.',
    number: 'Share of judged messages flagged. Red bar.',
    ideal: '0%',
  },
  {
    key: 'completeness',
    label: 'Completeness',
    flagged:
      'The partner asked something concrete, the reply engages but plainly leaves a part unanswered, and no later message the same day returns to it. "Will check, back by 15:00" with a same-day return is NOT a case.',
    number: '100 − share flagged. Blue bar.',
    ideal: '100%',
  },
  {
    key: 'courtesy',
    label: 'Courtesy & register',
    flagged:
      'Register clearly off for the relationship: a one-word reply to a polite, detailed request; a new partner’s greeting ignored; talking down. Informal "ты", slang, emoji between people who know each other are NOT a case.',
    number: '100 − share flagged. Blue bar.',
    ideal: '100%',
  },
  {
    key: 'deescalation',
    label: 'Handling complaints',
    flagged:
      'The partner is visibly unhappy (complaint, frustration, threat to leave, repeated pings) and the reply ignores that entirely and offers no step — or answers with a counter-complaint. A brief acknowledgement plus a step or a time is NOT a case.',
    number: 'COUNT of clear misses, plus per 100 judged messages. Red bar.',
    ideal: '0',
  },
  {
    key: 'initiative',
    label: 'Initiative',
    flagged:
      'The manager moved first: warned about a delay, a holiday or a payment problem; suggested an improvement, a new offer or GEO; followed up unprompted. The one POSITIVE dimension — a flag here is good.',
    number: 'COUNT of such moves, plus per 100 judged messages. Green bar.',
    ideal: 'the more the better',
  },
]

export default function HowToRead({ data }: { data: ReportData }) {
  const isAdmin = data.viewer?.role === 'admin'
  const [collapsed, setCollapsed] = useState<boolean>(() => loadCollapsed().howto)
  const toggle = () =>
    setCollapsed((prev) => {
      persistCollapsed('howto', !prev)
      return !prev
    })

  const t = data.thresholds
  const tone = data.tone
  const minAssessed = tone?.minAssessed ?? 20
  const toneRows = tone
    ? TONE_ROWS.filter((r) => tone.metrics.some((m) => m.key === r.key))
    : TONE_ROWS

  return (
    <section className="mt-9">
      <FoldableTitle open={!collapsed} onToggle={toggle} className="mb-2 mt-0 text-[13px]">
        How to read this
      </FoldableTitle>
      {collapsed ? null : (
        <div className="rounded-md border bg-card p-4 text-[13px] leading-relaxed text-muted-foreground">
          <H>The page and the period switch</H>
          <Ul>
            <li>
              <B>One period for the whole page.</B> The Days · Weeks · Months · Quarters buttons in the
              analytics block rescope every tile, chart, table, risk card, chat list and tone gauge at
              once. The current bucket is &quot;from its start until today&quot;: Days = today, Weeks =
              this week from Monday, Months = this month from the 1st. A custom range exists only in
              Days (two date fields + Reset). The small line under the buttons always says what the
              numbers cover.
            </li>
            <li>
              <B>The chart shows the whole history</B> (up to 120 days), never just the selected period
              — otherwise an empty &quot;today&quot; would look like no data at all. Days with nothing to
              measure (mostly weekends) are bridged by a dashed line, which is not data.
            </li>
            <li>
              <B>The dates in the header</B> are the 30-day window the server collected detail for. The
              numbers on screen follow the selected period, not the header.
            </li>
            <li>
              <B>Tiles</B> are buttons: click one to change the big chart&apos;s metric. The arrow next
              to a tile compares the same number of elapsed days in the previous period (15 August vs
              1–15 July, not vs the whole of July). <K>pp</K> = percentage points. <K>no base</K> = the
              previous period reaches past the history horizon, so no comparison is shown.{' '}
              <K>partial data</K> = the bucket is not finished.
            </li>
          </Ul>

          <H>SLA — reply speed</H>
          <Ul>
            <li>
              A partner message starts a timer; the manager&apos;s first reply stops it. Replies inside{' '}
              <B>{min(t.slaSeconds)} min</B> count as on time, and so do substantial replies (over{' '}
              {t.substantiveChars} characters) inside <B>{min(t.graceSeconds)} min</B>. Everything else
              is late.
            </li>
            <li>
              Timers start <B>only inside the manager&apos;s working hours</B> — never at night, on
              weekends or holidays. A burst of partner messages is one wait, timed from the first.
            </li>
            <li>
              <B>A wait somebody answered belongs to the person who answered</B> — on time or late
              by their own timing. A reply by someone not on this page (an admin) closes the wait
              and is credited to nobody; the team still counts it.
            </li>
            <li>
              <B>A wait nobody answered is charged to the chat&apos;s crew</B>: the managers present
              in the chat who had written there within the last {t.crewLookbackDays} days and whose
              working hours covered that moment. One chat usually has several managers; a silence is
              shared by the people who were working it, not pinned on whoever added the bot. A chat
              nobody has worked yet falls back to the manager who added it.
            </li>
            <li>
              A dash (<K>—</K>) means nothing waited in this period. It is not a failure and not a zero.
            </li>
          </Ul>

          <H>Offline</H>
          <Ul>
            <li>
              Waits with no reply for <B>{min(t.offlineSeconds)} min</B> or more (or never answered).
              Counted separately and kept <B>out of the SLA %</B>: absence is not slowness, and
              averaging it in would hide it. Red when above zero.
            </li>
            <li>
              <B>The tile shows both figures — <K>73 / 41%</K>.</B> Every wait ends either rated
              (someone answered inside the window) or offline, so the share is{' '}
              <K>offline ÷ (offline + rated)</K>: 41% of all waits in the period got no reply at
              all. A bare count cannot say &quot;out of what&quot; and grows with sheer traffic, so
              the arrow tracks the share and reads in pp.
            </li>
            <li>
              This is <B>not</B> the SLA percentage upside down. SLA divides by the rated waits
              only; offline divides by all of them. Two denominators on purpose — SLA 59% and
              Offline 41% are not meant to add up to 100.
            </li>
          </Ul>

          <H>Active chats</H>
          <Ul>
            <li>
              A chat is active with at least <B>{t.activeChatMinMessages} messages per 30 days</B>. For
              a shorter or longer period the threshold scales: {t.activeChatMinMessages} × days / 30,
              minimum 1 — so 3 for a week, 1 for a day.
            </li>
            <li>
              The denominator is the manager&apos;s whole portfolio — <B>every chat they are present
              in</B>, as Telegram reports it now, so one chat counts for each manager in it. Silent
              chats stay in it: a quiet chat is a fact about the portfolio, not a gap in the data. A
              chat created after the period ended is left out.
            </li>
            <li>
              Membership is re-read from Telegram every few hours and on every join or leave the bot
              sees; group titles are refreshed the same way, so a renamed group shows its current
              name.
            </li>
          </Ul>

          <H>Proposals</H>
          <Ul>
            <li>
              Times the manager came to the partner with a proposal on their own initiative, as detected
              by the LLM during the regular analysis. Counted by the author of the message.
            </li>
          </Ul>

          <H>Risk</H>
          <Ul>
            <li>
              A case a manager <B>wrote</B> appears on that manager&apos;s page, marked{' '}
              <K>manager&apos;s action</K>, and counts. A case someone else raised appears, marked{' '}
              <K>in their chat</K>, on the page of every manager in that chat&apos;s crew that day —
              shown for context and <B>never counted</B> (the grey <K>+N</K>). A colleague&apos;s case
              counts against the colleague only. An unattributable author is context, not conduct.
            </li>
            <li>
              The <B>Risk by category</B> chart on the overview includes context cases: it asks
              &quot;what is happening across the business&quot;, not &quot;who did it&quot;.
            </li>
            <li>There is deliberately no combined manager score.</li>
          </Ul>

          <H>Tone of voice</H>
          <Ul>
            <li>
              <B>Where the numbers come from.</B> Once a day, after Kyiv midnight, an LLM reads the
              previous day&apos;s manager messages in partner chats (with a tail of the day before as
              context) and flags only <B>clear</B> cases on five dimensions. The instruction is strict:
              if a flag needs arguing for, it is not a flag. An ordinary day yields none, and that is the
              expected result. Today is never judged — only finished days.
            </li>
            <li>
              <B>Not judged at all:</B> message length, reply speed (that is SLA), industry jargon,
              mixed Russian/English, typos. Only the manager&apos;s own messages are judged; partner and
              colleague messages are context.
            </li>
          </Ul>
          <div className="my-3 overflow-x-auto rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[150px]">Gauge</TableHead>
                  <TableHead>What counts as a case</TableHead>
                  <TableHead className="w-[190px]">The big number</TableHead>
                  <TableHead className="w-[110px]">Ideal</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {toneRows.map((r) => (
                  <TableRow key={r.key}>
                    <TableCell className="font-semibold text-foreground">{r.label}</TableCell>
                    <TableCell className="text-[12.5px]">{r.flagged}</TableCell>
                    <TableCell className="text-[12.5px]">{r.number}</TableCell>
                    <TableCell className="num text-[12.5px]">{r.ideal}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          <Ul>
            <li>
              <B>How to read a percentage — an example.</B> A gauge says <K>Completeness 95.6%</K>{' '}
              with <K>10 flagged · 225 judged</K> under it. That means: in this period the model
              judged 225 of the manager&apos;s messages and in ten of them found a clearly unanswered
              part of a question. 100 − 10/225 = 95.6%.
            </li>
            <li>
              <B>It does NOT mean &quot;10 of 225 questions went unanswered&quot;.</B> Questions are not
              counted at all. The denominator of every percentage gauge is the same:{' '}
              <B>all judged manager messages in the period</B>, whatever they were about. So the two
              &quot;gap&quot; gauges (Completeness, Courtesy) sit high and move by tenths — that is how
              the metric is built, not a merit. Read the <B>small count</B> under the percentage (how
              many cases) and the <B>Tone flags</B> list in the dossier (which ones). 95.6% is ten
              gaps at 225 messages and a hundred at 2 250.
            </li>
            <li>
              <B>How to read a count — Handling complaints.</B> The gauge shows <K>1</K> with{' '}
              <K>1 in 225 messages · 0.4 per 100</K> under it: one clear case in the period of an
              unhappy partner getting neither acknowledgement nor a step, across 225 judged messages.
              It is a count, not a share of complaints — complaints themselves are not counted. Ideal
              is 0; compare managers by the <B>per 100</B> figure, not by the bare number, when their
              volumes differ.
            </li>
            <li>
              <B>The small line</B> under every gauge is the raw material. Rate gauges:{' '}
              <K>N flagged · M judged</K> — N cases out of M judged messages. Count gauges (Handling
              complaints, Initiative): <K>N in M messages · X per 100</K>.
            </li>
            <li>
              <B>The arrow</B> next to a gauge name compares with the equal-length range just before
              the selected one: <K>+1.2 pp</K> for percentages, <K>+3</K> for counts. Green = better in
              the gauge&apos;s own direction (for Toxicity and Handling complaints, down is better),
              red = worse. No arrow = one side lacked messages.
            </li>
            <li>
              <B><K>too few messages</K> instead of a number.</B> A <B>rate</B> is shown only once at
              least <B>{minAssessed}</B> manager messages were judged in the selected period. Below
              that the gauge says <K>too few messages</K> and shows how many were judged out of{' '}
              {minAssessed}. With 5 messages one flag would print as &quot;20% toxicity&quot; — noise,
              not a measurement. The raw counts under the gauge and the Tone flags list still show.
              The two <B>count</B> gauges (Handling complaints, Initiative) are never hidden: one miss
              is one miss whatever the volume, and the per-100 figure next to it carries the volume.
            </li>
            <li>
              <B>What to do:</B> widen the period. A manager who writes 3–5 messages a day will read{' '}
              <K>too few messages</K> on Days and Weeks almost always — a fact about their volume of
              correspondence, not a fault. On Months they usually clear the floor.
            </li>
            <li>
              <B>It is not a zero.</B> Zero cases with enough messages shows as <K>0%</K> for Toxicity,{' '}
              <K>100%</K> for the two gap gauges, <K>0</K> for Handling complaints (the best possible
              result) and <K>0</K> for Initiative (no plus seen). <K>too few messages</K> speaks only
              about the denominator (little judged), nothing about the numerator.
            </li>
            <li>
              <B><K>nothing judged in this period</K></B> — no judged messages for this manager (or the
              team) in the period: they wrote nothing in partner chats, the day is not finished and
              processed yet, or the daily pass is switched off (the card header then says{' '}
              <K>daily review is OFF</K>).
            </li>
            <li>
              <B>What is judged:</B> text messages (and voice transcriptions) by a manager in{' '}
              <B>active</B>, non-test partner groups and topics. Private Business chats, the imported
              archive and test chats are out. Stickers and captionless photos are context, not subjects.
              Attribution is by the <B>author</B> of the message, not by the chat owner: a reply in a
              colleague&apos;s chat counts toward the person who wrote it.
            </li>
            <li>
              <B>How a flag is accepted:</B> the model must give a verbatim quote and a confidence; a
              flag survives only if the confidence clears the floor, the quote really occurs in the
              message (a paraphrase is rejected), the message is one it was asked to judge, and no flag
              already stands on that message for that gauge. Rejected flags reach neither the numbers
              nor the list.
            </li>
          </Ul>

          <H>Who sees what</H>
          <Ul>
            <li>
              <B>You sign in as yourself.</B> There is no shared password: the link is the
              same for everyone and the page is built for whoever opened it. Send{' '}
              <K>/dashboard</K> to the bot for a sign-in link, or use the Telegram button on
              the login page. A session lasts 90 days per device; <K>sign out</K> in the corner
              ends it.
            </li>
            {/* Access rules are for admins only. A head is never told that their own
                activity is read, that it is removed from their view, or that a wider
                view exists — the page simply shows them the team. */}
            {isAdmin ? (
              <>
              <li>
                <B>Admin</B> sees the whole team and both modes — this page and the risk report.
              </li>
              <li>
                <B>Head of affiliates</B> sees the team summary; the risk report is admin-only.
              </li>
              <li>
                Managers and viewers have no access to this page at all, and the bot does not
                acknowledge that the command exists.
              </li>
              </>
            ) : null}
          </Ul>

          <H>Several managers in one chat</H>
          <Ul>
            <li>
              A partner group normally holds three to five of our managers. The page follows that:
              the chat sits in <B>each</B> of their portfolios, while the team counts it once. So the
              managers&apos; chat counts add up to more than the team&apos;s — by design, not by
              double counting.
            </li>
            <li>
              <B>What is personal:</B> replies given (SLA), messages written (Tone, Proposals), cases
              written (Risk). <B>What is shared:</B> an unanswered wait and a partner-raised case, which
              go to the chat&apos;s crew — the managers present who had been working the chat.
            </li>
            <li>
              A manager present in many chats but writing in few has a large portfolio and a small
              crew footprint: many chats, few waits. That is the head of department&apos;s normal shape.
            </li>
          </Ul>

          <H>Accounts: old and new</H>
          <Ul>
            <li>
              A person with more than one Telegram account gets one row per account under their own
              row in the overview table, with the same columns for what that account did. The
              person&apos;s row is the total and the only one counted in the team numbers.
            </li>
            <li>
              <K>old</K> is the account seen first; <K>new</K> the one that came later. An admin can
              pin the label.
            </li>
            <li>
              <B>In the dossier</B> the switch <K>All accounts · Old account · New account</K> reruns
              the whole page for one account: SLA from the replies that account gave, Active chats
              over the chats it is a member of, proposals and own risk cases it wrote, its tone flags
              and, in the chat table, how much it wrote in each chat. <B>Offline and the tone gauges
              stay on the person</B> — an unanswered wait belongs to nobody&apos;s account.
            </li>
            <li>
              <B>Moving to the new account</B> (All view): the weekly share of messages written from
              the new account, the chats where the new account is not a member yet, and the chats
              where both are members but only the old one still writes in the selected period.
            </li>
          </Ul>

          <H>Deactivated</H>
          <Ul>
            <li>
              <K>deactivated · since …</K> marks a person who stopped working but was kept on the page
              on purpose. Their history stays exactly as it was; from that date no chat, wait or case
              is attributed to them, so the current period reads <K>—</K> and <K>0 / 0</K>. Replies
              they personally give after that date would still count as theirs.
            </li>
          </Ul>

          <H>A manager with no data at all</H>
          <Ul>
            <li>
              <K>no data</K> in the roster, <K>—</K> for SLA and <K>0 / 0</K> chats while Tone of voice
              still shows something is not a fault. Two different attribution rules meet here.
            </li>
            <li>
              <B>Active chats follow presence</B>: a manager in no active chat has <K>0 / 0</K>.{' '}
              <B>SLA follows replies and the crew</B>: a manager who answered nothing and worked no
              chat in the last {t.crewLookbackDays} days has nothing to be charged, so SLA is <K>—</K>.
            </li>
            <li>
              <B>Tone of voice, Proposals and own risks follow the author.</B> So the same person gets
              tone numbers from exactly what they wrote, wherever they wrote it — and{' '}
              <K>too few messages</K> if that was little.
            </li>
          </Ul>

          <H>Badges</H>
          <Ul>
            <li>
              <K>private</K> (red) — a Telegram Business chat; rare, so marked loudest. <K>topic</K>{' '}
              (orange) — a topic inside a forum group. <K>group</K> (grey) — an ordinary group, the bulk.
            </li>
            <li>
              <K>personal</K> / <K>assumed</K> — working hours set by the person (<K>/set_hours</K>) or
              taken from the default. An assumed schedule is a guess; do not compare it head-on with a
              personal one.
            </li>
            <li>
              <K>critical 80–100</K> · <K>high 60–79</K> · <K>medium 30–59</K> · <K>low 0–29</K> — risk
              level and score, set by the LLM alone.
            </li>
            <li>
              In the Tone flags list an orange label (toxicity, completeness, courtesy, handling
              complaints) is a remark; a green <K>initiative</K> label is a plus.
            </li>
          </Ul>

          <H>Dash, zero and empty are three different things</H>
          <Ul>
            <li>
              <K>—</K> — nothing to measure; nobody waited. Not a failure.
            </li>
            <li>
              <K>0%</K> in SLA — people waited and nobody answered on time. A failure.
            </li>
            <li>
              <K>0</K> in Offline / Risk — good: nothing lost, nothing fired.
            </li>
            <li>
              <K>0%</K> Toxicity, <K>100%</K> Completeness / Courtesy, <K>0</K> Handling complaints —
              good: messages judged, no clear cases found. <K>0</K> Initiative — messages judged, no
              unprompted moves seen; the absence of a plus, not a failure.
            </li>
            <li>
              <K>too few messages</K> — under {minAssessed} judged; a rate is hidden so one case cannot
              become a headline. Counts still show. Widen the period.
            </li>
            <li>
              <K>no base</K> — no valid previous period; the delta is hidden, not zeroed.
            </li>
            <li>An empty day on the chart — a weekend, night, or simply nobody wrote.</li>
          </Ul>
          <p className="mt-2 italic">
            The rule: the page never substitutes a zero for &quot;no data&quot;, because a zero reads as a
            failing grade.
          </p>

          <H>Deliberately excluded from every number</H>
          <Ul>
            <li>The imported message archive — excluded everywhere, tone included.</li>
            <li>Risk cases written by someone other than the manager — shown, never counted.</li>
            <li>Offline waits — never inside the SLA %.</li>
            <li>Waits outside working hours — nights, weekends and holidays start no timer.</li>
            <li>
              Stub &quot;managers&quot; minted from an aff_id in a chat title — only real people with a
              Telegram account appear here.
            </li>
            <li>The dictionary (Tier-1) score — a queue priority only; the LLM alone sets risk levels.</li>
            <li>
              Tone: Business private chats, test chats, partner and colleague messages, empty messages,
              and today.
            </li>
          </Ul>
        </div>
      )}
    </section>
  )
}
