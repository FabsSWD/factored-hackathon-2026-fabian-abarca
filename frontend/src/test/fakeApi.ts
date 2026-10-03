// A backend double for the chat's tests. Every interface text is a marker (⟦key⟧ or, in
// Portuguese, ⟪key⟫), so a test can prove the chat shows only what the backend sent.
import { vi } from "vitest";

import { ApiError, NetworkError, type Api, type Language, type TurnResponse, type UiTexts } from "../api/client";

export const KEYS = [
  "app_name", "app_tagline", "language_label", "language_es", "language_pt", "login_title",
  "login_intro", "document_label", "request_code", "otp_title", "otp_intro", "otp_label",
  "verify_code", "back", "login_error_invalid", "login_error_rate_limited", "login_error_generic",
  "session_expired", "authentication_requested", "after_login_message", "chat_title", "welcome",
  "conversation_label", "assistant_label", "customer_label", "input_label", "input_placeholder",
  "send", "typing", "confirm_hint", "confirm_yes", "confirm_no", "case_created_title",
  "case_reference_label", "handed_off_title", "handed_off_notice", "network_error", "retry",
  "new_conversation", "logout",
];

export const es = (key: string) => `⟦${key}⟧`;
export const pt = (key: string) => `⟪${key}⟫`;

export function catalog(language: Language): UiTexts {
  const mark = language === "es" ? es : pt;
  return { version: "1.0.0", language, texts: Object.fromEntries(KEYS.map((k) => [k, mark(k)])) };
}

export function reply(overrides: Partial<TurnResponse> = {}): TurnResponse {
  return {
    conversation_id: "CONV-1",
    turn_index: 0,
    reply: "Respuesta del asistente",
    language: "es",
    handed_off: false,
    trace_id: "TRC-1",
    status: "in_progress",
    case_reference: null,
    ...overrides,
  };
}

type TurnStep = TurnResponse | Error;

export interface FakeApi extends Api {
  turns: TurnStep[]; // answered in order; the last one repeats
  sent: { message: string; conversationId: string | null; token: string | null }[];
  failTexts: boolean;
  verifyError: Error | null;
}

export function fakeApi(): FakeApi & { [K in keyof Api]: ReturnType<typeof vi.fn> & Api[K] } {
  const api = {
    turns: [reply()] as TurnStep[],
    sent: [] as FakeApi["sent"],
    failTexts: false,
    verifyError: null as Error | null,
    texts: vi.fn(async (language: Language) => {
      if (api.failTexts) throw new NetworkError("down");
      return catalog(language);
    }),
    requestCode: vi.fn(async () => undefined),
    verify: vi.fn(async () => {
      if (api.verifyError) throw api.verifyError;
      return "secret-token-123";
    }),
    turn: vi.fn(async (message: string, conversationId: string | null, token: string | null) => {
      api.sent.push({ message, conversationId, token });
      const step = api.turns.length > 1 ? api.turns.shift()! : api.turns[0]!;
      if (step instanceof Error) throw step;
      return step;
    }),
    logout: vi.fn(async () => undefined),
  };
  return api as never;
}

export const unauthorized = () => new ApiError(401);
export const offline = () => new NetworkError("Failed to fetch");
