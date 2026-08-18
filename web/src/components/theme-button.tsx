import { useEffect, useState } from "react";

const STORAGE_KEY = "citadel-info-theme";

function currentTheme(): "light" | "dark" {
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}

function Sun() {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor" aria-hidden="true">
      <circle cx="8" cy="8" r="3" />
      <path
        d="M8 1.25v1.7M8 13.05v1.7M1.25 8h1.7M13.05 8h1.7M3.05 3.05l1.2 1.2M11.75 11.75l1.2 1.2M3.05 12.95l1.2-1.2M11.75 4.25l1.2-1.2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
      />
    </svg>
  );
}

function Moon() {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor" aria-hidden="true">
      <path d="M12.6 10.4A6 6 0 0 1 5.6 3.4 4.75 4.75 0 1 0 12.6 10.4z" />
    </svg>
  );
}

/* The light/dark switch.
 *
 * public/theme.js has already applied the remembered choice by the time this
 * hydrates; this only moves the knob and writes the next choice. The export
 * ships with aria-checked=false (light is the default) so the static HTML does
 * not guess the visitor's stored theme. The glyph sits on the knob in the
 * inverse ink colour so it stays readable in both themes.
 */
export function ThemeButton() {
  const [theme, setTheme] = useState<"light" | "dark" | null>(null);

  useEffect(() => setTheme(currentTheme()), []);

  function toggle() {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* storage blocked — the choice holds for this page and is not remembered */
    }
    setTheme(next);
  }

  const dark = theme === "dark";

  return (
    <button
      type="button"
      role="switch"
      aria-checked={dark}
      aria-label="Toggle light or dark theme"
      onClick={toggle}
      className="ml-1 inline-flex h-6 w-11 shrink-0 cursor-pointer items-center border border-border bg-surface-2 p-0.5 transition-[border-color] duration-150 hover:border-border-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
    >
      <span
        aria-hidden="true"
        className={`flex size-5 items-center justify-center rounded-full bg-ink text-ground transition-transform duration-150 ${
          dark ? "translate-x-5" : "translate-x-0"
        }`}
      >
        {dark ? <Moon /> : <Sun />}
      </span>
    </button>
  );
}
