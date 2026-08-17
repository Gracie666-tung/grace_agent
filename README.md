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

## 全球原物料價格監控

第三個推播工具：每天 08:00（台北）抓銅、鋁、原油、天然氣、黃金、白銀、玉米期貨，
以及台股原物料期貨 ETF 對照組（00715L），推一份分析日報到 Telegram，並更新一個
GitHub Pages 靜態 dashboard。

```
GitHub Actions cron (00:00 UTC = 08:00 台北)
  → src/commodity_monitor.py 打 Yahoo Finance chart API（query1.finance.yahoo.com/
    v8/finance/chart/{symbol}）為主資料源，首次執行回補約 2 年日線，之後每天累積進
    data/commodity_history.json
  → 單一品項 Yahoo 抓取失敗時，若有 FRED（美國聖路易聯準銀行公開 CSV，免 API key）
    對照數列就自動改用 FRED 備援；兩者皆失敗但有歷史資料則沿用舊值並標示「今日
    未更新」，絕不用空白覆蓋既有好資料
  → 算日/週/月/年變動率、MA20/60 乖離率、30日年化波動度、相對 250 日報酬分布的
    z-score（判定異常波動）、60日品項間相關係數矩陣、naive／移動平均／線性趨勢外推
    三種成本預測 baseline；同時有 Yahoo＋FRED 日頻資料的品項會做兩來源收盤價交叉驗證
  → Claude API 判讀（沒有 API key 或呼叫失敗 → 自動降級為規則版摘要，不會中斷）
  → 推播到 Telegram → 把歷史資料與 docs/commodity/data.json 提交回 repo
  → GitHub Pages 讀 docs/commodity/data.json 畫出 dashboard（純 SVG/表格，無外部函式庫）
```

### 方法論重點

- 週/月/年變動率、波動度、z-score 都用**交易日數**估算（5/21/252/30/250），不是
  日曆天數 —— 金融資料常見近似，跟看盤軟體「近1年」可能差幾天。
- 「異常波動」= 當日對數報酬相對近 250 個交易日報酬分布的 z-score，`|z| ≥ 2`
  才標記，避免把正常波動也標成異常。
- 60 日相關係數矩陣用**兩品項皆有報價的重疊交易日**計算，不是各自最近 60 筆
  —— 不同交易所行事曆不同，直接對齊會時間錯位。共同交易日不足 10 筆就不計算。
- 成本預測是 baseline，不是模型：naive（沿用今日收盤）、移動平均、對近期收盤做
  最小平方法線性迴歸外推 30 個交易日，區間用該迴歸窗口的歷史殘差標準差
  （±1 倍），不是信賴區間，只反映「歷史配適誤差多大」。**不構成採購或投資建議。**
- **多資料源與交叉驗證**：Yahoo 為主、FRED 為備援。FRED 的原油/天然氣為日頻，
  銅/鋁（IMF 商品價格數列）只有**月頻**——月頻品項只算月/年變動率，不套用日頻的
  波動度／z-score／日變動率公式，dashboard 與報告會標「月頻」。對同時有兩來源
  日頻資料的品項，比較最近共同交易日的收盤價差：Yahoo 是期貨、FRED 多為現貨，
  本有價差屬預期，差異超過門檻只是提示「可能抓錯／單位換算錯／快取過期」的資料
  品質檢查，不是套利訊號。

### 本機測試

```bash
pip install -r requirements.txt
python src/commodity_monitor.py --dry-run                    # 只印報告，不推播、不寫歷史
python src/commodity_monitor.py --dry-run --write-dashboard  # 同上，另外產生
                                                                docs/commodity/data.json
                                                                方便本機用瀏覽器預覽 dashboard
                                                                （需要用本機伺服器開，file://
                                                                會被瀏覽器擋 fetch）
python src/commodity_monitor.py --dry-run --source fred      # 跳過 Yahoo、只走 FRED 備援
                                                                （Yahoo 被限流時用來驗證備援路徑）

# 真的推一次
COMMODITY_TELEGRAM_BOT_TOKEN=xxx COMMODITY_TELEGRAM_CHAT_ID=xxx ANTHROPIC_API_KEY=xxx \
  python src/commodity_monitor.py
```

