// The app's views by path, and the router's links.
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Root } from "./Root";
import { Link, navigate } from "./router";
import { fakeAgentApi } from "./test/fakeAgentApi";
import { fakeApi } from "./test/fakeApi";

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("Root", () => {
  it("shows the customer chat at / and the agent console under /agent", async () => {
    const api = fakeApi();
    const agentApi = fakeAgentApi();
    render(<Root api={api} agentApi={agentApi} />);
    expect(await screen.findByRole("heading", { name: "⟦login_title⟧" })).toBeInTheDocument();
    act(() => navigate("/agent"));
    expect(screen.getByRole("heading", { name: "Agent console" })).toBeInTheDocument();
    act(() => {
      window.history.replaceState(null, "", "/");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(await screen.findByRole("heading", { name: "⟦login_title⟧" })).toBeInTheDocument();
  });

  it("does not treat /agents as the console", () => {
    window.history.replaceState(null, "", "/agents");
    render(<Root api={fakeApi()} agentApi={fakeAgentApi()} />);
    expect(screen.queryByRole("heading", { name: "Agent console" })).toBeNull();
  });
});

describe("Link", () => {
  it("navigates in the page on a plain click and leaves modified clicks to the browser", () => {
    render(
      <Link to="/agent/traces" current>
        traces
      </Link>,
    );
    const link = screen.getByRole("link", { name: "traces" });
    expect(link).toHaveAttribute("aria-current", "page");
    expect(fireEvent.click(link, { ctrlKey: true })).toBe(true); // not prevented
    expect(window.location.pathname).toBe("/");
    expect(fireEvent.click(link)).toBe(false); // prevented: handled in the page
    expect(window.location.pathname).toBe("/agent/traces");
  });

  it("does not push the same location twice", () => {
    window.history.replaceState(null, "", "/agent");
    const before = window.history.length;
    navigate("/agent");
    expect(window.history.length).toBe(before);
  });
});
