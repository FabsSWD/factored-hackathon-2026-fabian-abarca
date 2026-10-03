// Sign-in with the agent access token (AGENT_API_TOKEN). The token is checked against the
// backend and handed to the console, which keeps it in memory only: never in the bundle, a
// URL or browser storage.
import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { ApiError } from "../api/client";
import type { AgentApi } from "./client";

function message(error: unknown): string {
  if (error instanceof ApiError && (error.status === 403 || error.status === 401)) {
    return "This token does not have the agent role.";
  }
  if (error instanceof Error && /^HTTP 50[0-9]$/.test(error.message)) {
    return "The agent console is not available on this server.";
  }
  return "Could not reach the server. Try again.";
}

export function AgentLogin({
  api,
  notice,
  onToken,
}: {
  api: AgentApi;
  notice: string | null;
  onToken: (token: string) => void;
}) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const field = useRef<HTMLInputElement>(null);

  useEffect(() => field.current?.focus(), []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const given = token.trim();
    setBusy(true);
    setError(null);
    try {
      await api.session(given);
      setToken("");
      onToken(given);
    } catch (caught) {
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card login reveal" aria-labelledby="agent-login-title">
      <h1 id="agent-login-title" className="title">
        Agent console
      </h1>
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      <p className="muted">
        Sign in with the agent access token. It is kept in this tab&apos;s memory only and is never
        stored in the browser.
      </p>
      <form className="login__form" onSubmit={submit} noValidate>
        <label className="field">
          <span className="field__label">Agent access token</span>
          <input
            ref={field}
            className="field__input"
            type="password"
            name="agent-token"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            required
            aria-invalid={error !== null}
            aria-describedby={error ? "agent-login-error" : undefined}
          />
        </label>
        {error && (
          <p id="agent-login-error" className="error" role="alert">
            {error}
          </p>
        )}
        <div className="login__actions">
          <button type="submit" className="button button--copper" disabled={busy || !token.trim()}>
            Sign in
          </button>
        </div>
      </form>
    </section>
  );
}
