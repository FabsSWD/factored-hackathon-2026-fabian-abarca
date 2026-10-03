// A backend double for the agent console's tests. The records are invented. Each one also
// carries fields the API never sends (a customer ID, a document number, protected attributes):
// the console must render only the contract's fields, so none of them may reach the screen.
import { vi } from "vitest";

import { ApiError } from "../api/client";
import type { AgentApi, AuditMetrics, HandoffPacket, HandoffSummary, Paging, TraceRecord, TraceSummary } from "../agent/client";

export const AGENT_TOKEN = "agent-secret-token-xyz";

/** Values that must never be shown, and the fields that carry them in the fixtures. */
export const PROHIBITED = {
  customer_id: "CUST-SECRET-0001",
  document_number: "X7654321",
  date_of_birth: "1981-04-02",
  gender: "female-secret",
  credit_score: "812-secret",
  segment: "premium-secret",
  estimated_monthly_income: "9999999-secret",
  full_name: "Jane Secret Doe",
  email: "jane.secret@example.com",
};

const extra = <T extends object>(record: T): T => ({ ...record, ...PROHIBITED });

export const summaries: HandoffSummary[] = [
  extra({
    handoff_id: "HO-20261003-000002",
    created_at: "2026-10-03T09:15:00Z",
    queue: "security_review",
    priority: "high",
    status: "acknowledged",
    language: "es",
    request_summary: "Customer reports a stolen phone and charges they did not make.",
    triggered_rules: ["ESC-04"],
  }),
  extra({
    handoff_id: "HO-20261003-000001",
    created_at: "2026-10-03T08:00:00Z",
    queue: "disputes",
    priority: "normal",
    status: "acknowledged",
    language: "pt",
    request_summary: "Customer disputes a duplicate charge above the automatic limit.",
    triggered_rules: ["ESC-02"],
  }),
];

export const packet: HandoffPacket = extra({
  handoff_id: "HO-20261003-000002",
  created_at: "2026-10-03T09:15:00Z",
  business_date: "2026-06-17",
  language: "es",
  queue: "security_review",
  priority: "high",
  customer_ref: "CUS-0a1b2c3d4e5f6a7b",
  auth: { status: "authenticated", method: "otp", session_age_min: 4.2 },
  request_summary: "Customer reports a stolen phone and charges they did not make.",
  reason_code: "RC_UNRECOGNIZED",
  triggered_rules: ["ESC-04"],
  escalation_reasons: [
    {
      rule_id: "ESC-04",
      description: "Possible account takeover reported by the customer.",
      evidence: [
        extra({
          kind: "flag",
          name: "account_takeover_reported",
          value: "yes",
          origin: "customer statement (LLM extraction)",
          claims: ["me robaron el celular ayer"],
        }),
      ],
    },
  ],
  verified_facts: [
    extra({ fact: "Card ****4321 is active", source: "products", record_id: "PRD-FAKE0001" }),
    extra({ fact: "Charge of USD 120.00 at Fake Store on 2026-06-15", source: "transactions", record_id: "TRX-FAKE0001" }),
  ],
  customer_claims: ["me robaron el celular ayer", "no hice esa compra"],
  collected_slots: [{ name: "reason_code", value: "no reconozco la compra", turn_index: 1, verified: false }],
  actions_taken: [
    extra({ action: "ACT-03", result: "success", verified: true, detail: "card blocked", at: "2026-10-03T09:14:00Z" }),
    { action: "ACT-05", result: "success", verified: true },
  ],
  draft_case: { transaction_ref: "TRX-FAKE0001", amount_usd: "120.00", tier: "T3", provisional_credit_flag: null },
  model_signals: extra({
    source: "kev" as const,
    reason_code_probs: { RC_UNRECOGNIZED: 0.82, RC_DUPLICATE: 0.05 },
    reason_code_other: 0.03,
    escalation_risk: 0.57,
    model_version: "kev-fake-1",
    model_info: {},
    calibrated: false,
  }),
  open_questions: ["Confirm when the phone was stolen."],
  transcript_ref: "CONV-FAKE-01",
  policy_version: "0.4.12",
  post_handoff_messages: [{ received_at: "2026-10-03T09:20:00Z", text: "¿ya me atienden?" }],
});

