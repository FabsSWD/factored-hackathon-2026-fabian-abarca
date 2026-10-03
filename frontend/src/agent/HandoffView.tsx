// One handoff packet (policy §13). Verified facts, each with its source table and record ID
// (DATA-04), are kept apart from what the customer said, which is always a claim (DATA-03).
// Model signals are informative, never a decision. Only the packet's known fields are shown.
import type { AgentApi, Evidence, HandoffPacket } from "./client";
import { action, dateTime, percent, QUEUES } from "./format";
import { Link } from "../router";
import { Chip, Empty, Fields, Loading, Section, useResource, Verified } from "./ui";

export function EvidenceList({ evidence }: { evidence: Evidence[] }) {
  return (
    <ul className="evidence">
      {evidence.map((item, index) => (
        <li key={`${item.name}-${index}`} className="evidence__item">
          <span className="evidence__head">
            <Chip>{item.kind}</Chip>
            <strong>{item.name}</strong>
            <span className="evidence__value">{item.value}</span>
          </span>
          <span className="muted evidence__origin">
            {item.origin}
            {item.source && ` · ${item.source}`}
            {item.record_id && ` · ${item.record_id}`}
          </span>
          {(item.claims ?? []).map((claim) => (
            <q key={claim} className="claim">
              {claim}
            </q>
          ))}
        </li>
      ))}
    </ul>
  );
}

