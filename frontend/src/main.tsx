import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/600.css";
import "@fontsource/jetbrains-mono/800.css";
import "./styles.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { httpAgentApi } from "./agent/client";
import { httpApi } from "./api/client";
import { Root } from "./Root";

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <Root api={httpApi()} agentApi={httpAgentApi()} />
    </StrictMode>,
  );
}
