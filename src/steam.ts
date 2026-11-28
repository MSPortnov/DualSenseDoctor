// Thin wrappers over Steam's internal client API (names confirmed by the device probe unless noted).

export interface GameInfo {
  appid: number;
  name: string;
  isShortcut: boolean; // non-Steam game
  gameId: string; // what RunGame expects (differs from appid for shortcuts)
}

const SHORTCUT_APP_TYPE = 1073741824; // AppType.Shortcut (from community plugins, unverified)
const TOOL_NAME = /^(Proton[ -]|Steam Linux Runtime|Steamworks Common)/i;

function toArray(x: any): any[] {
  if (Array.isArray(x)) return x;
  if (x && typeof x.values === "function") return [...x.values()];
  return [];
}

function fromOverview(a: any): GameInfo | null {
  if (!a?.appid || !a?.display_name) return null;
  const isShortcut = a.app_type === SHORTCUT_APP_TYPE;
  return {
    appid: Number(a.appid),
    name: String(a.display_name),
    isShortcut,
    gameId: String(a.m_gameid ?? a.appid),
  };
}

export function getGames(): GameInfo[] {
  const out = new Map<number, GameInfo>();
  try {
    for (const a of toArray(collectionStore?.localGamesCollection?.allApps)) {
      const g = fromOverview(a);
      if (g && !TOOL_NAME.test(g.name)) out.set(g.appid, g);
    }
  } catch (e) {
    console.error("[DSD] local games failed", e);
  }
  try {
    for (const a of toArray(appStore?.allApps)) {
      if (a?.app_type !== SHORTCUT_APP_TYPE) continue;
      const g = fromOverview(a);
      if (g && !out.has(g.appid)) out.set(g.appid, g);
    }
  } catch (e) {
    console.error("[DSD] non-Steam games failed", e);
  }
  return [...out.values()].sort((a, b) => a.name.localeCompare(b.name));
}

export function gameInfoFor(appid: number): GameInfo | null {
  try {
    return fromOverview(appStore?.GetAppOverviewByAppID?.(appid));
  } catch {
    return null;
  }
}

export function getGameName(appid: number): string {
  return gameInfoFor(appid)?.name ?? "";
}

export function fetchAppDetails(appid: number, timeoutMs = 3000): Promise<any | null> {
  return new Promise((resolve) => {
    let done = false;
    let reg: any = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const unregister = () => {
      try {
        reg?.unregister?.();
      } catch {
        /* ignore */
      }
    };
    const finish = (v: any | null) => {
      if (done) return;
      done = true;
      if (timer) clearTimeout(timer);
      unregister();
      resolve(v);
    };
    try {
      reg = SteamClient.Apps.RegisterForAppDetails(appid, (d: any) => finish(d));
    } catch {
      finish(null);
      return;
    }
    if (done) unregister(); // Steam can call back during registration.
    else timer = setTimeout(() => finish(null), timeoutMs);
  });
}

export function readLaunchOptions(d: any | null, g: GameInfo): string | null {
  if (!d) return null;
  // non-Steam shortcuts keep their options in strShortcutLaunchOptions
  const v = g.isShortcut ? d.strShortcutLaunchOptions ?? d.strLaunchOptions : d.strLaunchOptions;
  return typeof v === "string" ? v : "";
}

export async function confirmAppDetails(
  appid: number,
  matches: (details: any) => boolean,
): Promise<boolean> {
  for (let i = 0; i < 4; i++) {
    if (typeof appDetailsStore !== "undefined") appDetailsStore?.RequestAppDetails?.(appid);
    const d = await fetchAppDetails(appid);
    if (d && matches(d)) return true;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return false;
}

export async function setLaunchOptions(g: GameInfo, options: string): Promise<void> {
  if (g.isShortcut && typeof SteamClient.Apps.SetShortcutLaunchOptions === "function") {
    await SteamClient.Apps.SetShortcutLaunchOptions(g.appid, options);
  } else {
    await SteamClient.Apps.SetAppLaunchOptions(g.appid, options);
  }
  if (!await confirmAppDetails(g.appid, (d) => readLaunchOptions(d, g) === options)) {
    throw new Error("Steam did not confirm the launch options");
  }
}

export function runGame(g: GameInfo): void {
  SteamClient.Apps.RunGame(g.gameId, "", -1, 100);
}
