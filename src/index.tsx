import { ButtonItem, DropdownItem, Field, Navigation, PanelSection, PanelSectionRow, SliderField, ToggleField, staticClasses } from "@decky/ui";
import { addEventListener, callable, definePlugin, removeEventListener, routerHook, toaster } from "@decky/api";
import { useEffect, useRef, useState } from "react";
import { FaGamepad } from "react-icons/fa";
import LabPage from "./LabPage";
import { num, setPref, syncLang, t, useLang } from "./i18n";
import type { LangPref } from "./i18n";
import { VARIANTS } from "./ladder";
import * as nav from "./nav";
import * as steam from "./steam";
import * as store from "./store";
import { startTracker, useRunningIds } from "./tracker";

/* ====================================================================================================
 * Layout of the Quick Access menu, top to bottom:
 *   1. "Game settings": the running game, recent games, the entry to the game list.
 *      Used often, so it is at the top.
 *   2. "DualSense Doctor": Bluetooth fixes after sleep. Set up once, so it is lower and collapsed:
 *      only the status and the last entry are visible, settings and tools are behind a button.
 *   3. Language of the interface (Auto / English / Русский), at the very bottom.
 * ================================================================================================== */

const ROUTE = "/dualsense-doctor-lab";

/* ------------------------------------------------ Lab -------------------------------------------- */

function onPluginRoute(): boolean {
  try {
    const p = window.location.pathname;
    return p === ROUTE || p.endsWith(ROUTE);
  } catch {
    return false;
  }
}

// Every Navigate() adds a history entry, so repeated opens made Back walk through old copies of the
// plugin. If the page is already current just switch the game; if a game is running (Steam UI is
// hidden behind it) replace our entry instead of adding a second one.
// Without appid the page shows the game list (search on top), never a game.
function open(appid?: number, gameRunning = false) {
  const want = appid ?? 0;
  nav.requestCollapse(); // going to the game settings: the Bluetooth settings fold up
  Navigation.CloseSideMenus();
  if (onPluginRoute()) {
    if (gameRunning) {
      nav.suppressReopen(); // the page is replaced on purpose: do not pop the plugin panel up
      (Navigation as any).NavigateBack?.();
      setTimeout(() => {
        nav.requestOpen(want);
        Navigation.Navigate(ROUTE);
      }, 150);
    } else {
      nav.requestOpen(want);
    }
    return;
  }
  nav.requestOpen(want);
  Navigation.Navigate(ROUTE);
}

const RECENT_IN_QAM = 3;

function LabSection() {
  const data = store.useStoreData();
  const ids = useRunningIds();
  const runId = ids[0];
  const runName = runId ? steam.getGameName(runId) || data.games[String(runId)]?.name || String(runId) : "";
  const recents = Object.values(data.games)
    .filter((r) => r.lastUsed > 0 && r.appid !== runId)
    .sort((a, b) => b.lastUsed - a.lastUsed)
    .slice(0, RECENT_IN_QAM);

  return (
    <PanelSection title={t("qam.lab")}>
      {runId && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => open(runId, true)}>
            {t("qam.running", { name: runName })}
          </ButtonItem>
        </PanelSectionRow>
      )}
      {recents.map((r) => {
        const v = VARIANTS.find((x) => x.id === r.selected);
        return (
          <PanelSectionRow key={r.appid}>
            <ButtonItem layout="below" description={v ? t(v.labelKey) : undefined} onClick={() => open(r.appid, ids.length > 0)}>
              {r.name}
            </ButtonItem>
          </PanelSectionRow>
        );
      })}
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => open(undefined, ids.length > 0)}>
          {t("qam.open")}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

/* ------------------------------------------------ DualSense Doctor ------------------------------- */

type LogEntry = {
  id: number;
  iso: string;
  result: string;
  t_hid: number | null;
  fixes_applied: string[];
  unknown_handle: number;
  after_resume: boolean;
};
type State = {
  error?: string;
  version: string;
  settings: Record<string, any>;
  fix_log: LogEntry[];
};

const getState = callable<[], State>("get_state");
const setSetting = callable<[string, any], Record<string, any>>("set_setting");
const buildReport = callable<[], any>("build_report");

// A focusable row: otherwise Steam's gamepad cursor skips it and the page does not scroll to it
const FOCUS: any = { focusable: true };

const STATUS_COLOR: Record<string, string> = {
  FIXED: "#9ccc65",
  NOHID: "#ef5350",
  NOGYRO: "#ffb74d",
  NOSTEAM: "#ffb74d",
};

function statusText(result: string): string {
  const key = "bt.status." + result;
  const s = t(key);
  return s === key ? result : s;
}

