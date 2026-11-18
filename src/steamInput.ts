// Steam Input per-game override. Values (read from Steam's own UI code):
//   0 = Off ("#AppProperties_SteamInputOff"), 1 = Default settings, 2 = On.
// "Default" is on or off depending on Steam's global controller settings, so the plugin never relies on it:
// it always writes an EXPLICIT value (2 = on, 0 = off).
//
// Switching (verified on the device):
//   SetThirdPartyControllerConfiguration(app, Default); SetThirdPartyControllerConfiguration(app, On/Off);
//   store.ClearSelectedConfigCache(app)
// The two Set calls are awaited in order. The first one (Default) makes Steam register the change even if the
// stored value is already the wanted one. ClearSelectedConfigCache resets the JS-side cache of the controller
// settings pages, otherwise they keep showing the old state until the Steam interface restarts.
//
// NOT used for the decision any more: GetConfigForAppAndController(...).bConfigurationEnabled. It comes from the
// same cache that goes stale, so after a change it can report the old state; that made the previous version
// skip a needed write ("already as wanted") or leave the override on Default, which is "on" on this machine.
import { findControllerStore } from "./controllerStore";
import { fetchAppDetails } from "./steam";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export const SI_OFF = 0;
const SI_DEFAULT = 1;
export const SI_ON = 2;

export function getSteamInputMode(details: any | null): number | undefined {
  const v = details?.eEnableThirdPartyControllerConfiguration;
  return typeof v === "number" ? v : undefined;
}

// override value is on/off for the setups: Off = off, Default or On = on
export const isOn = (mode: number | undefined): boolean | undefined =>
  mode === undefined ? undefined : mode !== SI_OFF;

// override value according to Steam's backend
export async function readMode(appid: number): Promise<number | undefined> {
  return getSteamInputMode(await fetchAppDetails(appid));
}

async function waitFor(appid: number, ok: (m: number | undefined) => boolean): Promise<number | undefined> {
  let m: number | undefined;
  for (let i = 0; i < 20; i++) {
    m = await readMode(appid);
    if (ok(m)) break;
    await sleep(50);
  }
  return m;
}

// Refreshes Steam's UI cache the way its properties page does. Returns false if the store could not be found or
// used (then only restarting the Steam interface helps).
function refreshUi(appid: number): boolean {
  const store = findControllerStore();
  if (!store) return false;
  try {
    store.ClearSelectedConfigCache(appid);
    return true;
  } catch {
    return false;
  }
}

interface SetResult {
  ok: boolean; // Steam reports the wanted override value
  uiStale: boolean; // the Steam UI could not be refreshed (restart of the interface needed)
}

export async function setEnabled(appid: number, wantOn: boolean): Promise<SetResult> {
  const target = wantOn ? SI_ON : SI_OFF;
  const value = await readMode(appid);
  if (value === undefined) return { ok: false, uiStale: false };

  // already explicitly as wanted: only make sure the settings pages are not showing a stale state
  if (value === target) return { ok: true, uiStale: !refreshUi(appid) };

  const A = SteamClient.Apps;
  const I = SteamClient.Input;
  try {
    await A.SetThirdPartyControllerConfiguration(appid, SI_DEFAULT);
    await A.SetThirdPartyControllerConfiguration(appid, target);
  } catch {
    return { ok: false, uiStale: false };
  }

  let uiOk = refreshUi(appid);
  if (!uiOk) {
    // no store: fall back to restarting the editor session, which the page does as a side effect
    try {
      await I.StopEditingControllerConfiguration(0);
      await I.StartEditingControllerConfigurationForAppIDAndControllerIndex(appid, 0);
      await sleep(300);
      await I.StopEditingControllerConfiguration(0);
    } catch {
      /* optional */
    }
  }
  try {
    await I?.UploadChangesForCloudedControllerConfigs?.();
    await I?.SyncCloudedControllerConfigs?.();
  } catch {
    /* optional */
  }
  try {
    appDetailsStore?.RequestAppDetails?.(appid);
  } catch {
    /* optional */
  }

  const ok = (await waitFor(appid, (m) => m === target)) === target;
  if (!ok) uiOk = true; // nothing to refresh if the change itself failed
  return { ok, uiStale: ok && !uiOk };
}
