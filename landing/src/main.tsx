// The NucleiLabs site's entry point: mounts the page.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "../../console/src/styles/tokens.css";
import "./landing.css";

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
