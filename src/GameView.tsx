import { ButtonItem, DropdownItem, Field, PanelSection, PanelSectionRow, TextField } from "@decky/ui";
import { useEffect, useState } from "react";
import * as compat from "./compat";
import { t } from "./i18n";
import { VARIANTS } from "./ladder";
import * as nav from "./nav";
import * as steam from "./steam";
import * as si from "./steamInput";
import * as store from "./store";
import { useRunning } from "./tracker";

const fmtVer = (v: [number, number]) => `${v[0]}-${v[1]}`;

export default function GameView({ game }: { game: steam.GameInfo }) {
  const running = useRunning(game.appid);

  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState(""); // launch options as shown / edited
  const [written, setWritten] = useState(""); // launch options last written to / read from Steam
  const [siMode, setSiMode] = useState<number | undefined>(undefined);
  const [tools, setTools] = useState<compat.CompatTool[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [uiStale, setUiStale] = useState(false);

  const selected = VARIANTS.find((v) => v.id === selectedId) ?? null;

  // re-read what Steam currently reports (Steam Input can be reset behind our back)
  const refresh = async (reloadOptions = false) => {
    const d = await steam.fetchAppDetails(game.appid);
    setSiMode(si.getSteamInputMode(d));
    const actual = steam.readLaunchOptions(d, game);
    if (reloadOptions && actual !== null) {
      setText(actual);
      setWritten(actual);
    }
  };

  useEffect(() => {
    let alive = true;
    (async () => {
      const d = await steam.fetchAppDetails(game.appid);
      const lo = steam.readLaunchOptions(d, game) ?? "";
      const list = await compat.listTools(game.appid);
      const mode = si.getSteamInputMode(d);
      if (!alive) return;
      const rec = store.get().games[String(game.appid)];
      const match = VARIANTS.find(
        (v) => v.options === lo.trim() && mode !== undefined && si.isOn(mode) === v.siOn
      );
      setText(lo);
      setWritten(lo);
      setSiMode(mode);
      setTools(list);
      setSelectedId(rec?.selected ?? match?.id ?? null);
      setLoaded(true);
      await store.update((dd) => {
        store.ensureRecord(dd, game.appid, game.name).lastUsed = Date.now();
        dd.lastAppid = game.appid;
        dd.lastName = game.name;
      });
    })();
    return () => {
      alive = false;
    };
  }, [game.appid]);

  // Choosing a setup applies it right away (Proton, launch options, then Steam Input).
  const applyVariant = async (i: number) => {
    const v = VARIANTS[i];
    if (!v || busy) return;
    setBusy(true);
    setNotice("");
    const rollback: (() => Promise<void>)[] = [];
    try {
      const details = await steam.fetchAppDetails(game.appid);
      const oldOptions = steam.readLaunchOptions(details, game);
      const oldMode = si.getSteamInputMode(details);
      if (!details || oldOptions === null || oldMode === undefined) {
        throw new Error(t("preset.readFailed"));
      }
      const currentTool = await compat.getCurrent(game.appid, details);
      const forced = compat.isForced(details);
      let tool: string | undefined;
      if (v.minGe) {
        if (!compat.isGeAtLeast(currentTool, v.minGe)) {
          const ge = compat.newestGe(tools, v.minGe);
          if (!ge) {
            setNotice(t("proton.missing", { v: fmtVer(v.minGe) }));
            return;
          }
          tool = ge.name;
        }
      } else if (compat.isGeName(currentTool) || forced) {
        tool = "";
      }
      if (tool !== undefined) {
        if ((currentTool && typeof details.nCompatToolPriority !== "number") || (forced && !currentTool)) {
          throw new Error(t("preset.readFailed"));
        }
        rollback.push(() => compat.setTool(game.appid, forced ? currentTool : ""));
        await compat.setTool(game.appid, tool);
      }
      if (oldOptions !== v.options) {
        rollback.push(() => steam.setLaunchOptions(game, oldOptions));
        await steam.setLaunchOptions(game, v.options);
      }
      if (oldMode !== (v.siOn ? si.SI_ON : si.SI_OFF)) {
        rollback.push(async () => {
          const restored = await si.setMode(game.appid, oldMode);
          if (!restored.ok) throw new Error("Steam Input rollback failed");
          setUiStale(restored.uiStale);
        });
      }
      const r = await si.setEnabled(game.appid, v.siOn);
      setUiStale(r.uiStale);
      if (!r.ok) throw new Error(t("si.failed"));
      setSiMode(v.siOn ? si.SI_ON : si.SI_OFF);
      setText(v.options);
      setWritten(v.options);
      setSelectedId(v.id);
      await store.update((d) => {
        store.ensureRecord(d, game.appid, game.name).selected = v.id;
      });
      setNotice(t("applied", { name: t(v.labelKey) }));
    } catch (e) {
      let restored = true;
      for (const undo of rollback.reverse()) {
        try {
          await undo();
        } catch (error) {
          console.error("[DSD] preset rollback failed", error);
          restored = false;
        }
      }
      if (rollback.length) await refresh(true);
      if (!restored) setSelectedId(null);
      const reason = e instanceof Error ? e.message : String(e);
      setNotice(t(restored ? "preset.failed" : "preset.partial", { reason }));
    } finally {
      setBusy(false);
    }
  };

  const launch = async () => {
    if (busy) return;
    setBusy(true);
    setNotice("");
    const opts = text.trim();
    try {
      // only write launch options if they changed, then set Steam Input as the setup needs
      if (opts !== written.trim()) {
        await steam.setLaunchOptions(game, opts);
        setWritten(opts);
      }
      let ok = true;
      if (selected) {
        const r = await si.setEnabled(game.appid, selected.siOn);
        ok = r.ok;
        setUiStale(r.uiStale);
      }
      const finalSi = await si.readMode(game.appid);
      setSiMode(finalSi);
      if (!ok) {
        setNotice(t("si.failed"));
        return;
      }
      nav.suppressReopen(); // the page closes because the game starts: do not pop the plugin panel up
      steam.runGame(game);
    } catch (e) {
      setNotice(t("game.launchFailed", { reason: e instanceof Error ? e.message : String(e) }));
    } finally {
      setBusy(false);
    }
  };

  const variantOptions = [
    ...VARIANTS.map((v, i) => ({ data: i, label: t(v.labelKey) })),
    ...(loaded && !selected ? [{ data: -1, label: t("variant.none") }] : []),
  ];
  const siLabel =
    siMode === undefined
      ? "?"
      : `${siMode === si.SI_OFF ? t("si.off") : siMode === si.SI_ON ? t("si.on") : t("si.default")} (${siMode})`;

  return (
    <>
      <PanelSection title={game.name}>
        {notice && (
          <PanelSectionRow>
            <Field label={notice} />
          </PanelSectionRow>
        )}
        {uiStale && (
          <>
            <PanelSectionRow>
              <Field label={t("ui.stale")} />
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => SteamClient.Browser?.RestartJSContext?.()}>
                {t("ui.restart")}
              </ButtonItem>
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      <PanelSection>
        <PanelSectionRow>
          <DropdownItem
            label={t("game.variant")}
            description={selected ? t(selected.descKey) : t("variant.none.desc")}
            rgOptions={variantOptions}
            selectedOption={selected ? VARIANTS.indexOf(selected) : -1}
            disabled={!loaded || busy}
            onChange={(o: any) => {
              if (o.data >= 0) applyVariant(o.data);
            }}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label={t("si.status")}>{siLabel}</Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label={t("game.options")}
            value={text}
            onChange={(e: any) => { if (!busy) setText(e.target.value); }}
          />
        </PanelSectionRow>
        {text.trim() !== "" && !text.includes("%command%") && (
          <PanelSectionRow>
            <Field label={t("game.options.warn")} />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={!loaded || busy || running} onClick={launch}>
            {running ? t("game.running") : busy ? t("loading") : t("game.launch")}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

    </>
  );
}