function Packet({ packet }: { packet: HandoffPacket }) {
  const signals = packet.model_signals;
  const probabilities = Object.entries(signals?.reason_code_probs ?? {}).sort((a, b) => b[1] - a[1]);
  const facts = packet.verified_facts ?? [];
  const claims = packet.customer_claims ?? [];
  const slots = packet.collected_slots ?? [];
  const actions = packet.actions_taken ?? [];
  const questions = packet.open_questions ?? [];
  const later = packet.post_handoff_messages ?? [];
  return (
    <>
      <div className="page-head reveal">
        <p className="crumbs">
          <Link to="/agent">Queue</Link>
          <span aria-hidden="true"> / </span>
          <span>{packet.handoff_id}</span>
        </p>
        <h1 className="title title--big gradient">{packet.handoff_id}</h1>
        <p className="chips">
          <Chip tone={packet.priority === "high" ? "copper" : "plain"}>
            {packet.priority === "high" ? "High priority" : "Normal priority"}
          </Chip>
          <Chip>{QUEUES[packet.queue]}</Chip>
          {packet.triggered_rules.map((rule) => (
            <Chip key={rule}>{rule}</Chip>
          ))}
        </p>
      </div>

      <Section id="summary" title="Request">
        <p className="lead">{packet.request_summary}</p>
        <Fields
          items={[
            ["Created", dateTime(packet.created_at)],
            ["Business date", packet.business_date],
            ["Language", packet.language.toUpperCase()],
            ["Customer", packet.customer_ref],
            [
              "Authentication",
              `${packet.auth.status}${packet.auth.method ? ` · ${packet.auth.method}` : ""}${
                packet.auth.session_age_min != null ? ` · session ${Math.round(packet.auth.session_age_min)} min` : ""
              }`,
            ],
            ["Reason code", packet.reason_code ?? "—"],
            ["Policy version", packet.policy_version],
            [
              "Conversation",
              <Link key="traces" to={`/agent/traces?search=${encodeURIComponent(packet.transcript_ref)}`}>
                {`${packet.transcript_ref} · turn traces`}
              </Link>,
            ],
          ]}
        />
      </Section>

      <Section id="reasons" title="Why it was escalated">
        {(packet.escalation_reasons ?? []).map((reason) => (
          <article key={reason.rule_id} className="reason">
            <h3 className="reason__title">
              <Chip tone="copper">{reason.rule_id}</Chip> {reason.description}
            </h3>
            <EvidenceList evidence={reason.evidence} />
          </article>
        ))}
      </Section>

      <Section id="facts" title="Verified facts">
        {facts.length === 0 ? (
          <Empty>No verified facts.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Fact</th>
                  <th scope="col">Source</th>
                  <th scope="col">Record ID</th>
                </tr>
              </thead>
              <tbody>
                {facts.map((fact) => (
                  <tr key={`${fact.source}-${fact.record_id}-${fact.fact}`}>
                    <td>{fact.fact}</td>
                    <td>{fact.source}</td>
                    <td className="mono-id">{fact.record_id}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section id="claims" title="Customer claims (not verified)">
        {claims.length === 0 ? (
          <Empty>The customer made no claims.</Empty>
        ) : (
          <ul className="claims">
            {claims.map((claim) => (
              <li key={claim}>
                <q className="claim">{claim}</q>
              </li>
            ))}
          </ul>
        )}
        {slots.length > 0 && (
          <>
            <h3 className="subtitle">Details already given</h3>
            <Fields
              items={slots.map((slot) => [
                slot.name,
                `${slot.value}${slot.turn_index != null ? ` (turn ${slot.turn_index})` : ""}`,
              ])}
            />
          </>
        )}
      </Section>

      <Section id="actions" title="Actions taken">
        {actions.length === 0 ? (
          <Empty>No actions were attempted.</Empty>
        ) : (
          <ul className="rows">
            {actions.map((taken, index) => (
              <li key={`${taken.action}-${index}`} className="rows__item">
                <strong>{action(taken.action)}</strong>
                <Chip tone={taken.result === "success" ? "success" : "danger"}>{taken.result}</Chip>
                <Verified value={taken.verified} />
                {taken.detail && <span className="muted">{taken.detail}</span>}
                {taken.at && <span className="muted">{dateTime(taken.at)}</span>}
              </li>
            ))}
          </ul>
        )}
        {packet.draft_case && (
          <>
            <h3 className="subtitle">Draft case</h3>
            <Fields
              items={[
                ["Transaction", packet.draft_case.transaction_ref],
                ["Amount (USD)", packet.draft_case.amount_usd ?? "—"],
                ["Tier", packet.draft_case.tier ?? "—"],
                ["Provisional credit", packet.draft_case.provisional_credit_flag ?? "—"],
              ]}
            />
          </>
        )}
      </Section>

      <Section id="questions" title="Open questions">
        {questions.length === 0 ? (
          <Empty>No open questions.</Empty>
        ) : (
          <ul className="bullets">
            {questions.map((question) => (
              <li key={question}>{question}</li>
            ))}
          </ul>
        )}
      </Section>

      <Section id="signals" title="Model signals (informative, never a decision)">
        <Fields
          items={[
            ["Source", signals?.source ?? "—"],
            ["Model", signals?.model_version ?? "—"],
            ["Escalation risk", percent(signals?.escalation_risk)],
            ["Reason outside the five codes", percent(signals?.reason_code_other)],
            ["Calibrated", signals?.calibrated ? "yes" : "no"],
          ]}
        />
        {probabilities.length > 0 && (
          <ul className="bars" aria-label="Reason code probabilities">
            {probabilities.map(([code, value]) => (
              <li key={code} className="bars__item">
                <span className="bars__label">{code}</span>
                <span className="bars__track">
                  <span className="bars__fill" style={{ width: `${Math.round(value * 100)}%` }} />
                </span>
                <span className="bars__value">{percent(value)}</span>
              </li>
            ))}
          </ul>
        )}
      </Section>

      {later.length > 0 && (
        <Section id="later" title="Messages after the transfer">
          <ul className="rows">
            {later.map((message) => (
              <li key={message.received_at} className="rows__item rows__item--stack">
                <span className="muted">{dateTime(message.received_at)}</span>
                <q className="claim">{message.text}</q>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </>
  );
}

export function HandoffView({ api, token, handoffId }: { api: AgentApi; token: string; handoffId: string }) {
  const packet = useResource(`handoff:${handoffId}`, () => api.handoff(handoffId, token));
  return (
    <Loading resource={packet} missing={`Handoff ${handoffId} was not found.`}>
      {(data) => <Packet packet={data} />}
    </Loading>
  );
}
