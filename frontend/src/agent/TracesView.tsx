// The Audit Viewer's entry: one search box for part of a trace, conversation, session or handoff
// ID, an outcome filter, and the matching turns newest first, a page at a time. Search, outcome
// and page live in the URL (the handoff links here with its conversation).
import { useState } from "react";
import type { FormEvent } from "react";

import type { AgentApi, Outcome } from "./client";
import { dateTime, ms, usd } from "./format";
import { Link, navigate, useLocation } from "../router";
import { Chip, Empty, Loading, PAGE_SIZE, Pager, usePage, useResource, withQuery } from "./ui";

const OUTCOMES: Outcome[] = ["RESOLVE", "CLARIFY", "INFORM", "ESCALATE", "REFUSE"];
const PATH = "/agent/traces";

export function TracesView({ api, token }: { api: AgentApi; token: string }) {
  const { query } = useLocation();
  const search = query.get("search") ?? "";
  const outcomeParam = query.get("outcome");
  const outcome = OUTCOMES.includes(outcomeParam as Outcome) ? (outcomeParam as Outcome) : null;
  const { offset } = usePage();
  const [draft, setDraft] = useState(search);
  const [shown, setShown] = useState(search); // the search the box was last synced with
  if (shown !== search) {
    // The URL changed (a link, back, clear): the box follows it.
    setShown(search);
    setDraft(search);
  }
  const traces = useResource(`traces:${search}:${outcome}:${offset}`, () =>
    api.traces({ search: search || null, outcome, offset, limit: PAGE_SIZE }, token),
  );

  function submit(event: FormEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (text === search && offset === 0) traces.reload(); // the same search again: refresh it
    else navigate(withQuery(PATH, query, { search: text }));
  }

  const filtered = search !== "" || outcome !== null;
  return (
    <>
      <div className="page-head reveal">
        <h1 className="title title--big gradient">Turn traces</h1>
        <p className="muted">Gates, rules, model and tool calls, latency and tokens of every turn.</p>
      </div>
      <form className="card filters reveal" role="search" aria-label="Search traces" onSubmit={submit}>
        <label className="field filters__grow">
          <span className="field__label">Trace, conversation, session or handoff ID</span>
          <input
            className="field__input"
            type="search"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            maxLength={64}
          />
        </label>
        <label className="field">
          <span className="field__label">Outcome</span>
          <select
            className="field__input"
            value={outcome ?? ""}
            onChange={(event) => navigate(withQuery(PATH, query, { outcome: event.target.value }))}
          >
            <option value="">All outcomes</option>
            {OUTCOMES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className="button button--copper">
          Search
        </button>
        {filtered && (
          <button type="button" className="button button--ghost" onClick={() => navigate(PATH)}>
            Clear
          </button>
        )}
      </form>
      <Loading resource={traces} missing="No traces were found.">
        {(page) =>
          page.total === 0 ? (
            <div className="card">
              <Empty>{filtered ? "No traces match this search." : "No traces yet."}</Empty>
            </div>
          ) : (
            <section className="card section" aria-labelledby="traces-count">
              <h2 id="traces-count" className="section__title">
                {page.total === 1 ? "1 turn" : `${page.total} turns`}
              </h2>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th scope="col">Time</th>
                      <th scope="col">Trace</th>
                      <th scope="col">Conversation · turn</th>
                      <th scope="col">Outcome</th>
                      <th scope="col">Handoff</th>
                      <th scope="col">Latency</th>
                      <th scope="col">Cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {page.items.map((row) => (
                      <tr key={row.trace_id}>
                        <td>{dateTime(row.created_at)}</td>
                        <td>
                          <Link to={`${PATH}/${encodeURIComponent(row.trace_id)}`} className="mono-id">
                            {row.trace_id}
                          </Link>
                        </td>
                        <td className="mono-id">{`${row.conversation_id} · ${row.turn_index}`}</td>
                        <td>
                          {row.outcome ? (
                            <Chip tone={row.outcome === "ESCALATE" ? "copper" : "plain"}>{row.outcome}</Chip>
                          ) : (
                            "—"
                          )}
                          {row.reply_kind && <span className="muted cell-note">{row.reply_kind}</span>}
                        </td>
                        <td>
                          {row.handoff_id ? (
                            <Link to={`/agent/handoffs/${encodeURIComponent(row.handoff_id)}`} className="mono-id">
                              {row.handoff_id}
                            </Link>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td>{ms(row.total_latency_ms)}</td>
                        <td>{usd(row.estimated_cost_usd)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Pager total={page.total} offset={page.offset} count={page.items.length} path={PATH} />
            </section>
          )
        }
      </Loading>
    </>
  );
}