export const trace: TraceRecord = extra({
  trace_id: "TRC-FAKE-0001",
  conversation_id: "CONV-FAKE-01",
  session_id: "SES-FAKE-01",
  turn_index: 2,
  created_at: "2026-10-03T09:14:30Z",
  language: "es",
  message: "me robaron el celular, mi documento es [number]",
  input_guard: { flagged: false, strikes: 0, pattern_id: null, escalate_security: false },
  model_calls: [
    extra({
      provider: "openai",
      model: "gpt-fake",
      response_model: "gpt-fake-2026",
      prompt_version: "extract@1.13.0",
      purpose: "extract_slots",
      input_tokens: 1200,
      cached_input_tokens: 800,
      output_tokens: 90,
      latency_ms: 1840,
      success: true,
      adjustments: ["transaction_id_discarded"],
      model_info: {},
    }),
    {
      provider: "kev",
      model: "kev-fake-1",
      purpose: "decision_signals",
      latency_ms: 310,
      server_latency_ms: 120,
      success: false,
      error: "timeout",
      model_info: { run: "run-7" },
    },
  ],
  signals: {
    source: "kev",
    reason_code_probs: { RC_UNRECOGNIZED: 0.82 },
    ambiguity: 0.4,
    escalation_risk: 0.57,
    manipulation: 0.02,
    model_info: {},
  },
  decisions: [
    extra({
      outcome: "ESCALATE",
      policy_version: "0.4.12",
      gates_evaluated: [
        { gate_id: "GATE-02", passed: true },
        { gate_id: "GATE-05", passed: false },
      ],
      triggered_rules: ["ESC-04"],
      authorized_actions: ["ACT-03", "ACT-05"],
      queue: "security_review",
      priority: "high",
      transaction_id: "TRX-FAKE0001",
      reason_code: "RC_UNRECOGNIZED",
      tier: "T3",
      amount_usd: "120.00",
      notes: ["fraud_score_missing"],
      card_already_blocked: false,
      duplicate_reason_reask: false,
      evidence: [
        {
          rule_id: "ESC-04",
          evidence: [{ kind: "flag", name: "account_takeover_reported", value: "yes", origin: "customer statement" }],
        },
      ],
    }),
  ],
  tool_calls: [
    extra({ action: "ACT-03", status: "success", verified: true, attempts: 1, record_id: "BLK-FAKE0001" }),
    { action: "ACT-05", status: "failed", verified: false, attempts: 2, error: "queue write failed" },
  ],
  outcome: "ESCALATE",
  reply_kind: "handoff",
  handoff_id: "HO-20261003-000002",
  stage_latencies_ms: { input_guard: 2, extraction: 1840, decision_signals: 310, policy: 4 },
  total_latency_ms: 2210,
  estimated_cost_usd: "0.000412",
  policy_version: "0.4.12",
});

/** The trace as the list shows it (the backend's TraceSummary). */
export function summary(record: TraceRecord, overrides: Partial<TraceSummary> = {}): TraceSummary {
  return extra({
    trace_id: record.trace_id,
    conversation_id: record.conversation_id,
    session_id: record.session_id ?? null,
    turn_index: record.turn_index,
    created_at: record.created_at,
    language: record.language ?? null,
    outcome: record.outcome ?? null,
    reply_kind: record.reply_kind ?? null,
    handoff_id: record.handoff_id ?? null,
    total_latency_ms: record.total_latency_ms ?? null,
    estimated_cost_usd: record.estimated_cost_usd ?? null,
    ...overrides,
  });
}

/** `count` more turns of other conversations, newest first, after the fixture's trace. */
export function moreTraces(count: number): TraceSummary[] {
  return Array.from({ length: count }, (_, index) =>
    summary(trace, {
      trace_id: `TRC-MORE-${String(index + 1).padStart(4, "0")}`,
      conversation_id: `CONV-MORE-${Math.floor(index / 3)}`,
      session_id: null,
      turn_index: index % 3,
      outcome: "CLARIFY",
      handoff_id: null,
    }),
  );
}

/** The metrics endpoint's answer over a few invented conversations (numbers chosen so every
 * derived figure is easy to check by hand). */
