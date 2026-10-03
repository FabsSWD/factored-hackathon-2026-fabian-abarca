// One turn's trace (architecture §9): what happened, in the order it happened. Every section is
// always shown, saying "none" when the stage did not run, so a missing stage is visible. Only the
// trace's known fields are rendered; the message is shown as the audit record kept it (masked by
// default, AUDIT_MESSAGE_MODE).
import type { AgentApi, TraceRecord } from "./client";
import { action, count, dateTime, GATES, ms, percent, usd } from "./format";
import { EvidenceList } from "./HandoffView";
import { Link } from "../router";
import { Chip, Empty, Fields, Loading, Section, useResource, Verified } from "./ui";

function Gates({ trace }: { trace: TraceRecord }) {
  const decisions = trace.decisions ?? [];
  if (decisions.every((d) => (d.gates_evaluated ?? []).length === 0)) {
    return <Empty>No gates were evaluated in this turn.</Empty>;
  }
  return (
    <>
      {decisions.map((decision, index) => (
        <div key={index} className="decision">
          {decisions.length > 1 && (
            <h3 className="subtitle">{`Decision ${index + 1}${decision.transaction_id ? ` · ${decision.transaction_id}` : ""}`}</h3>
          )}
          <ul className="gates">
            {(decision.gates_evaluated ?? []).map((gate) => {
              const about = GATES[gate.gate_id];
              const tip = `gate-tip-${index}-${gate.gate_id}`;
              return (
                <li key={gate.gate_id} className={`gate gate--${gate.passed ? "passed" : "failed"}`}>
                  <button type="button" className="gate__trigger" aria-describedby={about ? tip : undefined}>
                    <span className="gate__id">{gate.gate_id}</span>{" "}
                    <span>{gate.passed ? "passed" : "not passed"}</span>
                  </button>
                  {about && (
                    <span id={tip} role="tooltip" className="gate__tip">
                      <strong>{about.name}</strong>
                      <span>{about.checks}</span>
                      <span className="gate__tip-otherwise">{`If not: ${about.otherwise}`}</span>
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </>
  );
}

function Rules({ trace }: { trace: TraceRecord }) {
  const decisions = trace.decisions ?? [];
  if (decisions.length === 0) return <Empty>The Policy Engine was not called in this turn.</Empty>;
  return (
    <>
      {decisions.map((decision, index) => (
        <div key={index} className="decision">
          <p className="chips">
            <Chip tone={decision.outcome === "ESCALATE" ? "copper" : "plain"}>{decision.outcome}</Chip>
            {(decision.triggered_rules ?? []).length === 0 ? (
              <span className="muted">No rule triggered.</span>
            ) : (
              (decision.triggered_rules ?? []).map((rule) => <Chip key={rule}>{rule}</Chip>)
            )}
          </p>
          <Fields
            items={[
              ["Transaction", decision.transaction_id ?? "—"],
              ["Reason code", decision.reason_code ?? "—"],
              ["Tier", decision.tier ?? "—"],
              ["Amount (USD)", decision.amount_usd ?? "—"],
              ["Queue · priority", decision.queue ? `${decision.queue} · ${decision.priority}` : "—"],
              ["Clarify / inform", decision.clarify_target ?? decision.inform_reason ?? "—"],
              [
                "Authorized actions",
                (decision.authorized_actions ?? []).length ? (decision.authorized_actions ?? []).map(action).join(", ") : "none",
              ],
              ["Notes", (decision.notes ?? []).join(", ") || "—"],
              ["Policy version", decision.policy_version],
            ]}
          />
          {(decision.evidence ?? []).map((rule) => (
            <article key={rule.rule_id} className="reason">
              <h3 className="reason__title">
                <Chip tone="copper">{rule.rule_id}</Chip> evidence
              </h3>
              <EvidenceList evidence={rule.evidence} />
            </article>
          ))}
        </div>
      ))}
    </>
  );
}

function ModelCalls({ trace }: { trace: TraceRecord }) {
  const calls = trace.model_calls ?? [];
  if (calls.length === 0) return <Empty>No model was called in this turn.</Empty>;
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Purpose</th>
            <th scope="col">Model</th>
            <th scope="col">Prompt</th>
            <th scope="col">Tokens in · cached · out</th>
            <th scope="col">Latency (server)</th>
            <th scope="col">Result</th>
          </tr>
        </thead>
        <tbody>
          {calls.map((call, index) => (
            <tr key={`${call.purpose}-${index}`}>
              <td>{call.purpose}</td>
              <td>
                {`${call.provider} · ${call.response_model ?? call.model}`}
                {Object.keys(call.model_info ?? {}).length > 0 && (
                  <span className="muted cell-note">
                    {Object.entries(call.model_info ?? {})
                      .map(([key, value]) => `${key}: ${value}`)
                      .join(", ")}
                  </span>
                )}
              </td>
              <td>{call.prompt_version ?? "—"}</td>
              <td>{`${count(call.input_tokens)} · ${count(call.cached_input_tokens)} · ${count(call.output_tokens)}`}</td>
              <td>{`${ms(call.latency_ms)}${call.server_latency_ms != null ? ` (${ms(call.server_latency_ms)})` : ""}`}</td>
              <td>
                <Chip tone={call.success ? "success" : "danger"}>{call.success ? "ok" : "failed"}</Chip>
                {call.error && <span className="error cell-note">{call.error}</span>}
                {(call.adjustments ?? []).length > 0 && (
                  <span className="muted cell-note">{(call.adjustments ?? []).join(", ")}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ToolCalls({ trace }: { trace: TraceRecord }) {
  const calls = trace.tool_calls ?? [];
  if (calls.length === 0) return <Empty>No tool action ran in this turn.</Empty>;
  return (
    <ul className="rows">
      {calls.map((call, index) => (
        <li key={`${call.action}-${index}`} className="rows__item">
          <strong>{action(call.action)}</strong>
          <Chip tone={call.status === "success" ? "success" : "danger"}>{call.status}</Chip>
          <Verified value={call.verified} />
          <span className="muted">{`attempts ${call.attempts}`}</span>
          {call.record_id && <span className="mono-id">{call.record_id}</span>}
          {call.detail && <span className="muted">{call.detail}</span>}
          {call.error && <span className="error">{call.error}</span>}
          {call.completed_at && <span className="muted">{dateTime(call.completed_at)}</span>}
        </li>
      ))}
    </ul>
  );
}

function Latency({ trace }: { trace: TraceRecord }) {
  const stages = Object.entries(trace.stage_latencies_ms ?? {});
  const total = trace.total_latency_ms ?? stages.reduce((sum, [, value]) => sum + value, 0);
  return (
    <>
      <p className="lead">{`Total ${ms(trace.total_latency_ms)}`}</p>
      {stages.length === 0 ? (
        <Empty>No stage latencies were recorded.</Empty>
      ) : (
        <ul className="bars">
          {stages.map(([stage, value]) => (
            <li key={stage} className="bars__item">
              <span className="bars__label">{stage}</span>
              <span className="bars__track">
                <span
                  className="bars__fill"
                  style={{ width: `${total > 0 ? Math.min(100, (value / total) * 100) : 0}%` }}
                />
              </span>
              <span className="bars__value">{ms(value)}</span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

function Tokens({ trace }: { trace: TraceRecord }) {
  const calls = trace.model_calls ?? [];
  const sum = (pick: (call: (typeof calls)[number]) => number | null | undefined) =>
    calls.some((call) => pick(call) != null) ? calls.reduce((total, call) => total + (pick(call) ?? 0), 0) : null;
  return (
    <Fields
      items={[
        ["Input tokens", count(sum((call) => call.input_tokens))],
        ["Cached input tokens", count(sum((call) => call.cached_input_tokens))],
        ["Output tokens", count(sum((call) => call.output_tokens))],
        ["Estimated cost", usd(trace.estimated_cost_usd, 6)],
      ]}
    />
  );
}

function Trace({ trace }: { trace: TraceRecord }) {
  const guard = trace.input_guard;
  const signals = trace.signals;
  return (
    <>
      <div className="page-head reveal">
        <p className="crumbs">
          <Link to="/agent/traces">Traces</Link>
          <span aria-hidden="true"> / </span>
          <span>{trace.trace_id}</span>
        </p>
        <h1 className="title title--big gradient">{trace.trace_id}</h1>
        <p className="chips">
          {trace.outcome && <Chip tone={trace.outcome === "ESCALATE" ? "copper" : "plain"}>{trace.outcome}</Chip>}
          {trace.reply_kind && <Chip>{trace.reply_kind}</Chip>}
          {trace.error && <Chip tone="danger">{trace.error}</Chip>}
        </p>
      </div>

      <Section id="overview" title="Overview">
        <Fields
          items={[
            ["Time", dateTime(trace.created_at)],
            [
              "Conversation · turn",
              <Link key="conversation" to={`/agent/traces?search=${encodeURIComponent(trace.conversation_id)}`}>
                {`${trace.conversation_id} · ${trace.turn_index}`}
              </Link>,
            ],
            ["Session", trace.session_id ?? "not authenticated"],
            ["Language", trace.language ? trace.language.toUpperCase() : "—"],
            ["Side question", trace.side_question ?? "—"],
            [
              "Handoff",
              trace.handoff_id ? (
                <Link key="handoff" to={`/agent/handoffs/${encodeURIComponent(trace.handoff_id)}`}>
                  {trace.handoff_id}
                </Link>
              ) : (
                "—"
              ),
            ],
            ["Policy version", trace.policy_version],
          ]}
        />
        <h3 className="subtitle">Customer message (as kept in the audit record)</h3>
        {trace.message ? <q className="claim">{trace.message}</q> : <Empty>Not kept.</Empty>}
      </Section>

      <Section id="guard" title="Input guard">
        {guard ? (
          <Fields
            items={[
              ["Flagged", guard.flagged ? "yes" : "no"],
              ["Pattern", guard.pattern_id ?? "—"],
              ["Strikes", String(guard.strikes)],
              ["Escalates to security", guard.escalate_security ? "yes" : "no"],
            ]}
          />
        ) : (
          <Empty>The input guard did not run.</Empty>
        )}
      </Section>

      <Section id="gates" title="Gates">
        <Gates trace={trace} />
      </Section>

      <Section id="rules" title="Rules and decision">
        <Rules trace={trace} />
      </Section>

      <Section id="models" title="Model calls">
        <ModelCalls trace={trace} />
      </Section>

      <Section id="decision-signals" title="Decision signals (informative)">
        {signals ? (
          <Fields
            items={[
              ["Source", signals.source],
              ["Model", signals.model_version ?? "—"],
              [
                "Reason codes",
                Object.entries(signals.reason_code_probs ?? {})
                  .map(([code, value]) => `${code} ${percent(value)}`)
                  .join(", ") || "—",
              ],
              ["Ambiguity", percent(signals.ambiguity)],
              ["Escalation risk", percent(signals.escalation_risk)],
              ["Manipulation", percent(signals.manipulation)],
            ]}
          />
        ) : (
          <Empty>No decision signals in this turn.</Empty>
        )}
      </Section>

      <Section id="tools" title="Tool calls">
        <ToolCalls trace={trace} />
      </Section>

      <Section id="latency" title="Latency">
        <Latency trace={trace} />
      </Section>

      <Section id="tokens" title="Tokens and cost">
        <Tokens trace={trace} />
      </Section>
    </>
  );
}

export function TraceView({ api, token, traceId }: { api: AgentApi; token: string; traceId: string }) {
  const trace = useResource(`trace:${traceId}`, () => api.trace(traceId, token));
  return (
    <Loading resource={trace} missing={`Trace ${traceId} was not found.`}>
      {(data) => <Trace trace={data} />}
    </Loading>
  );
}
