// The queue of escalated cases: high priority first, then the oldest (the backend's order),
// filtered by queue and priority, a page at a time. Filters and page live in the URL, so back and
// reload keep them.
import type { AgentApi, Priority, Queue } from "./client";
import { dateTime, QUEUES } from "./format";
import { Link, navigate, useLocation } from "../router";
import { Chip, Empty, Loading, PAGE_SIZE, Pager, usePage, useResource, withQuery } from "./ui";

const QUEUE_VALUES = Object.keys(QUEUES) as Queue[];
const PRIORITIES: Priority[] = ["high", "normal"];

function pick<T extends string>(value: string | null, allowed: readonly T[]): T | null {
  return allowed.includes(value as T) ? (value as T) : null;
}

export function QueueView({ api, token }: { api: AgentApi; token: string }) {
  const { query } = useLocation();
  const queue = pick(query.get("queue"), QUEUE_VALUES);
  const priority = pick(query.get("priority"), PRIORITIES);
  const { offset } = usePage();
  const handoffs = useResource(`queue:${queue}:${priority}:${offset}`, () =>
    api.handoffs({ queue, priority, offset, limit: PAGE_SIZE }, token),
  );

  const filter = (name: "queue" | "priority", value: string) =>
    navigate(withQuery("/agent", query, { [name]: value }));

  return (
    <>
      <div className="page-head reveal">
        <h1 className="title title--big gradient">Escalated cases</h1>
        <p className="muted">High priority first, then the longest waiting.</p>
      </div>
      <form className="card filters reveal" aria-label="Filters" onSubmit={(e) => e.preventDefault()}>
        <label className="field">
          <span className="field__label">Queue</span>
          <select
            className="field__input"
            value={queue ?? ""}
            onChange={(event) => filter("queue", event.target.value)}
          >
            <option value="">All queues</option>
            {QUEUE_VALUES.map((value) => (
              <option key={value} value={value}>
                {QUEUES[value]}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="field__label">Priority</span>
          <select
            className="field__input"
            value={priority ?? ""}
            onChange={(event) => filter("priority", event.target.value)}
          >
            <option value="">All priorities</option>
            <option value="high">High</option>
            <option value="normal">Normal</option>
          </select>
        </label>
        <button type="button" className="button button--ghost filters__refresh" onClick={handoffs.reload}>
          Refresh
        </button>
      </form>
      <Loading resource={handoffs} missing="The queue was not found.">
        {(page) =>
          page.items.length === 0 && page.total === 0 ? (
            <div className="card">
              <Empty>No escalated cases match these filters.</Empty>
            </div>
          ) : (
            <section aria-labelledby="queue-count">
              <h2 id="queue-count" className="section__title">
                {page.total === 1 ? "1 case" : `${page.total} cases`}
              </h2>
              <ul className="queue">
                {page.items.map((row) => (
                  <li key={row.handoff_id} className="queue__item reveal">
                    <Link to={`/agent/handoffs/${encodeURIComponent(row.handoff_id)}`} className="queue__link">
                      <span className="queue__top">
                        <Chip tone={row.priority === "high" ? "copper" : "plain"}>
                          {row.priority === "high" ? "High" : "Normal"}
                        </Chip>
                        <Chip>{QUEUES[row.queue]}</Chip>
                        <span className="queue__id">{row.handoff_id}</span>
                      </span>
                      <span className="queue__summary">{row.request_summary}</span>
                      <span className="queue__meta">
                        <span>{dateTime(row.created_at)}</span>
                        <span>{row.language.toUpperCase()}</span>
                        <span>{row.status}</span>
                        <span>{row.triggered_rules.join(" · ")}</span>
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
              <Pager total={page.total} offset={page.offset} count={page.items.length} path="/agent" />
            </section>
          )
        }
      </Loading>
    </>
  );
}
