// M15: the queue and its filters, the handoff detail with the source and record ID of every
// fact, every section of a trace, access blocked without the agent role, and no prohibited data.
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ApiError, NetworkError } from "../api/client";
import { navigate } from "../router";
import {
  AGENT_TOKEN,
  fakeAgentApi,
  moreTraces,
  packet,
  PROHIBITED,
  summaries,
  summary,
  trace,
} from "../test/fakeAgentApi";
import { AgentApp, route } from "./AgentApp";

type Fake = ReturnType<typeof fakeAgentApi>;

beforeEach(() => {
  window.history.replaceState(null, "", "/agent");
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

function at(path: string) {
  window.history.replaceState(null, "", path);
}

async function signIn(api: Fake = fakeAgentApi(), path = "/agent") {
  at(path);
  const user = userEvent.setup();
  render(<AgentApp api={api} />);
  await user.type(screen.getByLabelText("Agent access token"), AGENT_TOKEN);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  await waitFor(() => expect(screen.queryByLabelText("Agent access token")).toBeNull());
  return { api, user };
}

describe("access", () => {
  it("asks for the agent token before requesting anything, even on a deep link", async () => {
    const api = fakeAgentApi();
    at("/agent/handoffs/HO-20261003-000002");
    render(<AgentApp api={api} />);
    expect(screen.getByRole("heading", { name: "Agent console" })).toBeInTheDocument();
    expect(screen.getByLabelText("Agent access token")).toHaveAttribute("type", "password");
    expect(api.handoffs).not.toHaveBeenCalled();
    expect(api.handoff).not.toHaveBeenCalled();
    expect(api.trace).not.toHaveBeenCalled();
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("blocks a token without the agent role", async () => {
    const api = fakeAgentApi();
    const user = userEvent.setup();
    render(<AgentApp api={api} />);
    await user.type(screen.getByLabelText("Agent access token"), "customer-session-token");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("does not have the agent role");
    expect(screen.getByLabelText("Agent access token")).toHaveAttribute("aria-invalid", "true");
    expect(api.handoffs).not.toHaveBeenCalled();
  });

  it.each([
    [new NetworkError("HTTP 503"), "not available on this server"],
    [new NetworkError("Failed to fetch"), "Could not reach the server"],
  ])("explains a sign-in that could not be checked (%s)", async (error, text) => {
    const api = fakeAgentApi();
    api.session.mockRejectedValueOnce(error);
    const user = userEvent.setup();
    render(<AgentApp api={api} />);
    await user.type(screen.getByLabelText("Agent access token"), AGENT_TOKEN);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
  });

  it("opens the deep link after signing in", async () => {
    const { api } = await signIn(fakeAgentApi(), "/agent/handoffs/HO-20261003-000002");
    expect(await screen.findByRole("heading", { level: 1, name: packet.handoff_id })).toBeInTheDocument();
    expect(api.handoff).toHaveBeenCalledWith(packet.handoff_id, AGENT_TOKEN);
  });

  it("a 403 during the session clears every view and asks for the token again", async () => {
    const api = fakeAgentApi();
    await signIn(api);
    await screen.findByText(/stolen phone/);
    api.handoff.mockRejectedValueOnce(new ApiError(403));
    act(() => navigate("/agent/handoffs/HO-20261003-000002"));
    expect(await screen.findByText(/refused or has expired/)).toHaveAttribute("role", "status");
    expect(screen.getByLabelText("Agent access token")).toBeInTheDocument();
    expect(screen.queryByText(/stolen phone/)).toBeNull();
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("signs out", async () => {
    const { user } = await signIn();
    await screen.findByText(/stolen phone/);
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(screen.getByLabelText("Agent access token")).toBeInTheDocument();
    expect(screen.queryByText(/stolen phone/)).toBeNull();
  });

  it("keeps the token out of URLs and browser storage", async () => {
    const { user } = await signIn();
    await screen.findByText(/stolen phone/);
    await user.click(screen.getByRole("link", { name: /HO-20261003-000002/ }));
    await screen.findByRole("heading", { level: 1, name: packet.handoff_id });
    const kept = [
      window.location.href,
      ...Object.values(window.localStorage),
      ...Object.values(window.sessionStorage),
      document.cookie,
      document.body.innerHTML,
    ].join(" ");
    expect(kept).not.toContain(AGENT_TOKEN);
  });
});

describe("queue", () => {
  it("lists the escalated cases in the backend's order", async () => {
    const { api } = await signIn();
    const list = await screen.findByRole("list");
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("High");
    expect(items[0]).toHaveTextContent("Security review");
    expect(items[0]).toHaveTextContent("ESC-04");
    expect(items[0]).toHaveTextContent("2026-10-03 09:15:00 UTC");
    expect(items[1]).toHaveTextContent("Disputes");
    expect(screen.getByRole("heading", { name: "2 cases" })).toBeInTheDocument();
    expect(api.handoffs).toHaveBeenCalledWith({ queue: null, priority: null, offset: 0, limit: 20 }, AGENT_TOKEN);
    expect(screen.queryByRole("navigation", { name: "Pages" })).toBeNull(); // one page only
  });

  it("filters by queue and by priority, and keeps the filters in the URL", async () => {
    const { api, user } = await signIn();
    await screen.findByRole("heading", { name: "2 cases" });
    await user.selectOptions(screen.getByLabelText("Queue"), "disputes");
    expect(await screen.findByRole("heading", { name: "1 case" })).toBeInTheDocument();
    expect(api.handoffs).toHaveBeenLastCalledWith({ queue: "disputes", priority: null, offset: 0, limit: 20 }, AGENT_TOKEN);
    expect(window.location.search).toBe("?queue=disputes");
    await user.selectOptions(screen.getByLabelText("Priority"), "high");
    expect(await screen.findByText("No escalated cases match these filters.")).toBeInTheDocument();
    expect(api.handoffs).toHaveBeenLastCalledWith({ queue: "disputes", priority: "high", offset: 0, limit: 20 }, AGENT_TOKEN);
    await user.selectOptions(screen.getByLabelText("Queue"), "");
    expect(await screen.findByRole("heading", { name: "1 case" })).toBeInTheDocument();
    expect(window.location.search).toBe("?priority=high");
  });

  it("ignores an unknown filter value in the URL", async () => {
    const { api } = await signIn(fakeAgentApi(), "/agent?queue=everything&priority=urgent");
    await screen.findByRole("heading", { name: "2 cases" });
    expect(api.handoffs).toHaveBeenCalledWith({ queue: null, priority: null, offset: 0, limit: 20 }, AGENT_TOKEN);
  });

  it("pages through a long queue, and a filter goes back to the first page", async () => {
    const many = Array.from({ length: 25 }, (_, index) => ({
      ...summaries[1]!,
      handoff_id: `HO-20261003-${String(index + 1).padStart(6, "0")}`,
    }));
    const { api, user } = await signIn(fakeAgentApi({ handoffs: many }));
    expect(await screen.findByRole("heading", { name: "25 cases" })).toBeInTheDocument();
    const pager = screen.getByRole("navigation", { name: "Pages" });
    expect(pager).toHaveTextContent("1–20 of 25");
    expect(pager).toHaveTextContent("of 2");
    expect(within(pager).getByLabelText("Page")).toHaveValue(1);
    expect(within(pager).getByRole("button", { name: "First page" })).toBeDisabled();
    expect(within(pager).getByRole("button", { name: "Previous page" })).toBeDisabled();
    expect(screen.getAllByRole("listitem")).toHaveLength(20);
    await user.click(within(pager).getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("21–25 of 25")).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
    expect(window.location.search).toBe("?page=2");
    expect(api.handoffs).toHaveBeenLastCalledWith({ queue: null, priority: null, offset: 20, limit: 20 }, AGENT_TOKEN);
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Previous page" }));
    expect(await screen.findByText("1–20 of 25")).toBeInTheDocument();
    expect(window.location.search).toBe("");
    await user.click(screen.getByRole("button", { name: "Next page" }));
    await screen.findByText("21–25 of 25");
    await user.selectOptions(screen.getByLabelText("Queue"), "disputes");
    await waitFor(() => expect(window.location.search).toBe("?queue=disputes"));
    expect(await screen.findByText("1–20 of 25")).toBeInTheDocument();
  });

  it("an out-of-range page still says how many cases there are", async () => {
    await signIn(fakeAgentApi(), "/agent?page=5");
    const pager = await screen.findByRole("navigation", { name: "Pages" });
    expect(pager).toHaveTextContent("0 of 2");
    expect(screen.getByRole("heading", { name: "2 cases" })).toBeInTheDocument();
  });

  it("shows a network error with a retry", async () => {
    const api = fakeAgentApi();
    api.handoffs.mockRejectedValueOnce(new NetworkError("Failed to fetch"));
    const { user } = await signIn(api);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the server.");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { name: "2 cases" })).toBeInTheDocument();
  });

  it("says when the server is not configured", async () => {
    const api = fakeAgentApi();
    api.handoffs.mockRejectedValueOnce(new NetworkError("HTTP 503"));
    await signIn(api);
    expect(await screen.findByRole("alert")).toHaveTextContent("may not be configured");
  });

  it("refreshes the queue", async () => {
    const { api, user } = await signIn();
    await screen.findByRole("heading", { name: "2 cases" });
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(api.handoffs).toHaveBeenCalledTimes(2));
  });
});

describe("handoff detail", () => {
  async function openPacket() {
    const opened = await signIn();
    await opened.user.click(await screen.findByRole("link", { name: /HO-20261003-000002/ }));
    await screen.findByRole("heading", { level: 1, name: packet.handoff_id });
    return opened;
  }

  it("shows each verified fact with its source and record ID", async () => {
    await openPacket();
    const facts = screen.getByRole("region", { name: "Verified facts" });
    const rows = within(facts).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getAllByRole("cell").map((c) => c.textContent)).toEqual([
      "Card ****4321 is active",
      "products",
      "PRD-FAKE0001",
    ]);
    expect(within(rows[1]!).getAllByRole("cell").map((c) => c.textContent)).toEqual([
      "Charge of USD 120.00 at Fake Store on 2026-06-15",
      "transactions",
      "TRX-FAKE0001",
    ]);
  });

  it("separates claims, actions, questions, reasons and informative signals", async () => {
    await openPacket();
    const claims = screen.getByRole("region", { name: "Customer claims (not verified)" });
    expect(within(claims).getByText("no hice esa compra")).toBeInTheDocument();
    expect(within(claims).getByText("no reconozco la compra (turn 1)")).toBeInTheDocument();

    const actions = screen.getByRole("region", { name: "Actions taken" });
    expect(within(actions).getByText("ACT-03 · Block card")).toBeInTheDocument();
    expect(within(actions).getAllByText("verified")).toHaveLength(2);
    expect(within(actions).getByText("TRX-FAKE0001")).toBeInTheDocument(); // draft case

    const questions = screen.getByRole("region", { name: "Open questions" });
    expect(within(questions).getByText("Confirm when the phone was stolen.")).toBeInTheDocument();

    const reasons = screen.getByRole("region", { name: "Why it was escalated" });
    expect(within(reasons).getByText("Possible account takeover reported by the customer.")).toBeInTheDocument();
    expect(within(reasons).getByText("account_takeover_reported")).toBeInTheDocument();
    expect(within(reasons).getByText("me robaron el celular ayer")).toBeInTheDocument();

    const signals = screen.getByRole("region", { name: "Model signals (informative, never a decision)" });
    expect(within(signals).getByText("82%")).toBeInTheDocument();
    expect(within(signals).getByText("57%")).toBeInTheDocument();

    const later = screen.getByRole("region", { name: "Messages after the transfer" });
    expect(within(later).getByText("¿ya me atienden?")).toBeInTheDocument();
  });

  it("links to the traces of the conversation", async () => {
    const { api, user } = await openPacket();
    await user.click(screen.getByRole("link", { name: /CONV-FAKE-01 · turn traces/ }));
    expect(await screen.findByRole("heading", { name: "1 turn" })).toBeInTheDocument();
    expect(window.location.search).toBe("?search=CONV-FAKE-01");
    expect(screen.getByRole("searchbox")).toHaveValue("CONV-FAKE-01");
    expect(api.traces).toHaveBeenLastCalledWith(
      { search: "CONV-FAKE-01", outcome: null, offset: 0, limit: 20 },
      AGENT_TOKEN,
    );
  });

  it("says when a handoff does not exist", async () => {
    await signIn(fakeAgentApi(), "/agent/handoffs/HO-20990101-000001");
    expect(await screen.findByRole("alert")).toHaveTextContent("Handoff HO-20990101-000001 was not found.");
  });

  it("renders a packet with only the required fields", async () => {
    const api = fakeAgentApi();
    api.handoff.mockResolvedValueOnce({
      handoff_id: "HO-20261003-000009",
      created_at: "2026-10-03T10:00:00Z",
      business_date: "2026-06-17",
      language: "pt",
      queue: "fraud",
      priority: "normal",
      customer_ref: "CUS-unauthenticated",
      auth: { status: "unauthenticated" },
      request_summary: "Customer not authenticated.",
      triggered_rules: ["ESC-01"],
      escalation_reasons: [
        { rule_id: "ESC-01", description: "Not authenticated.", evidence: [{ kind: "counter", name: "auth_attempts", value: "3", origin: "Orchestrator" }] },
      ],
      transcript_ref: "CONV-X",
      policy_version: "0.4.12",
    });
    await signIn(api, "/agent/handoffs/HO-20261003-000009");
    await screen.findByRole("heading", { level: 1, name: "HO-20261003-000009" });
    expect(screen.getByText("No verified facts.")).toBeInTheDocument();
    expect(screen.getByText("The customer made no claims.")).toBeInTheDocument();
    expect(screen.getByText("No actions were attempted.")).toBeInTheDocument();
    expect(screen.getByText("No open questions.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Messages after the transfer" })).toBeNull();
  });
});

describe("trace viewer", () => {
  const SECTIONS = [
    "Overview",
    "Input guard",
    "Gates",
    "Rules and decision",
    "Model calls",
    "Decision signals (informative)",
    "Tool calls",
    "Latency",
    "Tokens and cost",
  ];

  it("renders every section of a trace", async () => {
    await signIn(fakeAgentApi(), "/agent/traces/TRC-FAKE-0001");
    await screen.findByRole("heading", { level: 1, name: trace.trace_id });
    for (const name of SECTIONS) expect(screen.getByRole("region", { name })).toBeInTheDocument();

    const overview = screen.getByRole("region", { name: "Overview" });
    expect(within(overview).getByText("me robaron el celular, mi documento es [number]")).toBeInTheDocument();
    expect(within(overview).getByRole("link", { name: "HO-20261003-000002" })).toBeInTheDocument();

    const gates = screen.getByRole("region", { name: "Gates" });
    expect(within(gates).getByText("GATE-02").parentElement).toHaveTextContent("passed");
    expect(within(gates).getByText("GATE-05").parentElement).toHaveTextContent("not passed");
    // each gate explains itself on hover or focus
    const gate05 = within(gates).getByRole("button", { name: "GATE-05 not passed" });
    expect(gate05).toHaveAccessibleDescription(/Transaction identified/);
    expect(within(gate05.closest("li")!).getByRole("tooltip")).toHaveTextContent("If not: Clarification");

    const rules = screen.getByRole("region", { name: "Rules and decision" });
    expect(within(rules).getAllByText("ESC-04").length).toBeGreaterThan(0);
    expect(within(rules).getByText("ACT-03 · Block card, ACT-05 · Transfer to a human")).toBeInTheDocument();
    expect(within(rules).getByText("fraud_score_missing")).toBeInTheDocument();
    expect(within(rules).getByText("account_takeover_reported")).toBeInTheDocument();

    const models = screen.getByRole("region", { name: "Model calls" });
    expect(within(models).getByText("extract_slots")).toBeInTheDocument();
    expect(within(models).getByText("1,200 · 800 · 90")).toBeInTheDocument();
    expect(within(models).getByText("1.84 s")).toBeInTheDocument();
    expect(within(models).getByText("310 ms (120 ms)")).toBeInTheDocument();
    expect(within(models).getByText("timeout")).toBeInTheDocument();
    expect(within(models).getByText("run: run-7")).toBeInTheDocument();

    const tools = screen.getByRole("region", { name: "Tool calls" });
    expect(within(tools).getByText("BLK-FAKE0001")).toBeInTheDocument();
    expect(within(tools).getByText("queue write failed")).toBeInTheDocument();
    expect(within(tools).getByText("attempts 2")).toBeInTheDocument();

    const latency = screen.getByRole("region", { name: "Latency" });
    expect(within(latency).getByText("Total 2.21 s")).toBeInTheDocument();
    expect(within(latency).getByText("extraction")).toBeInTheDocument();
    expect(within(latency).getByText("4 ms")).toBeInTheDocument();

    const tokens = screen.getByRole("region", { name: "Tokens and cost" });
    expect(within(tokens).getByText("1,200")).toBeInTheDocument();
    expect(within(tokens).getByText("USD 0.000412")).toBeInTheDocument();
  });

  it("says so when a stage did not run", async () => {
    const api = fakeAgentApi();
    api.trace.mockResolvedValueOnce({
      trace_id: "TRC-EMPTY",
      conversation_id: "CONV-E",
      turn_index: 0,
      created_at: "2026-10-03T10:00:00Z",
      policy_version: "0.4.12",
    });
    await signIn(api, "/agent/traces/TRC-EMPTY");
    await screen.findByRole("heading", { level: 1, name: "TRC-EMPTY" });
    for (const name of SECTIONS) expect(screen.getByRole("region", { name })).toBeInTheDocument();
    expect(screen.getByText("No gates were evaluated in this turn.")).toBeInTheDocument();
    expect(screen.getByText("The Policy Engine was not called in this turn.")).toBeInTheDocument();
    expect(screen.getByText("No model was called in this turn.")).toBeInTheDocument();
    expect(screen.getByText("No tool action ran in this turn.")).toBeInTheDocument();
    expect(screen.getByText("No stage latencies were recorded.")).toBeInTheDocument();
    expect(screen.getByText("The input guard did not run.")).toBeInTheDocument();
    expect(screen.getByText("not authenticated")).toBeInTheDocument();
  });

  it("one search box finds a trace by its trace, conversation, session or handoff ID", async () => {
    const rows = [summary(trace), ...moreTraces(4)];
    const { api, user } = await signIn(fakeAgentApi({ traces: rows }));
    await screen.findByRole("heading", { name: "2 cases" });
    await user.click(screen.getByRole("link", { name: "Traces" }));
    expect(await screen.findByRole("heading", { name: "5 turns" })).toBeInTheDocument();
    const box = screen.getByRole("searchbox", { name: "Trace, conversation, session or handoff ID" });
    const cases: [string, string][] = [
      ["trc-fake", "1 turn"], // part of a trace ID, any case
      ["CONV-MORE-1", "1 turn"], // a conversation ID
      ["SES-FAKE", "1 turn"], // a session ID
      ["000002", "1 turn"], // a handoff ID
      ["CONV-MORE", "4 turns"],
    ];
    for (const [text, found] of cases) {
      await user.clear(box);
      await user.type(box, `${text}{Enter}`);
      expect(await screen.findByRole("heading", { name: found })).toBeInTheDocument();
      expect(api.traces).toHaveBeenLastCalledWith({ search: text, outcome: null, offset: 0, limit: 20 }, AGENT_TOKEN);
    }
    expect(window.location.search).toBe("?search=CONV-MORE");
    await user.click(screen.getByRole("link", { name: "TRC-MORE-0001" }));
    await waitFor(() => expect(api.trace).toHaveBeenCalledWith("TRC-MORE-0001", AGENT_TOKEN));
  });

  it("filters by outcome, says when nothing matches, and clears the search", async () => {
    const rows = [summary(trace), ...moreTraces(2)];
    const { api, user } = await signIn(fakeAgentApi({ traces: rows }), "/agent/traces");
    const table = await screen.findByRole("table");
    expect(within(table).getByRole("link", { name: "TRC-FAKE-0001" })).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "HO-20261003-000002" })).toBeInTheDocument();
    expect(within(table).getAllByText("2.21 s")).toHaveLength(3);
    expect(screen.queryByRole("button", { name: "Clear" })).toBeNull();
    await user.selectOptions(screen.getByLabelText("Outcome"), "ESCALATE");
    expect(await screen.findByRole("heading", { name: "1 turn" })).toBeInTheDocument();
    expect(api.traces).toHaveBeenLastCalledWith({ search: null, outcome: "ESCALATE", offset: 0, limit: 20 }, AGENT_TOKEN);
    await user.type(screen.getByRole("searchbox"), "nothing-like-this{Enter}");
    expect(await screen.findByText("No traces match this search.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(await screen.findByRole("heading", { name: "3 turns" })).toBeInTheDocument();
    expect(window.location.search).toBe("");
    expect(screen.getByRole("searchbox")).toHaveValue("");
    expect(screen.getByLabelText("Outcome")).toHaveValue("");
  });

  it("searching the same text again refreshes the results", async () => {
    const { api, user } = await signIn(fakeAgentApi(), "/agent/traces?search=CONV");
    await screen.findByRole("heading", { name: "1 turn" });
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(api.traces).toHaveBeenCalledTimes(2));
  });

  it("says when there are no traces yet", async () => {
    await signIn(fakeAgentApi({ traces: [] }), "/agent/traces");
    expect(await screen.findByText("No traces yet.")).toBeInTheDocument();
  });

  it("pages through the traces, and a new search goes back to the first page", async () => {
    const { api, user } = await signIn(fakeAgentApi({ traces: moreTraces(45) }), "/agent/traces");
    expect(await screen.findByRole("heading", { name: "45 turns" })).toBeInTheDocument();
    const pager = screen.getByRole("navigation", { name: "Pages" });
    expect(pager).toHaveTextContent("1–20 of 45");
    expect(pager).toHaveTextContent("of 3");
    expect(within(pager).getByLabelText("Page")).toHaveValue(1);
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(21);
    await user.click(screen.getByRole("button", { name: "Next page" }));
    await screen.findByText("21–40 of 45");
    await user.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("41–45 of 45")).toBeInTheDocument();
    expect(window.location.search).toBe("?page=3");
    expect(api.traces).toHaveBeenLastCalledWith({ search: null, outcome: null, offset: 40, limit: 20 }, AGENT_TOKEN);
    await user.type(screen.getByRole("searchbox"), "CONV-MORE-1{Enter}");
    // CONV-MORE-1 and CONV-MORE-10 to 14: six conversations, three turns each
    expect(await screen.findByRole("heading", { name: "18 turns" })).toBeInTheDocument();
    expect(window.location.search).toBe("?search=CONV-MORE-1");
    expect(screen.queryByRole("navigation", { name: "Pages" })).toBeNull();
  });

  it("jumps to a typed page, clamps it to the last one, and goes to the first and last pages", async () => {
    const { api, user } = await signIn(fakeAgentApi({ traces: moreTraces(45) }), "/agent/traces");
    await screen.findByRole("heading", { name: "45 turns" });
    const box = screen.getByLabelText("Page");
    await user.clear(box);
    await user.type(box, "2{Enter}");
    expect(await screen.findByText("21–40 of 45")).toBeInTheDocument();
    expect(window.location.search).toBe("?page=2");
    expect(screen.getByLabelText("Page")).toHaveValue(2);
    await user.clear(screen.getByLabelText("Page"));
    await user.type(screen.getByLabelText("Page"), "99{Enter}");
    expect(await screen.findByText("41–45 of 45")).toBeInTheDocument();
    expect(window.location.search).toBe("?page=3");
    expect(screen.getByRole("button", { name: "Last page" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "First page" }));
    expect(await screen.findByText("1–20 of 45")).toBeInTheDocument();
    expect(window.location.search).toBe("");
    await user.click(screen.getByRole("button", { name: "Last page" }));
    expect(await screen.findByText("41–45 of 45")).toBeInTheDocument();
    expect(api.traces).toHaveBeenLastCalledWith({ search: null, outcome: null, offset: 40, limit: 20 }, AGENT_TOKEN);
  });

  it("says when a trace does not exist", async () => {
    await signIn(fakeAgentApi(), "/agent/traces/TRC-NOPE");
    expect(await screen.findByRole("alert")).toHaveTextContent("Trace TRC-NOPE was not found.");
  });
});

describe("prohibited data", () => {
  it.each(["/agent", "/agent/handoffs/HO-20261003-000002", "/agent/traces", "/agent/traces/TRC-FAKE-0001"])(
    "never reaches the screen (%s)",
    async (path) => {
      await signIn(fakeAgentApi(), path);
      await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
      const html = document.body.innerHTML;
      for (const [field, value] of Object.entries(PROHIBITED)) {
        expect(html).not.toContain(value);
        expect(html).not.toContain(field);
      }
      expect(html).not.toContain(AGENT_TOKEN);
    },
  );
});

describe("route", () => {
  it.each([
    ["/agent", { view: "queue" }],
    ["/agent/", { view: "queue" }],
    ["/agent/unknown", { view: "queue" }],
    ["/agent/handoffs/HO-1", { view: "handoff", id: "HO-1" }],
    ["/agent/traces", { view: "traces" }],
    ["/agent/traces/TRC%2F1", { view: "trace", id: "TRC/1" }],
  ])("%s", (path, expected) => {
    expect(route(path)).toEqual(expected);
  });
});
