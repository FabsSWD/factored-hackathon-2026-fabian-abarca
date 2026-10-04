// The conversation state follows the backend's status for each reply.
import { describe, expect, it } from "vitest";

import type { TurnResponse } from "./api/client";
import { chatReducer, initialChat, type ChatState } from "./chat";
import { reply } from "./test/fakeApi";

const sending = (): ChatState => chatReducer(initialChat, { type: "send", id: "a", text: "hola" });
const answered = (overrides: Partial<TurnResponse>) =>
  chatReducer(sending(), { type: "reply", id: "a", reply: reply(overrides), replyId: "b" });

describe("chatReducer", () => {
  it("welcomes only an empty conversation", () => {
    const welcomed = chatReducer(initialChat, { type: "welcome", text: "Bienvenido" });
    expect(welcomed.messages).toHaveLength(1);
    expect(chatReducer(welcomed, { type: "welcome", text: "otra vez" })).toBe(welcomed);
  });

  it("a message is sending until the reply arrives", () => {
    const state = sending();
    expect(state.busy).toBe(true);
    expect(state.messages[0]?.delivery).toBe("sending");
    const replied = answered({});
    expect(replied.busy).toBe(false);
    expect(replied.conversationId).toBe("CONV-1");
    expect(replied.messages.map((m) => m.delivery)).toEqual(["sent", undefined]);
  });

  it("keeps the case reference only for a created case", () => {
    expect(answered({ status: "case_created", case_reference: "DSP-1" }).messages[1]?.caseReference).toBe("DSP-1");
    expect(answered({ status: "in_progress", case_reference: "DSP-1" }).messages[1]?.caseReference).toBeNull();
    expect(answered({ status: "case_created", case_reference: undefined }).messages[1]?.caseReference).toBeNull();
  });

  it("a handoff stays", () => {
    const handed = answered({ status: "handed_off", handed_off: true });
    expect(handed.handedOff).toBe(true);
    expect(handed.messages[1]?.transferred).toBe(true);
    expect(handed.messages[1]?.handoffReference).toBeNull();
  });

  it("keeps the tracking number of the handoff", () => {
    const handed = answered({ status: "handed_off", handed_off: true, handoff_reference: "HO-20261003-000007" });
    expect(handed.messages[1]?.handoffReference).toBe("HO-20261003-000007");
    expect(answered({ handoff_reference: "HO-1" }).messages[1]?.handoffReference).toBeNull();
  });

  it("a reply that asks to log in remembers to say so after it", () => {
    const asked = answered({ status: "authentication_required" });
    expect(asked.loginReason).toBe("authentication_requested");
    expect(asked.sayLoggedIn).toBe(true);
    const back = chatReducer(asked, { type: "logged_in" });
    expect(back.loginReason).toBeNull();
    expect(back.sayLoggedIn).toBe(false);
  });

  it("a 401 keeps the message for after the login", () => {
    const expired = chatReducer(sending(), { type: "unauthorized", id: "a" });
    expect(expired).toMatchObject({ busy: false, loginReason: "session_expired", resendId: "a" });
    expect(expired.messages[0]?.delivery).toBe("waiting_login");
    const resent = chatReducer(expired, { type: "resend", id: "a" });
    expect(resent).toMatchObject({ busy: true, resendId: null });
    expect(resent.messages[0]?.delivery).toBe("sending");
  });

  it("a network failure marks the message", () => {
    const failed = chatReducer(sending(), { type: "failed", id: "a" });
    expect(failed.busy).toBe(false);
    expect(failed.messages[0]?.delivery).toBe("failed");
  });

  it("reset starts over", () => {
    expect(chatReducer(sending(), { type: "reset" })).toBe(initialChat);
  });
});
