#!/usr/bin/env bash
# DualSense Doctor installer / updater for the SteamOS machine (run it in a Desktop Mode terminal or over SSH).
#   curl -fsSL https://raw.githubusercontent.com/MSPortnov/DualSenseDoctor/main/install.sh | bash
# Downloads the latest GitHub release, installs it to ~/homebrew/plugins/"DualSense Doctor" and restarts Decky.
# Running it again updates the plugin. Settings are kept (stored outside the plugin folder).
set -euo pipefail

BASE="${DSD_BASE:-https://github.com/MSPortnov/DualSenseDoctor/releases/latest/download}"
DEST="$HOME/homebrew/plugins/DualSense Doctor"

command -v curl >/dev/null    || { echo "curl not found"; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo ">> Downloading the latest DualSense Doctor release"
curl -fsSL "$BASE/DualSenseDoctor.zip" -o "$TMP/DualSenseDoctor.zip"
curl -fsSL "$BASE/DualSenseDoctor.zip.sha256" -o "$TMP/DualSenseDoctor.zip.sha256"
(cd "$TMP" && sha256sum -c DualSenseDoctor.zip.sha256 >/dev/null) || { echo "Checksum mismatch, aborting."; exit 1; }

python3 - "$TMP" <<'PY'
import sys, zipfile
zipfile.ZipFile(sys.argv[1] + "/DualSenseDoctor.zip").extractall(sys.argv[1] + "/x")
PY
[ -f "$TMP/x/DualSense Doctor/install-plugin.sh" ] || { echo "Unexpected archive layout, aborting."; exit 1; }

echo ">> Installing to $DEST (the sudo password may be asked)"
sudo bash "$TMP/x/DualSense Doctor/install-plugin.sh" "$TMP/x/DualSense Doctor" "$(dirname "$DEST")"
echo ">> Done. Installed version: $(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$DEST/package.json" | head -1)"
echo ">> If the Steam Store opens blank right after an update, open it once more: Steam reloads its interface after Decky restarts, it is not related to the plugin."
