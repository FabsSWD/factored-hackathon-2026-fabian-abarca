// The conversation as the customer sees it, kept in memory only. Its state follows the backend's
// `status` for each reply; the texts are the replies themselves.
import type { TurnResponse, TurnStatus } from "./api/client";

export type Delivery = "sending" | "sent" | "failed" | "waiting_login";

export interface Message {
  id: string;
  role: "assistant" | "customer";
  text: string;
  delivery?: Delivery; // customer messages only
  caseReference?: string | null; // a case created and read back (COM-04)
  transferred?: boolean; // the reply that handed the conversation to an agent
}

/** Why the login is shown inside a conversation that already started. */
export type LoginReason = "session_expired" | "authentication_requested";

export interface ChatState {
  conversationId: string | null;
  messages: Message[];
  status: TurnStatus | null;
  handedOff: boolean;
  busy: boolean;
  loginReason: LoginReason | null;
  // After the login: the message to send again (a 401), or a note that the backend asked for it.
  resendId: string | null;
  sayLoggedIn: boolean;
}

export type ChatAction =
  | { type: "welcome"; text: string }
  | { type: "send"; id: string; text: string }
  | { type: "resend"; id: string }
  | { type: "reply"; id: string; reply: TurnResponse; replyId: string }
  | { type: "failed"; id: string }
  | { type: "unauthorized"; id: string }
  | { type: "logged_in" }
  | { type: "reset" };

export const initialChat: ChatState = {
  conversationId: null,
  messages: [],
  status: null,
  handedOff: false,
  busy: false,
  loginReason: null,
  resendId: null,
  sayLoggedIn: false,
};

function deliver(messages: Message[], id: string, delivery: Delivery): Message[] {
  return messages.map((m) => (m.id === id ? { ...m, delivery } : m));
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "welcome":
      if (state.messages.length > 0) return state;
      return { ...state, messages: [{ id: "welcome", role: "assistant", text: action.text }] };
    case "send":
      return {
        ...state,
        busy: true,
        messages: [
          ...state.messages,
          { id: action.id, role: "customer", text: action.text, delivery: "sending" },
        ],
      };
    case "resend":
      return { ...state, busy: true, resendId: null, messages: deliver(state.messages, action.id, "sending") };
    case "reply": {
      const { reply } = action;
      const needsLogin = reply.status === "authentication_required";
      return {
        ...state,
        busy: false,
        conversationId: reply.conversation_id,
        status: reply.status,
        handedOff: state.handedOff || reply.handed_off,
        loginReason: needsLogin ? "authentication_requested" : state.loginReason,
        sayLoggedIn: needsLogin || state.sayLoggedIn,
        messages: [
          ...deliver(state.messages, action.id, "sent"),
          {
            id: action.replyId,
            role: "assistant",
            text: reply.reply,
            caseReference: reply.status === "case_created" ? (reply.case_reference ?? null) : null,
            transferred: reply.handed_off,
          },
        ],
      };
    }
    case "failed":
      return { ...state, busy: false, messages: deliver(state.messages, action.id, "failed") };
    case "unauthorized":
      // 401: back to the login; the message waits and is sent again once logged in.
      return {
        ...state,
        busy: false,
        loginReason: "session_expired",
        resendId: action.id,
        messages: deliver(state.messages, action.id, "waiting_login"),
      };
    case "logged_in":
      return { ...state, loginReason: null, sayLoggedIn: false };
    case "reset":
      return initialChat;
  }
}
