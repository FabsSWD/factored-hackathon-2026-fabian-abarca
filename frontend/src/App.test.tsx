// M14: every state of the Customer Chat, the login flow, sending and receiving, the 401 and the
// network error, texts from the backend only, and no token in a URL or in storage.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { ApiError, NetworkError } from "./api/client";
import { App } from "./App";
import { es, fakeApi, offline, pt, reply, unauthorized } from "./test/fakeApi";

type Fake = ReturnType<typeof fakeApi>;

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

async function start(api: Fake = fakeApi()) {
  const user = userEvent.setup();
  render(<App api={api} />);
  await screen.findByRole("heading", { name: es("login_title") });
  return { api, user };
}

async function login(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText(es("document_label")), "X1234567");
  await user.click(screen.getByRole("button", { name: es("request_code") }));
  await user.type(await screen.findByLabelText(es("otp_label")), "123456");
  await user.click(screen.getByRole("button", { name: es("verify_code") }));
  await screen.findByRole("log", { name: es("conversation_label") });
}

async function say(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(screen.getByLabelText(es("input_label")), text);
  await user.click(screen.getByRole("button", { name: es("send") }));
}

describe("login", () => {
  it("asks for the document, then the code, then opens the conversation", async () => {
    const { api, user } = await start();
    expect(screen.getByText(es("login_intro"))).toBeInTheDocument();
    await user.type(screen.getByLabelText(es("document_label")), " X1234567 ");
    await user.click(screen.getByRole("button", { name: es("request_code") }));
    expect(api.requestCode).toHaveBeenCalledWith("X1234567");
    expect(await screen.findByRole("heading", { name: es("otp_title") })).toBeInTheDocument();
    await user.type(screen.getByLabelText(es("otp_label")), "123456");
    await user.click(screen.getByRole("button", { name: es("verify_code") }));
    expect(api.verify).toHaveBeenCalledWith("X1234567", "123456");
    const log = await screen.findByRole("log", { name: es("conversation_label") });
    expect(within(log).getByText(es("welcome"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: es("logout") })).toBeInTheDocument();
  });

  it("goes back to the document step", async () => {
    const { user } = await start();
    await user.type(screen.getByLabelText(es("document_label")), "X1");
    await user.click(screen.getByRole("button", { name: es("request_code") }));
    await user.click(await screen.findByRole("button", { name: es("back") }));
    expect(screen.getByRole("heading", { name: es("login_title") })).toBeInTheDocument();
  });

  it.each([
    [new ApiError(401), "login_error_invalid"],
    [new NetworkError("HTTP 429"), "login_error_rate_limited"],
    [new NetworkError("Failed to fetch"), "login_error_generic"],
  ])("explains a failed code (%s)", async (error, key) => {
    const api = fakeApi();
    api.verifyError = error;
    const { user } = await start(api);
    await user.type(screen.getByLabelText(es("document_label")), "X1");
    await user.click(screen.getByRole("button", { name: es("request_code") }));
    await user.type(await screen.findByLabelText(es("otp_label")), "000000");
    await user.click(screen.getByRole("button", { name: es("verify_code") }));
    expect(await screen.findByRole("alert")).toHaveTextContent(es(key));
    expect(screen.getByLabelText(es("otp_label"))).toHaveAttribute("aria-invalid", "true");
  });
});

describe("conversation", () => {
  it("sends a message, shows the reply and keeps the conversation", async () => {
    const api = fakeApi();
    api.turns = [reply({ reply: "¿Qué cargo es?" }), reply({ reply: "Gracias", turn_index: 1 })];
    const { user } = await start(api);
    await login(user);
    await say(user, "No reconozco un cargo");
    expect(await screen.findByText("¿Qué cargo es?")).toBeInTheDocument();
    expect(screen.getByText("No reconozco un cargo")).toBeInTheDocument();
    await say(user, "El de ayer");
    await screen.findByText("Gracias");
    expect(api.sent.map((s) => s.conversationId)).toEqual([null, "CONV-1"]);
    expect(api.sent.every((s) => s.token === "secret-token-123")).toBe(true);
  });

  it("shows the typing cursor while the reply is on its way", async () => {
    const api = fakeApi();
    let answer: (value: ReturnType<typeof reply>) => void = () => {};
    api.turn.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)));
    const { user } = await start(api);
    await login(user);
    await say(user, "Hola");
    expect(screen.getByRole("status", { name: es("typing") })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: es("send") })).toBeDisabled();
    answer(reply());
    await waitFor(() => expect(screen.queryByRole("status", { name: es("typing") })).toBeNull());
  });

  it("offers quick replies when the backend awaits a confirmation", async () => {
    const api = fakeApi();
    api.turns = [reply({ status: "awaiting_confirmation", reply: "Resumen" }), reply()];
    const { user } = await start(api);
    await login(user);
    await say(user, "Ese cargo");
    const group = await screen.findByRole("group", { name: es("confirm_hint") });
    await user.click(within(group).getByRole("button", { name: es("confirm_yes") }));
    await waitFor(() => expect(api.sent.at(-1)?.message).toBe(es("confirm_yes")));
  });

  it("shows the case reference once the case is created", async () => {
    const api = fakeApi();
    api.turns = [
      reply({ status: "case_created", case_reference: "DSP-20261003-000001", reply: "Registrada" }),
    ];
    const { user } = await start(api);
    await login(user);
    await say(user, "Sí, confirmo");
    const badge = await screen.findByRole("group", { name: es("case_created_title") });
    expect(within(badge).getByText("DSP-20261003-000001")).toBeInTheDocument();
    expect(within(badge).getByText(es("case_reference_label"))).toBeInTheDocument();
    expect(screen.getByLabelText(es("input_label"))).toBeEnabled(); // another dispute may follow
  });

  it("disables the input with the transfer notice after a handoff", async () => {
    const api = fakeApi();
    api.turns = [reply({ status: "handed_off", handed_off: true, reply: "Lo transfiero" })];
    const { user } = await start(api);
    await login(user);
    await say(user, "Quiero hablar con una persona");
    expect(await screen.findByText(es("handed_off_notice"))).toBeInTheDocument();
    expect(screen.getByText(es("handed_off_title"))).toBeInTheDocument();
    expect(screen.getByLabelText(es("input_label"))).toBeDisabled();
    expect(screen.queryByRole("button", { name: es("send") })).toBeNull();
    await user.click(screen.getByRole("button", { name: es("new_conversation") }));
    expect(screen.getByLabelText(es("input_label"))).toBeEnabled();
    expect(screen.queryByText("Lo transfiero")).toBeNull();
  });
});