### 新增 secret

Repo → Settings → Secrets and variables → Actions → 新增：
`COMMODITY_TELEGRAM_BOT_TOKEN`、`COMMODITY_TELEGRAM_CHAT_ID`（獨立 bot，與職缺/
股票推播分開）。`ANTHROPIC_API_KEY` 若已為股票顧問設定過可直接共用，不設也能跑
（規則版摘要降級）。

## IG 收藏分類（Notion 看板）

把 Instagram 收藏的貼文/影片分成 **數據分析 / AI / Finance / 其他** 四類加自動
子標籤，同步到 Notion database，並產生一份整體分析。

`src/ig_curator.py`、`ig_config.json`。

### 先決條件：IG 官方資料匯出

**IG 的「收藏」沒有任何 API、webhook 或通知**，官方資料匯出是唯一合規的出口。
所以這是手動觸發的工具，不是排程。

IG App → 設定 → 帳號中心 → 你的資訊與權限 → 下載你的資訊 → 選「部分資訊」→
勾**已儲存的內容** → 格式選 **JSON** → 日期選全部。等它產生（幾小時到一天），
下載後解壓縮，整個資料夾放到 `data/ig_export/`。

匯出檔**只有貼文網址和收藏時間，沒有內文**。程式會逐筆去抓公開貼文的
`og:description` 來補，抓不到的（私人帳號、已刪除、登入牆）會標成「需人工補」，
分類多半會落在「其他」，需要你在 Notion 手動修。

如果你在 IG 建過收藏夾，`saved_collections.json` 也會被讀進來——那是你自己分的
類，程式會拿它當分類依據，比模型猜的準。

### Notion 設定

1. notion.so/my-integrations → New integration → 拿 token（`ntn_` 開頭）
2. 到你要放資料庫的 Notion 頁面 → 右上 ⋯ → 連結 → 加入該 integration
3. 設環境變數：

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export NOTION_TOKEN=ntn_...
export NOTION_PARENT_PAGE='https://www.notion.so/你的頁面網址'
```

第一次跑會自動建資料庫並印出 `NOTION_DATABASE_ID`，設起來之後就不會重建。

### 本機測試

```bash
# 不碰 Notion，只看分類結果
python src/ig_curator.py --dry-run

# 先試 20 筆再全跑（分類要花錢，先確認品質）
python src/ig_curator.py --limit 20 --dry-run

# 完整跑
python src/ig_curator.py
```

### 不會覆寫你手改的東西

程式用貼文網址當唯一鍵做 upsert，而且**對已存在的頁面只填空欄位**。你在 Notion
把分類從 AI 改成 Finance、或在「我的筆記」寫了東西，重跑都不會被蓋掉。

「我的筆記」欄位程式永遠不寫。整體分析每次跑會以當天日期為標題附加在 parent
頁面後面，保留歷次紀錄。

## 音檔轉文字 + 摘要（TAKEAWAY / SUMMARY）

把錄音變成「逐字稿 + 可直接用的筆記」。轉錄在**本機離線**跑（faster-whisper），
摘要走 Claude API。錄音不會上傳到任何地方，只有逐字稿文字會送給 Claude。

```bash
pip install -r requirements-audio.txt

# 課程／講座（預設）：TL;DR、TAKEAWAYS、概念架構、SUMMARY、可複習清單、關鍵名詞
python src/audio_notes.py ~/Desktop/財管第三週.m4a

# 會議／實習工作：TL;DR、TAKEAWAYS、決策表、待辦事項（負責人/期限）、風險與未解
python src/audio_notes.py ~/Desktop/週會.m4a --type meeting