export const metrics: AuditMetrics = {
  turns: 412,
  conversations: 80,
  outcomes: { ESCALATE: 22, RESOLVE: 30, INFORM: 16, CLARIFY: 8, REFUSE: 4 },
  attempted_cases: 60,
  ended_before_transaction: { authentication: 12, language: 3, no_decision: 5 },
  abandoned: 8,
  automated_resolutions: 29,
  contained: 46,
  containment_rate: 0.575,
  escalated: 22,
  escalation_rate: 0.275,
  escalations_by_queue: { disputes: 12, fraud: 6, security_review: 4 },
  escalations_by_rule: { "ESC-01": 7, "ESC-04": 5 },
  turn_latency: { count: 412, p50_ms: 5812.4, p95_ms: 8231 },
  stage_latency: {
    extraction: { count: 380, p50_ms: 1840, p95_ms: 3120.5 },
    input_guard: { count: 412, p50_ms: 2, p95_ms: 6 },
    never_ran: { count: 0, p50_ms: null, p95_ms: null },
  },
  total_cost_usd: "0.081200",
  cost_per_attempted_case_usd: "0.001353",
  cost_per_automated_resolution_usd: "0.002800",
  turns_without_cost: 3,
  outcomes_by_language: {
    es: { RESOLVE: 16, ESCALATE: 12, INFORM: 10, CLARIFY: 4 },
    pt: { RESOLVE: 14, ESCALATE: 9, INFORM: 6, CLARIFY: 4, REFUSE: 4 },
    unknown: { ESCALATE: 1 },
  },
  outcomes_by_tier: {
    T1: { RESOLVE: 18, ESCALATE: 2 },
    T2: { RESOLVE: 12, ESCALATE: 4, INFORM: 4 },
    T3: { ESCALATE: 7 },
    unknown: { CLARIFY: 8 },
  },
};

/** The metrics endpoint's answer before any trace is stored. */
export const emptyMetrics: AuditMetrics = {
  turns: 0,
  conversations: 0,
  outcomes: {},
  attempted_cases: 0,
  ended_before_transaction: {},
  abandoned: 0,
  automated_resolutions: 0,
  contained: 0,
  containment_rate: null,
  escalated: 0,
  escalation_rate: null,
  escalations_by_queue: {},
  escalations_by_rule: {},
  turn_latency: { count: 0, p50_ms: null, p95_ms: null },
  stage_latency: {},
  total_cost_usd: null,
  cost_per_attempted_case_usd: null,
  cost_per_automated_resolution_usd: null,
  turns_without_cost: 0,
  outcomes_by_language: {},
  outcomes_by_tier: {},
};

function paged<T>(rows: T[], paging: Paging) {
  const offset = paging.offset ?? 0;
  const limit = paging.limit ?? 20;
  return { items: rows.slice(offset, offset + limit), total: rows.length, offset, limit };
}

export function fakeAgentApi(
  options: { handoffs?: HandoffSummary[]; traces?: TraceSummary[]; metrics?: AuditMetrics } = {},
) {
  const handoffRows = options.handoffs ?? summaries;
  const traceRows = options.traces ?? [summary(trace)];
  const api = {
    accepts: AGENT_TOKEN,
    session: vi.fn(async (token: string) => {
      if (token !== api.accepts) throw new ApiError(403);
    }),
    handoffs: vi.fn<AgentApi["handoffs"]>(async (filters) =>
      paged(
        handoffRows.filter(
          (row) =>
            (!filters.queue || row.queue === filters.queue) &&
            (!filters.priority || row.priority === filters.priority),
        ),
        filters,
      ),
    ),
    handoff: vi.fn<AgentApi["handoff"]>(async (id) => {
      if (id !== packet.handoff_id) throw new ApiError(404);
      return packet;
    }),
    traces: vi.fn<AgentApi["traces"]>(async (filters) => {
      const text = (filters.search ?? "").trim().toLowerCase();
      const matches = traceRows.filter(
        (row) =>
          (!filters.outcome || row.outcome === filters.outcome) &&
          [row.trace_id, row.conversation_id, row.session_id, row.handoff_id].some((id) =>
            (id ?? "").toLowerCase().includes(text),
          ),
      );
      return paged(matches, filters);
    }),
    trace: vi.fn<AgentApi["trace"]>(async (id) => {
      if (id !== trace.trace_id) throw new ApiError(404);
      return trace;
    }),
    metrics: vi.fn<AgentApi["metrics"]>(async () => options.metrics ?? metrics),
  };
  return api;
}