describe("authentication inside a conversation", () => {
  it("a 401 goes back to the login and sends the message again after it", async () => {
    const api = fakeApi();
    api.turns = [unauthorized(), reply({ reply: "Seguimos" })];
    const { user } = await start(api);
    await login(user);
    await say(user, "Mi mensaje");
    expect(await screen.findByRole("status")).toHaveTextContent(es("session_expired"));
    expect(screen.getByRole("heading", { name: es("login_title") })).toBeInTheDocument();
    expect(screen.getByText("Mi mensaje")).toBeInTheDocument(); // the conversation stays
    await login(user);
    expect(await screen.findByText("Seguimos")).toBeInTheDocument();
    expect(api.sent.map((s) => s.message)).toEqual(["Mi mensaje", "Mi mensaje"]);
  });

  it("a reply that asks to log in opens the login and resumes after it", async () => {
    const api = fakeApi();
    api.turns = [
      reply({ status: "authentication_required", reply: "Necesito verificar su identidad" }),
      reply({ reply: "Gracias, continuemos" }),
    ];
    const { user } = await start(api);
    await login(user);
    await say(user, "Un cargo");
    expect(await screen.findByText(es("authentication_requested"))).toBeInTheDocument();
    await login(user);
    expect(await screen.findByText("Gracias, continuemos")).toBeInTheDocument();
    expect(api.sent.at(-1)?.message).toBe(es("after_login_message"));
  });
});

describe("network errors", () => {
  it("marks the message and sends it again on retry", async () => {
    const api = fakeApi();
    api.turns = [offline(), reply({ reply: "Llegó" })];
    const { user } = await start(api);
    await login(user);
    await say(user, "Un cargo raro");
    expect(await screen.findByRole("alert")).toHaveTextContent(es("network_error"));
    await user.click(screen.getByRole("button", { name: es("retry") }));
    expect(await screen.findByText("Llegó")).toBeInTheDocument();
    expect(screen.queryByText(es("network_error"))).toBeNull();
    expect(api.sent.map((s) => s.message)).toEqual(["Un cargo raro", "Un cargo raro"]);
  });

  it("without the interface texts shows the bilingual fallback and retries", async () => {
    const api = fakeApi();
    api.failTexts = true;
    const user = userEvent.setup();
    render(<App api={api} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("No pudimos conectar");
    api.failTexts = false;
    await user.click(screen.getByRole("button"));
    expect(await screen.findByRole("heading", { name: es("login_title") })).toBeInTheDocument();
  });
});

describe("texts and language", () => {
  it("shows only texts from the backend, and switches to Portuguese", async () => {
    const api = fakeApi();
    api.turns = [reply({ reply: "RESPUESTA" })];
    const { user } = await start(api);
    await login(user);
    await say(user, "MENSAJE");
    await screen.findByText("RESPUESTA");
    // Remove every backend text and the conversation itself: nothing readable may remain.
    const visible = document.body.textContent ?? "";
    const rest = visible.replace(/⟦[a-z_]+⟧/g, "").replace("RESPUESTA", "").replace("MENSAJE", "");
    expect(rest.replace(/[\s·]/g, "")).toBe("");
    for (const element of document.querySelectorAll("[aria-label],[placeholder]")) {
      const text = element.getAttribute("aria-label") ?? element.getAttribute("placeholder") ?? "";
      expect(text).toMatch(/^⟦[a-z_]+⟧$/);
    }
    expect(document.title).toBe(es("app_name"));
    expect(document.documentElement.lang).toBe("es");

    await user.selectOptions(screen.getByLabelText(es("language_label")), "pt");
    expect(await screen.findByText(pt("chat_title"))).toBeInTheDocument();
    expect(api.texts).toHaveBeenLastCalledWith("pt");
    expect(document.documentElement.lang).toBe("pt");
    expect(window.localStorage.getItem("chat.language")).toBe("pt");
  });

  it("never puts the token in a URL or in storage", async () => {
    const { user } = await start();
    await login(user);
    await say(user, "Hola");
    await screen.findByText("Respuesta del asistente");
    const stored = [
      ...Object.values(window.localStorage),
      ...Object.values(window.sessionStorage),
      document.cookie,
      window.location.href,
    ].join(" ");
    expect(stored).not.toContain("secret-token-123");
  });

  it("logs out and returns to the login", async () => {
    const { api, user } = await start();
    await login(user);
    await user.click(screen.getByRole("button", { name: es("logout") }));
    expect(api.logout).toHaveBeenCalledWith("secret-token-123");
    expect(await screen.findByRole("heading", { name: es("login_title") })).toBeInTheDocument();
  });
});
