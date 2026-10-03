// The HTTP client: the token goes in the Authorization header and never in a URL, and each
// failure becomes the error the chat knows how to show.
import { describe, expect, it, vi } from "vitest";

import { ApiError, httpApi, NetworkError } from "./client";

type FetchMock = ReturnType<typeof vi.fn<typeof fetch>>;

function answering(status: number, body: unknown = {}): FetchMock {
  return vi.fn<typeof fetch>(async () =>
    new Response(status === 204 ? null : JSON.stringify(body), { status }),
  );
}

function request(fetchMock: FetchMock, index = 0) {
  const [url, init] = fetchMock.mock.calls[index] as [string, RequestInit];
  return { url, init, headers: new Headers(init.headers) };
}

describe("httpApi", () => {
  it("loads the interface texts of a language", async () => {
    const fetchMock = answering(200, { version: "1.0.0", language: "pt", texts: {} });
    const texts = await httpApi(fetchMock, "http://api").texts("pt");
    expect(texts.language).toBe("pt");
    expect(request(fetchMock).url).toBe("http://api/api/ui/texts/pt");
  });

  it("requests a code and verifies it without a token", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: "sent" }), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: "tok-1" }), { status: 200 }));
    const api = httpApi(fetchMock);
    await api.requestCode("X1");
    expect(await api.verify("X1", "123456")).toBe("tok-1");
    const login = request(fetchMock, 0);
    expect(login.url).toBe("/auth/login");
    expect(JSON.parse(login.init.body as string)).toEqual({ document_number: "X1" });
    expect(login.headers.get("Authorization")).toBeNull();
    const verify = request(fetchMock, 1);
    expect(JSON.parse(verify.init.body as string)).toEqual({ document_number: "X1", otp: "123456" });
  });

  it("sends a turn with the token in the header only", async () => {
    const fetchMock = answering(200, { conversation_id: "C1" });
    const api = httpApi(fetchMock);
    await api.turn("hola", null, "secret-token-123");
    await api.turn("otra", "C1", "secret-token-123");
    const first = request(fetchMock, 0);
    expect(first.url).toBe("/api/turn");
    expect(first.url).not.toContain("secret-token-123");
    expect(first.headers.get("Authorization")).toBe("Bearer secret-token-123");
    expect(first.headers.get("Content-Type")).toBe("application/json");
    expect(first.init.body).not.toContain("secret-token-123");
    expect(first.init.referrerPolicy).toBe("no-referrer");
    expect(first.init.cache).toBe("no-store");
    expect(JSON.parse(first.init.body as string)).toEqual({ message: "hola" });
    expect(JSON.parse(request(fetchMock, 1).init.body as string)).toEqual({
      message: "otra",
      conversation_id: "C1",
    });
  });

  it("sends a turn without a session when there is no token", async () => {
    const fetchMock = answering(200, {});
    await httpApi(fetchMock).turn("hola", null, null);
    expect(request(fetchMock).headers.get("Authorization")).toBeNull();
  });

  it("logs out with the token in the header", async () => {
    const fetchMock = answering(204);
    await httpApi(fetchMock).logout("secret-token-123");
    const { url, headers } = request(fetchMock);
    expect(url).toBe("/auth/logout");
    expect(headers.get("Authorization")).toBe("Bearer secret-token-123");
  });

  it("a 401 is an ApiError with its status", async () => {
    const error = await httpApi(answering(401)).turn("x", null, "t").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(401);
  });

  it.each([500, 503, 429])("a %s can be sent again: NetworkError", async (status) => {
    await expect(httpApi(answering(status)).texts("es")).rejects.toThrow(NetworkError);
  });

  it("a failed fetch is a NetworkError", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(httpApi(fetchMock).texts("es")).rejects.toThrow(NetworkError);
  });

  it("a non-Error rejection is still a NetworkError", async () => {
    const fetchMock = vi.fn<typeof fetch>(() => Promise.reject(new Error("boom")));
    fetchMock.mockImplementationOnce(() => Promise.reject("boom" as unknown as Error));
    await expect(httpApi(fetchMock).texts("es")).rejects.toThrow("network error");
  });

  it("aborts a request that takes too long", async () => {
    vi.useFakeTimers();
    try {
      const fetchMock = vi.fn<typeof fetch>(
        (_url, init) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
          }),
      );
      const pending = httpApi(fetchMock).texts("es");
      const check = expect(pending).rejects.toThrow(NetworkError);
      await vi.advanceTimersByTimeAsync(15_000);
      await check;
    } finally {
      vi.useRealTimers();
    }
  });

  it("uses the global fetch by default", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 200 }));
    try {
      await httpApi().texts("es");
      expect(spy).toHaveBeenCalledOnce();
    } finally {
      spy.mockRestore();
    }
  });
});
