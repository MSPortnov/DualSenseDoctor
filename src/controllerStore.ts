// Finds Steam's JS-side controller configuration store (the object the settings pages call
// ClearSelectedConfigCache / EnsureEditingConfiguration on). It is not a global, so it is looked up in the
// webpack module cache by the method it has. Seen in Steam's own code (report 154734):
//   properties page: SetThirdPartyControllerConfiguration(app, value); store.ClearSelectedConfigCache(app)
//   configurator toggle: Set(app, Default); GetConfigForAppAndController(app, idx).bConfigurationEnabled (stale, not used);
//     Set(app, On/Off) if that differs; store.SaveEditingConfiguration(app); store.ClearSelectedConfigCache(app);
//     store.EnsureEditingConfiguration(app, idx)
import { findModuleChild } from "@decky/ui";

const hasClear = (v: any): boolean => {
  try {
    return !!v && typeof v.ClearSelectedConfigCache === "function";
  } catch {
    return false;
  }
};

let found: any = null;

export function findControllerStore(): any | null {
  if (found) return found;
  try {
    const wc: any = (window as any).webpackChunksteamui;
    let req: any = null;
    wc?.push?.([[Symbol("dsd-store")], {}, (r: any) => {
      req = r;
    }]);
    const cache = req?.c ?? {};
    for (const id of Object.keys(cache)) {
      const exp = cache[id]?.exports;
      if (!exp || (typeof exp !== "object" && typeof exp !== "function")) continue;
      const candidates: any[] = [];
      try {
        candidates.push(exp, exp.default);
        for (const k of Object.keys(exp)) candidates.push(exp[k]);
      } catch {
        /* getter threw: skip this module */
      }
      for (const v of candidates) {
        try {
          if (hasClear(v)) {
            found = v;
            return v;
          }
        } catch {
          /* ignore */
        }
      }
    }
  } catch {
    /* webpack not reachable */
  }
  // fallback 1: objects on window
  try {
    for (const n of Object.getOwnPropertyNames(window)) {
      let o: any;
      try {
        o = (window as any)[n];
      } catch {
        continue;
      }
      if (hasClear(o)) {
        found = o;
        return o;
      }
    }
  } catch {
    /* ignore */
  }
  // fallback 2: the module finder of @decky/ui (this is what worked in the standalone Steam Input test plugin)
  try {
    const r: any = findModuleChild((m: any) => {
      if (!m || typeof m !== "object") return undefined;
      for (const k in m) {
        try {
          if (hasClear(m[k])) return m[k];
        } catch {
          /* ignore */
        }
      }
      return undefined;
    });
    if (r) {
      found = r;
      return r;
    }
  } catch {
    /* ignore */
  }
  return null;
}
