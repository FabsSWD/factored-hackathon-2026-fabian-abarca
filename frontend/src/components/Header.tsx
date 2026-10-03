import type { Language } from "../api/client";
import { useTexts } from "../texts";

export function Header({
  onLanguage,
  onLogout,
}: {
  onLanguage: (language: Language) => void;
  onLogout: (() => void) | null;
}) {
  const { t, language } = useTexts();
  return (
    <header className="nav">
      <div className="nav__inner">
        <span className="brand">
          <span className="brand__mark" aria-hidden="true" />
          <span className="brand__name">{t("app_name")}</span>
        </span>
        <div className="nav__actions">
          <label className="lang">
            <span className="lang__label">{t("language_label")}</span>
            <select
              className="lang__select"
              value={language}
              onChange={(event) => onLanguage(event.target.value as Language)}
            >
              <option value="es">{t("language_es")}</option>
              <option value="pt">{t("language_pt")}</option>
            </select>
          </label>
          {onLogout && (
            <button type="button" className="button button--ghost" onClick={onLogout}>
              {t("logout")}
            </button>
          )}
        </div>
      </div>
    </header>
  );
}
