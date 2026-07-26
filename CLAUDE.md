# grace_agent

> 中文摘要：專案路由檔。通用制度在全域，這裡只放本專案專屬事項。

All operating rules live in the global institution — follow `~/.claude/CLAUDE.md` and the
files it routes to. Do not restate global rules here; this file only points.

## Project-specific facts

- **Purpose: 每日職缺推播工具。** 每天 09:00（台北）用 GitHub Actions 抓 104 公開
  搜尋 API，去重後把新職缺推到使用者 Grace 的 Telegram。設定與使用方式見 `README.md`。
- **Language/run**: Python 3.12+，只依賴 `requests`。入口 `src/daily_jobs.py`，
  本機測試用 `python src/daily_jobs.py --dry-run`（不推播、不寫狀態）。
- **Key files**: `config.json`（搜尋條件）、`data/seen_jobs.json`（去重狀態，由
  Actions 每天 commit 回來）、`.github/workflows/daily-jobs.yml`（排程）。
- **104 端點注意**（驗證 2026-07-06）：用 `https://www.104.com.tw/jobs/search/api/jobs`
  （舊的 `/jobs/search/list` 已 302 到 404）。必須帶完整瀏覽器 User-Agent 與
  Referer，UA 太短會被 Cloudflare 403。搜尋是模糊比對，靠 config 的
  must_all/must_any/exclude 做客戶端過濾。
- **v2 backlog**：LinkedIn 訪客端點（不登入；登入爬取違反 ToS 禁止採用）、
  Claude API 依履歷相關性排序職缺。
- **第二個工具：台股 AI 投資顧問**（`src/stock_advisor.py`、`stock_config.json`、
  `.github/workflows/stock-advisor.yml`）。細節見 README「台股 AI 投資顧問」一節。
- **第三個工具：全球原物料價格監控**（`src/commodity_monitor.py`、
  `commodity_config.json`、`.github/workflows/commodity-monitor.yml`、
  `docs/commodity/index.html` dashboard）。只依賴 `requests`，不用 pandas/numpy/
  yfinance；Yahoo Finance `v8/finance/chart` 端點對高頻請求會 429（IP+UA 短時間
  限流，非永久封鎖，2026-07-20 實測），程式已內建重試 backoff；Yahoo 抓不到時
  以 FRED（免 API key，銅/鋁僅月頻、貴金屬與玉米無對應）備援並做兩來源交叉驗證。
  Stooq 已改 JS proof-of-work 驗證頁故不採用。細節見 README「全球原物料價格監控」
  一節與 memory `commodity-data-sources`。
- Project memory: `~/.claude/projects/-Users-yutong-Desktop-grace-agent/memory/` —
  check `MEMORY.md` there for handoffs before asking the user about prior work.
