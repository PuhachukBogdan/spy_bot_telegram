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
              <B>The wait belongs to the manager who owns the chat</B>, not to whoever happened to
              answer. That is what keeps an unanswered chat attached to someone.
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
              The denominator is the manager&apos;s whole portfolio (the chats they own). Silent chats
              stay in it: a quiet chat is a fact about the portfolio, not a gap in the data. A chat
              created after the period ended is left out.
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
              A case appears on the page of the manager who <B>owns the chat</B>. Cases the manager
              wrote are marked <K>manager&apos;s action</K> and counted; cases someone else raised in
              their chat are marked <K>in their chat</K>, shown for context and <B>never counted</B>{' '}
              (the grey <K>+N</K>). An unattributable author is context, not conduct.
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
            <li>
              <B>Admin</B> sees the whole team and both modes — this page and the risk report.
            </li>
            <li>
              <B>Head of affiliates</B> sees every manager <B>except themselves</B>: their own
              row is gone from the roster, from the team totals, from the charts, and any risk
              case or tone flag they wrote is removed too. The team numbers a head reads are
              genuinely the team minus one person, not the full team with a row hidden. The
              risk report mode is not offered — it is built from stored snapshots that cannot
              be filtered per reader.
            </li>
            <li>
              Managers and viewers have no access to this page at all, and the bot does not
              acknowledge that the command exists.
            </li>
          </Ul>

          <H>A manager with no data at all</H>
          <Ul>
            <li>
              <K>no data</K> in the roster, <K>—</K> for SLA and <K>0 / 0</K> chats while Tone of voice
              still shows something is not a fault. Two different attribution rules meet here.
            </li>
            <li>
              <B>SLA, Offline, Active chats and context risks follow the chat owner.</B> A manager who
              owns no chat (never added the bot, no chat authorised under them) has nobody waiting
              &quot;in their chats&quot;, so SLA is <K>—</K> and chats are <K>0 / 0</K>. Replies they give
              in a colleague&apos;s chat go to that colleague&apos;s SLA.
            </li>
            <li>
              <B>Tone of voice, Proposals and own risks follow the author.</B> So the same person gets
              tone numbers from exactly what they wrote, wherever they wrote it — and{' '}
              <K>too few messages</K> if that was little.
            </li>
            <li>
              For such a manager to get SLA and Active chats, chats have to be assigned to them as
              owner. Until then the page shows a dash, not a zero.
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
