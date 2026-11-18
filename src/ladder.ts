// The four setups that proved useful in real tests. Everything else was dropped.
interface Variant {
  id: string;
  labelKey: string;
  descKey: string;
  options: string; // launch options, "" = empty
  siOn: boolean; // Steam Input must be on (true) or off (false) for this setup
  minGe: [number, number] | null; // GE-Proton version required, if any
}

export const VARIANTS: Variant[] = [
  { id: "native", labelKey: "v.native.label", descKey: "v.native.desc", options: "", siOn: false, minGe: null },
  {
    id: "hidraw_off",
    labelKey: "v.hidraw_off.label",
    descKey: "v.hidraw_off.desc",
    options: "PROTON_DISABLE_HIDRAW=1 %command%",
    siOn: false,
    minGe: null,
  },
  {
    id: "ds4",
    labelKey: "v.ds4.label",
    descKey: "v.ds4.desc",
    options: "PROTON_SONY_DUALSENSE_AS_DUALSHOCK4=1 %command%",
    siOn: false,
    minGe: [11, 4],
  },
  { id: "steam_input", labelKey: "v.steam_input.label", descKey: "v.steam_input.desc", options: "", siOn: true, minGe: null },
];
