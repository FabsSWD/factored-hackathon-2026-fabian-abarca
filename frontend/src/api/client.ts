// The chat's only way to the backend. The session token travels in the Authorization header,
// never in a URL, and is kept in memory only (never in localStorage, sessionStorage or cookies
// written here).
import type { components } from "./schema";

export type TurnResponse = components["schemas"]["TurnResponse"];
export type TurnStatus = TurnResponse["status"];
export type UiTexts = components["schemas"]["UiTexts"];
export type Language = "es" | "pt";

/** The API answered with an error status (401: the session is missing or expired). */
export class ApiError extends Error {
  constructor(public readonly status: number) {
    super(`HTTP ${status}`);
    this.name = "ApiError";
  }
}

/** No usable answer: the network failed, the request timed out, or the server failed (5xx,
 * 429). The same request can be sent again. */
export class NetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NetworkError";
  }
}

export interface Api {
  texts(language: Language): Promise<UiTexts>;
  requestCode(documentNumber: string): Promise<void>;
  verify(documentNumber: string, otp: string): Promise<string>;
  turn(message: string, conversationId: string | null, token: string | null): Promise<TurnResponse>;
  logout(token: string): Promise<void>;
}

type Fetch = typeof fetch;

const TURN_TIMEOUT_MS = 90_000; // a turn calls the language model: up to tens of seconds
const DEFAULT_TIMEOUT_MS = 15_000;

export function httpApi(fetchImpl: Fetch = (...args) => fetch(...args), base = ""): Api {
  async function call(
    path: string,
    init: RequestInit & { token?: string | null; timeoutMs?: number } = {},
  ): Promise<Response> {
    const { token, timeoutMs = DEFAULT_TIMEOUT_MS, ...rest } = init;
    const headers = new Headers(rest.headers);
    if (rest.body !== undefined) headers.set("Content-Type", "application/json");
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let response: Response;
    try {
      response = await fetchImpl(`${base}${path}`, {
        ...rest,
        headers,
        signal: controller.signal,
        credentials: "same-origin",
        cache: "no-store",
        referrerPolicy: "no-referrer",
      });
    } catch (error) {
      throw new NetworkError(error instanceof Error ? error.message : "network error");
    } finally {
      clearTimeout(timer);
    }
    if (response.status >= 500 || response.status === 429) {
      throw new NetworkError(`HTTP ${response.status}`);
    }
    if (!response.ok) throw new ApiError(response.status);
    return response;
  }

  return {
    async texts(language) {
      return (await call(`/api/ui/texts/${language}`)).json() as Promise<UiTexts>;
    },
    async requestCode(documentNumber) {
      await call("/auth/login", {
        method: "POST",
        body: JSON.stringify({ document_number: documentNumber }),
      });
    },
    async verify(documentNumber, otp) {
      const response = await call("/auth/verify", {
        method: "POST",
        body: JSON.stringify({ document_number: documentNumber, otp }),
      });
      const issued = (await response.json()) as { access_token: string };
      return issued.access_token;
    },
    async turn(message, conversationId, token) {
      const body: { message: string; conversation_id?: string } = { message };
      if (conversationId) body.conversation_id = conversationId;
      const response = await call("/api/turn", {
        method: "POST",
        body: JSON.stringify(body),
        token,
        timeoutMs: TURN_TIMEOUT_MS,
      });
      return response.json() as Promise<TurnResponse>;
    },
    async logout(token) {
      await call("/auth/logout", { method: "POST", token });
    },
  };
}
