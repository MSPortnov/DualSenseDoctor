# DualSense Doctor

[![build](https://github.com/MSPortnov/DualSenseDoctor/actions/workflows/build.yml/badge.svg)](https://github.com/MSPortnov/DualSenseDoctor/actions/workflows/build.yml) [![License: GPL v3](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)

**English** | [Русский](README.ru.md)

A [Decky Loader](https://decky.xyz/) plugin for the DualSense controller on SteamOS. Two parts in one plugin:

1. **Lab** picks launch options for each game (Proton, `PROTON_*` variables, Steam Input) from a grid of cover art.
2. **Doctor** fixes the Bluetooth connection after sleep when the controller wakes the PC.

Tested with a DualSense Edge over Bluetooth on a SteamOS desktop PC. Other setups may behave differently, reports are welcome (see [Reporting a problem](#reporting-a-problem)).

## Lab: launch options per game

Open the Quick Access menu, press "Set up games" and pick a game from the cover grid (7 per row, alphabetical, like Steam's library). Covers come from Steam's local cache and your own artwork in `userdata/*/config/grid` (this is how non-Steam games get covers), then from Steam's servers; a name tile is shown when there is no cover.

For each game you choose one setup: native support, `PROTON_DISABLE_HIDRAW=1`, DualShock 4 emulation and others, or "Steam Input only". The plugin writes the launch options and the Proton version, switches Steam Input on or off for you and starts the game.

Steam Input is enabled only by the "Steam Input only" setup and is off for the others. The plugin always writes an explicit value (2 = on, 0 = off) instead of "default", which depends on global Steam settings. If Steam does not accept the value, the game is not launched. If the launch options were edited by hand and match no setup, Steam Input is left alone.

## Doctor: Bluetooth after sleep

The problem: when the PC wakes up the kernel drops the controller's first packet. The PC then closes the "empty" connection at about 4 seconds, and if the link is kept the controller itself drops it at about 28 seconds. Steam may also open the controller the wrong way (default lightbar color, buttons A and B swapped).

What the plugin does (all of it works without configuration):

- **Early connect:** if the HID channel has not appeared after about 1 second, the plugin opens it itself.
- **Answer to the controller:** sends an L2CAP "pending" response to the lost request 4 times, 15 seconds apart, which stops the 28-second drop.
- **Steam pickup:** the HID device is created right away (the link needs it), but its driver is detached for about 5 seconds, so Steam sees the controller for the first time when it is no longer busy waking up and opens it the right way (one "connected" notification). If Steam still opens it wrongly (the Steam log says "Invalid or missing unit serial number"), the plugin rebinds the driver, up to twice.
- Optionally keeps the Bluetooth adapter from autosuspending, and records an HCI trace (btmon) for diagnostics.

Known limitation: because of the pickup step the controller becomes usable about 5 to 6 seconds after wake, so buttons pressed earlier are ignored.

## Installing and updating

Four ways, pick the one that suits you. The same command (or the same ZIP) installs the plugin and updates it later,
and settings are kept because they are stored outside the plugin folder.

**1. From GitHub, on the SteamOS machine itself.** Switch to Desktop Mode, open a terminal (Konsole) and run:

    curl -fsSL https://raw.githubusercontent.com/MSPortnov/DualSenseDoctor/main/install.sh | bash

It downloads the latest release, checks its SHA-256 and installs it into `~/homebrew/plugins/DualSense Doctor`.
The first time you need a password for the `deck` user (set it with `passwd` if you never did): `sudo` asks for it.
The script is short, [read it here](install.sh) before you run it.

**2. From GitHub, from another computer (macOS or Linux).** One time, on the SteamOS machine, turn SSH on and note its IP address:

    sudo systemctl enable --now sshd
    ip -4 addr | grep inet

Then, on your computer (put your IP address instead of `192.168.1.101`):

    ssh -t deck@192.168.1.101 'curl -fsSL https://raw.githubusercontent.com/MSPortnov/DualSenseDoctor/main/install.sh | bash'

**3. Through Decky, without a terminal.** In Decky open Settings > General and turn on *Developer mode*. Then open
*Developer*, choose *Install Plugin from URL* and paste:

    https://github.com/MSPortnov/DualSenseDoctor/releases/latest/download/DualSenseDoctor.zip

You can also download `DualSenseDoctor.zip` from the [Releases](https://github.com/MSPortnov/DualSenseDoctor/releases)
page and choose *Install Plugin from ZIP* in the same section.

**4. From source.** Needs a Mac or Linux computer with SSH access to the SteamOS machine (Node.js 18+ and
[pnpm](https://pnpm.io/) 9 are installed automatically if missing):

    git clone https://github.com/MSPortnov/DualSenseDoctor.git
    bash DualSenseDoctor/deploy.sh 192.168.1.101

The address is remembered in `~/.dsd_host`. To update: `cd DualSenseDoctor && git pull && bash deploy.sh`.
Directly on the SteamOS machine the same is done by `bash update.sh`. Both scripts build the plugin
(`pnpm install --frozen-lockfile`, `pnpm run build`) and install it; Node.js goes into `~/.local/node` and pnpm into `~/.local/pnpm`.

Installing or updating restarts Decky Loader, and Steam reloads its interface when that happens. If the Steam Store
opens blank right after an update, open it once more; this is not related to the plugin (it also happens after
`sudo systemctl restart plugin_loader`).

## Why the plugin runs as root

`plugin.json` declares the `root` flag. Doctor needs it for things a normal user cannot do:

- writing to `/sys` to detach and re-attach the HID driver (`unbind` / `bind`) and to turn off USB autosuspend of the Bluetooth adapter;
- opening a raw HCI socket to send the L2CAP response;
- running `btmon` for the optional HCI trace.

Lab alone would not need it. Nothing is downloaded or sent anywhere, and all changes are listed above.

## Reporting a problem

In the plugin open "Bluetooth settings" and press "Collect Bluetooth report". The report is saved in the home folder of the SteamOS user (`dualsense-doctor-DATE-summary.txt` and a full `.tar.gz` archive). To download it from another computer:

```bash
bash deploy.sh ADDRESS fetch   # report and archive go to ~/Downloads
bash deploy.sh ADDRESS logs    # Decky and plugin logs go to ~/Downloads/dsd-diag.txt
```

The report contains system logs and Bluetooth device addresses, so look through it before posting it publicly. The plugin's own logs are in `~/homebrew/logs/DualSense Doctor/`.

## Settings

Doctor works out of the box. The Bluetooth settings section has switches for each fix (early connect and its delay, answer to the controller, Steam pickup, no adapter autosuspend, HCI trace). The language (Auto follows Steam, English or Русский) is chosen at the bottom of the panel. It is used for the interface, for the plugin's own log messages (`events.log`, session notes) and for the Bluetooth report, so a report is written in the language you picked. The helper scripts print their messages in the system language.

## For developers

- Backend: `main.py` (Python standard library only, runs as root). Frontend: `src/` (React and TypeScript, built with
  `pnpm run build` into `dist/`).
- The Bluetooth engine follows the controller through BlueZ (`dbus-monitor`, `bluetoothctl`), the kernel log and an HCI trace (`btmon`).
- CI: `build.yml` builds the frontend and compiles `main.py` on every push and pull request.
- Release: raise the version in `package.json` **and** `VERSION` in `main.py`, add a line to `CHANGELOG.md`, commit,
  then `git tag v1.2.3 && git push origin v1.2.3`. GitHub Actions builds the plugin and attaches `DualSenseDoctor.zip`
  (always this name, plus a `.sha256` file) to a new release; the build fails if the tag, `package.json` and `main.py` differ.
- Commit `pnpm-lock.yaml`: the build uses `pnpm install --frozen-lockfile`.

## License

Copyright (C) 2026 MSPortnov. This program is free software, licensed under the [GNU General Public License v3.0](LICENSE). Third-party components and icon credits: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
