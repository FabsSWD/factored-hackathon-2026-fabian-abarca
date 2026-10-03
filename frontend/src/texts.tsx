// The interface texts come from the backend (GET /api/ui/texts/{language}); the chat writes none
// of its own. A key the catalog does not have shows as the key itself, so a gap is visible.
import { createContext, useContext } from "react";
import type { ReactNode } from "react";

import type { Language, UiTexts } from "./api/client";

export type Translate = (key: string) => string;

interface TextsValue {
  language: Language;
  t: Translate;
}

const TextsContext = createContext<TextsValue | null>(null);

export function translator(catalog: UiTexts): Translate {
  return (key) => catalog.texts[key] ?? key;
}

export function TextsProvider({
  catalog,
  language,
  children,
}: {
  catalog: UiTexts;
  language: Language;
  children: ReactNode;
}) {
  return (
    <TextsContext.Provider value={{ language, t: translator(catalog) }}>
      {children}
    </TextsContext.Provider>
  );
}

export function useTexts(): TextsValue {
  const value = useContext(TextsContext);
  if (value === null) throw new Error("useTexts outside TextsProvider");
  return value;
}
