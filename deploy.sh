#!/bin/bash
# Run on a MacBook (or any Linux/macOS machine). Builds the plugin locally and installs it on a SteamOS PC over SSH.
#   bash deploy.sh [host]          build + install
#   bash deploy.sh [host] logs     diagnostics: Decky and plugin logs -> ~/Downloads/dsd-diag.txt
#   bash deploy.sh [host] fetch    get the latest Bluetooth report (summary + archive) -> ~/Downloads
# host: IP or PC name. If omitted: $DECK_HOST, then ~/.dsd_host, then steamdeck.local
set -e
# Messages: Russian if the system language is Russian, English otherwise
case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in ru*) RU=1 ;; *) RU=0 ;; esac
say() { if [ "$RU" = 1 ]; then echo "$1"; else echo "$2"; fi; }
cd "$(dirname "$0")"
HOST="${1:-${DECK_HOST:-$(cat "$HOME/.dsd_host" 2>/dev/null || echo steamdeck.local)}}"
MODE="${2:-deploy}"
RUSER="${DECK_USER:-deck}"
TARGET="$RUSER@$HOST"
SSHO="-o StrictHostKeyChecking=accept-new -o ControlMaster=auto -o ControlPath=/tmp/dsd-ssh-%C -o ControlPersist=120"
say ">> ПК: $TARGET" ">> PC: $TARGET"

if [ "$MODE" = "logs" ]; then
  OUT="$HOME/Downloads/dsd-diag.txt"
  ssh -t $SSHO "$TARGET" 'echo "### plugin_loader"; sudo journalctl -u plugin_loader -n 150 --no-pager | grep -iE -B2 -A12 "dualsense|traceback|error" | tail -n 150; echo; echo "### logs dir"; ls -la ~/homebrew/logs/ ~/homebrew/logs/DualSense*/ 2>&1; echo; echo "### plugin log"; tail -n 80 ~/homebrew/logs/DualSense*/*.log 2>&1; echo; echo "### plugin dir"; ls -laR ~/homebrew/plugins/DualSense*/ 2>&1 | head -40' | tee "$OUT"
  say ">> Сохранено: $OUT" ">> Saved: $OUT"
  exit 0
fi

if [ "$MODE" = "fetch" ]; then
  F=$(ssh $SSHO "$TARGET" 'ls -t ~/dualsense-doctor-*-summary.txt 2>/dev/null | head -1')
  [ -n "$F" ] || { say "Отчёта нет. Сначала нажми «Собрать отчёт Bluetooth» в плагине (Настройки)." "No report yet. First press 'Collect Bluetooth report' in the plugin (Settings)."; exit 1; }
  scp $SSHO "$TARGET:$F" "$HOME/Downloads/"
  say ">> Скачано в ~/Downloads: $(basename "$F")" ">> Downloaded to ~/Downloads: $(basename "$F")"
  T="${F%-summary.txt}.tar.gz"
  scp $SSHO "$TARGET:$T" "$HOME/Downloads/" 2>/dev/null && say ">> и полный архив: $(basename "$T")" ">> and the full archive: $(basename "$T")"
  exit 0
fi

# --- Node.js (the system one, downloaded earlier, or download to ~/.local/node)
if ! command -v npm >/dev/null 2>&1; then export PATH="$HOME/.local/node/bin:$PATH"; fi
if ! command -v npm >/dev/null 2>&1; then
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) NP="darwin-arm64" ;;
    Darwin-*)     NP="darwin-x64" ;;
    Linux-*)      NP="linux-x64" ;;
  esac
  say ">> Node.js не найден, скачиваю в ~/.local/node ($NP)" ">> Node.js not found, downloading to ~/.local/node ($NP)"
  mkdir -p "$HOME/.local/node"
  curl -fsSL "https://nodejs.org/dist/v20.18.0/node-v20.18.0-$NP.tar.gz" | tar -xz -C "$HOME/.local/node" --strip-components=1
fi
# --- pnpm (as in the Decky template). If missing, it is installed into ~/.local/pnpm, the system is not touched
export PATH="$HOME/.local/pnpm/bin:$PATH"
if ! command -v pnpm >/dev/null 2>&1; then
  say ">> pnpm не найден, ставлю pnpm@9 в ~/.local/pnpm" ">> pnpm not found, installing pnpm@9 into ~/.local/pnpm"
  npm install -g pnpm@9 --prefix "$HOME/.local/pnpm" --no-audit --no-fund
fi
echo ">> node $(node -v), pnpm $(pnpm -v)"

# pnpm cannot use a node_modules created by npm: remove it once
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
echo ">> pnpm run build"; pnpm run build
[ -f dist/index.js ] || { say "Сборка не создала dist/index.js" "The build did not create dist/index.js"; exit 1; }

say ">> отправка файлов" ">> copying files"
ssh $SSHO "$TARGET" 'rm -rf /tmp/dsd-deploy && mkdir -p /tmp/dsd-deploy'
rsync -az -e "ssh $SSHO" plugin.json package.json main.py LICENSE dist "$TARGET:/tmp/dsd-deploy/"

say ">> установка (спросит пароль sudo на ПК)" ">> installing (asks for the sudo password on the PC)"
ssh -t $SSHO "$TARGET" 'D="$HOME/homebrew/plugins/DualSense Doctor"; sudo rm -rf "$D" && sudo mkdir -p "$D/dist" && sudo cp /tmp/dsd-deploy/plugin.json /tmp/dsd-deploy/package.json /tmp/dsd-deploy/main.py /tmp/dsd-deploy/LICENSE "$D/" && sudo cp -r /tmp/dsd-deploy/dist/* "$D/dist/" && sudo systemctl restart plugin_loader && echo OK'
echo "$HOST" > "$HOME/.dsd_host"
say ">> Готово. Quick Access Menu (кнопка ...) -> DualSense Doctor." ">> Done. Quick Access Menu (... button) -> DualSense Doctor."
say ">> Если сразу после обновления магазин Steam откроется пустым, откройте его ещё раз: это Steam перезагружает интерфейс после перезапуска Decky, к плагину не относится." ">> If the Steam Store opens blank right after an update, open it once more: Steam reloads its interface after Decky restarts, it is not related to the plugin."
