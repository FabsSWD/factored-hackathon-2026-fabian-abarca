// M16: every chart with sample data and with empty data, the numbers shown equal the endpoint's
// JSON, the loading and error states, the manual refresh, and the agent role.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, NetworkError } from "../api/client";
import { AGENT_TOKEN, emptyMetrics, fakeAgentApi, metrics } from "../test/fakeAgentApi";
import { AgentApp } from "./AgentApp";
import { rate, Tip } from "./MetricsView";

type Fake = ReturnType<typeof fakeAgentApi>;

beforeEach(() => {
  window.history.replaceState(null, "", "/agent/metrics");
  // jsdom lays nothing out; the charts measure their container, so give that one a size.
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const sized = this.classList.contains("recharts-responsive-container");
    return DOMRect.fromRect(sized ? { width: 640, height: 300 } : {});
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

async function open(api: Fake = fakeAgentApi()) {
  const user = userEvent.setup();
  render(<AgentApp api={api} />);
  await user.type(screen.getByLabelText("Agent access token"), AGENT_TOKEN);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  await waitFor(() => expect(screen.queryByLabelText("Agent access token")).toBeNull());
  return { api, user };
}

const section = (name: string) => screen.getByRole("region", { name });

/** Opens a category's tab once the dashboard has loaded. */
async function showTab(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(await screen.findByRole("tab", { name }));
}

/** The rows of a chart's data table, each as its cells' text. */
function table(name: string): string[][] {
  const rows = within(screen.getByRole("table", { name })).getAllByRole("row");
  return rows.map((row) => [...row.querySelectorAll("th, td")].map((cell) => cell.textContent ?? ""));
}

/** The value labels and category ticks in a chart's SVG. Recharts writes each word in its own
 * tspan, so the words are joined again with spaces. */
function svgText(name: string): string[] {
  return [...section(name).querySelectorAll("svg text")].map((node) => {
    const words = [...node.querySelectorAll("tspan")].map((span) => span.textContent);
    return words.length ? words.join(" ") : (node.textContent ?? "");
  });
}

describe("with sample data", () => {
  it("loads the metrics with the agent token and opens from the nav", async () => {
    const { api } = await open();
    expect(await screen.findByRole("heading", { level: 1, name: "Metrics" })).toBeInTheDocument();
    expect(api.metrics).toHaveBeenCalledWith(AGENT_TOKEN);
    expect(screen.getByRole("link", { name: "Metrics" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Traces" })).not.toHaveAttribute("aria-current");
    expect(await screen.findByText(/^Loaded \d{4}-\d{2}-\d{2} /)).toBeInTheDocument();
  });

  it("shows the headline figures as the JSON has them", async () => {
    await open();
    const headline = await screen.findByRole("region", { name: "Headline figures" });
    const tile = (label: string) => within(headline).getByText(label).parentElement as HTMLElement;
    expect(tile("Conversations")).toHaveTextContent("80412 turns");
    expect(tile("Attempted cases")).toHaveTextContent("60");
    expect(tile("Automated resolutions")).toHaveTextContent("29");
    expect(tile("Containment")).toHaveTextContent("57.5%46 without a handoff");
    expect(tile("Escalation rate")).toHaveTextContent("27.5%22 handed off");
    expect(tile("Abandoned")).toHaveTextContent("8");
  });

  it("draws conversations by outcome, one bar and label per outcome", async () => {
    await open();
    await screen.findByRole("region", { name: "Conversations by outcome" });
    expect(table("Conversations by outcome")).toEqual([
      ["Outcome", "Conversations", "Share"],
      ["RESOLVE", "30", "37.5%"],
      ["INFORM", "16", "20.0%"],
      ["CLARIFY", "8", "10.0%"],
      ["ESCALATE", "22", "27.5%"],
      ["REFUSE", "4", "5.0%"],
    ]);
    const chart = section("Conversations by outcome");
    expect(chart.querySelectorAll(".recharts-bar-rectangle")).toHaveLength(5);
    expect(svgText("Conversations by outcome")).toEqual(expect.arrayContaining(["30", "16", "8", "22", "4", "RESOLVE"]));
    // the conversations that stopped before a transaction, as the JSON counts them
    expect(within(chart).getByText("Authentication (GATE-02)").nextSibling).toHaveTextContent("12");
    expect(within(chart).getByText("Language (GATE-01)").nextSibling).toHaveTextContent("3");
    expect(within(chart).getByText("No decision (e.g. an error)").nextSibling).toHaveTextContent("5");
  });

  it("draws automation for T1 and T2", async () => {
    const { user } = await open();
    await showTab(user, "Automation");
    await screen.findByRole("region", { name: "Automation by tier" });
    expect(table("Automation by tier")).toEqual([
      ["Tier", "Conversations", "Resolved", "Automation"],
      ["T1", "20", "18", "90.0%"],
      ["T2", "20", "12", "60.0%"],
    ]);
    expect(section("Automation by tier").querySelectorAll(".recharts-bar-rectangle")).toHaveLength(2);
    expect(svgText("Automation by tier")).toEqual(expect.arrayContaining(["90.0% · 18 of 20", "60.0% · 12 of 20"]));
  });

  it("draws latency p50 and p95 per turn and per stage", async () => {
    const { user } = await open();
    await showTab(user, "Latency");
    await screen.findByRole("region", { name: "Latency p50 / p95" });
    expect(table("Latency p50 and p95")).toEqual([
      ["Stage", "Turns", "p50", "p95"],
      ["Whole turn", "412", "5.81 s", "8.23 s"],
      ["Extraction", "380", "1.84 s", "3.12 s"],
      ["Input guard", "412", "2 ms", "6 ms"],
    ]);
    const chart = section("Latency p50 / p95");
    expect(chart.querySelectorAll(".recharts-bar-rectangle")).toHaveLength(6);
    const legend = [...chart.querySelectorAll(".recharts-legend-item")].map((item) => item.textContent);
    expect(legend).toEqual(["p50", "p95"]);
  });

  it("draws the cost per attempted case and per automated resolution", async () => {
    const { user } = await open();
    await showTab(user, "Cost");
    await screen.findByRole("region", { name: "Cost" });
    expect(table("Cost")).toEqual([
      ["Cost", "USD", "Divided by"],
      ["Per attempted case", "USD 0.001353", "60 attempted cases"],
      ["Per automated resolution", "USD 0.002800", "29 automated resolutions"],
    ]);
    expect(svgText("Cost")).toEqual(expect.arrayContaining(["USD 0.0014", "USD 0.0028"]));
    expect(section("Cost")).toHaveTextContent("Total: USD 0.0812. 3 turns have no cost");
  });

  it("draws conversations by language, split by outcome", async () => {
    await open();
    await screen.findByRole("region", { name: "Conversations by language" });
    expect(table("Conversations by language and outcome")).toEqual([
      ["Language", "RESOLVE", "INFORM", "CLARIFY", "ESCALATE", "REFUSE", "Total"],
      ["Spanish", "16", "10", "4", "12", "0", "42"],
      ["Portuguese", "14", "6", "4", "9", "4", "37"],
      ["Unknown", "0", "0", "0", "1", "0", "1"],
    ]);
    const chart = section("Conversations by language");
    const legend = [...chart.querySelectorAll(".recharts-legend-item")].map((item) => item.textContent);
    expect(legend).toEqual(["RESOLVE", "INFORM", "CLARIFY", "ESCALATE", "REFUSE"]); // the bars' order
    expect(svgText("Conversations by language")).toEqual(expect.arrayContaining(["Spanish", "Portuguese", "Unknown"]));
  });

  it("shows each series' formatted value in the tooltip, only while hovering", () => {
    const payload = [
      { dataKey: "p50", name: "p50", value: 1840, color: "#3987e5", graphicalItemId: "p50" },
      { dataKey: "p95", name: "p95", value: 3120.5, color: "#d95926", graphicalItemId: "p95" },
    ];
    const { container, rerender } = render(<Tip active payload={payload} label="Extraction" format={(v) => `${v} ms`} />);
    expect(container).toHaveTextContent("Extractionp501840 msp953120.5 ms");
    expect(container.querySelectorAll(".chart-tip__swatch")).toHaveLength(2);
    rerender(<Tip active payload={payload} label="Extraction" swatches={false} format={String} />);
    expect(container.querySelectorAll(".chart-tip__swatch")).toHaveLength(0);
    rerender(<Tip active={false} payload={payload} label="Extraction" format={String} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("writes rates with one decimal, as the evaluation report does", () => {
    expect(rate(0.3667)).toBe("36.7%");
    expect(rate(null)).toBe("—");
  });
});

describe("categories", () => {
  it("opens on Outcomes and shows one category's charts at a time", async () => {
    const { user } = await open();
    const outcomes = await screen.findByRole("tab", { name: "Outcomes" });
    expect(outcomes).toHaveAttribute("aria-selected", "true");
    const panel = screen.getByRole("tabpanel", { name: "Outcomes" });
    expect(within(panel).getByRole("region", { name: "Conversations by outcome" })).toBeInTheDocument();
    expect(within(panel).getByRole("region", { name: "Conversations by language" })).toBeInTheDocument();
    expect(within(panel).getAllByRole("region")).toHaveLength(2);
    await showTab(user, "Latency");
    expect(screen.getByRole("tab", { name: "Latency" })).toHaveAttribute("aria-selected", "true");
    expect(outcomes).toHaveAttribute("aria-selected", "false");
    expect(within(screen.getByRole("tabpanel", { name: "Latency" })).getAllByRole("region")).toHaveLength(1);
    expect(screen.queryByRole("region", { name: "Conversations by outcome" })).toBeNull();
    // the headline figures stay above every category
    expect(screen.getByRole("region", { name: "Headline figures" })).toBeInTheDocument();
  });

  it("moves between the tabs with the arrow keys, Home and End", async () => {
    const { user } = await open();
    const tab = (name: string) => screen.getByRole("tab", { name });
    await user.click(await screen.findByRole("tab", { name: "Outcomes" }));
    expect(tab("Outcomes")).toHaveAttribute("tabindex", "0");
    expect(tab("Cost")).toHaveAttribute("tabindex", "-1");
    await user.keyboard("{ArrowRight}");
    expect(tab("Automation")).toHaveFocus();
    expect(tab("Automation")).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{End}");
    expect(tab("Cost")).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(tab("Outcomes")).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(tab("Cost")).toHaveFocus();
    await user.keyboard("{Home}");
    expect(tab("Outcomes")).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowDown}"); // not a tab key: nothing moves
    expect(tab("Outcomes")).toHaveFocus();
  });

  it("stays on the same tab after a Refresh", async () => {
    const { api, user } = await open();
    await showTab(user, "Cost");
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(api.metrics).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("region", { name: "Cost" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Cost" })).toHaveAttribute("aria-selected", "true");
  });
});

describe("with empty data", () => {
  it("says each chart has nothing to show, and draws no chart", async () => {
    const { user } = await open(fakeAgentApi({ metrics: emptyMetrics }));
    await screen.findByRole("region", { name: "Conversations by outcome" });
    const nothingDrawn = () => {
      expect(document.querySelector(".recharts-wrapper")).toBeNull();
      expect(screen.queryByRole("table")).toBeNull();
    };
    expect(within(section("Conversations by outcome")).getByText("No conversations yet.")).toBeInTheDocument();
    expect(within(section("Conversations by language")).getByText("No conversations yet.")).toBeInTheDocument();
    nothingDrawn();
    await showTab(user, "Automation");
    expect(within(section("Automation by tier")).getByText(/No conversation has reached a T1 or T2/)).toBeInTheDocument();
    nothingDrawn();
    await showTab(user, "Latency");
    expect(within(section("Latency p50 / p95")).getByText(/No turn with a measured latency/)).toBeInTheDocument();
    nothingDrawn();
    await showTab(user, "Cost");
    expect(within(section("Cost")).getByText(/No cost to divide yet/)).toBeInTheDocument();
    nothingDrawn();
    const headline = screen.getByRole("region", { name: "Headline figures" });
    expect(within(headline).getByText("Containment").parentElement).toHaveTextContent("—");
  });

  it("leaves out only the charts the data does not cover", async () => {
    const { user } = await open(
      fakeAgentApi({ metrics: { ...metrics, outcomes_by_tier: { T3: { ESCALATE: 7 } }, cost_per_attempted_case_usd: null } }),
    );
    expect((await screen.findByRole("region", { name: "Conversations by outcome" })).querySelector(".recharts-wrapper")).not.toBeNull();
    await showTab(user, "Automation");
    expect(within(section("Automation by tier")).getByText(/No conversation has reached/)).toBeInTheDocument();
    await showTab(user, "Cost");
    expect(table("Cost")).toHaveLength(2); // the header and the one figure left
  });
});

describe("loading, errors and refresh", () => {
  it("shows the loading state until the metrics arrive", async () => {
    const api = fakeAgentApi();
    let release: (value: typeof metrics) => void = () => {};
    api.metrics.mockImplementationOnce(() => new Promise((resolve) => (release = resolve)));
    await open(api);
    expect(await screen.findByRole("status")).toHaveTextContent("Loading");
    expect(screen.getByRole("button", { name: "Refresh" })).toBeDisabled();
    release(metrics);
    expect(await screen.findByRole("region", { name: "Headline figures" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled();
  });

  it.each([
    [new NetworkError("HTTP 503"), "could not answer"],
    [new NetworkError("Failed to fetch"), "Could not reach the server"],
  ])("explains a failure and retries (%s)", async (error, text) => {
    const api = fakeAgentApi();
    api.metrics.mockRejectedValueOnce(error);
    const { user } = await open(api);
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("region", { name: "Headline figures" })).toBeInTheDocument();
    expect(api.metrics).toHaveBeenCalledTimes(2);
  });

  it("asks again only on Refresh, and shows the new figures", async () => {
    const api = fakeAgentApi();
    const { user } = await open(api);
    await screen.findByRole("region", { name: "Headline figures" });
    expect(api.metrics).toHaveBeenCalledTimes(1);
    api.metrics.mockResolvedValueOnce({ ...metrics, conversations: 81, turns: 415 });
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    const headline = await screen.findByRole("region", { name: "Headline figures" });
    expect(within(headline).getByText("Conversations").parentElement).toHaveTextContent("81415 turns");
    expect(api.metrics).toHaveBeenCalledTimes(2);
  });

  it("a 403 sends the agent back to the sign-in", async () => {
    const api = fakeAgentApi();
    api.metrics.mockRejectedValueOnce(new ApiError(403));
    const user = userEvent.setup();
    render(<AgentApp api={api} />);
    await user.type(screen.getByLabelText("Agent access token"), AGENT_TOKEN);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText(/refused or has expired/)).toBeInTheDocument();
    expect(screen.getByLabelText("Agent access token")).toBeInTheDocument();
  });
});
