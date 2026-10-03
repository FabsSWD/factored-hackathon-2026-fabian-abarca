// Customer Chat (M14): login with a one-time code, a language selector, and the conversation.
//
// - Every text comes from the backend: the interface's from GET /api/ui/texts/{language}, the
//   conversation's from /api/turn.
// - The session token lives in memory only and goes in the Authorization header, never in a URL.
// - A 401, or a reply that asks to log in, shows the login again and keeps the conversation; a
//   message that waited for the login is sent again after it.
// - A network error marks the message and offers to send it again.
import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import { ApiError, type Api, type Language, type UiTexts } from "./api/client";
import { chatReducer, initialChat } from "./chat";
import { Composer } from "./components/Composer";
import { Conversation } from "./components/Conversation";
import { Header } from "./components/Header";
import { LoginPanel } from "./components/LoginPanel";
import { Unreachable } from "./components/Unreachable";
import { TextsProvider, translator } from "./texts";

const LANGUAGE_KEY = "chat.language"; // a preference, never a secret

function storedLanguage(): Language {
  try {
    const value = window.localStorage.getItem(LANGUAGE_KEY);
    return value === "pt" ? "pt" : "es";
  } catch {
    return "es";
  }
}

function storeLanguage(language: Language): void {
  try {
    window.localStorage.setItem(LANGUAGE_KEY, language);
  } catch {
    // private windows and blocked storage: the language just is not remembered
  }
}

let counter = 0;
const nextId = () => `m${++counter}`;

export function App({ api }: { api: Api }) {
  const [language, setLanguage] = useState<Language>(storedLanguage);
  const [catalog, setCatalog] = useState<UiTexts | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [token, setToken] = useState<string | null>(null);
  const [chat, dispatch] = useReducer(chatReducer, initialChat);
  const [attempt, setAttempt] = useState(0); // a new attempt to load the texts
  const tokenRef = useRef<string | null>(null);

  useEffect(() => {
    tokenRef.current = token;
  }, [token]);

  useEffect(() => {
    let current = true;
    api
      .texts(language)
      .then((texts) => {
        if (!current) return;
        setCatalog(texts);
        setUnreachable(false);
      })
      .catch(() => {
        if (current) setUnreachable(true);
      });
    return () => {
      current = false;
    };
  }, [api, language, attempt]);

  useEffect(() => {
    document.documentElement.lang = language;
    if (catalog) document.title = translator(catalog)("app_name");
  }, [language, catalog]);

  const deliver = useCallback(
    async (id: string, text: string) => {
      try {
        const reply = await api.turn(text, chat.conversationId, tokenRef.current);
        if (reply.status === "authentication_required") {
          tokenRef.current = null;
          setToken(null);
        }
        dispatch({ type: "reply", id, reply, replyId: nextId() });
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          tokenRef.current = null;
          setToken(null);
          dispatch({ type: "unauthorized", id });
        } else {
          dispatch({ type: "failed", id });
        }
      }
    },
    [api, chat.conversationId],
  );

  const send = useCallback(
    (text: string) => {
      const id = nextId();
      dispatch({ type: "send", id, text });
      void deliver(id, text);
    },
    [deliver],
  );

  const retry = useCallback(
    (id: string) => {
      const message = chat.messages.find((m) => m.id === id);
      if (!message) return;
      dispatch({ type: "resend", id });
      void deliver(id, message.text);
    },
    [chat.messages, deliver],
  );

  function loggedIn(newToken: string) {
    tokenRef.current = newToken;
    setToken(newToken);
    const { resendId, sayLoggedIn } = chat;
    dispatch({ type: "logged_in" });
    if (!catalog) return;
    const t = translator(catalog);
    if (chat.messages.length === 0) dispatch({ type: "welcome", text: t("welcome") });
    if (resendId) retry(resendId);
    else if (sayLoggedIn) send(t("after_login_message"));
  }

  async function logout() {
    const current = tokenRef.current;
    setToken(null);
    dispatch({ type: "reset" });
    if (current) {
      try {
        await api.logout(current);
      } catch {
        // the session expires on its own; nothing to show
      }
    }
  }

  function changeLanguage(next: Language) {
    storeLanguage(next);
    setLanguage(next);
  }

  if (unreachable) {
    return (
      <main className="page page--center">
        <Unreachable
          onRetry={() => {
            setUnreachable(false);
            setAttempt((n) => n + 1);
          }}
        />
      </main>
    );
  }
  if (!catalog) {
    return <main className="page page--center" aria-busy="true" />;
  }

  const t = translator(catalog);
  const loginNotice = chat.loginReason ? t(chat.loginReason) : null;
  const showLogin = token === null && (chat.messages.length === 0 || chat.loginReason !== null);

  return (
    <TextsProvider catalog={catalog} language={language}>
      <div className="halo" aria-hidden="true" />
      <Header onLanguage={changeLanguage} onLogout={token ? () => void logout() : null} />
      <main className="page">
        {chat.messages.length === 0 && (
          <div className="hero reveal">
            <h2 className="hero__title gradient">{t("app_name")}</h2>
            <p className="muted">{t("app_tagline")}</p>
          </div>
        )}
        {chat.messages.length > 0 && (
          <section className="card chat reveal" aria-labelledby="chat-title">
            <h1 id="chat-title" className="chat__title">
              {t("chat_title")}
            </h1>
            <Conversation messages={chat.messages} busy={chat.busy} onRetry={retry} />
            {!showLogin && (
              <Composer
                disabled={chat.busy}
                handedOff={chat.handedOff}
                confirming={chat.status === "awaiting_confirmation"}
                onSend={send}
                onNewConversation={() => {
                  dispatch({ type: "reset" });
                  dispatch({ type: "welcome", text: t("welcome") });
                }}
              />
            )}
          </section>
        )}
        {showLogin && <LoginPanel api={api} notice={loginNotice} onToken={loggedIn} />}
      </main>
    </TextsProvider>
  );
}
