// Login with the document number and a one-time code (GATE-02). The document is kept only while
// the code is asked for; the token goes back to the app, which keeps it in memory.
import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { ApiError, type Api } from "../api/client";
import { useTexts } from "../texts";

type Step = "document" | "code";

function errorKey(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) return "login_error_invalid";
  if (error instanceof Error && error.message === "HTTP 429") return "login_error_rate_limited";
  return "login_error_generic";
}

export function LoginPanel({
  api,
  notice,
  onToken,
}: {
  api: Api;
  notice: string | null;
  onToken: (token: string) => void;
}) {
  const { t } = useTexts();
  const [step, setStep] = useState<Step>("document");
  const [documentNumber, setDocumentNumber] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const field = useRef<HTMLInputElement>(null);

  useEffect(() => field.current?.focus(), [step]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (step === "document") {
        await api.requestCode(documentNumber.trim());
        setStep("code");
      } else {
        const token = await api.verify(documentNumber.trim(), code.trim());
        setDocumentNumber("");
        setCode("");
        onToken(token);
      }
    } catch (caught) {
      setError(errorKey(caught));
    } finally {
      setBusy(false);
    }
  }

  const isDocument = step === "document";
  return (
    <section className="card login reveal" aria-labelledby="login-title">
      <h1 id="login-title" className="title">
        {t(isDocument ? "login_title" : "otp_title")}
      </h1>
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      <p className="muted">{t(isDocument ? "login_intro" : "otp_intro")}</p>
      <form className="login__form" onSubmit={submit} noValidate>
        <label className="field">
          <span className="field__label">{t(isDocument ? "document_label" : "otp_label")}</span>
          <input
            ref={field}
            className="field__input"
            name={isDocument ? "document" : "otp"}
            value={isDocument ? documentNumber : code}
            onChange={(event) => (isDocument ? setDocumentNumber : setCode)(event.target.value)}
            autoComplete={isDocument ? "username" : "one-time-code"}
            inputMode={isDocument ? "text" : "numeric"}
            required
            maxLength={isDocument ? 32 : 16}
            aria-invalid={error !== null}
            aria-describedby={error ? "login-error" : undefined}
          />
        </label>
        {error && (
          <p id="login-error" className="error" role="alert">
            {t(error)}
          </p>
        )}
        <div className="login__actions">
          {!isDocument && (
            <button
              type="button"
              className="button button--ghost"
              onClick={() => {
                setStep("document");
                setCode("");
                setError(null);
              }}
            >
              {t("back")}
            </button>
          )}
          <button
            type="submit"
            className="button button--copper"
            disabled={busy || (isDocument ? !documentNumber.trim() : !code.trim())}
          >
            {t(isDocument ? "request_code" : "verify_code")}
          </button>
        </div>
      </form>
    </section>
  );
}
