// Theme: dark (the default), light, or the operating system's. Remembered in this browser only.
import { useSyncExternalStore } from "react";

export type Theme = "dark" | "light" | "system";
const KEY = "neura.theme";
const listeners = new Set<() => void>();

function read(): Theme {
  try {
    const value = localStorage.getItem(KEY);
    return value === "light" || value === "system" ? value : "dark";
  } catch {
    return "dark";
  }
}

/** Apply the theme to the document: data-theme for dark and light; none follows the OS. */
export function applyTheme(theme: Theme = read()): void {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function setTheme(theme: Theme): void {
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // A preference only.
  }
  applyTheme(theme);
  listeners.forEach((l) => l());
}

export function useTheme(): Theme {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    read,
    () => "dark",
  );
}