function describe(x: LogEntry): string {
  if (x.result === "FIXED") {
    const parts: string[] = [];
    const bt = x.fixes_applied.includes("early_connect") || x.fixes_applied.includes("l2cap_answer");
    if (bt) parts.push(x.unknown_handle > 0 ? t("bt.d.lostPacket") : t("bt.d.unstable"));
    if (x.fixes_applied.includes("steam_pickup")) parts.push(t("bt.d.steamPickup"));
    if (x.fixes_applied.includes("early_connect")) {
      parts.push(x.t_hid !== null ? t("bt.d.earlyHid", { n: x.t_hid }) : t("bt.d.early"));
    }
    if (x.fixes_applied.includes("l2cap_answer")) parts.push(t("bt.d.answer"));
    return parts.join("; ");
  }
  if (x.result === "NOSTEAM") return t("bt.d.steamWrong");
  return t("bt.d.notEstablished");
}

function LogRow({ x }: { x: LogEntry }) {
  const color = STATUS_COLOR[x.result] ?? "#fff";
  return (
    <PanelSectionRow>
      <Field
        {...FOCUS}
        label={
          <span>
            <span style={{ opacity: 0.8 }}>{x.iso.slice(8, 10) + "." + x.iso.slice(5, 7) + " " + x.iso.slice(11, 19)} </span>
            <span style={{ color, fontWeight: 600 }}>{statusText(x.result)}</span>
          </span>
        }
        description={describe(x)}
      />
    </PanelSectionRow>
  );
}

