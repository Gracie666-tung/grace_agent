# grace_agent — 每日職缺推播

每天早上 9 點（台北時間）自動抓 104 的新職缺（資料分析、BA、PM 實習、金融科技），
去重後推播到 Telegram。跑在 GitHub Actions 上，電腦不用開機、全程免費。

```
GitHub Actions cron (01:00 UTC)
  → src/daily_jobs.py 抓 104 公開搜尋 API（config.json 定義搜尋條件）
  → 比對 data/seen_jobs.json 去重，只留新職缺
  → 推播到 Telegram（超過 4096 字自動拆多則）
  → 把更新後的 seen_jobs.json 提交回 repo
```

## 一次性設定

### 1. 建 Telegram bot（約 10 分鐘）

1. 在 Telegram 搜尋 **@BotFather** → 傳 `/newbot` → 取名字 → 拿到 **bot token**。
2. 跟你的新 bot 說一句話（隨便傳什麼，這步是必要的）。
3. 開瀏覽器打 `https://api.telegram.org/bot<TOKEN>/getUpdates`，
   在回傳 JSON 裡找 `"chat":{"id":123456789,...}` — 那個數字就是你的 **chat id**。

### 2. 本機測試

```bash
pip install -r requirements.txt
python src/daily_jobs.py --dry-run          # 只印訊息，不推播

TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=xxx python src/daily_jobs.py   # 真的推一次
```

### 3. 上 GitHub 啟用排程

1. 在 GitHub 建一個 **private** repo（例如 `grace-agent`），把這個資料夾 push 上去。
2. Repo → Settings → Secrets and variables → Actions → 加兩個 secret：
   `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`。
3. Repo → Actions 頁面 → 選 `daily-jobs` workflow → **Run workflow** 手動跑一次驗證。
4. 之後每天 09:00（台北）自動執行。60 天沒有任何 commit 的 repo，GitHub 會自動停
   用排程並寄信提醒 — 點一下重新啟用即可（本 workflow 每天會 commit seen_jobs.json，
   正常情況不會遇到）。

## 調整搜尋條件

改 `config.json`：`keyword`（104 搜尋字串）、`area`（地區代碼，台北 6001001000、
新北 6001002000、高雄 6001016000，`[]` = 全台）、`must_all` / `must_any` / `exclude`
（客戶端過濾，因為 104 的搜尋是模糊比對，會混進不相關職缺）。

## 已知限制（誠實聲明）

- 104 的 `jobs/search/api/jobs` 是非官方公開端點（2026-07-06 實測可用），104 改版
  就會壞 — 壞的症狀是 Actions 跑失敗，GitHub 會寄信通知。
- User-Agent 必須是完整瀏覽器字串，太短會被 Cloudflare 403。
- LinkedIn 未納入 v1：登入爬取違反其 ToS 且有封號風險；規劃 v2 用不登入的
  訪客搜尋端點。
