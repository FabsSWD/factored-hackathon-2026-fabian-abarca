import js from "@eslint/js";
import jsxA11y from "eslint-plugin-jsx-a11y";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "coverage", "src/api/schema.d.ts"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: { globals: globals.browser },
    settings: { react: { version: "19" } },
    plugins: { react, "react-hooks": reactHooks, "jsx-a11y": jsxA11y },
    rules: {
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.flatConfigs.recommended.rules,
      // M14: every text the customer reads comes from the backend (GET /api/ui/texts or the
      // reply of /api/turn). No literal text in JSX, nor in the props that screen readers read.
      "react/jsx-no-literals": ["error", { noStrings: true, allowedStrings: ["·"], ignoreProps: true }],
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "JSXAttribute[name.name=/^(aria-label|aria-description|aria-roledescription|placeholder|title|alt)$/] > Literal",
          message: "Texts a person reads come from the backend (GET /api/ui/texts).",
        },
      ],
    },
  },
  {
    // Tests write the fake backend's texts; the one bilingual fallback is documented there.
    files: ["src/test/**", "src/**/*.test.tsx", "src/components/Unreachable.tsx"],
    rules: { "react/jsx-no-literals": "off" },
  },
);
