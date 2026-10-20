#!/usr/bin/env bash
# 解除安裝「KEDGE JobTeaser 通知信 → Telegram」：卸載 launchd 排程、刪 plist、刪 Keychain 兩筆憑證。
# 去重狀態檔（~/Library/Application Support/jobteaser-mail/seen.json）預設保留，--purge 才刪。
#   scripts/uninstall_jobteaser_mail.sh [--purge]      DRY_RUN=1 只印出會做什麼
set -eu

LABEL="com.grace.jobteaser-mail"
SVC_TOKEN="jobteaser-mail-telegram-token"
SVC_CHAT="jobteaser-mail-telegram-chat"
PLIST="${PLIST_PATH:-${HOME}/Library/LaunchAgents/${LABEL}.plist}"
STATE_DIR="${JOBTEASER_MAIL_STATE_DIR:-${HOME}/Library/Application Support/jobteaser-mail}"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

run() {
  if [ "${DRY_RUN:-0}" = "1" ]; then echo "[DRY_RUN] $*"; else "$@"; fi
}
runq() {
  if [ "${DRY_RUN:-0}" = "1" ]; then echo "[DRY_RUN] $*"; else "$@" >/dev/null 2>&1; fi
}

runq launchctl bootout "gui/$(id -u)/${LABEL}" || echo "（排程原本就沒載入）"
if [ -e "${PLIST}" ]; then run rm -f "${PLIST}"; else echo "（沒有 ${PLIST}）"; fi
for svc in "${SVC_TOKEN}" "${SVC_CHAT}"; do
  runq security delete-generic-password -s "${svc}" || echo "（Keychain 沒有 ${svc}）"
done
if [ "${PURGE}" = "1" ]; then
  run rm -rf "${STATE_DIR}"
  echo "已刪除去重狀態 ${STATE_DIR}"
else
  echo "保留去重狀態 ${STATE_DIR}（要連它一起刪請加 --purge）。log 在 ~/Library/Logs/jobteaser-mail.log，需要的話自己刪。"
fi
echo "解除安裝完成。"
