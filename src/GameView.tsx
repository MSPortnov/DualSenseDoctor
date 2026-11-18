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
  const [currentTool, setCurrentTool] = useState("");
  const [forced, setForced] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [uiStale, setUiStale] = useState(false);

  const selected = VARIANTS.find((v) => v.id === selectedId) ?? null;

  // re-read what Steam currently reports (Steam Input can be reset behind our back)
  const refresh = async () => {
    const d = await steam.fetchAppDetails(game.appid);
    setSiMode(si.getSteamInputMode(d));
    setCurrentTool(compat.readCurrent(d).name);
    setForced(compat.isForced(d));
  };

  useEffect(() => {
    let alive = true;
    (async () => {
      const d = await steam.fetchAppDetails(game.appid);
      const lo = steam.readLaunchOptions(d, game) ?? "";
      const cur = compat.readCurrent(d);
      const curName = cur.name || (await compat.getCurrent(game.appid, null));
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
      setCurrentTool(curName);
      setForced(compat.isForced(d));
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
    try {
      // 1. Proton, silently: GE where the setup needs it, otherwise the system default
      if (v.minGe) {
        if (!compat.isGeAtLeast(currentTool, v.minGe)) {
          const ge = compat.newestGe(tools, v.minGe);
          if (!ge) {
            setNotice(t("proton.missing", { v: fmtVer(v.minGe) }));
            return;
          }
          const r = compat.setTool(game.appid, ge.name);
          if (!r.ok) {
            setNotice(t("proton.failed", { err: r.err ?? "?" }));
            return;
          }
        }
      } else if (compat.isGeName(currentTool) || forced) {
        compat.clearTool(game.appid);
      }
      // 2. launch options
      steam.setLaunchOptions(game, v.options);
      setText(v.options);
      setWritten(v.options);
      // 3. Steam Input, last
      const r = await si.setEnabled(game.appid, v.siOn);
      const ok = r.ok;
      setUiStale(r.uiStale);
      await refresh();
      if (!ok) {
        setNotice(t("si.failed"));
        return;
      }
      setSelectedId(v.id);
      await store.update((d) => {
        store.ensureRecord(d, game.appid, game.name).selected = v.id;
      });
      setNotice(t("applied", { name: t(v.labelKey) }));
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
        steam.setLaunchOptions(game, opts);
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
            onChange={(e: any) => setText(e.target.value)}
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
