// Where the customer writes. A yes/no question (the case summary or the card block offer) adds
// two quick replies; after a transfer the input is disabled and says why.
import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { useTexts } from "../texts";

export function Composer({
  disabled,
  handedOff,
  confirming,
  onSend,
  onNewConversation,
}: {
  disabled: boolean;
  handedOff: boolean;
  confirming: boolean;
  onSend: (text: string) => void;
  onNewConversation: () => void;
}) {
  const { t } = useTexts();
  const [text, setText] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!disabled && !handedOff) input.current?.focus();
  }, [disabled, handedOff]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const message = text.trim();
    if (!message || disabled || handedOff) return;
    setText("");
    onSend(message);
  }

  if (handedOff) {
    return (
      <div className="composer composer--closed" role="status">
        <p className="composer__notice">{t("handed_off_notice")}</p>
        <label className="sr-only" htmlFor="message">
          {t("input_label")}
        </label>
        <textarea id="message" className="composer__input" disabled rows={1} />
        <button type="button" className="button button--ghost" onClick={onNewConversation}>
          {t("new_conversation")}
        </button>
      </div>
    );
  }

  return (
    <form className="composer" onSubmit={submit}>
      {confirming && (
        <div className="quick" role="group" aria-label={t("confirm_hint")}>
          <button
            type="button"
            className="button button--copper"
            disabled={disabled}
            onClick={() => onSend(t("confirm_yes"))}
          >
            {t("confirm_yes")}
          </button>
          <button
            type="button"
            className="button button--ghost"
            disabled={disabled}
            onClick={() => onSend(t("confirm_no"))}
          >
            {t("confirm_no")}
          </button>
        </div>
      )}
      <div className="composer__row">
        <label className="sr-only" htmlFor="message">
          {t("input_label")}
        </label>
        <textarea
          id="message"
          ref={input}
          className="composer__input"
          rows={1}
          maxLength={2000}
          placeholder={t("input_placeholder")}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) submit(event);
          }}
        />
        <button type="submit" className="button button--copper" disabled={disabled || !text.trim()}>
          {t("send")}
        </button>
      </div>
    </form>
  );
}
