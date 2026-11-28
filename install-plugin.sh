#!/usr/bin/env bash
# Install a prepared build into Decky on SteamOS. Run as root.
set -euo pipefail

source_dir=$1
plugins_dir=$2
dest="$plugins_dir/DualSense Doctor"
homebrew_dir=$(dirname "$plugins_dir")

for file in plugin.json package.json main.py LICENSE dist/index.js py_modules/dsd/engine.py; do
    if [ ! -s "$source_dir/$file" ]; then
        echo "Incomplete plugin build: $file" >&2
        exit 1
    fi
done

mkdir -p "$plugins_dir"
exec 9>"$homebrew_dir/.dualsense-doctor-install.lock"
flock -x 9
if [ "$(stat -c %d "$homebrew_dir")" != "$(stat -c %d "$plugins_dir")" ]; then
    echo "The staging and Decky plugin folders must be on the same filesystem." >&2
    exit 1
fi

work=$(mktemp -d "$homebrew_dir/.dualsense-doctor-install.XXXXXX")
candidate="$work/new"
backup="$work/previous"
switching=false

cleanup() {
    local status=$?
    trap - EXIT
    trap '' INT TERM
    if [ "$status" -ne 0 ] && $switching; then
        if { [ ! -e "$candidate" ] && ! mv -T -- "$dest" "$candidate"; } ||
           { { [ -e "$backup" ] || [ -L "$backup" ]; } && ! mv -T -- "$backup" "$dest"; }; then
            echo "Could not roll back the plugin directory. Staging kept at $work" >&2
            exit "$status"
        fi
        if ! systemctl restart plugin_loader; then
            echo "Plugin directory rolled back. Decky Loader could not restart." >&2
        else
            echo "Installation failed. Plugin directory rolled back." >&2
        fi
    fi
    rm -rf -- "$work"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir "$candidate"
cp -r -- "$source_dir"/{plugin.json,package.json,main.py,LICENSE,dist,py_modules} "$candidate/"
switching=true
if [ -e "$dest" ] || [ -L "$dest" ]; then
    mv -T -- "$dest" "$backup"
fi
mv -T -- "$candidate" "$dest"
systemctl restart plugin_loader
systemctl is-active --quiet plugin_loader
switching=false
