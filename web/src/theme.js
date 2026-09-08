export const THEME_STORAGE_KEY = "openbear.theme.v1";
export const THEME_MODES = Object.freeze(["light", "dark", "auto"]);
const DARK_QUERY = "(prefers-color-scheme: dark)";

export function normalizeThemeMode(value) {
  return THEME_MODES.includes(value) ? value : "auto";
}

export function resolveTheme(mode, systemDark) {
  const normalized = normalizeThemeMode(mode);
  if (normalized === "dark") return "dark";
  if (normalized === "light") return "light";
  return systemDark ? "dark" : "light";
}

export function readStoredThemeMode(storage) {
  try {
    return normalizeThemeMode(storage?.getItem?.(THEME_STORAGE_KEY));
  } catch {
    return "auto";
  }
}

function writeStoredThemeMode(storage, mode) {
  try {
    storage?.setItem?.(THEME_STORAGE_KEY, mode);
  } catch {
    // Storage may be blocked by browser/privacy policy. The in-page choice still applies.
  }
}

export function createThemeController({ windowObject, documentObject, storageObject } = {}) {
  const win = windowObject;
  const doc = documentObject;
  const media = typeof win?.matchMedia === "function" ? win.matchMedia(DARK_QUERY) : null;
  let mode = readStoredThemeMode(storageObject);
  let state = null;
  const listeners = new Set();

  function nextState() {
    const resolvedTheme = resolveTheme(mode, Boolean(media?.matches));
    return Object.freeze({ mode, resolvedTheme, dark: resolvedTheme === "dark" });
  }

  function applyRoot(next) {
    const root = doc?.documentElement;
    if (!root) return;
    root.classList.toggle("dark", next.dark);
    root.classList.toggle("light", !next.dark);
    root.style.colorScheme = next.resolvedTheme;
    root.dataset.theme = next.resolvedTheme;
    root.dataset.themeMode = next.mode;
  }

  function publish(next) {
    const changed = !state || state.mode !== next.mode || state.resolvedTheme !== next.resolvedTheme;
    state = next;
    applyRoot(next);
    if (!changed) return state;
    for (const listener of listeners) listener(state);
    if (typeof win?.dispatchEvent === "function" && typeof win?.CustomEvent === "function") {
      win.dispatchEvent(new win.CustomEvent("openbear:theme-change", { detail: state }));
    }
    return state;
  }

  function setMode(value) {
    mode = normalizeThemeMode(value);
    writeStoredThemeMode(storageObject, mode);
    return publish(nextState());
  }

  function handleSystemThemeChange() {
    if (mode === "auto") publish(nextState());
  }

  if (typeof media?.addEventListener === "function") media.addEventListener("change", handleSystemThemeChange);
  else media?.addListener?.(handleSystemThemeChange);

  state = nextState();
  applyRoot(state);

  return Object.freeze({
    getState: () => state,
    setMode,
    subscribe(listener) {
      if (typeof listener !== "function") return () => {};
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    destroy() {
      if (typeof media?.removeEventListener === "function") media.removeEventListener("change", handleSystemThemeChange);
      else media?.removeListener?.(handleSystemThemeChange);
      listeners.clear();
    },
  });
}

let browserStorage = null;
if (typeof window !== "undefined") {
  try { browserStorage = window.localStorage; } catch { browserStorage = null; }
}

const browserTheme = typeof window !== "undefined" && typeof document !== "undefined"
  ? createThemeController({ windowObject: window, documentObject: document, storageObject: browserStorage })
  : null;
const fallbackState = Object.freeze({ mode: "auto", resolvedTheme: "light", dark: false });

export function getThemeState() {
  return browserTheme?.getState() || fallbackState;
}

export function setThemeMode(mode) {
  return browserTheme?.setMode(mode) || fallbackState;
}

export function subscribeTheme(listener) {
  return browserTheme?.subscribe(listener) || (() => {});
}

export function isDarkTheme() {
  return getThemeState().dark;
}
