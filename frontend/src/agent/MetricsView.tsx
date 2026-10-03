// The metrics dashboard (M16): the operating metrics of every stored turn trace, as the backend
// computes them (GET /api/agent/metrics). Loaded once when opened and again on Refresh only; each
// chart has its numbers in a table too, so nothing depends on reading a color or a bar length.
// The headline figures are always shown; the charts are grouped in tabs by category, so each
// category fits on one screen.
import { useRef, useState } from "react";
import type { KeyboardEvent, ReactElement, ReactNode } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TooltipContentProps } from "recharts";

import type { AgentApi, AuditMetrics } from "./client";
import { count, dateTime, ms, usd } from "./format";
import {
  costRows,
  latencyRows,
  languageRows,
  outcomeColor,
  outcomeRows,
  P50_COLOR,
  P95_COLOR,
  tierRows,
} from "./metrics";
import { Empty, Loading, Section, useResource } from "./ui";

const INK = "#a3a9b3"; // --muted: axis and label text
const GRID = "rgba(222, 228, 236, 0.09)"; // --line
const AXIS = "rgba(222, 228, 236, 0.16)"; // --line-strong
const TICK = { fill: INK, fontSize: 12 };
const CURSOR = { fill: "rgba(253, 251, 218, 0.04)" };
const BAR = 24; // thickest bar, px
const ROUNDED_TOP: [number, number, number, number] = [4, 4, 0, 0];
const ROUNDED_END: [number, number, number, number] = [0, 4, 4, 0];

const STOPPED: Record<string, string> = {
  language: "Language (GATE-01)",
  authentication: "Authentication (GATE-02)",
  account: "Account (GATE-03, GATE-04)",
  no_decision: "No decision (e.g. an error)",
};

/** "36.7%": one decimal, as the evaluation report writes rates. */
export function rate(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
}

