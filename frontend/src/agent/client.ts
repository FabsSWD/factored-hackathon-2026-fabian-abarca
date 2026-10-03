// The agent console's way to the backend: the handoff queue and the trace list (/api/agent, both
// paged), the operating metrics (/api/agent/metrics), and each full trace (/api/audit/{trace_id}).
// The agent token travels in the Authorization header only; the console keeps it in memory and
// never in a URL or in browser storage.
import { defaultFetch, request, type Fetch } from "../api/client";
import type { components } from "../api/schema";

type Schemas = components["schemas"];
export type HandoffSummary = Schemas["HandoffSummary"];
export type HandoffPage = Schemas["HandoffPage"];
export type TraceSummary = Schemas["TraceSummary"];
export type TracePage = Schemas["TracePage"];
export type HandoffPacket = Schemas["HandoffPacket"];
export type TraceRecord = Schemas["TraceRecord"];
export type Queue = Schemas["Queue"];
export type Priority = Schemas["Priority"];
export type Outcome = Schemas["Outcome"];
export type Evidence = Schemas["Evidence"];
export type AuditMetrics = Schemas["AuditMetrics"];
export type LatencySummary = Schemas["LatencySummary"];

/** Which page of a list: `offset` items skipped, at most `limit` returned. */
export interface Paging {
  offset?: number;
  limit?: number;
}

export interface HandoffFilters extends Paging {
  queue?: Queue | null;
  priority?: Priority | null;
}

export interface TraceFilters extends Paging {
  /** Part of a trace, conversation, session or handoff ID. */
  search?: string | null;
  outcome?: Outcome | null;
}

export interface AgentApi {
  /** Resolves if the token proves the agent role; ApiError(403) otherwise. */
  session(token: string): Promise<void>;
  handoffs(filters: HandoffFilters, token: string): Promise<HandoffPage>;
  handoff(handoffId: string, token: string): Promise<HandoffPacket>;
  traces(filters: TraceFilters, token: string): Promise<TracePage>;
  trace(traceId: string, token: string): Promise<TraceRecord>;
  /** The metrics over every stored turn trace (definitions in app/audit/metrics.py). */
  metrics(token: string): Promise<AuditMetrics>;
}

function query(values: object): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== null && value !== undefined && value !== "") params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function httpAgentApi(fetchImpl: Fetch = defaultFetch, base = ""): AgentApi {
  const get = async <T>(path: string, token: string): Promise<T> =>
    (await request(fetchImpl, `${base}${path}`, { token })).json() as Promise<T>;
  return {
    async session(token) {
      await request(fetchImpl, `${base}/api/agent/session`, { token });
    },
    handoffs: (filters, token) => get(`/api/agent/handoffs${query(filters)}`, token),
    handoff: (id, token) => get(`/api/agent/handoffs/${encodeURIComponent(id)}`, token),
    traces: (filters, token) => get(`/api/agent/traces${query(filters)}`, token),
    trace: (id, token) => get(`/api/audit/${encodeURIComponent(id)}`, token),
    metrics: (token) => get("/api/agent/metrics", token),
  };
}