function DoctorSection() {
  const [st, setSt] = useState<State | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [fails, setFails] = useState(0);
  const refreshing = useRef(false);
  const mounted = useRef(false);
  // Folded by default. Folds again when the plugin is closed (the panel is unmounted or hidden) and when
  // the user goes to the game settings.
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    const collapse = () => setExpanded(false);
    const onVisibility = () => {
      if (document.visibilityState === "hidden") collapse();
    };
    document.addEventListener("visibilitychange", onVisibility);
    const off = nav.onCollapse(collapse);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      off();
    };
  }, []);

  const refresh = async () => {
    if (refreshing.current) return;
    refreshing.current = true;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      const r = await Promise.race([
        getState(),
        new Promise<State>((_, rej) => {
          timeout = setTimeout(() => rej(new Error(t("bt.timeout"))), 5000);
        }),
      ]);
      if (!mounted.current) return;
      if (r && r.error) {
        setErr(String(r.error));
        setFails((n) => n + 1);
      } else {
        setErr("");
        setFails(0);
        setSt(r);
      }
    } catch (e) {
      if (!mounted.current) return;
      setErr(String(e));
      setFails((n) => n + 1);
    } finally {
      if (timeout) clearTimeout(timeout);
      refreshing.current = false;
    }
  };
  useEffect(() => {
    mounted.current = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      if (!mounted.current || document.visibilityState === "hidden") return;
      await refresh();
      if (mounted.current && document.visibilityState === "visible") {
        if (timer) clearTimeout(timer);
        timer = setTimeout(poll, 2000);
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        if (timer) clearTimeout(timer);
        timer = undefined;
      } else {
        if (timer) clearTimeout(timer);
        timer = undefined;
        void poll();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    void poll();
    return () => {
      mounted.current = false;
      document.removeEventListener("visibilitychange", onVisibility);
      if (timer) clearTimeout(timer);
    };
  }, []);

  const set = async (k: string, v: any) => {
    try {
      const s = await setSetting(k, v);
      setSt((prev) => (prev ? { ...prev, settings: s } : prev));
    } catch (e) {
      setErr(String(e));
    }
    refresh();
  };
  const act = async (fn: () => Promise<string>) => {
    setBusy(true);
    try {
      setMsg(await fn());
    } catch (e) {
      setMsg(t("bt.error", { e: String(e) }));
    }
    setBusy(false);
    refresh();
  };
  const toggle = () => setExpanded(!expanded);

  if (!st) {
    return (
      <PanelSection title={t("doctor.bt")}>
        <PanelSectionRow>
          <Field
            {...FOCUS}
            label={fails < 3 ? t("bt.starting") : t("bt.noreply")}
            description={err ? err.slice(-600) : undefined}
          />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const s = st.settings;
  const shown = st.fix_log.slice(0, expanded ? 3 : 1);
  return (
    <>
      <PanelSection title={t("doctor.bt")}>
        {st.fix_log.length === 0 ? (
          <PanelSectionRow>
            <Field {...FOCUS} label={t("bt.working")} description={t("bt.noFixes")} />
          </PanelSectionRow>
        ) : (
          <>
            {shown.map((x) => (
              <LogRow key={x.id} x={x} />
            ))}
          </>
        )}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={toggle}>
            {expanded ? t("doctor.less") : t("doctor.more")}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {expanded && (
        <>
          <PanelSection title={t("bt.settings")}>
            <PanelSectionRow>
              <ToggleField
                label={t("bt.early")}
                description={t("bt.early.desc")}
                checked={!!s.early_connect}
                onChange={(v: boolean) => set("early_connect", v)}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField
                label={t("bt.answer")}
                description={t("bt.answer.desc")}
                checked={!!s.l2cap_answer}
                onChange={(v: boolean) => set("l2cap_answer", v)}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField
                label={t("bt.pickup")}
                description={t("bt.pickup.desc", { n: num(typeof s.steam_bind_at === "number" ? s.steam_bind_at : 2) })}
                checked={!!s.steam_pickup}
                onChange={(v: boolean) => set("steam_pickup", v)}
              />
            </PanelSectionRow>
            {/* shown only after the plugin has raised the time on its own: at the default 2 s everything works */}
            {s.steam_pickup && typeof s.steam_bind_at === "number" && s.steam_bind_at > 2 && (
              <PanelSectionRow>
                <SliderField
                  // the built-in value box is too narrow for "2,5 с" and cuts it off, so the value is part of the label
                  label={t("bt.bind") + ": " + num(typeof s.steam_bind_at === "number" ? s.steam_bind_at : 2) + " " + t("bt.sec")}
                  description={t("bt.bind.desc")}
                  value={typeof s.steam_bind_at === "number" ? s.steam_bind_at : 2}
                  min={2}
                  max={6}
                  step={0.5}
                  onChange={(v: number) => set("steam_bind_at", v)}
                />
              </PanelSectionRow>
            )}
            <PanelSectionRow>
              <ToggleField
                label={t("bt.btmon")}
                description={t("bt.btmon.desc")}
                checked={!!s.btmon}
                onChange={(v: boolean) => set("btmon", v)}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField
                label={t("bt.journal")}
                description={t("bt.journal.desc")}
                checked={!!s.action_journal}
                onChange={(v: boolean) => set("action_journal", v)}
              />
            </PanelSectionRow>
          </PanelSection>

          <PanelSection title={t("bt.tools")}>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                disabled={busy}
                onClick={() =>
                  act(async () => {
                    toaster.toast({ title: "DualSense Doctor", body: t("bt.report.collecting") });
                    const r = await buildReport();
                    return t("bt.report.done", { size: r.size_kb }) + "\n" + r.summary + "\n" + r.tar;
                  })
                }
              >
                {t("bt.report")}
              </ButtonItem>
            </PanelSectionRow>
            {msg && (
              <PanelSectionRow>
                <Field {...FOCUS} label={t("bt.result")} description={msg} />
              </PanelSectionRow>
            )}
          </PanelSection>

        </>
      )}
    </>
  );
}

/* ------------------------------------------------ Other (language) -------------------------------- */

const LANG_ORDER: LangPref[] = ["auto", "en", "ru"];

function OtherSection() {
  const pref = useLang();
  const options = [
    { data: 0, label: t("lang.auto") },
    { data: 1, label: "English" },
    { data: 2, label: "Русский" },
  ];
  return (
    <PanelSection title={t("other.title")}>
      <PanelSectionRow>
        <DropdownItem
          label={t("lang.label")}
          rgOptions={options}
          selectedOption={LANG_ORDER.indexOf(pref)}
          onChange={(o: any) => {
            const p = LANG_ORDER[o.data];
            if (p) setPref(p);
          }}
        />
      </PanelSectionRow>
    </PanelSection>
  );
}

/* ------------------------------------------------ Plugin ----------------------------------------- */

function Content() {
  useLang(); // redraw everything when the language changes
  return (
    <div>
      <LabSection />
      <DoctorSection />
      <OtherSection />
    </div>
  );
}

export default definePlugin(() => {
  syncLang();
  store.init();
  const stopTracker = startTracker();
  routerHook.addRoute(ROUTE, () => <LabPage />, { exact: true });
  // the backend raised the driver return time on its own (two wrong Steam pickups in a row): tell the user
  const onTuned = (_value: number, text: string) => {
    toaster.toast({ title: "DualSense Doctor", body: text, duration: 12000 });
  };
  addEventListener("dsd_tuned", onTuned);

  return {
    name: "DualSense Doctor",
    titleView: <div className={staticClasses.Title}>DualSense Doctor</div>,
    content: <Content />,
    icon: <FaGamepad />,
    onDismount() {
      stopTracker();
      removeEventListener("dsd_tuned", onTuned);
      nav.suppressReopen();
      routerHook.removeRoute(ROUTE);
    },
  };
});