function still(): boolean {
  return typeof window.matchMedia !== "function" || window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** The tooltip of every chart: the category, then each series with its formatted value. */
export function Tip({
  active,
  payload,
  label,
  format,
  swatches = true,
}: Partial<TooltipContentProps> & { format: (value: number) => string; swatches?: boolean }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="chart-tip">
      <p className="chart-tip__title">{label}</p>
      <ul className="chart-tip__list">
        {payload.map((item) => (
          <li key={String(item.dataKey)} className="chart-tip__row">
            {swatches && <span className="chart-tip__swatch" style={{ background: item.color }} aria-hidden="true" />}
            <span>{item.name}</span>
            <span className="chart-tip__value">{format(Number(item.value))}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** A chart's card: its title and note, the chart, and its data as a table; or why it is empty. */
function ChartCard({
  id,
  title,
  note,
  empty,
  table,
  children,
}: {
  id: string;
  title: string;
  note?: ReactNode;
  empty: string | null;
  table: ReactNode;
  children: ReactNode;
}) {
  return (
    <Section id={id} title={title}>
      {note && <p className="muted chart__note">{note}</p>}
      {empty !== null ? (
        <Empty>{empty}</Empty>
      ) : (
        <>
          <div className="chart">{children}</div>
          <details className="chart__data">
            <summary>Show the data</summary>
            <div className="table-wrap">{table}</div>
          </details>
        </>
      )}
    </Section>
  );
}

function DataTable({ caption, head, rows }: { caption: string; head: string[]; rows: ReactNode[][] }) {
  return (
    <table className="table">
      <caption className="sr-only">{caption}</caption>
      <thead>
        <tr>
          {head.map((cell) => (
            <th key={cell} scope="col">
              {cell}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={index}>
            {row.map((cell, column) =>
              column === 0 ? (
                <th key={column} scope="row">
                  {cell}
                </th>
              ) : (
                <td key={column}>{cell}</td>
              ),
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** A chart that takes its card's width; it starts at a fixed size so it renders before (or
 * without) a measurement. */
function Frame({ height, children }: { height: number; children: ReactElement }) {
  return (
    <ResponsiveContainer width="100%" height={height} initialDimension={{ width: 640, height }}>
      {children}
    </ResponsiveContainer>
  );
}

const rowsHeight = (rows: number, series = 1) => Math.max(140, rows * (series * BAR + 28) + 56);

function Tile({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="stat">
      <dt className="stat__label">{label}</dt>
      <dd className="stat__value">{value}</dd>
      {detail && <dd className="stat__detail">{detail}</dd>}
    </div>
  );
}

function Headline({ m }: { m: AuditMetrics }) {
  return (
    <section className="card reveal" aria-labelledby="metrics-headline">
      <h2 id="metrics-headline" className="sr-only">
        Headline figures
      </h2>
      <dl className="stats">
        <Tile label="Conversations" value={count(m.conversations)} detail={`${count(m.turns)} turns`} />
        <Tile label="Attempted cases" value={count(m.attempted_cases)} detail="reached a transaction" />
        <Tile
          label="Automated resolutions"
          value={count(m.automated_resolutions)}
          detail="case created, verified, no handoff"
        />
        <Tile label="Containment" value={rate(m.containment_rate)} detail={`${count(m.contained)} without a handoff`} />
        <Tile label="Escalation rate" value={rate(m.escalation_rate)} detail={`${count(m.escalated)} handed off`} />
        <Tile label="Abandoned" value={count(m.abandoned)} detail="last outcome CLARIFY" />
      </dl>
    </section>
  );
}

function Outcomes({ m, animate }: { m: AuditMetrics; animate: boolean }) {
  const rows = outcomeRows(m.outcomes);
  const stopped = Object.entries(m.ended_before_transaction).filter(([, n]) => n > 0);
  return (
    <ChartCard
      id="chart-outcomes"
      title="Conversations by outcome"
      note="The outcome of each conversation's last turn."
      empty={rows.length ? null : "No conversations yet."}
      table={
        <DataTable
          caption="Conversations by outcome"
          head={["Outcome", "Conversations", "Share"]}
          rows={rows.map((row) => [row.outcome, count(row.count), rate(row.share)])}
        />
      }
    >
      <Frame height={260}>
        <BarChart data={rows} margin={{ top: 24, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="outcome" interval={0} tick={{ ...TICK, fontSize: 11 }} stroke={AXIS} tickLine={false} />
          <YAxis allowDecimals={false} tick={TICK} stroke={AXIS} tickLine={false} axisLine={false} width={40} />
          <Tooltip
            cursor={CURSOR}
            content={(props) => <Tip {...props} swatches={false} format={(v) => `${count(v)} conversations`} />}
          />
          <Bar dataKey="count" name="Conversations" maxBarSize={BAR} radius={ROUNDED_TOP} isAnimationActive={animate}>
            {rows.map((row) => (
              <Cell key={row.outcome} fill={outcomeColor(row.outcome)} />
            ))}
            <LabelList dataKey="count" position="top" fill={INK} fontSize={12} />
          </Bar>
        </BarChart>
      </Frame>
      {stopped.length > 0 && (
        <div className="chart__aside">
          <h3 className="subtitle">Ended before a transaction was evaluated</h3>
          <dl className="inline-counts">
            {stopped.map(([where, n]) => (
              <div key={where}>
                <dt>{STOPPED[where] ?? where}</dt>
                <dd>{count(n)}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
    </ChartCard>
  );
}

function Automation({ m, animate }: { m: AuditMetrics; animate: boolean }) {
  const rows = tierRows(m.outcomes_by_tier);
  const data = rows.map((row) => ({ ...row, value: row.rate ?? 0, label: tierLabel(row.rate, row.resolved, row.conversations) }));
  return (
    <ChartCard
      id="chart-automation"
      title="Automation by tier"
      note="Share of the T1 and T2 conversations (policy §6) whose final outcome is RESOLVE. T3 is never automated."
      empty={rows.length ? null : "No conversation has reached a T1 or T2 amount yet."}
      table={
        <DataTable
          caption="Automation by tier"
          head={["Tier", "Conversations", "Resolved", "Automation"]}
          rows={rows.map((row) => [row.tier, count(row.conversations), count(row.resolved), rate(row.rate)])}
        />
      }
    >
      <Frame height={rowsHeight(rows.length)}>
        <BarChart data={data} layout="vertical" margin={{ top: 8, right: 132, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={GRID} horizontal={false} />
          <XAxis
            type="number"
            domain={[0, 1]}
            ticks={[0, 0.25, 0.5, 0.75, 1]}
            tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
            tick={TICK}
            stroke={AXIS}
            tickLine={false}
          />
          <YAxis type="category" dataKey="tier" tick={TICK} stroke={AXIS} tickLine={false} width={40} />
          <Tooltip cursor={CURSOR} content={(props) => <Tip {...props} swatches={false} format={(v) => rate(v)} />} />
          <Bar dataKey="value" name="Automation" fill={P50_COLOR} maxBarSize={BAR} radius={ROUNDED_END} isAnimationActive={animate}>
            <LabelList dataKey="label" position="right" fill={INK} fontSize={12} />
          </Bar>
        </BarChart>
      </Frame>
    </ChartCard>
  );
}

function tierLabel(value: number | null, resolved: number, conversations: number): string {
  return value === null ? "no conversations" : `${rate(value)} · ${resolved} of ${conversations}`;
}

function Latency({ m, animate }: { m: AuditMetrics; animate: boolean }) {
  const rows = latencyRows(m);
  return (
    <ChartCard
      id="chart-latency"
      title="Latency p50 / p95"
      note="Per turn, and per stage of the turn (a stage counts only the turns it ran in)."
      empty={rows.length ? null : "No turn with a measured latency yet."}
      table={
        <DataTable
          caption="Latency p50 and p95"
          head={["Stage", "Turns", "p50", "p95"]}
          rows={rows.map((row) => [row.name, count(row.count), ms(row.p50), ms(row.p95)])}
        />
      }
    >
      <Frame height={rowsHeight(rows.length, 2)}>
        <BarChart data={rows} layout="vertical" barGap={2} margin={{ top: 8, right: 24, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={GRID} horizontal={false} />
          <XAxis type="number" tickFormatter={(v: number) => ms(v)} tick={TICK} stroke={AXIS} tickLine={false} />
          <YAxis type="category" dataKey="name" tick={TICK} stroke={AXIS} tickLine={false} width={120} />
          <Tooltip cursor={CURSOR} content={(props) => <Tip {...props} format={(v) => ms(v)} />} />
          <Legend verticalAlign="top" align="left" height={32} iconType="circle" iconSize={8} itemSorter={null} />
          <Bar dataKey="p50" name="p50" fill={P50_COLOR} maxBarSize={BAR} radius={ROUNDED_END} isAnimationActive={animate} />
          <Bar dataKey="p95" name="p95" fill={P95_COLOR} maxBarSize={BAR} radius={ROUNDED_END} isAnimationActive={animate} />
        </BarChart>
      </Frame>
    </ChartCard>
  );
}

function Cost({ m, animate }: { m: AuditMetrics; animate: boolean }) {
  const rows = costRows(m);
  const data = rows.map((row) => ({ ...row, label: usd(row.usd) }));
  return (
    <ChartCard
      id="chart-cost"
      title="Cost"
      note={
        <>
          The total cost of every turn divided by the attempted cases, and by the automated resolutions, so
          conversations that did not resolve count too. Total: {usd(m.total_cost_usd)}.
          {m.turns_without_cost > 0 && ` ${count(m.turns_without_cost)} turns have no cost (no token rates) and are not in it.`}
        </>
      }
      empty={rows.length ? null : "No cost to divide yet (no priced turn, or no attempted case)."}
      table={
        <DataTable
          caption="Cost"
          head={["Cost", "USD", "Divided by"]}
          rows={rows.map((row) => [row.name, usd(row.usd, 6), row.basis])}
        />
      }
    >
      <Frame height={rowsHeight(rows.length)}>
        <BarChart data={data} layout="vertical" margin={{ top: 8, right: 96, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={GRID} horizontal={false} />
          <XAxis type="number" tickFormatter={(v: number) => v.toFixed(4)} tick={TICK} stroke={AXIS} tickLine={false} />
          <YAxis type="category" dataKey="name" tick={TICK} stroke={AXIS} tickLine={false} width={120} />
          <Tooltip cursor={CURSOR} content={(props) => <Tip {...props} swatches={false} format={(v) => usd(v, 6)} />} />
          <Bar dataKey="usd" name="Cost" fill={P50_COLOR} maxBarSize={BAR} radius={ROUNDED_END} isAnimationActive={animate}>
            <LabelList dataKey="label" position="right" fill={INK} fontSize={12} />
          </Bar>
        </BarChart>
      </Frame>
    </ChartCard>
  );
}

function Languages({ m, animate }: { m: AuditMetrics; animate: boolean }) {
  const { rows, outcomes } = languageRows(m.outcomes_by_language);
  return (
    <ChartCard
      id="chart-languages"
      title="Conversations by language"
      note="Each language's conversations, split by final outcome; the language is the conversation's last known one."
      empty={rows.length ? null : "No conversations yet."}
      table={
        <DataTable
          caption="Conversations by language and outcome"
          head={["Language", ...outcomes, "Total"]}
          rows={rows.map((row) => [row.language, ...outcomes.map((o) => count(Number(row[o] ?? 0))), count(row.total)])}
        />
      }
    >
      <Frame height={rowsHeight(rows.length) + 32}>
        <BarChart data={rows} layout="vertical" margin={{ top: 8, right: 24, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={GRID} horizontal={false} />
          <XAxis type="number" allowDecimals={false} tick={TICK} stroke={AXIS} tickLine={false} />
          <YAxis type="category" dataKey="language" tick={TICK} stroke={AXIS} tickLine={false} width={100} />
          <Tooltip cursor={CURSOR} content={(props) => <Tip {...props} format={(v) => count(v)} />} />
          <Legend verticalAlign="top" align="left" height={32} iconType="circle" iconSize={8} itemSorter={null} />
          {outcomes.map((outcome, index) => (
            <Bar
              key={outcome}
              dataKey={outcome}
              name={outcome}
              stackId="outcome"
              fill={outcomeColor(outcome)}
              stroke="#262626"
              strokeWidth={2}
              maxBarSize={BAR}
              radius={index === outcomes.length - 1 ? ROUNDED_END : 0}
              isAnimationActive={animate}
            />
          ))}
        </BarChart>
      </Frame>
    </ChartCard>
  );
}

const TABS = [
  { id: "outcomes", label: "Outcomes" },
  { id: "automation", label: "Automation" },
  { id: "latency", label: "Latency" },
  { id: "cost", label: "Cost" },
] as const;

type TabId = (typeof TABS)[number]["id"];

/** The categories as tabs (WAI-ARIA tabs pattern): one tab stop, arrows, Home and End move
 * between them and select the tab they land on. */
function Tabs({ selected, onSelect }: { selected: TabId; onSelect: (tab: TabId) => void }) {
  const refs = useRef<Partial<Record<TabId, HTMLButtonElement | null>>>({});
  const move = (event: KeyboardEvent, index: number) => {
    const last = TABS.length - 1;
    const keys: Record<string, number> = {
      ArrowRight: index === last ? 0 : index + 1,
      ArrowLeft: index === 0 ? last : index - 1,
      Home: 0,
      End: last,
    };
    const next = TABS[keys[event.key] ?? -1];
    if (!next) return;
    event.preventDefault();
    onSelect(next.id);
    refs.current[next.id]?.focus();
  };
  return (
    <div className="tabs reveal" role="tablist" aria-label="Metric categories">
      {TABS.map((tab, index) => (
        <button
          key={tab.id}
          ref={(node) => {
            refs.current[tab.id] = node;
          }}
          type="button"
          role="tab"
          id={`metrics-tab-${tab.id}`}
          aria-selected={tab.id === selected}
          aria-controls={`metrics-panel-${tab.id}`}
          tabIndex={tab.id === selected ? 0 : -1}
          className="tabs__tab"
          onClick={() => onSelect(tab.id)}
          onKeyDown={(event) => move(event, index)}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

function Dashboard({ m, tab, onTab }: { m: AuditMetrics; tab: TabId; onTab: (tab: TabId) => void }) {
  // Mounted again on every load, so this is when these figures arrived.
  const [loadedAt] = useState(() => new Date().toISOString());
  const animate = !still();
  return (
    <>
      <p className="muted metrics__stamp" role="status">
        Loaded {dateTime(loadedAt)}.
      </p>
      <Headline m={m} />
      <Tabs selected={tab} onSelect={onTab} />
      {/* Only the selected panel is mounted, so only its charts are drawn. */}
      <div
        className="metrics__grid metrics__panel"
        role="tabpanel"
        id={`metrics-panel-${tab}`}
        aria-labelledby={`metrics-tab-${tab}`}
        tabIndex={0}
      >
        {tab === "outcomes" && (
          <>
            <Outcomes m={m} animate={animate} />
            <Languages m={m} animate={animate} />
          </>
        )}
        {tab === "automation" && <Automation m={m} animate={animate} />}
        {tab === "latency" && <Latency m={m} animate={animate} />}
        {tab === "cost" && <Cost m={m} animate={animate} />}
      </div>
    </>
  );
}

export function MetricsView({ api, token }: { api: AgentApi; token: string }) {
  const metrics = useResource("metrics", () => api.metrics(token));
  // Kept here, not in the dashboard, so a Refresh stays on the same tab.
  const [tab, setTab] = useState<TabId>("outcomes");
  return (
    <>
      <div className="page-head page-head--row reveal">
        <div>
          <h1 className="title title--big gradient">Metrics</h1>
          <p className="muted">Every stored turn trace, by conversation. They update when you refresh.</p>
        </div>
        <button type="button" className="button button--ghost" onClick={metrics.reload} disabled={metrics.loading}>
          Refresh
        </button>
      </div>
      <Loading resource={metrics} missing="The metrics were not found.">
        {(m) => <Dashboard m={m} tab={tab} onTab={setTab} />}
      </Loading>
    </>
  );
}
