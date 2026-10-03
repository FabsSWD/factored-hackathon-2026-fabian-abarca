// The console's HTTP client: the agent token goes in the Authorization header only; IDs and
// filters are the only things in the URL.
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { httpAgentApi } from "./client";

const TOKEN = "agent-secret-token-xyz";

function answering(status: number, body: unknown = []) {
  return vi.fn<typeof fetch>(async () => new Response(status === 204 ? null : JSON.stringify(body), { status }));
}

function sent(fetchMock: ReturnType<typeof answering>, index = 0) {
  const [url, init] = fetchMock.mock.calls[index] as [string, RequestInit];
  return { url, headers: new Headers(init.headers) };
}

describe("httpAgentApi", () => {
  it("checks the session with the token in the header", async () => {
    const fetchMock = answering(204);
    await httpAgentApi(fetchMock).session(TOKEN);
    const { url, headers } = sent(fetchMock);
    expect(url).toBe("/api/agent/session");
    expect(headers.get("Authorization")).toBe(`Bearer ${TOKEN}`);
  });

  it("refuses a token without the agent role", async () => {
    await expect(httpAgentApi(answering(403)).session("customer")).rejects.toEqual(new ApiError(403));
  });

  it("lists the queue with only the filters that are set", async () => {
    const fetchMock = answering(200, []);
    const api = httpAgentApi(fetchMock, "http://api");
    await api.handoffs({ queue: "fraud", priority: null }, TOKEN);
    await api.handoffs({}, TOKEN);
    expect(sent(fetchMock, 0).url).toBe("http://api/api/agent/handoffs?queue=fraud");
    expect(sent(fetchMock, 1).url).toBe("http://api/api/agent/handoffs");
  });

  it("reads a packet, the traces and a trace, escaping the IDs", async () => {
    const fetchMock = answering(200, {});
    const api = httpAgentApi(fetchMock);
    await api.handoff("HO-1/x", TOKEN);
    await api.traces({ search: "CONV 1", outcome: "ESCALATE", offset: 20, limit: 20 }, TOKEN);
    await api.trace("TRC?1", TOKEN);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/agent/handoffs/HO-1%2Fx",
      "/api/agent/traces?search=CONV+1&outcome=ESCALATE&offset=20&limit=20",
      "/api/audit/TRC%3F1",
    ]);
    for (const [url, init] of fetchMock.mock.calls) {
      expect(String(url)).not.toContain(TOKEN);
      expect(new Headers(init?.headers).get("Authorization")).toBe(`Bearer ${TOKEN}`);
    }
  });

  it("uses the global fetch by default", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("[]", { status: 200 }));
    try {
      await httpAgentApi().traces({}, TOKEN);
      expect(spy).toHaveBeenCalledOnce();
    } finally {
      spy.mockRestore();
    }
  });
});
