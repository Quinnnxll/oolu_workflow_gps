import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { captureEngineToken } from "./api";
import "./styles.css";

// Pick up the loopback engine's per-launch token (#auth=<token> or the
// shell-injected global) before anything issues a request.
captureEngineToken();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
