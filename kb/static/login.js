/* Citadel — /login behavior. The page is styled by info.css (the public design
   system), so it carries the same chrome as /info: theme toggle + Pixel Bastion
   mark. That chrome is duplicated from info.js rather than shared, because
   /login must not pull in the report script (chart + /api/state fetch).
   Loaded as a module under a strict CSP: nothing inline. */

// ---- theme (prefers-color-scheme by default; toggle persists an override) ----
// Same storage key as info.js, so the choice carries across the public pages.
const root = document.documentElement;
try {
  const saved = localStorage.getItem("citadel-info-theme");
  if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
} catch (e) { /* storage blocked — fall back to prefers-color-scheme */ }

// Light is the default. Dark is only ever an explicit, remembered choice.
function currentTheme() {
  return root.getAttribute("data-theme") || "light";
}

const themebtn = document.getElementById("themebtn");
function updateThemeBtn() {
  if (themebtn) themebtn.textContent = currentTheme() === "dark" ? "☾ Dark" : "☀ Light";
}
if (themebtn) {
  updateThemeBtn();
  themebtn.addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("citadel-info-theme", next); } catch (e) { /* ignore */ }
    updateThemeBtn();
  });
}

// ---- Pixel Bastion mark (7x7 crenellated castle) ----
const markGrid = ["1010101", "1111111", "1111111", "1111111", "1101011", "1101011", "1101011"];
const mark = document.getElementById("mark");
if (mark) {
  for (const c of markGrid.join("")) {
    const cell = document.createElement("i");
    if (c === "1") cell.className = "on";
    mark.appendChild(cell);
  }
}

// ---- seat token exchange ----
const form = document.getElementById("loginForm");
const error = document.getElementById("loginError");
const button = document.getElementById("loginSubmit");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.textContent = "";
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = "Checking";
  const access_key = new FormData(form).get("accessKey");
  try {
    const response = await fetch("/admin/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ access_key }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || "Seat token or access key was rejected.");
    }
    window.location.assign("/");
  } catch (err) {
    error.textContent = err.message;
  } finally {
    button.disabled = false;
    button.setAttribute("aria-busy", "false");
    button.textContent = "Open workspace";
  }
});
