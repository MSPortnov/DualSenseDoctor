import { callable } from "@decky/api";
import { useEffect, useState } from "react";

type Dict = Record<string, string>;

const en: Dict = {
  "qam.lab": "Game settings",
  "qam.open": "Set up games",
  "doctor.bt": "Bluetooth: reconnect after sleep",
  "doctor.more": "Bluetooth settings ▼",
  "doctor.less": "Bluetooth settings ▲",
  "qam.running": "Tune running game: {name}",
  "loading": "Loading…",
  "list.empty": "No games found",
  "back": "Back",
  "game.variant": "Setup",
  "game.options": "Launch options (editable)",
  "game.options.warn": "No %command% in the string: Steam will pass it to the game as arguments.",
  "game.launch": "Launch game",
  "game.running": "Game is running",
  "variant.none": "(custom settings)",
  "variant.none.desc": "Current settings do not match any of the setups below.",
  "applied": "Applied: {name}",
  "v.native.label": "DualSense: native support",
  "v.native.desc": "Launch options empty, Steam Input disabled.",
  "v.hidraw_off.label": "DualSense: PROTON_DISABLE_HIDRAW=1",
  "v.hidraw_off.desc": "The game sees the DualSense via SDL. Steam Input disabled.",
  "v.ds4.label": "DualShock: DUALSENSE_AS_DUALSHOCK4",
  "v.ds4.desc": "DualSense is presented as a DualShock 4. Steam Input disabled. Needs GE-Proton 11-4 or newer, switched automatically.",
  "v.steam_input.label": "Steam Input only",
  "v.steam_input.desc": "Launch options empty, Steam Input enabled.",
  "si.status": "Steam Input",
  "ui.stale": "Steam Input is switched correctly, but Steam\u2019s own settings pages may show the old state until the Steam interface is restarted.",
  "ui.restart": "Restart Steam interface",
  "si.off": "Disabled",
  "si.on": "Enabled",
  "si.default": "Default",
  "si.failed": "Could not make Steam Input stick for this game. The game was not launched. Check game Properties > Controller.",
  "proton.missing": "This setup needs GE-Proton {v} or newer. Install it via ProtonUp-Qt.",
  "proton.failed": "Could not switch Proton: {err}",
  "bt.status.FIXED": "fixed",
  "bt.status.NOHID": "no HID, not fixed",
  "bt.status.NOGYRO": "no gyroscope",
  "bt.status.NOSTEAM": "Steam took it wrongly",
  "bt.d.lostPacket": "packet lost on wake-up",
  "bt.d.unstable": "the link was unstable",
  "bt.d.steamPickup": "Steam did not notice the controller, the device was reconnected",
  "bt.d.steamWrong": "Steam opened the controller in the wrong mode (default lighting, A and B swapped); reconnecting did not help",
  "bt.d.early": "early connect",
  "bt.d.earlyHid": "early connect (HID after {n} s)",
  "bt.d.answer": "reply to the controller",
  "bt.d.notEstablished": "the link was not established",
  "bt.starting": "Starting the backend…",
  "bt.noreply": "The backend is not responding",
  "bt.timeout": "no response from the backend (5 s timeout)",
  "bt.whereCause": "Where to look for the cause",
  "bt.working": "The plugin is working",
  "bt.noFixes": "No fixes were needed yet. Put the PC to sleep and wake it with the controller; an entry will appear here.",
  "bt.settings": "Bluetooth settings",
  "bt.early": "Early connect",
  "bt.early.desc": "If HID does not appear after wake-up, opens the HID channel itself before the PC drops the empty link (at 4 s).",
  "bt.answer": "Reply to the controller",
  "bt.answer.desc": "Stops the controller from dropping the link at about 28 s: answers the request that was lost on wake-up.",
  "bt.pickup": "Steam pickup",
  "bt.pickup.desc": "After wake-up the controller is hidden from Steam for about two seconds so that Steam opens it the right way (buttons A and B, light). If Steam still misses it or opens it wrongly, the plugin reconnects the device.",
  "bt.noSuspend": "Keep the Bluetooth adapter awake",
  "bt.noSuspend.desc": "After a SteamOS update the adapter got auto-suspend. The plugin restores the \"always on\" mode as before.",
  "bt.btmon": "HCI trace (diagnostics)",
  "bt.btmon.desc": "Records Bluetooth traffic for the report. Uses CPU, enable it only when hunting for problems.",
  "bt.tools": "Tools",
  "bt.report": "Collect Bluetooth report",
  "bt.report.collecting": "Collecting the report…",
  "bt.report.done": "Done ({size} KB):",
  "bt.clear": "Clear Bluetooth statistics and logs",
  "bt.cleared": "Data cleared.",
  "bt.error": "Error: {e}",
  "bt.result": "Result",
  "bt.where": "Where the reports are",
  "bt.where.deck": "On the Steam Deck",
  "bt.where.deck.desc": "Home folder (~): dualsense-doctor-DATE-summary.txt (short) and dualsense-doctor-DATE.tar.gz (full archive). Plugin logs: ~/homebrew/logs/DualSense Doctor/",
  "bt.where.mac": "Download to the Mac (to ~/Downloads)",
  "bt.where.logs": "Decky and plugin logs (dsd-diag.txt)",
  "bt.addr": "ADDRESS",
  "other.title": "Other",
  "lang.label": "Language",
  "lang.auto": "Auto (system language)",
};

