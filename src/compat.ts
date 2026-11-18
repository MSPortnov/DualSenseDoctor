// Proton (compatibility tool) handling. Steam API names are unverified: see the probe.
import { callable } from "@decky/api";

export const isGeName = (name: string): boolean => /^GE-Proton/i.test(name);

// Steam marks a tool the user forced for the game with a high priority (default picks have a lower one)
export function isForced(details: any | null): boolean {
  const p = details?.nCompatToolPriority;
  return typeof p === "number" && p >= 250;
}

export interface CompatTool {
  name: string; // internal tool name, e.g. GE-Proton11-7-x86_64 or proton_11
  display: string;
  kind?: "valve" | "custom";
}

const backendList = callable<[], CompatTool[]>("list_compat_tools");
const backendCurrent = callable<[number], string>("get_compat_tool");

function parseGe(name: string): [number, number] | null {
  const m = /^GE-Proton(\d+)-(\d+)/i.exec(name);
  return m ? [Number(m[1]), Number(m[2])] : null;
}

export function isGeAtLeast(name: string, min: [number, number]): boolean {
  const v = parseGe(name);
  if (!v) return false;
  return v[0] > min[0] || (v[0] === min[0] && v[1] >= min[1]);
}

export function newestGe(tools: CompatTool[], min: [number, number]): CompatTool | null {
  const c = tools.filter((x) => isGeAtLeast(x.name, min));
  c.sort((a, b) => {
    const va = parseGe(a.name)!;
    const vb = parseGe(b.name)!;
    return vb[0] - va[0] || vb[1] - va[1];
  });
  return c[0] ?? null;
}

export async function listTools(appid: number): Promise<CompatTool[]> {
  const out = new Map<string, CompatTool>();
  try {
    // not present in current Steam builds (probe), kept in case an older client has it
    if (typeof SteamClient?.Apps?.GetAvailableCompatTools === "function") {
      const r = await SteamClient.Apps.GetAvailableCompatTools(appid);
      if (Array.isArray(r)) {
        for (const x of r) {
          const name = String(x?.strToolName ?? "");
          if (name) out.set(name, { name, display: String(x?.strDisplayName || name) });
        }
      }
    }
  } catch {
    /* ignore */
  }
  try {
    const b = await backendList();
    for (const x of b ?? []) if (!out.has(x.name)) out.set(x.name, x);
  } catch {
    /* ignore */
  }
  return [...out.values()].sort((a, b) => a.display.localeCompare(b.display));
}

export function readCurrent(details: any | null): { name: string; display: string } {
  const name = typeof details?.strCompatToolName === "string" ? details.strCompatToolName : "";
  const display = typeof details?.strCompatToolDisplayName === "string" ? details.strCompatToolDisplayName : "";
  return { name, display: display || name };
}

export async function getCurrent(appid: number, details: any | null): Promise<string> {
  const fromDetails = details?.strCompatToolName;
  if (typeof fromDetails === "string" && fromDetails) return fromDetails;
  try {
    return (await backendCurrent(appid)) ?? "";
  } catch {
    return "";
  }
}

export function setTool(appid: number, name: string): { ok: boolean; err?: string } {
  try {
    if (typeof SteamClient?.Apps?.SpecifyCompatTool !== "function") {
      return { ok: false, err: "SpecifyCompatTool is not available" };
    }
    SteamClient.Apps.SpecifyCompatTool(appid, name);
    return { ok: true };
  } catch (e: any) {
    return { ok: false, err: String(e?.message ?? e) };
  }
}

// empty name = no forced tool, Steam falls back to the default one from its settings
export function clearTool(appid: number): { ok: boolean; err?: string } {
  return setTool(appid, "");
}
