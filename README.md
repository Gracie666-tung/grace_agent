# grace_agent — 每日推播工具（職缺 + 台股顧問）

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

## 台股 AI 投資顧問

第二個推播工具：每個交易日 08:30（盤前）與 14:30（收盤後）各推一份台股日報到
Telegram，內容包含觀察清單技術指標與訊號、持股損益與賣出警示、24 小時內財經新聞、
以及 Claude AI 判讀（偏多/中性/偏空、續抱/減碼/出場傾向）。

```
GitHub Actions cron (00:30 / 06:30 UTC，週一到週五)
  → src/stock_advisor.py 先讀 Telegram 指令（getUpdates）
  → yfinance 抓台股日線 → MA5/20/60、RSI(14)、KD(9,3,3)、量能、訊號旗標
  → 持股賣出警示（停損 -8%、高點回落 10%、死亡交叉、跌破 MA60，閾值在 stock_config.json）
  → 鉅亨網 RSS 抓 24h 台股新聞
  → Claude API 判讀（沒有 API key 或呼叫失敗 → 自動降級為純規則版日報，不會中斷）
  → 推播到 Telegram → 把 data/stock_state.json 提交回 repo
```

### Telegram 指令（直接傳訊息給 bot，下次排程執行時處理並回報）

| 指令 | 範例 | 作用 |
|---|---|---|
| `買 代號 均價 股數` | `買 2330 850 1000` | 登記持股，開始追蹤損益與賣出警示 |
| `賣 代號` | `賣 2330` | 平倉移除持股 |
| `觀察 代號` | `觀察 2317` | 加入觀察清單 |
| `移除 代號` | `移除 2317` | 從觀察清單移除 |

其他訊息一律忽略。**注意：bot 不會即時回覆。** 指令要等下一次排程執行（平日 08:30 / 14:30）才會被讀取並生效，處理結果在那份日報開頭確認（例：「✅ 已登記買入 2330」）。傳完指令沒有馬上收到回應是正常的。
觀察清單存在 `data/stock_state.json`，`stock_config.json` 的 watchlist 只是首次執行
的種子。

### 新增 secret：ANTHROPIC_API_KEY

1. 到 <https://console.anthropic.com/> → API Keys → 建一把 key。
2. Repo → Settings → Secrets and variables → Actions → New repository secret，
   名稱 `ANTHROPIC_API_KEY`，值貼上 key。
3. 不設也能跑：日報會註明「AI 判讀未啟用」，只出規則版指標與訊號。

### 本機測試

```bash
pip install -r requirements.txt
python src/stock_advisor.py --mode close --dry-run   # 收盤後日報，只印不推播
python src/stock_advisor.py --mode pre --dry-run     # 盤前日報

# 真的推一次（dry-run 不會處理 Telegram 指令，正式跑才會）
TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=xxx ANTHROPIC_API_KEY=xxx \
  python src/stock_advisor.py --mode close
```

## 已知限制（誠實聲明）

- 104 的 `jobs/search/api/jobs` 是非官方公開端點（2026-07-06 實測可用），104 改版
  就會壞 — 壞的症狀是 Actions 跑失敗，GitHub 會寄信通知。
- User-Agent 必須是完整瀏覽器字串，太短會被 Cloudflare 403。
- LinkedIn 未納入 v1：登入爬取違反其 ToS 且有封號風險；規劃 v2 用不登入的
  訪客搜尋端點。
- 台股顧問：yfinance 是非官方 Yahoo Finance 介面，可能因 Yahoo 改版而壞；
  台股資料偶有延遲或缺漏（程式會砍掉尾端 Volume=0 的未完成 bar）。技術指標為
  常見公式的近似實作（KD 用 ewm 遞迴），數值可能與看盤軟體有小數差異。
  賣出警示是機械規則，不構成投資建議。
