// Agent Console and Audit Viewer (M15), under /agent:
//
//   /agent                      the queue of escalated cases, filtered by queue and priority
//   /agent/handoffs/{id}        one handoff packet
//   /agent/traces               open a trace by ID, or list the latest (of one conversation)
//   /agent/traces/{trace_id}    one turn's trace
//
// Agent role only: nothing is requested before the agent token is accepted, and a 403 at any
// point clears the token and every view and asks for it again. The token lives in memory only.
// The console is in English, the language the system writes the handoff in (policy §13).
import { useCallback, useEffect, useState } from "react";

import type { AgentApi } from "./client";
import { AgentLogin } from "./AgentLogin";
import { HandoffView } from "./HandoffView";
import { QueueView } from "./QueueView";
import { TracesView } from "./TracesView";
import { TraceView } from "./TraceView";
import { Link, useLocation } from "../router";
import { ForbiddenContext } from "./ui";

const REFUSED = "The agent token was refused or has expired. Sign in again.";

type Route =
  | { view: "queue" }
  | { view: "handoff"; id: string }
  | { view: "traces" }
  | { view: "trace"; id: string };

export function route(path: string): Route {
  const parts = path.replace(/\/+$/, "").split("/").slice(2).map(decodeURIComponent);
  if (parts[0] === "handoffs" && parts[1]) return { view: "handoff", id: parts[1] };
  if (parts[0] === "traces" && parts[1]) return { view: "trace", id: parts[1] };
  if (parts[0] === "traces") return { view: "traces" };
  return { view: "queue" };
}

export function AgentApp({ api }: { api: AgentApi }) {
  const [token, setToken] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const { path } = useLocation();
  const current = route(path);

  useEffect(() => {
    document.documentElement.lang = "en";
    document.title = "Agent console";
  }, []);

  const forbidden = useCallback(() => {
    setToken(null);
    setNotice(REFUSED);
  }, []);

  const queueActive = current.view === "queue" || current.view === "handoff";
  return (
    <ForbiddenContext.Provider value={forbidden}>
      <div className="halo" aria-hidden="true" />
      <header className="nav">
        <div className="nav__inner nav__inner--wide">
          <span className="brand">
            <span className="brand__mark" aria-hidden="true" />
            <span className="brand__name">Agent console</span>
          </span>
          {token && (
            <nav className="nav__actions" aria-label="Agent console">
              <Link to="/agent" className="nav__link" current={queueActive}>
                Queue
              </Link>
              <Link to="/agent/traces" className="nav__link" current={!queueActive}>
                Traces
              </Link>
              <button
                type="button"
                className="button button--ghost button--small"
                onClick={() => {
                  setToken(null);
                  setNotice(null);
                }}
              >
                Sign out
              </button>
            </nav>
          )}
        </div>
      </header>
      <main className="page page--wide">
        {token === null ? (
          <AgentLogin
            api={api}
            notice={notice}
            onToken={(accepted) => {
              setNotice(null);
              setToken(accepted);
            }}
          />
        ) : current.view === "handoff" ? (
          <HandoffView key={current.id} api={api} token={token} handoffId={current.id} />
        ) : current.view === "trace" ? (
          <TraceView key={current.id} api={api} token={token} traceId={current.id} />
        ) : current.view === "traces" ? (
          <TracesView api={api} token={token} />
        ) : (
          <QueueView api={api} token={token} />
        )}
      </main>
    </ForbiddenContext.Provider>
  );
}