const ru: Dict = {
  "qam.lab": "Настройки игр",
  "qam.open": "Настроить игры",
  "doctor.bt": "Bluetooth: связь после сна",
  "doctor.more": "Настройки Bluetooth ▼",
  "doctor.less": "Настройки Bluetooth ▲",
  "qam.running": "Настроить запущенную игру: {name}",
  "loading": "Загрузка…",
  "list.empty": "Игры не найдены",
  "back": "Назад",
  "game.variant": "Настройка",
  "game.options": "Параметры запуска (можно править)",
  "game.options.warn": "В строке нет %command%: Steam передаст её игре как аргументы.",
  "game.launch": "Запустить игру",
  "game.running": "Игра запущена",
  "variant.none": "(свои настройки)",
  "variant.none.desc": "Текущие настройки не совпадают ни с одним из вариантов ниже.",
  "applied": "Применено: {name}",
  "v.native.label": "DualSense: нативная поддержка",
  "v.native.desc": "Параметры запуска пустые, Steam Input выключен.",
  "v.hidraw_off.label": "DualSense: PROTON_DISABLE_HIDRAW=1",
  "v.hidraw_off.desc": "Игра видит DualSense через SDL. Steam Input выключен.",
  "v.ds4.label": "DualShock: DUALSENSE_AS_DUALSHOCK4",
  "v.ds4.desc": "DualSense выдаётся за DualShock 4. Steam Input выключен. Нужен GE-Proton 11-4 или новее, переключается автоматически.",
  "v.steam_input.label": "Только Steam Input",
  "v.steam_input.desc": "Параметры запуска пустые, Steam Input включён.",
  "si.status": "Steam Input",
  "ui.stale": "Steam Input переключён верно, но собственные страницы настроек Steam могут показывать старое состояние, пока интерфейс Steam не перезагружен.",
  "ui.restart": "Перезагрузить интерфейс Steam",
  "si.off": "Выключен",
  "si.on": "Включён",
  "si.default": "По умолчанию",
  "si.failed": "Не удалось закрепить Steam Input для этой игры, игра не запущена. Проверьте Свойства игры > Контроллер.",
  "proton.missing": "Этой настройке нужен GE-Proton {v} или новее. Установите его через ProtonUp-Qt.",
  "proton.failed": "Не удалось сменить Proton: {err}",
  "bt.status.FIXED": "исправлено",
  "bt.status.NOHID": "нет HID, не исправлено",
  "bt.status.NOGYRO": "нет гироскопа",
  "bt.status.NOSTEAM": "Steam открыл неверно",
  "bt.d.lostPacket": "потерян пакет при пробуждении",
  "bt.d.unstable": "связь была нестабильна",
  "bt.d.steamPickup": "Steam не заметил контроллер, устройство переподключено",
  "bt.d.steamWrong": "Steam открыл контроллер неверно (подсветка заводская, A и B перепутаны), переподключение не помогло",
  "bt.d.early": "ранний connect",
  "bt.d.earlyHid": "ранний connect (HID через {n} с)",
  "bt.d.answer": "ответ контроллеру",
  "bt.d.notEstablished": "связь не установилась",
  "bt.starting": "Запуск бэкенда…",
  "bt.noreply": "Бэкенд не отвечает",
  "bt.timeout": "нет ответа от бэкенда (таймаут 5 с)",
  "bt.whereCause": "Где искать причину",
  "bt.working": "Плагин работает",
  "bt.noFixes": "Исправлений пока не требовалось. Усыпите ПК и разбудите геймпадом, здесь появится запись.",
  "bt.settings": "Настройки Bluetooth",
  "bt.early": "Ранний connect",
  "bt.early.desc": "Если после пробуждения HID не появился, сам открывает HID-канал, пока ПК не разорвал пустую связь (на 4-й секунде).",
  "bt.answer": "Ответ контроллеру",
  "bt.answer.desc": "Не даёт контроллеру оборвать связь на ~28-й секунде: отвечает ему на потерянный при пробуждении запрос.",
  "bt.pickup": "Подхват Steam",
  "bt.pickup.desc": "После пробуждения контроллер примерно на две секунды скрывается от Steam, чтобы Steam открыл его правильно (кнопки A и B, подсветка). Если Steam всё же его пропустил или открыл неверно, плагин переподключает устройство.",
  "bt.noSuspend": "Не усыплять Bluetooth-адаптер",
  "bt.noSuspend.desc": "После обновления SteamOS адаптер получил автосон. Плагин возвращает режим «всегда включён», как было раньше.",
  "bt.btmon": "HCI-трасса (диагностика)",
  "bt.btmon.desc": "Записывает Bluetooth-трафик для отчёта. Нагружает процессор, включать только для поиска проблем.",
  "bt.tools": "Инструменты",
  "bt.report": "Собрать отчёт Bluetooth",
  "bt.report.collecting": "Собираю отчёт…",
  "bt.report.done": "Готово ({size} КБ):",
  "bt.clear": "Очистить статистику и логи Bluetooth",
  "bt.cleared": "Данные очищены.",
  "bt.error": "Ошибка: {e}",
  "bt.result": "Результат",
  "bt.where": "Где лежат отчёты",
  "bt.where.deck": "На Steam Deck",
  "bt.where.deck.desc": "Домашняя папка (~): dualsense-doctor-ДАТА-summary.txt (кратко) и dualsense-doctor-ДАТА.tar.gz (полный архив). Логи плагина: ~/homebrew/logs/DualSense Doctor/",
  "bt.where.mac": "Скачать на Mac (в ~/Downloads)",
  "bt.where.logs": "Логи Decky и плагина (dsd-diag.txt)",
  "bt.addr": "АДРЕС",
  "other.title": "Прочее",
  "lang.label": "Язык",
  "lang.auto": "Авто (как в системе)",
};