# 只轉錄不摘要 / 換小模型換速度 / 英文錄音
python src/audio_notes.py 錄音.m4a --no-llm
python src/audio_notes.py 錄音.m4a --model medium
python src/audio_notes.py lecture.mp3 --lang en

# 已經有逐字稿，只要摘要
python src/audio_notes.py data/audio_notes/週會.transcript.txt --from-text --type meeting
```

輸出在 `data/audio_notes/`（可用 `--out` 改）：

| 檔案 | 內容 |
|---|---|
| `<名稱>.md` | 主要成果：摘要 + 重點 + 完整逐字稿 |
| `<名稱>.transcript.txt` | 純逐字稿（每行帶 `[mm:ss]` 時間戳） |
| `<名稱>.segments.json` | 逐段時間戳原始資料，要做剪輯或對照時用 |

支援 mp3 / m4a / wav / aac / flac / mp4 / mov 等（用 PyAV 解碼，**不需要另外裝
ffmpeg**）。

### 速度與模型選擇（實測：Apple M1，CPU int8，4 執行緒）

faster-whisper 在 Apple Silicon 上只跑 CPU（CTranslate2 沒有 Metal 後端）。
同一個 45 秒中文音檔實測：

| 模型 | 速度 | 一小時錄音約需 | 這段測試的品質 |
|---|---|---|---|
| `small` | 3.9x 實時 | 約 15 分鐘 | 內容對，但把 Excel 聽成 Xcel |
| `medium`（**預設**） | 1.0x 實時 | 約 1 小時 | 全對，含 Excel、台積電、聯發科、數字 |
| `large-v3` | 未實測（下載失敗，見下方限制） | 推估 2–3 小時 | — |

`cpu_threads` 預設 4：M1 是 4 效能核 + 4 節能核，實測開 8 執行緒**反而更慢**
（3.9x → 2.5x），因為工作被排到節能核上。

建議流程：**先用 `--model small` 跑一次**確認音檔沒問題（15 分鐘就有結果），
再決定要不要花時間跑 `medium`。趕時間的短錄音直接 `small` 通常就夠用。
模型第一次用會下載（`small` 約 460MB、`medium` 約 1.4GB），存在
`~/.cache/huggingface`，之後不用再下載。

副作用要知道：`medium` 的斷句比 `small` 粗（實測 30 秒一段 vs 12 秒一段），
時間戳精度較低。要精細時間戳做剪輯的話用 `small`。

### 中文品質

- `initial_prompt` 已設成繁體中文提示，加上 opencc 做簡→繁字形轉換（`s2tw`）。
- 刻意**不用** `s2twp`：它雖然會把「軟件」轉成「軟體」，但也會把「循環」誤轉成
  「迴圈」，對財金內容弊大於利。要改在 `audio_config.json` 的 `opencc_config`。
- 專有名詞、數字、英文縮寫最容易錯，所以每份筆記最後都有「待確認」章節，
  由 Claude 標出可疑處與時間戳，回去核對用。

### 網頁版：上傳錄音 → Notion／Telegram

不想打指令的話用網頁版。它跑在你自己的 Mac 上，**同一個 Wi-Fi 下手機也能連**，
所以手機錄完直接用瀏覽器上傳就好。

```bash
pip install -r requirements-audio.txt
python src/audio_web.py
```

啟動後會印兩個網址：

```
  電腦： http://localhost:8000/
  手機： http://192.168.x.x:8000/   （需同一個 Wi-Fi）
