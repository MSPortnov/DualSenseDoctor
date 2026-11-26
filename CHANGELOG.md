# Changelog

## 0.9.4
- The driver is detached for about 2 seconds after wake (`STEAM_BIND_AT = 2.0`, was 5): tested on the test machine, Steam still opens the controller the right way and it becomes usable about 3 seconds after wake instead of 6. The slider tried in 0.9.3 is removed. If Steam starts opening the controller the wrong way again (`steam_rebind` in the statistics goes up), the time can be raised in a separate fix.

## 0.9.2
- Removed the "First attempt after" slider: early connect now always starts 0.3 s after the connection (`EARLY_CONNECT_DELAY`). In the logs the HID channel appears about 0.3 s after that attempt; a slower start is not needed. If trouble shows up on some setup, the delay can be raised in a separate fix.
- The settings profile used in the statistics is now `early_connect+l2cap_answer@pending+steam_pickup@8` (no delay in it).

## 0.9.1
- Fixed the HCI trace parser: it looked for the old btmon wording ("ACL Data RX"), but current btmon prints "> ACL: Handle ..." / "> BR-ACL: ...", so the lost request was never found and the answer always used the fallback values. It now reads the real format (checked on traces recorded on the test machine: the lost request is `ident=1`, PSM 1, source channel 64, received before the "Connect Complete" event). The same wording fix restores the filter that keeps the HID report stream out of the dumps.
- Removed the tracking of how long a connection lasted: the "link held N s / link active" notes in the fix log, the `duration` and end time of a session, the `SHORT` result ("dropped after N s"), the disconnect-reason analysis and the `btmon_NNNN_END` dumps. A connection that ends before the check is finished is simply marked `ABORTED` (not a failure). The statistics are now counted per connection after sleep (`ok`, `fixed`, `failed`, `steam_rebind`, `avg_ttu`).

## 0.9.0
- L2CAP answer without guessing: btmon is started automatically when logind announces sleep (`PrepareForSleep`, plus the kernel's "PM: suspend entry" as a second trigger) and stopped about 90 seconds after wake, so `ident` and the source channel id come from the real lost request. When the trace has no such request, the old defaults are used, marked "guessed" in `events.log` (`l2cap_params` in the session) and not counted as a fix. The request is lost about 2.5 s before the kernel prints "PM: suspend exit", so a trace started on resume would be too late.
- The driver is detached for the Steam pickup only for connections after sleep, so a controller switched on at the desktop is not delayed.
- Honest results: the preventive driver detach is no longer counted in `fixes_applied`, so a normal session stays `OK`; `FIXED` means something was actually corrected. The statistics got `steam_rebind` (how often a rebind was needed).
- The fix log lists `SHORT` (dropped before the check) only for connections after sleep.
- README: how the L2CAP parameters are found and why that part is fragile.

## 0.8.11
- The focus pulse is once every 1.3 seconds (19 pulses, about 25 seconds).

## 0.8.10
- The focus pulse is once every 1.5 seconds (17 pulses, about 25 seconds).
- The plugin panel in the Quick Access Menu opens again only when the page is left with Back. Leaving the page any other way (library, home screen, store) no longer pops the panel up.

## 0.8.9
- The focus pulse is once a second (25 pulses, 25 seconds).
- Back: the window's own `history.back()` is no longer used; leaving the page is one `Navigation.NavigateBack()`.
- The empty Steam Store page seen right after installing the plugin is not caused by the plugin: the scripts restart Decky Loader and Steam reloads its interface. `deploy.sh` / `update.sh` and the README now say so.

## 0.8.7
- Game grid: the focus ring and glow pulse for about 25 seconds after the focus lands on a tile (like Steam), then stay steady. Tunable with `PULSE_PERIOD_S` and `PULSE_COUNT`.

## 0.8.6
- Game grid: removed the second (inner) focus ring that Steam drew around every focusable element on top of the tile's own focus look (`noFocusRing` plus a CSS override).

## 0.8.5
- Game grid redone to Steam's proportions (measured on a screenshot of Steam's own library): smaller tiles, side margin 3.3 % of the width, gaps 2.2 % across and 2.8 % down.
- Focus like Steam's: the cover grows by 4.5 %, a thin ring stands 3 px away from it, and a soft glow in the color of the cover spreads around the tile.

## 0.8.4
- Game grid: focus looks like Steam's library (the tile grows a little, white ring, soft glow, rises above its neighbours).
- The temporary layout diagnostics were removed. The bottom margin that works: a page as tall as the screen, an empty space at the end and `scroll-padding-bottom`, which Steam respects when it scrolls to the focused card.

## 0.8.3
- Reverted the measured page height of 0.8.2 (it was taken during Steam's slide-in animation and made the page too short). Added a temporary layout diagnostics line (`DEBUG_LAYOUT`) to set the bottom margin from real measurements.

## 0.8.1
- Preparation for publication: all code comments and docstrings are in English; the license is GPL-3.0; the Russian README is a full translation of the English one.
- No functional changes.

## 0.8.0
- The plugin is bilingual end to end: the interface, the plugin's log messages (`events.log`, session notes) and the Bluetooth report follow the language chosen in the plugin (Auto / English / Русский). The interface tells the backend the language at startup and whenever it changes; the backend keeps it in `settings.json`.
- `deploy.sh` and `update.sh` print their messages in the system language (Russian or English).
- Game grid: the page now fills the screen and ends with an empty space (`BOTTOM_PAD`), so the last row can be scrolled above Steam's bottom hint bar.

## 0.7.5
- Smaller bottom margin of the game grid.
- `plugin.json`: author set, only the `root` flag, English store description.
- README in English and Russian, GitHub Actions build and release workflows, issue template.

## 0.7.0 - 0.7.4
- Lab: games are shown as a grid of covers (7 per row, alphabetical). Covers come from the Steam cache on disk, custom artwork in `userdata/*/config/grid` and Steam's servers.
- Search and captions under the covers removed, the list is no longer limited in size.
- The scroll area ends above Steam's bottom hint bar.

## 0.6.4 - 0.6.9
- Steam pickup recognises when Steam opened the controller the wrong way (buttons A and B swapped, default lightbar) and fixes it.
- 0.6.9: the HID driver is detached for ~5 seconds after wake so Steam sees the controller for the first time in a calm moment: one "connected" notification and the right mode.
- L2CAP "pending" answers: 4 times, 15 seconds apart (0.6.5).
- Build moved to pnpm (0.6.6).

## 0.6.0 - 0.6.3
- Cleanup: removed disabled experimental options, legacy settings migrations and unused code; smaller state sent to the panel; the backend checks that the engine is running.
- The per-game launch journal was removed.
