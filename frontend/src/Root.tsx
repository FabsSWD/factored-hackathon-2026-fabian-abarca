// The single-page app's views: the Customer Chat at "/" and the Agent Console with the Audit
// Viewer under "/agent".
import type { Api } from "./api/client";
import type { AgentApi } from "./agent/client";
import { AgentApp } from "./agent/AgentApp";
import { App } from "./App";
import { useLocation } from "./router";

export function Root({ api, agentApi }: { api: Api; agentApi: AgentApi }) {
  const { path } = useLocation();
  const agent = path === "/agent" || path.startsWith("/agent/");
  return agent ? <AgentApp api={agentApi} /> : <App api={api} />;
}