```

網頁上可以選筆記模式、轉錄模型、語言，勾要不要摘要、要不要送 Notion／Telegram，
然後看即時進度（轉到第幾分鐘、Claude 在做哪一步）。轉完直接在頁面上看整理好的
筆記，也能複製或下載 `.md`／逐字稿。多個檔案會排隊依序處理——轉錄很吃 CPU，
同時跑只會互相拖慢。

```bash
python src/audio_web.py --local-only     # 只綁 localhost，手機連不到
python src/audio_web.py --port 9000      # 換 port
export AUDIO_WEB_TOKEN=隨便一組密碼        # 加一道密碼（見下方安全性）
```

**安全性**：預設綁 `0.0.0.0`，也就是**同網段的人都能開這個網址、上傳檔案、讀你
的筆記**。家裡 Wi-Fi 大致無妨，學校／公司／咖啡廳的網路請設 `AUDIO_WEB_TOKEN`
（設了之後網址要帶 `?token=你的密碼`），或改用 `--local-only`。

上傳的原始錄音留在 `data/audio_uploads/`（不會自動刪，會佔空間，已排除版控）。

### Notion 與 Telegram 設定

Notion：每個錄音在資料庫建一頁，屬性有標題、類型、錄音日期、長度、TL;DR、
轉錄模型，內文放完整摘要，逐字稿收在可摺疊的區塊裡。另有一個「我的筆記」欄位，
程式永遠不寫，留給你自己補。

```bash
export NOTION_TOKEN=ntn_...
export NOTION_PARENT_PAGE='https://www.notion.so/你要放資料庫的頁面'
# 第一次跑會自動建資料庫並在 log 印出 id，設起來之後就不會重建：
export AUDIO_NOTION_DATABASE_ID=...
```

Telegram：沿用職缺／台股工具的同一組 bot，摘要當訊息、完整筆記當 `.md` 附件。

```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
```

沒設的目的地在網頁上會顯示「未設定」並自動變灰，不會讓你勾了才失敗。投遞失敗
（token 過期、頁面沒授權）也**不會弄丟筆記**——本機檔案照寫，網頁上顯示錯誤原因。

### 設定

`audio_config.json` 調 whisper 模型／語言／輸出資料夾，以及 Claude 的模型與
`chunk_chars`（逐字稿超過這個字數會先分段做中繼筆記再合併，避免一次塞太長）。
摘要需要 `ANTHROPIC_API_KEY`：

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

沒設 key 也能用 —— 程式會照常轉錄存檔，只跳過摘要，並印出補做摘要的指令。

## 已知限制（誠實聲明）

- IG 收藏分類：**沒辦法做到「按收藏就自動分類」**——IG 對收藏完全沒開放介面，
  只能靠手動匯出，所以這是手動工具。匯出檔沒有內文，補內容靠抓公開貼文的 og
  tag，成功率取決於你收藏的帳號有多少是公開的，私人帳號一定抓不到。
  Notion API 版本釘在 `2022-06-28`（最後一個不需要解析 data source 層的版本）。
  建立資料庫的 request body 形狀是依該版慣例組的，**尚未對真實 API 驗證過**；
  若第一次跑報 400，改用 Notion UI 手動建好資料庫、設 `NOTION_DATABASE_ID` 即可
  繞過。分類與 summary 的實際輸出品質也還沒用真資料驗證過。
- 音檔轉文字：**沒有語者分離（speaker diarization）**，多人會議的逐字稿不會標
  「誰講的」，只有時間順序。要區分講者得自己聽著補，或改用付費 API（如
  AssemblyAI）。
- 音檔轉文字：Apple Silicon 上 faster-whisper 只跑 CPU（CTranslate2 沒有 Metal
  後端），所以預設的 `medium` 就是 1x 實時 —— 一小時的錄音要跑一小時。這不是設定
  問題，是套件限制。要更快只能換更小的模型（`small` 約 4x 實時），或改用付費 API。
- `large-v3` **在這台機器上沒實測過**：模型檔（約 1.5GB）從 Hugging Face 下載
  失敗兩次（xet CDN 回 connection reset / File reconstruction error）。README 裡
  它的速度是推估值。要試的話重跑就會續傳，或加 `HF_HUB_DISABLE_XET=1` 走傳統
  下載路徑。
- 音檔轉文字的 Claude 摘要路徑**尚未用真實 API 驗證**（開發機沒設
  `ANTHROPIC_API_KEY`）：`tests/test_audio_notes.py` 用假的 client 驗證了分段、
  prompt 組裝與輸出格式，但實際回應品質要你第一次跑才知道。轉錄路徑則已用真音檔
  端到端驗證過（CLI 與網頁版都測過）。
- 同理，**Notion 與 Telegram 也沒有用真 token 驗證過**（開發機沒有）。測試驗的是
  送出去的資料結構正確（Markdown 表格轉成 Notion 表格、逐字稿在 Notion 區塊上限
  內、Telegram 訊息不超過 4096 字且切割不漏字），不是「對方真的收到」。第一次跑
  若 Notion 回 400，多半是 integration 沒被加到那個頁面。
- 網頁版預設綁 `0.0.0.0`（為了手機能連），**同網段的人都能存取**。不在自家 Wi-Fi
  就設 `AUDIO_WEB_TOKEN` 或用 `--local-only`。這是本機工具，沒有帳號系統。
- 網頁版的任務狀態存在記憶體＋`data/audio_notes/jobs.json`，**伺服器重開，跑到
  一半的任務不會續跑**（會標成失敗），已完成的紀錄則保留。上傳的原始錄音不會自動
  清除。
- 104 的 `jobs/search/api/jobs` 是非官方公開端點（2026-07-06 實測可用），104 改版
  就會壞 — 壞的症狀是 Actions 跑失敗，GitHub 會寄信通知。
- User-Agent 必須是完整瀏覽器字串，太短會被 Cloudflare 403。
- LinkedIn 未納入 v1：登入爬取違反其 ToS 且有封號風險；規劃 v2 用不登入的
  訪客搜尋端點。
- 台股顧問：yfinance 是非官方 Yahoo Finance 介面，可能因 Yahoo 改版而壞；
  台股資料偶有延遲或缺漏（程式會砍掉尾端 Volume=0 的未完成 bar）。技術指標為
  常見公式的近似實作（KD 用 ewm 遞迴），數值可能與看盤軟體有小數差異。
  賣出警示是機械規則，不構成投資建議。
- 原物料監控：Yahoo Finance 的 `v8/finance/chart` 是非官方端點，2026-07-20 實測
  發現它會依 IP＋User-Agent 組合做**短時間高頻請求限流（429）**，不是永久封鎖 ——
  單一 symbol 重試幾秒內通常會過，但短時間內對同一個 UA 打滿 8 個 symbol 有機會
  整輪被限流；程式已內建重試 backoff 與 symbol 間延遲，仍可能在極端狀況下整輪抓
  不到資料，此時報告會誠實列出「資料取得失敗」品項，不會中斷或推播假資料。
- 原物料監控的備援設計：原本規劃用 Stooq 當 fallback，2026-07-20 實測發現 Stooq
  已改為 JavaScript proof-of-work 驗證頁（不再直接回 CSV），繞過它屬對抗反爬蟲機制，
  故不採用，改用 FRED。FRED 免 API key、資料官方且穩定，但代價是：銅、鋁只有**月頻**
  （IMF 商品價格數列），黃金、白銀、玉米、台股 ETF 在 FRED **沒有對應數列**（這幾項
  只靠 Yahoo 單一來源，Yahoo 掛了就只能沿用歷史）；且 FRED 的原油/天然氣日頻數列
  通常**落後約 5 個營業日**，不是即時報價。
- 原物料監控刻意不納入晶片（DRAM/NAND）與塑化（PE/PP）：這兩類沒有可靠的免費
  公開數列來源（多半鎖在付費終端如 Bloomberg/DRAMeXchange 訂閱），寧可不做也
  不要用假資料充數。
- 原物料監控的各品項為原幣別報價（期貨多為美元，00715L 為新台幣），沒有做匯率
  轉換，跨品項比較時請留意幣別不同。
