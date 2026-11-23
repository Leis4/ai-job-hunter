import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./index.css";

const stored = localStorage.getItem("ajh-theme");
const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
const initiallyDark = stored ? stored === "dark" : !!prefersDark;
document.documentElement.classList.toggle("dark", initiallyDark);
document.documentElement.style.colorScheme = initiallyDark ? "dark" : "light";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
