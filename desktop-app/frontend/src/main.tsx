import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { captureEngineToken } from "./api";
import { bootAppearance } from "./ui";
import "./styles.css";

// Pick up the loopback engine's per-launch token (#auth=<token> or the
// shell-injected global) before anything issues a request.
captureEngineToken();
// Paint the cached theme and language before the first frame — the
// authoritative settings values re-apply once they load.
bootAppearance();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
