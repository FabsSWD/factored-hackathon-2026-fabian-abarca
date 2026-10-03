// The conversation's history. New replies are announced to screen readers (role="log"); a case
// created shows its reference, a transfer says so, and a message that could not be sent offers
// to send it again.
import { useEffect, useRef } from "react";

import type { Message } from "../chat";
import { useTexts } from "../texts";

export function Conversation({
  messages,
  busy,
  onRetry,
}: {
  messages: Message[];
  busy: boolean;
  onRetry: (id: string) => void;
}) {
  const { t } = useTexts();
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [messages.length, busy]);

  return (
    <div className="log" role="log" aria-live="polite" aria-label={t("conversation_label")}>
      <ol className="log__list">
        {messages.map((message) => (
          <li key={message.id} className={`bubble bubble--${message.role} reveal`}>
            <span className="bubble__who">
              {t(message.role === "assistant" ? "assistant_label" : "customer_label")}
            </span>
            <p className="bubble__text">{message.text}</p>
            {message.caseReference && (
              <div className="badge badge--success" role="group" aria-label={t("case_created_title")}>
                <span className="badge__title">{t("case_created_title")}</span>
                <span className="badge__line">
                  <span className="muted">{t("case_reference_label")}</span>{" "}
                  <strong className="reference">{message.caseReference}</strong>
                </span>
              </div>
            )}
            {message.transferred && (
              <div className="badge badge--handoff">
                <span className="badge__title">{t("handed_off_title")}</span>
              </div>
            )}
            {message.delivery === "failed" && (
              <div className="bubble__failure" role="alert">
                <span>{t("network_error")}</span>
                <button
                  type="button"
                  className="button button--small button--copper"
                  onClick={() => onRetry(message.id)}
                >
                  {t("retry")}
                </button>
              </div>
            )}
          </li>
        ))}
      </ol>
      {busy && (
        <div className="typing" role="status" aria-label={t("typing")}>
          <span className="typing__cursor" aria-hidden="true" />
        </div>
      )}
      <div ref={end} />
    </div>
  );
}
