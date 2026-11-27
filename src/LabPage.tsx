import { Field, Focusable, PanelSection, PanelSectionRow } from "@decky/ui";
import { useEffect, useMemo, useRef, useState } from "react";
import * as covers from "./covers";
import GameView from "./GameView";
import { t, useLang } from "./i18n";
import * as nav from "./nav";
import * as steam from "./steam";
import * as store from "./store";

const COLUMNS = 7; // cards per row (like Steam's library)
const TOP_H = 40; // Steam's top bar
const BOTTOM_PAD = 72; // Steam's bottom hint bar (Menu / Select / Back): empty space at the end of the page and the scroll margin for focused cards

// Proportions measured on Steam's own library (1920 px wide): 7 tiles of 220x330 (2:3), side margin 64, gap 42 across and 54 down
const SIDE_VW = 3.333;
const COL_GAP_VW = 2.1875;

// Steam's focus ring pulses for a while after it lands on a tile, then stays steady: PULSE_COUNT x PULSE_PERIOD_S seconds
const PULSE_PERIOD_S = 1.3;
const PULSE_COUNT = 19; // 19 x 1.3 s = 24.7 s

// Belt and braces for noFocusRing: no default Steam focus ring on a game tile (the tile draws its own, see GameCard)
const CARD_CSS = `.dsd-card, .dsd-card:focus, .dsd-card.gpfocus, .dsd-card.gpfocuswithin { outline: none !important; box-shadow: none !important; }
.dsd-card::before, .dsd-card::after { display: none !important; }
@keyframes dsd-pulse {
  0%, 100% { outline-color: rgba(255, 255, 255, 0.95); box-shadow: 0 8px 48px 8px rgba(var(--dsd-glow), 0.5); }
  50% { outline-color: rgba(255, 255, 255, 0.35); box-shadow: 0 8px 48px 8px rgba(var(--dsd-glow), 0.18); }
}
.dsd-focused { animation: dsd-pulse ${PULSE_PERIOD_S}s ease-in-out ${PULSE_COUNT}; }`;
const ROW_GAP_VW = 2.8125;

// Average color of a cover (colorful pixels count more), used for the glow around the focused tile like in Steam.
// Returns "r, g, b", or null when the browser does not let us read the picture (cross-origin without CORS).
function coverColor(img: HTMLImageElement): string | null {
  try {
    const c = document.createElement("canvas");
    c.width = 8;
    c.height = 8;
    const ctx = c.getContext("2d");
    if (!ctx) return null;
    ctx.drawImage(img, 0, 0, 8, 8);
    const d = ctx.getImageData(0, 0, 8, 8).data;
    let r = 0;
    let g = 0;
    let b = 0;
    let w = 0;
    for (let i = 0; i < d.length; i += 4) {
      const hi = Math.max(d[i], d[i + 1], d[i + 2]);
      const lo = Math.min(d[i], d[i + 1], d[i + 2]);
      const weight = (hi - lo) / 255 + 0.1;
      r += d[i] * weight;
      g += d[i + 1] * weight;
      b += d[i + 2] * weight;
      w += weight;
    }
    r = Math.round(r / w);
    g = Math.round(g / w);
    b = Math.round(b / w);
    const m = Math.max(r, g, b, 1);
    if (m < 150) {
      const k = 150 / m; // dark covers get a visible glow too
      r = Math.min(255, Math.round(r * k));
      g = Math.min(255, Math.round(g * k));
      b = Math.min(255, Math.round(b * k));
    }
    return `${r}, ${g}, ${b}`;
  } catch {
    return null;
  }
}

