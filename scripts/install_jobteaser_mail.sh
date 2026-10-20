#!/usr/bin/env bash
# 安裝「KEDGE JobTeaser 通知信 → Telegram」的 Mac 本機排程（不需要 sudo）：
#   1. 把 Telegram bot token／chat id 存進 macOS Keychain
#   2. 寫 ~/Library/LaunchAgents/com.grace.jobteaser-mail.plist（每天 09:30 本地時間跑一次，沒有 RunAtLoad）
#   3. launchctl bootstrap 載入（已存在會先 bootout）
#
#   scripts/install_jobteaser_mail.sh              # 正式安裝
#   scripts/install_jobteaser_mail.sh --print-plist   # 只把 plist 印到 stdout：不問問題、不寫任何東西、不動 launchctl／Keychain
#   DRY_RUN=1 scripts/install_jobteaser_mail.sh       # 只印出每一步會做什麼，不執行
#
# 關於 token 的安全性：token 用 `security add-generic-password ... -w`（後面不接值）讓 security 自己在終端機
# 隱藏輸入，所以 token 不會出現在指令列（ps 看不到）、shell history 或這支腳本的變數裡。
# 只有 chat id 會走 `-w "${chat}"`（它不是秘密，只是個數字；代價是執行瞬間 ps 看得到它）。
set -eu

LABEL="com.grace.jobteaser-mail"
SVC_TOKEN="jobteaser-mail-telegram-token"
SVC_CHAT="jobteaser-mail-telegram-chat"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
PLIST="${PLIST_PATH:-${HOME}/Library/LaunchAgents/${LABEL}.plist}"
LOG="${LOG_PATH:-${HOME}/Library/Logs/jobteaser-mail.log}"
HOUR="${RUN_HOUR:-9}"
MINUTE="${RUN_MINUTE:-30}"

xml_escape() {
  local s="$1"
  s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"
  printf '%s' "${s}"
}

find_python() {
  local p
  p="$(command -v python3 || true)"
  if [ -z "${p}" ]; then
    echo "找不到 python3。請先安裝（例如 xcode-select --install 或 brew install python）。" >&2
    exit 1
  fi
  # 取實際路徑（排除 shim／symlink），launchd 沒有你的 shell PATH，要用絕對路徑
  "${p}" -c 'import os, sys; print(os.path.realpath(sys.executable))'
}

print_plist() {
  local py="$1"
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "${py}")</string>
    <string>$(xml_escape "${REPO}")/src/jobteaser_mail_digest.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$(xml_escape "${REPO}")</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>${HOUR}</integer>
    <key>Minute</key>
    <integer>${MINUTE}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>$(xml_escape "${LOG}")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "${LOG}")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin</string>
  </dict>
</dict>
</plist>
PLIST
}

if [ "${1:-}" = "--print-plist" ]; then
  print_plist "${PYTHON_PATH:-$(find_python)}"
  exit 0
fi

run() {
  if [ "${DRY_RUN:-0}" = "1" ]; then echo "[DRY_RUN] $*"; else "$@"; fi
}
# 同 run，但吞掉輸出（用在「可能本來就不存在」的步驟）
runq() {
  if [ "${DRY_RUN:-0}" = "1" ]; then echo "[DRY_RUN] $*"; else "$@" >/dev/null 2>&1; fi
}

if [ "$(uname -s)" != "Darwin" ]; then
  echo "這個工具只能在 macOS 上安裝（用 Apple Mail、Keychain、launchd）。" >&2
  exit 1
fi

PY="$(find_python)"
echo "Python：${PY}"
echo "專案：${REPO}"
if ! "${PY}" -c 'import requests' 2>/dev/null; then
  echo "缺少 requests。請先執行：${PY} -m pip install requests   （或 pip3 install requests），再重跑本腳本。" >&2
  exit 1
fi

echo
echo "步驟 1/3：存 Telegram 憑證到 Keychain（-U＝已存在就更新）"
echo "下面 security 會問 token：輸入時不會顯示，要輸入兩次。"
run security add-generic-password -U -s "${SVC_TOKEN}" -a "${USER}" -w
printf 'Telegram chat id（數字，可為負）：' >&2
if [ "${DRY_RUN:-0}" = "1" ]; then
  chat="(dry-run)"; echo
else
  IFS= read -r chat
fi
[ -n "${chat}" ] || { echo "chat id 不能是空的。" >&2; exit 1; }
run security add-generic-password -U -s "${SVC_CHAT}" -a "${USER}" -w "${chat}"

echo
echo "步驟 2/3：寫 ${PLIST}（每天 $(printf '%02d:%02d' "${HOUR}" "${MINUTE}") 本地時間）"
if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[DRY_RUN] 寫入 plist 內容："; print_plist "${PY}"
else
  mkdir -p "$(dirname "${PLIST}")" "$(dirname "${LOG}")"
  print_plist "${PY}" > "${PLIST}"
  plutil -lint "${PLIST}"
fi

echo
echo "步驟 3/3：載入排程"
UIDN="$(id -u)"
runq launchctl bootout "gui/${UIDN}/${LABEL}" || true
run launchctl bootstrap "gui/${UIDN}" "${PLIST}"

cat <<MSG

安裝完成。接下來：
  1. 先手動測一次（會跳出「Terminal 想控制郵件」的授權視窗，按「好」）：
       cd "${REPO}" && ${PY} src/jobteaser_mail_digest.py --dry-run
     沒跳視窗或被擋：系統設定 > 隱私權與安全性 > 自動化，打開 Terminal（或 python3）底下的「郵件」。
  2. 也要讓排程本身拿到授權（背景執行跳的視窗是另一個程式）：
       launchctl kickstart gui/${UIDN}/${LABEL}
     然後看 log：tail -n 20 "${LOG}"
  3. 之後每天 $(printf '%02d:%02d' "${HOUR}" "${MINUTE}") 自動跑。Mac 睡眠時錯過的排程會在醒來後補跑；關機則會錯過那天。
解除安裝：${SCRIPT_DIR}/uninstall_jobteaser_mail.sh   （加 --purge 連去重狀態一起刪）
MSG
