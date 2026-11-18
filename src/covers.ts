// Game covers for the grid. Source order: the plugin backend (Steam's local artwork on disk, including custom
// artwork and non-Steam shortcuts), then Steam's CDN for regular games, otherwise the card shows a name tile.
import { callable } from "@decky/api";
import type { GameInfo } from "./steam";

const getCover = callable<[number], string>("get_cover");

const cache = new Map<number, string>(); // appid -> image URL ("" = no cover)
const pending = new Map<number, Promise<string>>();
const MAX_PARALLEL = 3;
let active = 0;
const waiting: Array<() => void> = [];

function acquire(): Promise<void> {
  return new Promise((resolve) => {
    if (active < MAX_PARALLEL) {
      active++;
      resolve();
    } else {
      waiting.push(() => {
        active++;
        resolve();
      });
    }
  });
}

function release(): void {
  active--;
  const next = waiting.shift();
  if (next) next();
}

export function cached(appid: number): string | undefined {
  return cache.get(appid);
}

function cdnUrl(g: GameInfo): string {
  return `https://cdn.cloudflare.steamstatic.com/steam/apps/${g.appid}/library_600x900.jpg`;
}

export function load(g: GameInfo): Promise<string> {
  const hit = cache.get(g.appid);
  if (hit !== undefined) return Promise.resolve(hit);
  const running = pending.get(g.appid);
  if (running) return running;
  const p = (async () => {
    await acquire();
    try {
      let url = "";
      try {
        url = (await getCover(g.appid)) || "";
      } catch (e) {
        console.error("[DSD] cover failed", g.appid, e);
      }
      if (!url && !g.isShortcut) url = cdnUrl(g);
      cache.set(g.appid, url);
      return url;
    } finally {
      release();
      pending.delete(g.appid);
    }
  })();
  pending.set(g.appid, p);
  return p;
}

// a cover that failed to load in the page: remember it so the card keeps showing the name tile
export function markBroken(appid: number): void {
  cache.set(appid, "");
}