// One card: a portrait cover (like Steam's library, no caption). A name tile is shown only when there is no cover.
// The cover is requested only when the card is about to be on screen.
function GameCard({ g, onSelect }: { g: steam.GameInfo; onSelect: (g: steam.GameInfo) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [src, setSrc] = useState<string>(covers.cached(g.appid) ?? "");
  const [focused, setFocused] = useState(false);
  const [rgb, setRgb] = useState<string | null>(null);
  const [cors, setCors] = useState(true); // ask for CORS on remote covers so their color can be read; retry without it on failure
  const lastPick = useRef(0);

  useEffect(() => {
    if (covers.cached(g.appid) !== undefined) {
      setSrc(covers.cached(g.appid) as string);
      return;
    }
    const el = ref.current;
    if (!el) return;
    let cancelled = false;
    const start = () => {
      covers.load(g).then((u) => {
        if (!cancelled) setSrc(u);
      });
    };
    if (typeof IntersectionObserver === "undefined") {
      start();
      return () => {
        cancelled = true;
      };
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          io.disconnect();
          start();
        }
      },
      { rootMargin: "700px" }
    );
    io.observe(el);
    return () => {
      cancelled = true;
      io.disconnect();
    };
  }, [g.appid]);

  const pick = () => {
    const now = Date.now();
    if (now - lastPick.current < 400) return; // onActivate and onClick may both fire for one press
    lastPick.current = now;
    onSelect(g);
  };
  const focusProps: any = {
    className: "dsd-card",
    noFocusRing: true, // Steam draws its own ring around every focusable element; the tile has its own focus look
    onActivate: pick,
    onClick: pick,
    onFocus: () => setFocused(true),
    onBlur: () => setFocused(false),
  };

  return (
    <Focusable {...focusProps} style={{ minWidth: 0, position: "relative", zIndex: focused ? 3 : 1 }}>
      <div ref={ref}>
        <div
          className={focused ? "dsd-focused" : undefined}
          style={{
            ["--dsd-glow" as any]: rgb ?? "255, 235, 190",
            position: "relative",
            width: "100%",
            paddingTop: "150%", // 2:3 portrait
            borderRadius: "2px",
            overflow: "hidden",
            background: "#2a2f38",
            // Steam's focus (measured on its screenshot: 220x330 tile -> 240x355 frame): the cover grows by 4.5%, a thin light
            // ring stands 3 px away from it, and a soft glow in the color of the cover spreads around
            transform: focused ? "scale(1.045)" : "none",
            outline: focused ? "2px solid rgba(255, 255, 255, 0.95)" : "2px solid transparent",
            outlineOffset: "3px",
            boxShadow: focused ? `0 8px 48px 8px rgba(${rgb ?? "255, 235, 190"}, 0.5)` : "none",
            transition: "transform 0.15s ease-out, box-shadow 0.2s ease-out, outline-color 0.15s ease-out",
            boxSizing: "border-box",
          }}
        >
          {src ? (
            <img
              key={cors ? "cors" : "plain"}
              src={src}
              alt=""
              crossOrigin={cors && src.startsWith("http") ? "anonymous" : undefined}
              onLoad={(e: any) => setRgb(coverColor(e.currentTarget))}
              onError={() => {
                if (cors && src.startsWith("http")) {
                  setCors(false);
                  return;
                }
                covers.markBroken(g.appid);
                setSrc("");
              }}
              style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover" }}
            />
          ) : (
            <div
              style={{
                position: "absolute",
                inset: 0,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                padding: "8px",
                textAlign: "center",
                fontSize: "13px",
                color: "#c7cdd6",
                wordBreak: "break-word",
              }}
            >
              {g.name}
            </div>
          )}
        </div>
      </div>
    </Focusable>
  );
}

