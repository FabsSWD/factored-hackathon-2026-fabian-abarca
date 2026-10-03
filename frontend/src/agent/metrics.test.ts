// M16: what each chart draws comes from the endpoint's JSON unchanged, only ordered, labeled and
// divided; empty JSON gives empty charts.
import { describe, expect, it } from "vitest";

import { emptyMetrics, metrics } from "../test/fakeAgentApi";
import {
  costRows,
  languageRows,
  latencyRows,
  orderOutcomes,
  outcomeColor,
  OTHER_COLOR,
  outcomeRows,
  stageName,
  tierRows,
} from "./metrics";

describe("outcomes", () => {
  it("counts conversations by final outcome, in the policy's order, with their share", () => {
    expect(outcomeRows(metrics.outcomes)).toEqual([
      { outcome: "RESOLVE", count: 30, share: 0.375 },
      { outcome: "INFORM", count: 16, share: 0.2 },
      { outcome: "CLARIFY", count: 8, share: 0.1 },
      { outcome: "ESCALATE", count: 22, share: 0.275 },
      { outcome: "REFUSE", count: 4, share: 0.05 },
    ]);
  });

  it("puts an outcome it does not know last, in gray, and leaves out zeros", () => {
    const rows = outcomeRows({ unknown: 2, RESOLVE: 2, INFORM: 0 });
    expect(rows.map((row) => row.outcome)).toEqual(["RESOLVE", "unknown"]);
    expect(outcomeColor("unknown")).toBe(OTHER_COLOR);
    expect(orderOutcomes(["b", "ESCALATE", "a", "RESOLVE"])).toEqual(["RESOLVE", "ESCALATE", "a", "b"]);
  });

  it("is empty without conversations", () => {
    expect(outcomeRows({})).toEqual([]);
    expect(outcomeRows({ RESOLVE: 0 })).toEqual([]);
  });
});

describe("automation by tier", () => {
  it("is the share of T1 and T2 conversations that ended in RESOLVE", () => {
    expect(tierRows(metrics.outcomes_by_tier)).toEqual([
      { tier: "T1", conversations: 20, resolved: 18, rate: 0.9 },
      { tier: "T2", conversations: 20, resolved: 12, rate: 0.6 },
    ]);
  });

  it("keeps a tier without conversations as having no rate", () => {
    expect(tierRows({ T2: { ESCALATE: 3 } })).toEqual([
      { tier: "T1", conversations: 0, resolved: 0, rate: null },
      { tier: "T2", conversations: 3, resolved: 0, rate: 0 },
    ]);
  });

  it("is empty when neither tier has a conversation", () => {
    expect(tierRows({ T3: { ESCALATE: 1 } })).toEqual([]);
    expect(tierRows(undefined)).toEqual([]);
  });
});

describe("latency", () => {
  it("is the whole turn, then each stage that ran, with the endpoint's p50 and p95", () => {
    expect(latencyRows(metrics)).toEqual([
      { name: "Whole turn", count: 412, p50: 5812.4, p95: 8231 },
      { name: "Extraction", count: 380, p50: 1840, p95: 3120.5 },
      { name: "Input guard", count: 412, p50: 2, p95: 6 },
    ]);
    expect(stageName("decision_signals")).toBe("Decision signals");
  });

  it("is empty without measured turns", () => {
    expect(latencyRows(emptyMetrics)).toEqual([]);
  });
});

describe("cost", () => {
  it("is the endpoint's cost per attempted case and per automated resolution", () => {
    expect(costRows(metrics)).toEqual([
      { name: "Per attempted case", usd: 0.001353, basis: "60 attempted cases" },
      { name: "Per automated resolution", usd: 0.0028, basis: "29 automated resolutions" },
    ]);
  });

  it("leaves out a figure the backend could not compute", () => {
    expect(costRows({ ...metrics, cost_per_automated_resolution_usd: null })).toHaveLength(1);
    expect(costRows(emptyMetrics)).toEqual([]);
  });
});

describe("languages", () => {
  it("splits each language's conversations by outcome, the largest first", () => {
    const { rows, outcomes } = languageRows(metrics.outcomes_by_language);
    expect(outcomes).toEqual(["RESOLVE", "INFORM", "CLARIFY", "ESCALATE", "REFUSE"]);
    expect(rows).toEqual([
      { language: "Spanish", total: 42, RESOLVE: 16, ESCALATE: 12, INFORM: 10, CLARIFY: 4 },
      { language: "Portuguese", total: 37, RESOLVE: 14, ESCALATE: 9, INFORM: 6, CLARIFY: 4, REFUSE: 4 },
      { language: "Unknown", total: 1, ESCALATE: 1 },
    ]);
  });

  it("is empty without conversations", () => {
    expect(languageRows({})).toEqual({ rows: [], outcomes: [] });
    expect(languageRows(undefined)).toEqual({ rows: [], outcomes: [] });
    expect(languageRows({ es: { RESOLVE: 0 } })).toEqual({ rows: [], outcomes: [] });
  });
});
