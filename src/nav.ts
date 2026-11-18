import { Navigation } from "@decky/ui";

// Hand-off between the Quick Access Menu and the full page.
// requested: appid = open that game, 0 = show the game list, null = nothing requested.
let requested: number | null = null;
const listeners = new Set<() => void>();

export function requestOpen(appid: number): void {
  // pass 0 for the game list
  requested = appid;
  listeners.forEach((fn) => fn());
}

export function consumeRequested(): number | null {
  const r = requested;
  requested = null;
  return r;
}

// the page subscribes while mounted, so a request can switch the open game without navigating
export function onRequest(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

// ---- "collapse the Bluetooth settings" signal -------------------------------------------------------
const collapseListeners = new Set<() => void>();

export function onCollapse(fn: () => void): () => void {
  collapseListeners.add(fn);
  return () => {
    collapseListeners.delete(fn);
  };
}

export function requestCollapse(): void {
  collapseListeners.forEach((fn) => fn());
}

// ---- Back returns to the plugin panel in the Quick Access Menu -------------------------------------
// Steam's Back button closes our page and shows whatever was behind it, with the menu closed. The page
// calls onPageClosed() when it goes away and the plugin panel is opened again. Places that close the page
// on purpose (starting a game, switching the page) call suppressReopen() first.
const DECKY_TAB = 999; // QuickAccessTab.Decky
let quietUntil = 0;

export function suppressReopen(ms = 2500): void {
  quietUntil = Date.now() + ms;
}

function reopenPlugin(delay = 250): void {
  setTimeout(() => {
    try {
      (Navigation as any).OpenQuickAccessMenu?.(DECKY_TAB);
    } catch {
      /* ignore */
    }
  }, delay);
}

// Whether the page is being left with Back: a step back in Steam's history fires "popstate", going on to the library,
// the home screen or another page is a new history entry and does not.
let poppedAt = 0;

export function watchBack(): () => void {
  poppedAt = 0;
  const onPop = () => {
    poppedAt = Date.now();
  };
  window.addEventListener("popstate", onPop);
  return () => window.removeEventListener("popstate", onPop);
}

// The plugin panel pops up again only when the page was left with Back (B inside the page does it through
// exitToPlugin; this covers Steam's own Back when nothing on the page had focus). Leaving the page any other way
// (library, home screen, store) must leave the screen alone.
export function onPageClosed(): void {
  if (Date.now() < quietUntil) return;
  if (Date.now() - poppedAt > 800) return;
  reopenPlugin();
}

// for the on-screen back button: leave the page with exactly ONE step back in Steam's history
// (the window's own history is never touched: it belongs to Steam's UI and a step too far lands on an empty page)
export function exitToPlugin(): void {
  suppressReopen();
  try {
    (Navigation as any).NavigateBack?.();
  } catch {
    /* ignore */
  }
  reopenPlugin();
}