// The grid of covers (alphabetical, like Steam's library), scrolling inside the bounded page box
function GameList({ games, onSelect }: { games: steam.GameInfo[]; onSelect: (g: steam.GameInfo) => void }) {
  const gridProps: any = { "flow-children": "grid" };

  return (
    <div style={{ height: "100%", overflowY: "auto", scrollPaddingBottom: `${BOTTOM_PAD}px` }}>
      <style>{CARD_CSS}</style>
      {games.length === 0 && (
        <PanelSection>
          <PanelSectionRow>
            <Field label={t("list.empty")} />
          </PanelSectionRow>
        </PanelSection>
      )}
      <Focusable
        {...gridProps}
        style={{
          display: "grid",
          gridTemplateColumns: `repeat(${COLUMNS}, minmax(0, 1fr))`,
          columnGap: `${COL_GAP_VW}vw`,
          rowGap: `${ROW_GAP_VW}vw`,
          padding: `3vw ${SIDE_VW}vw 1vw`,
        }}
      >
        {games.map((g) => (
          <GameCard key={g.appid} g={g} onSelect={onSelect} />
        ))}
      </Focusable>
      <div style={{ height: `${BOTTOM_PAD}px` }} />
    </div>
  );
}

export default function LabPage() {
  useLang(); // redraw the page when the language changes
  const [ready, setReady] = useState(false);
  const [selected, setSelected] = useState<steam.GameInfo | null>(null);
  const [fromList, setFromList] = useState(false); // the open game was picked from the list
  const games = useMemo(() => {
    const list = steam.getGames();
    covers.prune(list.map((g) => g.appid)); // covers of games that left the library are dropped from memory
    return list;
  }, []);

  // for handlers that outlive a render
  const cur = useRef({ selected, fromList });
  cur.current = { selected, fromList };
  const lastBack = useRef(0);

  // a request from the Quick Access Menu: an appid opens that game, 0 shows the game list
  const openRequested = (): void => {
    const id = nav.consumeRequested();
    if (id === null) return;
    setFromList(false);
    if (id === 0) {
      setSelected(null);
      return;
    }
    const info = games.find((g) => g.appid === id) ?? steam.gameInfoFor(id);
    if (info) setSelected(info);
  };

  const pick = (g: steam.GameInfo) => {
    setFromList(true);
    setSelected(g);
  };

  // Steam's own "Back" (B, the hint at the bottom of the screen): from a game opened from the list back to
  // the list, from anywhere else back to the plugin panel in the Quick Access Menu.
  const goBack = () => {
    if (cur.current.selected && cur.current.fromList) {
      setSelected(null);
      setFromList(false);
    } else {
      nav.exitToPlugin();
    }
  };
  const onCancel = () => {
    const now = Date.now();
    if (now - lastBack.current < 400) return; // onCancel and onCancelButton may both fire for one press
    lastBack.current = now;
    goBack();
  };
  const cancelProps: any = { onCancel, onCancelButton: onCancel, onCancelActionDescription: t("back") };

  useEffect(() => {
    store.init().then(() => {
      openRequested();
      setReady(true);
    });
    const off = nav.onRequest(() => openRequested());
    const offBack = nav.watchBack();
    return () => {
      off();
      offBack();
      nav.onPageClosed();
    };
  }, []);

  // A box as tall as the screen under Steam's top bar; the content scrolls inside it
  // and ends with an empty space (BOTTOM_PAD), so at the end of the page nothing is left under the bottom hint bar.
  const scrolled = (children: any) => (
    <div style={{ height: "100%", overflowY: "auto", scrollPaddingBottom: `${BOTTOM_PAD}px` }}>
      {children}
      <div style={{ height: `${BOTTOM_PAD}px` }} />
    </div>
  );
  return (
    <div style={{ marginTop: `${TOP_H}px`, height: `calc(100vh - ${TOP_H}px)`, overflow: "hidden" }}>
      <Focusable {...cancelProps} style={{ height: "100%" }}>
        {!ready
          ? scrolled(
              <PanelSection>
                <PanelSectionRow>
                  <Field label={t("loading")} />
                </PanelSectionRow>
              </PanelSection>
            )
          : selected
          ? scrolled(<GameView key={selected.appid} game={selected} />)
          : <GameList games={games} onSelect={pick} />}
      </Focusable>
    </div>
  );
}