export type LangPref = "auto" | "en" | "ru";

const LANG_KEY = "dsd.lang";
let pref: LangPref = (() => {
  try {
    const v = localStorage.getItem(LANG_KEY);
    return v === "en" || v === "ru" ? v : "auto";
  } catch {
    return "auto";
  }
})();
const subs = new Set<() => void>();
const setBackendLang = callable<[string], string>("set_lang");

// The plugin backend writes its log messages and reports in the language chosen here, so tell it at startup
// and whenever the language changes.
export function syncLang(): void {
  try {
    setBackendLang(getLang()).catch(() => {});
  } catch {
    /* the backend is not ready yet: it is told again on the next change */
  }
}

export function setPref(p: LangPref): void {
  pref = p;
  try {
    localStorage.setItem(LANG_KEY, p);
  } catch {
    /* kept in memory only */
  }
  subs.forEach((fn) => fn());
  syncLang();
}

// call in a root component so that everything below it is redrawn when the language changes
export function useLang(): LangPref {
  const [, force] = useState(0);
  useEffect(() => {
    const fn = () => force((n) => n + 1);
    subs.add(fn);
    return () => {
      subs.delete(fn);
    };
  }, []);
  return pref;
}

function systemLang(): "ru" | "en" {
  try {
    const lm = (window as any).LocalizationManager;
    const loc =
      lm?.m_rgLocalesToUse?.[0] ??
      lm?.GetPreferredLocales?.()?.[0] ??
      navigator.language ??
      "en";
    return String(loc).toLowerCase().startsWith("ru") ? "ru" : "en";
  } catch {
    return "en";
  }
}

function getLang(): "ru" | "en" {
  return pref === "auto" ? systemLang() : pref;
}

export function t(key: string, vars?: Record<string, string | number>): string {
  const dict = getLang() === "ru" ? ru : en;
  const s = dict[key] ?? en[key] ?? key;
  return vars ? s.replace(/\{(\w+)\}/g, (_, k) => String(vars[k] ?? "")) : s;
}
