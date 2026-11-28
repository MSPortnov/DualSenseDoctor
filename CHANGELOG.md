# Changelog

## 0.10.4
- Timed-out commands are now killed and reaped; stopping a log stream waits for its child process to exit and escalates to kill if needed.
- The Quick Access status poll waits for each request to finish before scheduling the next one, clears its timeout, and avoids state updates after unmount.
- Added regression tests for subprocess timeout cleanup and stream shutdown.

## 0.10.3
- **Lighter background work.** Once a second the plugin used to read the wake-source counters of the whole system (dozens of sysfs files) and compare them after sleep. Now it does this only when logind announces the sleep (the exact "before sleep" values, so the wake source is found right) and as a rare fallback every 20 s; the once-a-second tick is two clock reads and one stat call. Resume detection is unchanged.
- **The original autosuspend value is kept on disk** (`adapter_pm.json` in the plugin's data folder). If the plugin is killed while the adapter is kept awake, the next start still knows it was `auto` and puts it back when Early connect is switched off or the plugin is unloaded. A damaged or unexpected file is ignored.
- Tests: 102 (were 96): the tick does not read the counters every second, the fallback refresh, the exact snapshot on the sleep announcement, the wake source against that snapshot, the original value across a restart, a damaged file.

## 0.10.2
- **The "Keep the Bluetooth adapter awake" switch is gone.** The adapter's USB autosuspend is now turned off together with **Early connect** (on = the adapter stays awake, off = SteamOS settings are left alone). A test report showed why it matters: with autosuspend left on, the controller woke up half-dead (buttons dead until PS was pressed, wrong lightbar) although every session looked fine to the plugin. If something turns autosuspend on again, the plugin turns it off again. The original value is saved and put back when Early connect is switched off and when the plugin is unloaded. The old `no_autosuspend` key in `settings.json` is dropped on load. `usb_power.txt` in the report says whether the adapter is protected.
- **`deploy.sh fetch` makes the report itself:** it leaves a request file (`~/.dsd-report-request`) in the home folder, the plugin (checking once a second) collects a fresh report like the button does, and the script waits up to 40 seconds and downloads it. The plugin accepts only a regular file owned by the user, never a link, and removes it. If the plugin does not answer, the script says so instead of downloading an old report.
- Tests: 96 (were 81): the adapter (turned off with early connect, undone if set again, original value restored on switch-off and on unload) and the report request (regular file, link, another user's file, folder, one request - one report, a failed report frees the flag).

## 0.10.1
- Code structure, no change in behaviour: `base.py` is split into `config` (paths, constants, defaults), `i18n` (language) and `util` (helpers); the actions on the driver and Steam moved from `engine.py` to `actions.py` (610 lines left in the engine instead of 813); `lab.py` is split into `covers` (cover lookup), `netinfo` (LAN address) and `lab` (Proton list, Steam VDF files, the per-game journal).
- `hid.py`: `sync_rebind` reuses `sync_unbind` / `sync_bind` instead of repeating them, the driver name is read in one place.
- Silent `except Exception` blocks narrowed to the errors that can really happen there (`OSError`, `ProcessLookupError` and so on); the broad handlers that remain are the task boundaries that log the error and keep the plugin running.
- Slider: the value is shown in its label ("Hide the controller from Steam: 2.5 s") because the built-in value box cut it off; it appears only after the plugin has raised the time itself. The Steam pickup hint shows the real return time.
- Tests: 81 (were 63). New: the whole connection sequence (normal, late HID with early connect, no HID, unhealthy HID, disconnect before the verdict), the Steam watch (normal, wrong opening with two rebinds, Steam never notices, Steam not running, two wrong sessions raise the time), the L2CAP answer (values from the trace, guessed values are not counted, stops with the session) and the layout of the raw packet itself.

## 0.10.0
- **The backend is split into modules** (`py_modules/dsd`: `base`, `hid`, `btmon`, `streams`, `store`, `stats`, `engine`, `report`, `lab`); `main.py` is only the entry point. The behaviour is unchanged; if the package cannot be imported, the panel shows the error text. Install and release scripts now copy `py_modules` too.
- **Autotests** (`tests/`, standard library only): the btmon parser (including an excerpt of a real trace), statistics, settings, driver restore, self-tuning, reports, clock jumps. They run in CI together with `tsc --noEmit`.
- **The driver always comes back.** Background tasks are tracked; on unload they are cancelled and the driver is put back. After the restore the plugin checks that the driver is really attached and retries (drivers_probe, then a full rebind); if the link is gone, it asks the controller to connect once. On start, a controller left without a driver (the plugin was killed) gets it back.
- **Self-tuning of the driver return time:** two wrong Steam pickups in a row at the same time raise it by 0.5 s (up to 6 s) and show a notification. The time is a setting again (default 2 s); its slider (2 to 6 s, step 0.5) appears in the settings only after the plugin has raised the time, at the default 2 s it is hidden.
- Settings are written atomically and checked on load (types, range); a damaged file falls back to defaults.
- Intervals around sleep are measured with the monotonic clock (a wall-clock correction after wake can no longer confuse the "after sleep" check).
- Reports: old reports are deleted when a new one is made (and by `deploy.sh`/`update.sh` on install); files are created with `O_EXCL | O_NOFOLLOW` and the archive is written by Python instead of `tar`, so a planted link is never followed; the last three bytes of every Bluetooth address are masked.
- Covers of games that left the library are dropped from the cache.
- Store: `plugin.json` has the store image, screenshots are in `assets/screenshots` and in both READMEs.

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
