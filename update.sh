#!/bin/bash
# Builds and installs DualSense Doctor into Decky Loader with one command.
set -e
# Messages: Russian if the system language is Russian, English otherwise
case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in ru*) RU=1 ;; *) RU=0 ;; esac
say() { if [ "$RU" = 1 ]; then echo "$1"; else echo "$2"; fi; }
cd "$(dirname "$(readlink -f "$0")")"
DEST="$HOME/homebrew/plugins/DualSense Doctor"
NODE_VER="v20.18.0"

# 1. Node.js: the system one, downloaded earlier, or download to ~/.local/node
if ! command -v npm >/dev/null 2>&1; then
  export PATH="$HOME/.local/node/bin:$PATH"
fi
if ! command -v npm >/dev/null 2>&1; then
  say ">> Node.js не найден, скачиваю $NODE_VER в ~/.local/node" ">> Node.js not found, downloading $NODE_VER to ~/.local/node"
  mkdir -p "$HOME/.local/node"
  curl -fsSL "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-x64.tar.xz" \
    | tar -xJ -C "$HOME/.local/node" --strip-components=1
fi

# 2. pnpm (as in the Decky template). If missing, it is installed into ~/.local/pnpm
export PATH="$HOME/.local/pnpm/bin:$PATH"
if ! command -v pnpm >/dev/null 2>&1; then
  say ">> pnpm не найден, ставлю pnpm@9 в ~/.local/pnpm" ">> pnpm not found, installing pnpm@9 into ~/.local/pnpm"
  npm install -g pnpm@9 --prefix "$HOME/.local/pnpm" --no-audit --no-fund
fi
echo ">> node $(node -v), pnpm $(pnpm -v)"

# 3. Dependencies (a node_modules created by npm is removed once)
if [ -d node_modules ] && [ ! -d node_modules/.pnpm ]; then
  say ">> удаляю node_modules, созданную npm" ">> removing node_modules created by npm"; rm -rf node_modules
fi
if [ ! -d node_modules ] || [ package.json -nt node_modules ] || [ pnpm-lock.yaml -nt node_modules ]; then
  if [ -f pnpm-lock.yaml ]; then
    echo ">> pnpm install --frozen-lockfile"; pnpm install --frozen-lockfile
  else
    say ">> pnpm install (создаст pnpm-lock.yaml)" ">> pnpm install (creates pnpm-lock.yaml)"; pnpm install
  fi
  touch node_modules
fi

# 4. Build the frontend
echo ">> pnpm run build"
pnpm run build
[ -f dist/index.js ] || { say "Сборка не создала dist/index.js" "The build did not create dist/index.js"; exit 1; }

# 5. Install into Decky and restart
say ">> установка в $DEST" ">> installing to $DEST"
sudo rm -rf "$DEST"
sudo mkdir -p "$DEST/dist"
sudo cp plugin.json package.json main.py LICENSE "$DEST/"
sudo cp -r dist/* "$DEST/dist/"
sudo systemctl restart plugin_loader
say ">> Готово. Quick Access Menu (кнопка ...) -> DualSense Doctor." ">> Done. Quick Access Menu (... button) -> DualSense Doctor."
say ">> Если сразу после обновления магазин Steam откроется пустым, откройте его ещё раз: это Steam перезагружает интерфейс после перезапуска Decky, к плагину не относится." ">> If the Steam Store opens blank right after an update, open it once more: Steam reloads its interface after Decky restarts, it is not related to the plugin."
