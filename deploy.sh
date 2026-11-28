#!/bin/bash
# Run on a MacBook (or any Linux/macOS machine). Builds the plugin locally and installs it on a SteamOS PC over SSH.
#   bash deploy.sh [host]          build + install
#   bash deploy.sh [host] logs     diagnostics: Decky and plugin logs -> ~/Downloads/dsd-diag.txt
#   bash deploy.sh [host] fetch    ask the plugin for a fresh Bluetooth report and download it (summary + archive) -> ~/Downloads
# host: IP or PC name. If omitted: $DECK_HOST, then ~/.dsd_host, then steamdeck.local
set -e
# Messages: Russian if the system language is Russian, English otherwise
case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in ru*) RU=1 ;; *) RU=0 ;; esac
say() { if [ "$RU" = 1 ]; then echo "$1"; else echo "$2"; fi; }
cd "$(dirname "$0")"
# bash deploy.sh [host] [deploy|fetch|logs]; the mode alone also works: "bash deploy.sh fetch" uses the remembered host
case "${1:-}" in
  deploy|fetch|logs) MODE="$1"; HOST_ARG="" ;;
  *) HOST_ARG="${1:-}"; MODE="${2:-deploy}" ;;
esac
HOST="${HOST_ARG:-${DECK_HOST:-$(cat "$HOME/.dsd_host" 2>/dev/null || echo steamdeck.local)}}"
RUSER="${DECK_USER:-deck}"
TARGET="$RUSER@$HOST"
SSHO="-o StrictHostKeyChecking=accept-new -o ControlMaster=auto -o ControlPath=/tmp/dsd-ssh-%C -o ControlPersist=120"
say ">> ПК: $TARGET" ">> PC: $TARGET"

# Remove old reports after a successful installation.
clean_reports() {
  if ssh $SSHO "$TARGET" 'rm -f ~/dualsense-doctor-[0-9]*-summary.txt ~/dualsense-doctor-[0-9]*.tar.gz'; then
    say ">> старые отчёты на ПК удалены" ">> old reports on the PC removed"
  else
    say ">> не удалось подключиться к ПК: проверьте адрес ($HOST)" ">> could not connect to the PC: check the address ($HOST)"
    exit 1
  fi
}

if [ "$MODE" = "logs" ]; then
  OUT="$HOME/Downloads/dsd-diag.txt"
  ssh -t $SSHO "$TARGET" 'echo "### plugin_loader"; sudo journalctl -u plugin_loader -n 150 --no-pager | grep -iE -B2 -A12 "dualsense|traceback|error" | tail -n 150; echo; echo "### logs dir"; ls -la ~/homebrew/logs/ ~/homebrew/logs/DualSense*/ 2>&1; echo; echo "### plugin log"; tail -n 80 ~/homebrew/logs/DualSense*/*.log 2>&1; echo; echo "### plugin dir"; ls -laR ~/homebrew/plugins/DualSense*/ 2>&1 | head -40' | tee "$OUT"
  say ">> Сохранено: $OUT" ">> Saved: $OUT"
  exit 0
fi

if [ "$MODE" = "fetch" ]; then
  # the plugin collects the report itself: a request file in the home folder (it checks for it every second)
  OLD=$(ssh $SSHO "$TARGET" 'ls -t ~/dualsense-doctor-*-summary.txt 2>/dev/null | head -1') || { say ">> не удалось подключиться к ПК: проверьте адрес ($HOST)" ">> could not connect to the PC: check the address ($HOST)"; exit 1; }
  ssh $SSHO "$TARGET" 'touch ~/.dsd-report-request'
  say ">> Запросил отчёт у плагина, жду..." ">> Asked the plugin for a report, waiting..."
  F=""
  for _ in $(seq 1 40); do
    sleep 1
    N=$(ssh $SSHO "$TARGET" 'ls -t ~/dualsense-doctor-*-summary.txt 2>/dev/null | head -1')
    if [ -n "$N" ] && [ "$N" != "$OLD" ] && ssh $SSHO "$TARGET" "test -f '${N%-summary.txt}.tar.gz'"; then F="$N"; break; fi
  done
  if [ -z "$F" ]; then
    ssh $SSHO "$TARGET" 'rm -f ~/.dsd-report-request'
    say "Плагин не собрал отчёт за 40 секунд (не запущен или завис). Нажми «Собрать отчёт Bluetooth» в плагине (Настройки) и повтори команду." "The plugin did not make a report in 40 seconds (not running or stuck). Press 'Collect Bluetooth report' in the plugin (Settings) and run the command again."
    exit 1
  fi
  scp $SSHO "$TARGET:$F" "$HOME/Downloads/"
  say ">> Скачано в ~/Downloads: $(basename "$F")" ">> Downloaded to ~/Downloads: $(basename "$F")"
  T="${F%-summary.txt}.tar.gz"
  scp $SSHO "$TARGET:$T" "$HOME/Downloads/"
  say ">> и полный архив: $(basename "$T")" ">> and the full archive: $(basename "$T")"
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
REMOTE_STAGE=$(ssh $SSHO "$TARGET" 'mktemp -d /tmp/dsd-deploy.XXXXXXXX')
case "$REMOTE_STAGE" in
  /tmp/dsd-deploy.[a-zA-Z0-9][a-zA-Z0-9][a-zA-Z0-9][a-zA-Z0-9][a-zA-Z0-9][a-zA-Z0-9][a-zA-Z0-9][a-zA-Z0-9]) ;;
  *) echo "Unexpected SSH staging path" >&2; exit 1 ;;
esac
trap 'ssh $SSHO "$TARGET" "rm -rf -- $REMOTE_STAGE" || true' EXIT
rsync -az --exclude __pycache__ -e "ssh $SSHO" plugin.json package.json main.py LICENSE install-plugin.sh dist py_modules "$TARGET:$REMOTE_STAGE/"

say ">> установка (спросит пароль sudo на ПК)" ">> installing (asks for the sudo password on the PC)"
ssh -t $SSHO "$TARGET" "sudo bash '$REMOTE_STAGE/install-plugin.sh' '$REMOTE_STAGE' \"\$HOME/homebrew/plugins\""
clean_reports
echo "$HOST" > "$HOME/.dsd_host"
say ">> Готово. Quick Access Menu (кнопка ...) -> DualSense Doctor." ">> Done. Quick Access Menu (... button) -> DualSense Doctor."
say ">> Если сразу после обновления магазин Steam откроется пустым, откройте его ещё раз: это Steam перезагружает интерфейс после перезапуска Decky, к плагину не относится." ">> If the Steam Store opens blank right after an update, open it once more: Steam reloads its interface after Decky restarts, it is not related to the plugin."
