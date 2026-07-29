import { useEffect, useState } from "react";

const STORAGE_KEY = "citadel-info-theme";

function currentTheme(): "light" | "dark" {
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}

/* The light/dark toggle.
 *
 * public/theme.js has already applied the remembered choice by the time this
 * hydrates; this only labels the button and writes the next choice. The label
 * therefore starts as the neutral word "theme" and is replaced on mount, which
 * is what the hand-written pages do and what keeps the exported HTML free of a
 * guess about which theme the visitor will see.
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

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label="Toggle light or dark theme"
      className="ml-1 cursor-pointer border border-border bg-surface px-[10px] py-1.5 text-xs font-medium text-ink-2 transition-[color,border-color] duration-150 hover:border-border-2 hover:text-accent-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
    >
      {theme === null ? "theme" : theme === "dark" ? "☾ Dark" : "☀ Light"}
    </button>
  );
}
