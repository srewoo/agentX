import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

// When opened as a standalone tab/window (?standalone=1), let the UI fill the viewport.
if (new URLSearchParams(window.location.search).get("standalone") === "1") {
  document.documentElement.classList.add("standalone");
}

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
}
