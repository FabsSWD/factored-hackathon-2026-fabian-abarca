// What the metrics dashboard draws, derived from the backend's AuditMetrics without recomputing
// any metric: the charts only reorder, label and divide what the endpoint sends, so every figure
// can be checked against the JSON (and against the evaluation report, when the traces are the
// evaluation run's). Definitions: app/audit/metrics.py.
import type { AuditMetrics } from "./client";

export const OUTCOME_ORDER = ["RESOLVE", "INFORM", "CLARIFY", "ESCALATE", "REFUSE"];

/** One color per outcome, whatever the chart: the dark steps of the categorical palette in a
 * fixed order, validated on the console's card surface (#2a2a2a) for color-vision deficiency and
 * contrast. An outcome the backend does not know yet ("unknown") is gray. */
export const OUTCOME_COLORS: Record<string, string> = {
  RESOLVE: "#3987e5",
  INFORM: "#d95926",
  CLARIFY: "#199e70",
  ESCALATE: "#c98500",
  REFUSE: "#d55181",
};
export const OTHER_COLOR = "#6f7680";

/** The two series of the latency chart (p50, p95): the first two slots of the same palette. */
export const P50_COLOR = "#3987e5";
export const P95_COLOR = "#d95926";

export const outcomeColor = (outcome: string) => OUTCOME_COLORS[outcome] ?? OTHER_COLOR;

export const AUTOMATED_TIERS = ["T1", "T2"] as const;

const LANGUAGES: Record<string, string> = { es: "Spanish", pt: "Portuguese", en: "English", unknown: "Unknown" };

export const languageName = (code: string) => LANGUAGES[code] ?? code;

/** "extraction_retry" → "Extraction retry". */
export function stageName(stage: string): string {
  const words = stage.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** The known outcomes first, in the policy's order, then any other in alphabetical order. */
export function orderOutcomes(outcomes: Iterable<string>): string[] {
  const all = [...new Set(outcomes)];
  const known = OUTCOME_ORDER.filter((outcome) => all.includes(outcome));
  const others = all.filter((outcome) => !OUTCOME_ORDER.includes(outcome)).sort();
  return [...known, ...others];
}

export interface OutcomeRow {
  outcome: string;
  count: number;
  share: number;
}

/** Conversations by final outcome; none when no conversation has one. */
export function outcomeRows(outcomes: Record<string, number>): OutcomeRow[] {
  const total = Object.values(outcomes).reduce((sum, n) => sum + n, 0);
  if (total === 0) return [];
  return orderOutcomes(Object.keys(outcomes))
    .map((outcome) => ({ outcome, count: outcomes[outcome] ?? 0 }))
    .filter((row) => row.count > 0)
    .map((row) => ({ ...row, share: row.count / total }));
}

export interface TierRow {
  tier: string;
  conversations: number;
  resolved: number;
  /** Resolved / conversations; null for a tier without conversations. */
  rate: number | null;
}

/** The automated tiers (policy §6): the share of their conversations whose final outcome is
 * RESOLVE (a dispute case created and verified). Empty when neither tier has a conversation. */
export function tierRows(byTier: AuditMetrics["outcomes_by_tier"] = {}): TierRow[] {
  const rows = AUTOMATED_TIERS.map((tier) => {
    const outcomes = byTier[tier] ?? {};
    const conversations = Object.values(outcomes).reduce((sum, n) => sum + n, 0);
    const resolved = outcomes.RESOLVE ?? 0;
    return { tier, conversations, resolved, rate: conversations ? resolved / conversations : null };
  });
  return rows.some((row) => row.conversations > 0) ? rows : [];
}

export interface LatencyRow {
  name: string;
  count: number;
  p50: number | null;
  p95: number | null;
}

/** The whole turn first, then each stage; stages that never ran are left out. */
export function latencyRows(metrics: AuditMetrics): LatencyRow[] {
  const rows: LatencyRow[] = [];
  const add = (name: string, { count, p50_ms, p95_ms }: AuditMetrics["turn_latency"]) => {
    if (count > 0 && (p50_ms !== null || p95_ms !== null)) rows.push({ name, count, p50: p50_ms, p95: p95_ms });
  };
  add("Whole turn", metrics.turn_latency);
  for (const [stage, summary] of Object.entries(metrics.stage_latency)) add(stageName(stage), summary);
  return rows;
}

export interface CostRow {
  name: string;
  usd: number;
  /** What the total cost is divided by. */
  basis: string;
}

/** Cost per attempted case and per automated resolution; a figure the backend could not compute
 * (no cost, or nothing to divide by) is left out. */
export function costRows(metrics: AuditMetrics): CostRow[] {
  const rows: CostRow[] = [];
  const add = (name: string, value: string | null, basis: string) => {
    if (value !== null && Number.isFinite(Number(value))) rows.push({ name, usd: Number(value), basis });
  };
  add("Per attempted case", metrics.cost_per_attempted_case_usd, `${metrics.attempted_cases} attempted cases`);
  add(
    "Per automated resolution",
    metrics.cost_per_automated_resolution_usd,
    `${metrics.automated_resolutions} automated resolutions`,
  );
  return rows;
}

export type LanguageRow = { language: string; total: number } & Record<string, number | string>;

/** Conversations by language, split by final outcome; also the outcomes that appear, in order. */
export function languageRows(byLanguage: AuditMetrics["outcomes_by_language"] = {}): {
  rows: LanguageRow[];
  outcomes: string[];
} {
  const rows: LanguageRow[] = [];
  for (const [code, outcomes] of Object.entries(byLanguage)) {
    const total = Object.values(outcomes).reduce((sum, n) => sum + n, 0);
    if (total > 0) rows.push({ ...outcomes, language: languageName(code), total });
  }
  rows.sort((a, b) => b.total - a.total || a.language.localeCompare(b.language));
  const outcomes = orderOutcomes(Object.values(byLanguage).flatMap((outcomes) => Object.keys(outcomes)));
  return { rows, outcomes: outcomes.filter((outcome) => rows.some((row) => Number(row[outcome] ?? 0) > 0)) };
}
