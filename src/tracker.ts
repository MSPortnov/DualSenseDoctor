import { useEffect, useState } from "react";

const running = new Set<number>();
const listeners = new Set<() => void>();

function isRunning(appid: number): boolean {
  return running.has(appid);
}

function subscribeRunning(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

function runningIds(): number[] {
  return [...running];
}

export function useRunningIds(): number[] {
  const [ids, setIds] = useState<number[]>(runningIds());
  useEffect(() => subscribeRunning(() => setIds(runningIds())), []);
  return ids;
}

export function useRunning(appid: number): boolean {
  const [r, setR] = useState(isRunning(appid));
  useEffect(() => {
    setR(isRunning(appid));
    return subscribeRunning(() => setR(isRunning(appid)));
  }, [appid]);
  return r;
}

export function startTracker(): () => void {
  let reg: any = null;
  try {
    reg = SteamClient.GameSessions.RegisterForAppLifetimeNotifications((n: any) => {
      const appid = Number(n.unAppID);
      if (n.bRunning) {
        running.add(appid);
      } else {
        running.delete(appid);
      }
      listeners.forEach((fn) => fn());
    });
  } catch (e) {
    console.error("[DSD] lifetime tracker failed", e);
  }
  return () => {
    try {
      reg?.unregister?.();
    } catch {
      /* ignore */
    }
  };
}
