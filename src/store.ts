import { callable } from "@decky/api";
import { useEffect, useState } from "react";

interface GameRecord {
  appid: number;
  name: string;
  lastUsed: number;
  selected?: string; // chosen setup id
}

export interface Data {
  version: 3;
  lastAppid: number | null;
  lastName: string;
  games: Record<string, GameRecord>;
}

const loadData = callable<[], any>("load_data");
const saveData = callable<[Data], boolean>("save_data");

const fresh = (): Data => ({ version: 3, lastAppid: null, lastName: "", games: {} });

let data: Data = fresh();
let loaded = false;
const listeners = new Set<() => void>();

export async function init(): Promise<void> {
  if (loaded) return;
  try {
    const raw = await loadData();
    if (raw && raw.version === 3 && raw.games) {
      // keep only the fields the plugin uses (drops the old per-launch journal from the file on the next save)
      const games: Record<string, GameRecord> = {};
      for (const [k, g] of Object.entries<any>(raw.games)) {
        games[k] = { appid: g.appid, name: g.name, lastUsed: g.lastUsed ?? 0, selected: g.selected };
      }
      data = { ...fresh(), lastAppid: raw.lastAppid ?? null, lastName: raw.lastName ?? "", games };
    }
  } catch (e) {
    console.error("[DSD] load failed", e);
  }
  loaded = true;
  listeners.forEach((fn) => fn());
}

export function get(): Data {
  return data;
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export function ensureRecord(d: Data, appid: number, name: string): GameRecord {
  const key = String(appid);
  if (!d.games[key]) d.games[key] = { appid, name, lastUsed: 0 };
  return d.games[key];
}

export async function update(mutator: (d: Data) => void): Promise<void> {
  mutator(data);
  listeners.forEach((fn) => fn());
  try {
    await saveData(data);
  } catch (e) {
    console.error("[DSD] save failed", e);
  }
}

export function useStoreData(): Data {
  const [, force] = useState(0);
  useEffect(() => subscribe(() => force((n) => n + 1)), []);
  return data;
}
