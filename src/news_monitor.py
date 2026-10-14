#!/usr/bin/env python3
"""新聞監測 Agent：盯 news_watchlist.json 內的股票，出現「新」新聞就用 Telegram 通知，
並（有 ANTHROPIC_API_KEY 時）附上重要性、預估股價影響與加減碼建議。

用法：
    python src/news_monitor.py --dry-run --no-llm --since-hours 72   # 只印出訊息，不推播、不寫狀態
    python src/news_monitor.py --ticker ANET --dry-run               # 只看單一標的
    python src/news_monitor.py                                       # 正式執行（需要環境變數
                                                                     # NEWS_TELEGRAM_BOT_TOKEN、
                                                                     # NEWS_TELEGRAM_CHAT_ID；
                                                                     # ANTHROPIC_API_KEY 可選，
                                                                     # 沒有就走「無分析」摘要降級）

可選環境變數：NEWS_UA（SEC EDGAR 要求的描述性 User-Agent）、NEWS_MODEL（覆蓋模型 id）、
NEWS_STATE_PATH（覆蓋去重狀態檔路徑，測試用）。

分級推播：持有（status=held）或 priority=true 的標的、以及 CPI 類總經 -> 即時推播（持股附「決策卡」）；
其餘候選股與一般總經 -> 只進佇列，每小時合併成一則「🗞 每小時摘要」。NEWS_FORCE_DIGEST=1 可在 dry-run
示範時略過 60 分鐘間隔。

另有兩條旁路：Jin10（金十數據）快訊來源（非公開介面，可能失效/被擋，失敗只略過）與
「📊 總經快訊」（news_watchlist.json 的 macro 區塊，不綁個股，每輪最多一則訊息）。
"""

import argparse
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "news_watchlist.json"
STATE_PATH = ROOT / "data" / "seen_news.json"

# 沿用其他工具已驗證的完整瀏覽器 UA（短 UA 容易被 Cloudflare 類反爬擋掉）。
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, text/html, */*"}
# SEC 要求描述性 UA；不把任何真實 email 寫進程式，預設用佔位字串，要換就設環境變數 NEWS_UA。
SEC_UA = os.environ.get("NEWS_UA", "grace-agent-news-monitor research@example.invalid")

TIMEOUT = 15
STATE_RETENTION_DAYS = 30
DIGEST_KEY = "_digest"  # seen_news.json 內的保留鍵：{"last_sent": ISO, "pending": [...]}，不是新聞紀錄
DIGEST_INTERVAL_MIN = 60
DIGEST_MAX_LISTED = 15  # 每則摘要最多列幾則，其餘只寫「另有 K 則未列出」
DIGEST_MAX_PENDING = 200  # 佇列上限（保留最新）
DIGEST_TITLE_MAX = 110
MAX_ITEMS_PER_ENTRY = 10  # 每檔每輪最多送進分析/推播的新聞數，其餘只記為已見
BODY_MAX_CHARS = 4000
BODY_TIMEOUT = 8  # 抓內文的逾時；20 檔 × 多則循序抓，不能拖過 30 分鐘排程間隔
MAX_DIGEST_ITEMS = 8  # 無 LLM / LLM 失敗時，每檔摘要最多列幾則（只列硬觸發關鍵字）
TG_LIMIT = 4096
DEFAULT_MODEL = "claude-sonnet-5-5"
DISCLAIMER = "預估僅供參考,非投資建議"

# Google News 語系參數
LOCALES = {
    "en-US": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
    "de-DE": {"hl": "de", "gl": "DE", "ceid": "DE:de"},
}

# 這些字眼出現在標題時，即使 LLM 判為 low 也要推（事件型催化劑）。
HARD_TRIGGERS = re.compile(
    r"earnings|guidance|\bFDA\b|\bEMA\b|PDUFA|upgrade|downgrade|offering|lawsuit|"
    r"contract|acquisition|to acquire|recall|\bprobe\b|investigation|"
    r"財報|財測|展望|評等|增資|訴訟|合約|併購|召回",
    re.IGNORECASE,
)

# 機構 13F 持股異動的灌水文（MarketBeat 類）：不是新資訊，直接濾掉。
NOISE = re.compile(
    r"\b(stock|shares|stake|position)\b.*\b(acquired|purchased|bought|sold|trimmed|raised|lowered|boosted|"
    r"cut|reduced|increased|lifted|newly \w+)\b.*\bby\b|"
    r"\b(acquires|buys|sells|boosts|trims|raises|lowers|cuts|lifts|purchases)\b.*\bshares of\b",
    re.IGNORECASE,
)
# 期權成交量/量化選股類灌水文：不是公司新聞，即使標題含 contract/earnings 等字也一律濾掉。
OPTIONS_SPAM = re.compile(
    r"contracts were traded|options income daily|options spot-on|unusual options|quant picks|"
    r"call bet|put bet|options? (flow|activity|alert)",
    re.IGNORECASE,
)

VALUES = ("high", "medium", "low")
DIRECTIONS = ("up", "down", "neutral")
ACTIONS = ("加倉", "持有", "減倉", "出場", "觀察")
CONFIDENCES = ("low", "medium", "high")
DIR_LABEL = {"up": "📈 偏多", "down": "📉 偏空", "neutral": "➖ 中性"}
VALUE_LABEL = {"high": "高", "medium": "中", "low": "低"}
CONF_LABEL = {"low": "低", "medium": "中", "high": "高"}


# ---------------------------------------------------------------------------
# 設定、狀態
# ---------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def is_past_end(cfg: dict, now: datetime | None = None) -> bool:
    """比賽結束日（含當天）之後不再做事。日期以 cfg.timezone（預設 Asia/Taipei）判定。"""
    end = cfg.get("end_date")
    if not end:
        return False
    tz = ZoneInfo(cfg.get("timezone") or "Asia/Taipei")
    today = (now or datetime.now(timezone.utc)).astimezone(tz).date()
    return today > datetime.strptime(end, "%Y-%m-%d").date()


def load_state(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        if DIGEST_KEY in data and not isinstance(data[DIGEST_KEY], dict):
            del data[DIGEST_KEY]  # 壞掉的保留鍵直接重建
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_digest(state: dict) -> dict:
    """取得（必要時建立）保留鍵 _digest，並確保欄位型別正確。"""
    d = state.setdefault(DIGEST_KEY, {})
    if not isinstance(d.get("pending"), list):
        d["pending"] = []
    if not isinstance(d.get("last_sent"), str):
        d["last_sent"] = None
    return d


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def prune_state(state: dict, now: datetime, days: int = STATE_RETENTION_DAYS) -> dict:
    cutoff = now - timedelta(days=days)
    kept = {}
    for key, rec in state.items():
        if key == DIGEST_KEY:  # 摘要佇列不套 30 天規則，原樣保留
            if isinstance(rec, dict):
                kept[key] = rec
            continue
        try:
            ts = datetime.fromisoformat(rec["ts"])
        except (KeyError, TypeError, ValueError):
            continue  # 壞掉的紀錄直接丟掉
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            kept[key] = rec
    return kept


def _norm_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def _norm_title(title: str) -> str:
    return re.sub(r"[\W_]+", "", (title or "").lower())


def item_key(item: dict) -> str:
    raw = _norm_url(item.get("link", "")) + "|" + _norm_title(item.get("title", ""))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 抓新聞（RSS / Atom）
# ---------------------------------------------------------------------------

def strip_tags(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        dt = parsedate_to_datetime(s)  # RFC 822（RSS）
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))  # ISO 8601（Atom）
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_feed(xml_text: str, origin: str) -> list[dict]:
    """解析 RSS 2.0 或 Atom，回傳 [{title, link, source, published, snippet, origin}]。"""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        f = {}
        for child in node:
            name = _local(child.tag)
            if name == "link":
                f["link"] = (child.get("href") or child.text or "").strip() or f.get("link", "")
            elif name == "source":
                f["source"] = (child.text or "").strip()
            elif name in ("title", "pubDate", "published", "updated", "description", "summary"):
                f.setdefault(name, (child.text or "").strip())
        title = strip_tags(f.get("title", ""))
        source = f.get("source", "")
        if not source and " - " in title:  # Google News 標題格式 "標題 - 媒體"
            source = title.rsplit(" - ", 1)[1]
        if not title or not f.get("link"):
            continue
        items.append({
            "title": title,
            "link": f["link"],
            "source": source or origin,
            "published": _parse_date(f.get("pubDate") or f.get("published") or f.get("updated")),
            "snippet": strip_tags(f.get("description") or f.get("summary") or ""),
            "origin": origin,
        })
    return items


def _get(url: str, headers: dict | None = None, **kw) -> requests.Response:
    resp = requests.get(url, headers=headers or BROWSER_HEADERS, timeout=TIMEOUT, **kw)
    resp.raise_for_status()
    return resp


def google_news_url(query: str, locale: str, since_hours: int) -> str:
    loc = LOCALES.get(locale, LOCALES["en-US"])
    when = f"{math.ceil(since_hours / 24)}d" if since_hours >= 24 else f"{max(1, math.ceil(since_hours))}h"
    return (
        f"https://news.google.com/rss/search?q={quote_plus(query)}+when:{when}"
        f"&hl={loc['hl']}&gl={loc['gl']}&ceid={quote_plus(loc['ceid'])}"
    )


def fetch_google(entry: dict, since_hours: int) -> list[dict]:
    out = []
    for locale in entry.get("locales") or ["en-US"]:
        for q in entry.get("queries", []):
            resp = _get(google_news_url(q, locale, since_hours))
            out += parse_feed(resp.text, "Google News")
            time.sleep(0.4)  # 對 Google 客氣一點
    return out


def fetch_sec(entry: dict, since_hours: int) -> list[dict]:
    cik = entry.get("sec_cik")
    if not cik:
        return []
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={cik}&type=&dateb=&owner=include&count=20&output=atom"
    )
    resp = _get(url, headers={"User-Agent": SEC_UA, "Accept": "application/atom+xml"})
    items = parse_feed(resp.text, "SEC EDGAR")
    for it in items:
        it["title"] = f"{entry['ticker']} SEC 申報：{it['title']}"
        it["trusted"] = True  # 官方申報來源，不套別名過濾
    return items


def fetch_yahoo(entry: dict, since_hours: int) -> list[dict]:
    sym = entry.get("yahoo_symbol") or entry["ticker"]
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote_plus(sym)}&region=US&lang=en-US"
    return parse_feed(_get(url).text, "Yahoo Finance")  # best-effort：不重試，429 就放棄


# ---------------------------------------------------------------------------
# Jin10（金十數據）快訊 — 非公開介面，失敗一律略過
# ---------------------------------------------------------------------------
# 端點如何找到：jin10.com 首頁 HTML 只有 ~6KB 外殼，載入 //www.jin10.com/new/js/index.*.js；
# 該 bundle 內建立 axios 實例 baseURL "//flash-api.jin10.com"，呼叫 get("/get_flash_list",{params})，
# header 為 x-app-id（網站 JS 內寫死的公開前端 id）與 x-version（bundle 內 flashXVersion = "1.0.0"）。
# 我們照網站 JS 送同樣的 header 加上 Referer/Origin，且只打「最新一頁」一次（不帶參數時回最新 20 則），
# 不分頁、不重試。若網站改版/擋掉，只會得到非 200 或空資料 -> 記 log 並略過，不影響其他來源。
# 回傳 JSON：{"status":200,"data":[{"id","time"(北京時間 "YYYY-MM-DD HH:MM:SS"),"type"(0=快訊,2=文章卡),
#   "important","data":{"content","title","link","lock",...}}]}
JIN10_URL = "https://flash-api.jin10.com/get_flash_list"
JIN10_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "x-app-id": "bVBF4FyRTn5NJF5n",
    "x-version": "1.0.0",
    "Referer": "https://www.jin10.com/",
    "Origin": "https://www.jin10.com",
}
JIN10_DETAIL = "https://flash.jin10.com/detail/"
JIN10_TZ = timezone(timedelta(hours=8))
JIN10_TITLE_MAX = 200
MACRO_MAX_ITEMS = 8


def parse_jin10(payload: dict) -> list[dict]:
    """把 get_flash_list 回傳解析成標準 item dict；略過 VIP 鎖定、空內容、非快訊/文章卡。"""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    items = []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") not in (0, 2):
            continue
        d = row.get("data") or {}
        if d.get("lock") or (row.get("extras") or {}).get("ad"):
            continue
        content = strip_tags(d.get("content") or "")
        title_raw = strip_tags(d.get("title") or "")
        text = (title_raw + "：" + content) if (title_raw and content and row.get("type") == 2) else (content or title_raw)
        if not text or not row.get("id"):
            continue
        published = None
        try:
            published = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JIN10_TZ).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            pass
        title = text if len(text) <= JIN10_TITLE_MAX else text[:JIN10_TITLE_MAX] + "…"
        items.append({
            "title": title,
            "link": (d.get("link") or "").strip() or JIN10_DETAIL + str(row["id"]),
            "source": "Jin10",
            "published": published,
            "snippet": text,
            "origin": "Jin10",
        })
    return items


def fetch_jin10() -> list[dict]:
    resp = requests.get(JIN10_URL, headers=JIN10_HEADERS, timeout=TIMEOUT)  # 單次請求，不重試
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict) or payload.get("status") != 200:
        raise ValueError("Jin10 回應 status 非 200")
    return parse_jin10(payload)


def collect_jin10(since_hours: int, now: datetime) -> list[dict]:
    """全 run 只抓一次；失敗回傳 []（不影響其他來源）。"""
    try:
        items = fetch_jin10()
    except Exception as e:  # noqa: BLE001
        print(f"Jin10 失敗（略過）：{type(e).__name__}")
        return []
    cutoff = now - timedelta(hours=since_hours)
    kept = [it for it in items if it["published"] is None or it["published"] >= cutoff]
    print(f"Jin10：原始快訊 {len(items)} 則，時間窗內 {len(kept)} 則")
    return kept


def _asc(term: str) -> str:
    """ASCII 邊界：中文字會被 regex 當成單字字元，所以不能用 word-boundary；英數字緊鄰才算不同詞。"""
    return rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"


def is_relevant_jin10(item: dict, entry: dict) -> bool:
    """Jin10 比對：aliases_zh（子字串）、英文 aliases（ASCII 邊界）、ticker（大小寫敏感，
    短代號只認 'TSLA.O' 這種交易所後綴寫法，避免 MU/NET/APP 誤中）。"""
    text = item.get("snippet") or item.get("title") or ""
    for a in entry.get("aliases_zh", []) or []:
        if a and a in text:
            return True
    for a in entry.get("aliases", []):
        if re.search(_asc(a), text, re.IGNORECASE):
            return True
    t = entry["ticker"]
    if re.search(rf"(?<![A-Za-z0-9]){re.escape(t)}\.[A-Z]{{1,2}}(?![A-Za-z])", text):
        return True
    return len(t) >= 4 and re.search(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])", text) is not None


# ---------------------------------------------------------------------------
# 總經快訊
# ---------------------------------------------------------------------------

# 硬觸發：Jin10 快訊與 Google 總經新聞都必須命中其一才推（keywords_zh/en 只是第一層候選過濾）。
MACRO_HARD = re.compile(
    r"(?<![A-Za-z])(?:CPI|PCE|FOMC|Fed|nonfarm|non-farm|payrolls|Brent|WTI|Powell)(?![A-Za-z])"
    r"|Federal Reserve|oil prices?|crude oil|(?:treasury|bond|10-year|2-year) yields?"
    r"|yields? (?:rise|rises|fall|falls|jump|jumps|surge|surges|drop|drops|climb|climbs)"
    r"|rate (?:hike|hikes|cut|cuts)|(?<![A-Za-z])hikes?(?![A-Za-z])|tariffs?"
    r"|美联储|联储|鲍威尔|非农|通胀|原油|油价|收益率|加息|降息|升息|关税",
    re.IGNORECASE,
)
INVERSE_ASSET = re.compile(r"收益率|原油|油价|汽油|美元指数|WTI|Brent|(?:oil|treasury|bond)\b|yields?|dollar index", re.IGNORECASE)
INFLATION_DATA = re.compile(r"(?<![A-Za-z])(?:CPI|PCE|PPI)(?![A-Za-z])|通胀|inflation", re.IGNORECASE)
JOBS_DATA = re.compile(r"非农|失业|nonfarm|non-farm|payrolls|jobs report|unemployment", re.IGNORECASE)
# 否定式（不加息/暂停降息…）先移除，避免被當成加降息訊號
MACRO_NEGATED = re.compile(r"(?:不会|不|未|无需|暂停|暂缓|否认)(?:再)?(?:加息|升息|降息)|按兵不动|维持利率不变|hold rates? steady")
MACRO_UP = re.compile(r"大涨|上涨|涨超|飙升|攀升|走高|创新高|拉升|\b(?:surge\w*|jump\w*|soar\w*|climb\w*|rall(?:y|ies|ied)|rises?|rose|gains?)\b", re.IGNORECASE)
MACRO_DOWN = re.compile(r"大跌|下跌|暴跌|跌超|重挫|下挫|走低|\b(?:plung\w*|tumbl\w*|slump\w*|sinks?|slid\w*|falls?|fell|drops?|dropped|declin\w*)\b", re.IGNORECASE)
MACRO_HAWK = re.compile(r"加息|升息|鹰派|rate hikes?|hawkish|(?<![A-Za-z])hikes?(?![A-Za-z])", re.IGNORECASE)
MACRO_DOVE = re.compile(r"降息|鸽派|rate cuts?|dovish", re.IGNORECASE)
TARIFF_ON = re.compile(r"加征|征收|提高关税|imposes? tariffs?|raises? tariffs?|new tariffs?|tariffs? (?:on|hit)", re.IGNORECASE)
TARIFF_OFF = re.compile(r"取消关税|豁免|降低关税|暂停关税|cuts? tariffs?|tariff (?:relief|pause|exemption)", re.IGNORECASE)
HOT = re.compile(r"超预期|高于预期|hotter|(?<![A-Za-z])hot(?![A-Za-z])|above expectations|beats? expectations", re.IGNORECASE)
COOL = re.compile(r"不及预期|低于预期|cooler|below expectations|misses? expectations|softer", re.IGNORECASE)
MACRO_BULL_LABEL, MACRO_BEAR_LABEL = "📈 對風險資產偏多", "📉 對風險資產偏空"

# 即時推播的總經範圍（目前只有 CPI 類）。其他命中硬觸發的總經（FOMC/Fed/非農/PCE/油價/殖利率/關稅…）
# 一律進每小時摘要。想把 FOMC 或非農升級成即時：在下面 pattern 尾端加 |FOMC 或 |非农|nonfarm|non-farm。
IMMEDIATE_MACRO = re.compile(
    r"(?<![A-Za-z])CPI(?![A-Za-z])|消费者物价|消費者物價|consumer price"
    r"|inflation (?:report|data|numbers|print)|通胀数据|通胀报告|通脹數據",
    re.IGNORECASE,
)


def macro_view(item: dict) -> dict:
    """總經規則判讀（對風險資產）：只做方向，不給幅度。通膨類資料「超預期」=偏熱=偏空；
    非農/失業的超預期方向不明（強勁可能被解讀為升息壓力），不計分。原油/收益率/美元上漲對風險資產偏空。"""
    text = MACRO_NEGATED.sub(" ", f"{item.get('title', '')} {item.get('snippet', '') or ''}")
    inverse = bool(INVERSE_ASSET.search(text))
    score, words = 0, []

    def add(pat, delta):
        nonlocal score
        for m in pat.finditer(text):
            score += delta
            words.append(m.group(0).lower())

    if inverse or not INFLATION_DATA.search(text):  # 「CPI 環比上漲」是數據本身，不是市場漲跌
        add(MACRO_UP, -1 if inverse else 1)
        add(MACRO_DOWN, 1 if inverse else -1)
    add(MACRO_HAWK, -1)
    add(MACRO_DOVE, 1)
    add(TARIFF_ON, -1)
    add(TARIFF_OFF, 1)
    if INFLATION_DATA.search(text):
        add(HOT, -1)
        add(COOL, 1)
    elif not JOBS_DATA.search(text):
        add(HOT, 1)
        add(COOL, -1)
    label = MACRO_BULL_LABEL if score > 0 else MACRO_BEAR_LABEL if score < 0 else "➖ 看不出"
    return {"label": label, "score": score, "words": list(dict.fromkeys(words))[:4]}


def macro_candidate_jin10(item: dict, macro: dict) -> bool:
    text = item.get("snippet") or item.get("title") or ""
    if any(x and x in text for x in macro.get("exclude_zh", [])):
        return False
    if any(k and k in text for k in macro.get("keywords_zh", [])):
        return True
    return any(re.search(_asc(k), text, re.IGNORECASE) for k in macro.get("keywords_en", []) if k)


def fetch_macro_google(macro: dict, since_hours: int) -> list[dict]:
    out = []
    for q in macro.get("google_queries", []):
        try:
            out += parse_feed(_get(google_news_url(q, "en-US", since_hours)).text, "Google News")
        except Exception as e:  # noqa: BLE001
            print(f"  [總經] Google News「{q}」失敗（略過）：{type(e).__name__}")
        time.sleep(0.4)
    return out


def select_macro(jin10_items: list[dict], google_items: list[dict], macro: dict, state: dict, claimed: set,
                 now: datetime, since_hours: int) -> list[dict]:
    """候選 -> 硬觸發 -> 未見過、不與個股新聞/彼此重複。回傳由新到舊，不截斷（截斷在呼叫端）。"""
    cutoff = now - timedelta(hours=since_hours)
    cands = [it for it in jin10_items if macro_candidate_jin10(it, macro)]
    cands += [it for it in google_items if it["published"] is None or it["published"] >= cutoff]
    fresh = []
    for it in sorted(cands, key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        if not MACRO_HARD.search(f"{it['title']} {it.get('snippet') or ''}" if it["origin"] == "Jin10" else it["title"]):
            continue
        if OPTIONS_SPAM.search(it["title"]):
            continue
        key = item_key(it)
        tkey = "t:" + _norm_title(it["title"])
        if key in state or key in claimed or tkey in claimed:
            continue
        claimed.update({key, tkey})
        it["key"] = key
        fresh.append(it)
    return fresh


def build_macro_message(rows: list[dict]) -> str:
    body = []
    for i, it in enumerate(rows, 1):
        v = macro_view(it)
        meta = f"判讀：{v['label']}" + (f"（依據：{', '.join(v['words'])}）" if v["words"] else "")
        body.append(f"{i}. {_e(it['title'])}\n{_e(meta)}\n{_link(it)}")
    note = "判讀為關鍵字規則（對風險資產的粗略方向，不含幅度），請點連結確認；Jin10 為簡體原文。"
    msg = f"📊 <b>總經快訊</b> — {len(rows)} 則\n\n" + "\n\n".join(body) + f"\n\n<i>{note}{DISCLAIMER}</i>"
    return msg


SOURCES = (("Google News", fetch_google), ("SEC EDGAR", fetch_sec), ("Yahoo Finance", fetch_yahoo))


def collect_for_entry(entry: dict, since_hours: int, now: datetime) -> list[dict]:
    """各來源各自 try/except：任何一個壞掉只記 log，不影響其他來源與其他標的。"""
    items = []
    for name, fn in SOURCES:
        try:
            got = fn(entry, since_hours)
            print(f"  [{entry['ticker']}] {name}: {len(got)} 則原始項目")
            items += got
        except Exception as e:  # noqa: BLE001 - 刻意寬鬆，來源失敗是常態
            print(f"  [{entry['ticker']}] {name} 失敗（略過）：{type(e).__name__}: {str(e).split(' for url')[0][:100]}")
    cutoff = now - timedelta(hours=since_hours)
    return [it for it in items if it["published"] is None or it["published"] >= cutoff]


def is_relevant(item: dict, entry: dict) -> bool:
    if item.get("trusted"):
        return True
    text = f"{item.get('title', '')} {item.get('snippet', '')}"
    for alias in entry.get("aliases", []):
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text, re.IGNORECASE):
            return True
    return False


noise_count = [0]  # 本輪濾掉的灌水文數量（只進 log）


def select_new(items: list[dict], entry: dict, state: dict, claimed: set) -> list[dict]:
    """相關 + 未見過 + 跨標的/跨來源去重（claimed 是本輪已被其他標的認領的 key）。"""
    fresh = []
    for it in sorted(items, key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        if not is_relevant(it, entry):
            continue
        if OPTIONS_SPAM.search(it["title"]) or (NOISE.search(it["title"]) and not HARD_TRIGGERS.search(it["title"])):
            noise_count[0] += 1
            continue
        key = item_key(it)
        # 同標題不同網址（例如 Google 轉載）也算同一則
        tkey = "t:" + _norm_title(it["title"])
        if key in state or key in claimed or tkey in claimed:
            continue
        claimed.update({key, tkey})
        it["key"] = key
        fresh.append(it)
    return fresh


# ---------------------------------------------------------------------------
# 內文
# ---------------------------------------------------------------------------

def fetch_body(item: dict) -> str | None:
    """抓文章內文（去標籤、截斷）。失敗或內容太少回傳 None，呼叫端要誠實告知 LLM。"""
    if item.get("origin") == "SEC EDGAR":
        return None
    # Google News 連結是 JS 轉址頁，且部分地區會導到 consent.google.com（同意頁文字不是內文），一律不抓
    if urlsplit(item["link"]).netloc.endswith("google.com"):
        return None
    try:
        resp = requests.get(item["link"], headers=BROWSER_HEADERS, timeout=BODY_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        if urlsplit(resp.url).netloc.endswith("google.com"):
            return None
        text = strip_tags(resp.text)
    except Exception:  # noqa: BLE001
        return None
    return text[:BODY_MAX_CHARS] if len(text) >= 300 else None


# ---------------------------------------------------------------------------
# LLM 分析
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "你是協助學生團隊參加股票操作比賽的研究分析師。比賽是長倉 only、單檔上限 20%、"
    "以相對全球指數的超額報酬計分，所以純產業/大盤雜訊要明講「只是雜訊」。\n"
    "規則：\n"
    "1. 只使用我提供的文字裡的事實；絕不編造數字、日期、財測或股價。\n"
    "2. 若某則的『內文』標示為不可取得，只能依標題判斷，必須在 reasoning_zh 註明「僅依標題」，"
    "且 confidence 只能是 low。\n"
    "3. est_move 是估計不是預測，格式如 \"+3% to +6% over 1-5 trading days\"；無法估計就給 null，不要假精確。\n"
    "4. 只回傳 JSON，不要任何其他文字或 markdown 圍欄。"
)


def build_prompt(entry: dict, items: list[dict], bodies: dict[int, str | None]) -> str:
    ctx = {
        "ticker": entry["ticker"], "name": entry.get("name"), "market": entry.get("market"),
        "status": entry.get("status"), "weight_pct": entry.get("weight_pct"),
        "entry_price": entry.get("entry_price"), "stop_pct": entry.get("stop_pct"),
        "next_events": entry.get("next_events", []), "thesis": entry.get("thesis"),
    }
    blocks = []
    for i, it in enumerate(items, 1):
        body = bodies.get(i - 1)
        blocks.append(
            f"[{i}] 標題：{it['title']}\n來源：{it['source']}（{it['origin']}）\n"
            f"時間：{it['published'].isoformat() if it['published'] else '未知'}\n"
            f"摘要：{it['snippet'] or '（無）'}\n"
            f"內文：{body if body else '【不可取得 — 只能依標題判斷】'}"
        )
    schema = (
        '{"items":[{"id":1,"material":true,"value":"high|medium|low","summary_zh":"<=60字繁體中文",'
        '"direction":"up|down|neutral","est_move":"字串或null","priced_in":true,"priced_in_why":"簡短理由",'
        '"action":"加倉|持有|減倉|出場|觀察","reasoning_zh":"<=100字繁體中文","confidence":"low|medium|high",'
        '"watch_next":"接下來要盯的事或null"}]}'
    )
    return (
        f"持股/標的背景（JSON）：\n{json.dumps(ctx, ensure_ascii=False)}\n\n"
        f"以下是 {len(items)} 則新消息，請逐則分析，id 對應方括號編號：\n\n" + "\n\n".join(blocks) +
        f"\n\n只回傳符合此結構的 JSON：\n{schema}"
    )


def call_claude(api_key: str, model: str, prompt: str) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": model, "max_tokens": 4000, "system": SYSTEM_PROMPT,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    resp.raise_for_status()
    return "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")


def _clip(v, n: int) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s[:n] if s and s.lower() != "null" else None


def _pick(v, allowed: tuple, default: str) -> str:
    v = str(v).strip().lower() if v is not None else ""
    for a in allowed:
        if v == a.lower():
            return a
    return default


def parse_analysis(text: str, n_items: int, body_ok: list[bool]) -> dict[int, dict] | None:
    """解析並夾限 LLM 回傳；整體無法解析回傳 None（呼叫端退回摘要格式）。回傳 {0-based index: analysis}。"""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)  # 容忍模型誤加 markdown 圍欄
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    rows = data.get("items") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return None
    out: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("id")) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < n_items:
            continue
        priced = row.get("priced_in")
        priced_why = row.get("priced_in_why")
        if isinstance(priced, dict):  # 容忍 {"value":bool,"why":str}
            priced_why = priced.get("why", priced_why)
            priced = priced.get("value", priced.get("priced_in"))
        conf = _pick(row.get("confidence"), CONFIDENCES, "low")
        if not body_ok[idx]:
            conf = "low"  # 沒內文，信心上限 low（程式強制，不只靠 prompt）
        out[idx] = {
            "material": row.get("material") is True or str(row.get("material")).lower() == "true",
            "value": _pick(row.get("value"), VALUES, "low"),
            "summary_zh": _clip(row.get("summary_zh"), 60) or "",
            "direction": _pick(row.get("direction"), DIRECTIONS, "neutral"),
            "est_move": _clip(row.get("est_move"), 80),
            "priced_in": priced if isinstance(priced, bool) else None,
            "priced_in_why": _clip(priced_why, 80),
            "action": _pick(row.get("action"), ACTIONS, "觀察"),
            "reasoning_zh": _clip(row.get("reasoning_zh"), 100) or "",
            "confidence": conf,
            "watch_next": _clip(row.get("watch_next"), 80),
        }
    return out or None


def should_send(item: dict, analysis: dict | None) -> bool:
    if analysis and analysis["material"] and analysis["value"] in ("high", "medium"):
        return True
    return item.get("origin") == "Jin10" or bool(HARD_TRIGGERS.search(item["title"]))  # Jin10 已先比對過個股名


# ---------------------------------------------------------------------------
# 訊息組裝與 Telegram
# ---------------------------------------------------------------------------

def _e(s) -> str:
    # RSS 標題常已含 &#39; 之類實體，先還原再跳脫，避免 Telegram 顯示成亂碼
    return html.escape(html.unescape(str(s)), quote=True).replace("&#x27;", "&#39;")


def _link(item: dict) -> str:
    return f'<a href="{_e(item["link"])}">{_e(item["source"])}</a>'


def format_item_analyzed(i: int, item: dict, a: dict | None) -> str:
    if a is None:  # 觸發關鍵字但 LLM 沒給這則的分析
        return f"{i}. {_e(item['title'])}\n（關鍵字觸發，未取得分析）{_link(item)}"
    lines = [f"<b>{i}. {_e(a['summary_zh'] or item['title'])}</b>  [重要性:{VALUE_LABEL[a['value']]}]"]
    lines.append(f"方向：{DIR_LABEL[a['direction']]}　預估（估計值，非預測）：{_e(a['est_move'] or '無法估計')}")
    if a["priced_in"] is not None:
        lines.append(f"已反映在股價：{'是' if a['priced_in'] else '否'}"
                     + (f"（{_e(a['priced_in_why'])}）" if a["priced_in_why"] else ""))
    lines.append(f"建議：<b>{_e(a['action'])}</b>（信心：{CONF_LABEL[a['confidence']]}）")
    if a["reasoning_zh"]:
        lines.append(_e(a["reasoning_zh"]))
    if a["watch_next"]:
        lines.append(f"接下來看：{_e(a['watch_next'])}")
    lines.append(f"{_link(item)} · {_e(item['title'][:90])}")
    return "\n".join(lines)


# 無 LLM 時的粗略判讀：只看標題/摘要裡的關鍵字，不是完整分析。
BULL_WORDS = re.compile(
    r"\b(?:beats?|tops?|surg\w*|soar\w*|jump\w*|rall(?:y|ies|ied)|record|raises?|raised|boosts?|"
    r"upgrad\w*|outperform\w*|approv\w*|wins?|won|accelerat\w*|lifts?|climbs?|gains?|strong\w*)\b"
    r"|超預期|調升|上修|大漲|創高|核准|獲准"
    r"|利好|大涨|暴涨|飙升|创新高|超预期|上调|上修|获批|获准|涨超|拉升|走高",
    re.IGNORECASE,
)
BEAR_WORDS = re.compile(
    r"\b(?:miss(?:es|ed)?|falls?|fell|drops?|dropped|plung\w*|slump\w*|tumbl\w*|sinks?|slid\w*|slides?|"
    r"cuts?|lowers?|downgrad\w*|warns?|probe|investigation|lawsuit|sued|recall\w*|halts?|delay\w*|"
    r"rejects?|weak\w*|sell-?off|dilut\w*|offering|layoffs?|fraud|short seller)\b"
    r"|不如預期|調降|下修|大跌|訴訟|召回"
    r"|利空|大跌|暴跌|重挫|下挫|不及预期|低于预期|下调|下修|诉讼|召回|跌超|走低|被调查",
    re.IGNORECASE,
)
EVENT_TYPES = [
    (r"earnings|財報", "財報"), (r"guidance|財測|展望", "財測"),
    (r"\bFDA\b|\bEMA\b|PDUFA", "監管審批"), (r"upgrade|downgrade|評等", "分析師評等"),
    (r"offering|增資", "增資"), (r"lawsuit|\bprobe\b|investigation|訴訟", "訴訟/調查"),
    (r"contract|合約", "合約"), (r"acquisition|to acquire|併購", "併購"), (r"recall|召回", "召回"),
]


def rule_view(item: dict) -> dict:
    """標題關鍵字判讀：回傳 {label, types[], words[]}。粗略訊號，不取代閱讀內文。"""
    text = f"{item.get('title', '')} {item.get('snippet', '') or ''}"
    bull = [m.group(0).lower() for m in BULL_WORDS.finditer(text)]
    bear = [m.group(0).lower() for m in BEAR_WORDS.finditer(text)]
    if bull and not bear:
        label = "📈 偏多"
    elif bear and not bull:
        label = "📉 偏空"
    elif bull and bear:
        label = "⚖️ 多空混雜" if len(bull) == len(bear) else ("📈 略偏多" if len(bull) > len(bear) else "📉 略偏空")
    else:
        label = "➖ 看不出方向"
    types = [name for pat, name in EVENT_TYPES if re.search(pat, text, re.IGNORECASE)]
    words = list(dict.fromkeys(bull + bear))[:4]
    return {"label": label, "types": types, "words": words}


def format_item_digest(i: int, item: dict) -> str:
    v = rule_view(item)
    meta = [f"判讀：{v['label']}"]
    if v["types"]:
        meta.append("類型：" + "、".join(v["types"]))
    if v["words"]:
        meta.append("依據：" + ", ".join(v["words"]))
    return f"{i}. {_e(item['title'])}\n{_e(' · '.join(meta))}\n{_link(item)}"


def build_message(entry: dict, rows: list[tuple[dict, dict | None]], analyzed: bool, today=None) -> str:
    """rows: [(item, analysis|None)]。analyzed=False 代表無分析的摘要版。"""
    head = f"🔔 <b>{_e(entry['ticker'])} {_e(entry.get('name', ''))}</b> — {len(rows)} 則新消息"
    meta = []
    if entry.get("status"):
        meta.append("持有" if entry["status"] == "held" else "候選")
    if entry.get("weight_pct") is not None:
        meta.append(f"權重 {entry['weight_pct']}%")
    if meta:
        head += f"\n<i>{_e(' · '.join(meta))}</i>"
    if is_immediate(entry):
        head += f"\n🧭 {_e(decision_card(entry, today or datetime.now(timezone.utc).date()))}"
    body = [format_item_analyzed(i, it, a) if analyzed else format_item_digest(i, it)
            for i, (it, a) in enumerate(rows, 1)]
    note = "" if analyzed else "判讀為標題關鍵字的粗略規則，請點連結看內文。"
    return head + "\n\n" + "\n\n".join(body) + f"\n\n<i>{note}{DISCLAIMER}</i>"


def is_immediate(entry: dict) -> bool:
    """持有或標了 priority 的標的即時推播；其餘（候選）進每小時摘要。"""
    return entry.get("status") == "held" or bool(entry.get("priority"))


def is_immediate_macro(item: dict) -> bool:
    return bool(IMMEDIATE_MACRO.search(f"{item.get('title', '')} {item.get('snippet', '') or ''}"))


def decision_card(entry: dict, today) -> str:
    """持股即時通知的一行決策卡；未知欄位（null）直接略過。不含現價/離停損距離（沒有報價來源）。"""
    parts = []
    w, ep, sp = entry.get("weight_pct"), entry.get("entry_price"), entry.get("stop_pct")
    if w is not None:
        parts.append(f"持有 {w:g}%")
    if ep is not None:
        parts.append(f"成本 ${ep:,.2f}")
    if ep is not None and sp is not None:
        parts.append(f"停損 ${ep * (1 - sp / 100):,.2f}（−{sp:g}%）")
    elif sp is not None:
        parts.append(f"停損 −{sp:g}%")
    upcoming = []
    for ev in entry.get("next_events") or []:
        try:
            d = datetime.strptime(str(ev.get("date")), "%Y-%m-%d").date()
        except (TypeError, ValueError, AttributeError):
            continue
        if d >= today:
            upcoming.append((d, ev.get("label") or ""))
    if upcoming:
        d, label = min(upcoming, key=lambda x: x[0])
        parts.append(f"下個事件 {d.month:02d}/{d.day:02d} {label}（{(d - today).days} 天後）")
    else:
        parts.append("無已排定事件")
    return " · ".join(parts)


def queue_digest(state: dict, ticker: str, name: str, it: dict, label: str, now: datetime) -> bool:
    """把一則加進摘要佇列（以 item key 去重）。回傳是否真的新增。"""
    d = get_digest(state)
    if any(p.get("key") == it["key"] for p in d["pending"]):
        return False
    ts = (it.get("published") or now).astimezone(timezone.utc).isoformat()
    d["pending"].append({"key": it["key"], "ticker": ticker, "name": name, "title": it["title"],
                         "link": it["link"], "source": it.get("source") or "", "label": label, "ts": ts})
    if len(d["pending"]) > DIGEST_MAX_PENDING:
        d["pending"] = sorted(d["pending"], key=lambda p: p.get("ts", ""))[-DIGEST_MAX_PENDING:]
    return True


def digest_due(state: dict, now: datetime) -> bool:
    d = get_digest(state)
    if not d["pending"]:
        return False
    if os.environ.get("NEWS_FORCE_DIGEST"):
        return True
    if not d["last_sent"]:
        return True
    try:
        last = datetime.fromisoformat(d["last_sent"])
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= timedelta(minutes=DIGEST_INTERVAL_MIN)


def build_digest_message(pending: list[dict]) -> str:
    rows = sorted(pending, key=lambda p: p.get("ts", ""), reverse=True)
    shown, rest = rows[:DIGEST_MAX_LISTED], len(rows) - DIGEST_MAX_LISTED
    groups: dict[str, list[dict]] = {}
    for p in shown:
        groups.setdefault(p["ticker"], []).append(p)
    order = [t for t in groups if t != "MACRO"] + (["MACRO"] if "MACRO" in groups else [])
    blocks = []
    for t in order:
        if t == "MACRO":
            head = "📊 <b>總經</b>"
        else:
            head = f"<b>{_e(t)} {_e(groups[t][0].get('name') or '')}</b>".rstrip()
        lines = []
        for p in groups[t]:
            title = p["title"] if len(p["title"]) <= DIGEST_TITLE_MAX else p["title"][:DIGEST_TITLE_MAX] + "…"
            lines.append(f"• {_e(p.get('label') or '')} {_e(title)} {_link(p)}")
        blocks.append(head + "\n" + "\n".join(lines))
    msg = f"🗞 <b>每小時摘要（候選股與一般總經）— {len(rows)} 則</b>\n\n" + "\n\n".join(blocks)
    if rest > 0:
        msg += f"\n\n另有 {rest} 則未列出"
    return msg + f"\n\n<i>判讀為標題關鍵字的粗略規則，請點連結看內文。{DISCLAIMER}</i>"


def flush_digest(state: dict, now: datetime, token: str, chat_id: str, can_send: bool, dry_run: bool) -> int:
    """到期就送一則摘要；成功才清佇列並更新 last_sent。回傳送出（或 dry-run 印出）的則數。"""
    if not digest_due(state, now):
        return 0
    d = get_digest(state)
    n = len(d["pending"])
    for part in split_message(build_digest_message(d["pending"])):
        if dry_run or not can_send:
            print("-" * 40 + f"\n{part}\n" + "-" * 40)
        else:
            try:
                send_telegram(token, chat_id, part)
            except Exception as e:  # noqa: BLE001 - 不印 URL（含 token）
                print(f"[摘要] Telegram 傳送失敗：{type(e).__name__}，保留佇列，下輪重試")
                return 0
    if not dry_run:
        d["pending"] = []
        d["last_sent"] = now.isoformat()
    return n


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """依空行（則與則之間）切段，每段 <= limit；單段超長才退而求其次按行/字元硬切。"""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:  # 病態情況：單則本身超長
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(para[:cut])
            para = para[cut:].lstrip("\n")
        if len(cur) + len(para) + 2 <= limit:
            cur = f"{cur}\n\n{para}" if cur else para
        else:
            chunks.append(cur)
            cur = para
    if cur:
        chunks.append(cur)
    return chunks


def send_telegram(token: str, chat_id: str, text: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def analyze_entry(entry: dict, items: list[dict], api_key: str, model: str) -> dict[int, dict] | None:
    bodies = {i: fetch_body(it) for i, it in enumerate(items)}
    body_ok = [bodies[i] is not None for i in range(len(items))]
    try:
        text = call_claude(api_key, model, build_prompt(entry, items, bodies))
    except Exception as e:  # noqa: BLE001 - 不印 response body，避免意外洩漏
        print(f"  [{entry['ticker']}] LLM 呼叫失敗，退回摘要：{type(e).__name__}")
        return None
    result = parse_analysis(text, len(items), body_ok)
    if result is None:
        print(f"  [{entry['ticker']}] LLM 回傳無法解析為 JSON，退回摘要")
    return result


def run_macro(cfg: dict, args, jin10_items: list[dict], state: dict, claimed: set, new_state: dict,
              now: datetime, token: str, chat_id: str, can_send: bool) -> int:
    """總經快訊：每輪最多一則訊息；沒有命中就完全靜默。回傳實際列出的則數。"""
    macro = cfg["macro"]
    try:
        google_items = fetch_macro_google(macro, args.since_hours)
        fresh = select_macro(jin10_items, google_items, macro, state, claimed, now, args.since_hours)
    except Exception as e:  # noqa: BLE001 - 總經失敗不影響個股結果
        print(f"[總經] 失敗（略過）：{type(e).__name__}")
        return 0
    j_n = sum(1 for it in fresh if it["origin"] == "Jin10")
    print(f"[總經] Jin10 命中 {j_n} 則、Google News 命中 {len(fresh) - j_n} 則（硬觸發、未見過）")
    if not fresh:
        return 0
    imm = [it for it in fresh if is_immediate_macro(it)]
    for it in fresh:
        if it in imm:
            continue
        queue_digest(state, "MACRO", "", it, macro_view(it)["label"], now)  # 非 CPI 總經：進每小時摘要
        new_state[it["key"]] = {"ts": now.isoformat(), "ticker": "MACRO", "title": it["title"][:120]}
    print(f"[總經] 即時（CPI 類）{len(imm)} 則、轉入每小時摘要 {len(fresh) - len(imm)} 則")
    if not imm:
        return 0
    fresh = imm
    rows = fresh[:MACRO_MAX_ITEMS]
    run_macro.jin10_listed = sum(1 for it in rows if it["origin"] == "Jin10")
    if len(rows) < len(fresh):
        print(f"[總經] 共 {len(fresh)} 則，僅列最新 {len(rows)} 則（其餘記為已見）")
    sent_ok = True
    for part in split_message(build_macro_message(rows)):
        if args.dry_run or not can_send:
            print("-" * 40 + f"\n{part}\n" + "-" * 40)
        else:
            try:
                send_telegram(token, chat_id, part)
            except Exception as e:  # noqa: BLE001 - 不印 URL（含 token）
                print(f"[總經] Telegram 傳送失敗：{type(e).__name__}，不標記已見，下輪重試")
                sent_ok = False
                break
    if sent_ok:
        for it in fresh:
            new_state[it["key"]] = {"ts": now.isoformat(), "ticker": "MACRO", "title": it["title"][:120]}
    return len(rows)


def run(args, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    cfg = load_config(Path(args.config))
    if is_past_end(cfg, now):
        print(f"已超過比賽結束日 {cfg.get('end_date')}，不做任何事。")
        return 0

    entries = cfg.get("entries", [])
    if args.ticker:
        entries = [e for e in entries if e["ticker"].upper() == args.ticker.upper()]
        if not entries:
            print(f"watchlist 內找不到 {args.ticker}", file=sys.stderr)
            return 2

    state_path = Path(os.environ.get("NEWS_STATE_PATH") or STATE_PATH)
    state = prune_state(load_state(state_path), now)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    use_llm = bool(api_key) and not args.no_llm
    model = os.environ.get("NEWS_MODEL") or cfg.get("model") or DEFAULT_MODEL
    token = os.environ.get("NEWS_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("NEWS_TELEGRAM_CHAT_ID", "").strip()
    can_send = bool(token and chat_id) and not args.dry_run
    print(f"模式：{'LLM 分析' if use_llm else '無分析摘要'}"
          f"{'（--no-llm）' if args.no_llm else '（無 ANTHROPIC_API_KEY）' if not api_key else ''}；"
          f"{'dry-run' if args.dry_run else ('Telegram 推播' if can_send else '未設定 Telegram，僅印出')}")

    claimed: set = set()
    new_state: dict = {}
    total_new = total_sent = total_suppressed = total_queued = 0
    jin10_items = collect_jin10(args.since_hours, now)  # 全 run 只打一次
    jin10_ticker_hits = 0
    for entry in entries:
        print(f"[{entry['ticker']}] {entry.get('name', '')}")
        items = collect_for_entry(entry, args.since_hours, now)
        j_hits = [dict(it, trusted=True) for it in jin10_items if is_relevant_jin10(it, entry)]
        if j_hits:
            print(f"  [{entry['ticker']}] Jin10: {len(j_hits)} 則比對到")
            jin10_ticker_hits += len(j_hits)
        items += j_hits
        fresh = select_new(items, entry, state, claimed)
        total_new += len(fresh)
        if fresh and not is_immediate(entry):
            # 候選股：不即時推，也不呼叫 LLM；硬觸發/Jin10 的進摘要佇列，全部標為已見
            hits = [it for it in fresh if it.get("origin") == "Jin10" or HARD_TRIGGERS.search(it["title"])]
            added = sum(queue_digest(state, entry["ticker"], entry.get("name", ""), it, rule_view(it)["label"], now)
                        for it in hits)
            total_queued += added
            total_suppressed += len(fresh) - len(hits)
            print(f"  [{entry['ticker']}] 候選股：{len(fresh)} 則新聞，{added} 則進每小時摘要佇列（不即時推播）")
            for it in fresh:
                new_state[it["key"]] = {"ts": now.isoformat(), "ticker": entry["ticker"], "title": it["title"][:120]}
            continue
        # LLM 每檔每輪只一次呼叫，批次上限 MAX_ITEMS_PER_ENTRY（取最新）；摘要版不設上限。
        batch = fresh[:MAX_ITEMS_PER_ENTRY] if use_llm else fresh
        if len(batch) < len(fresh):
            print(f"  [{entry['ticker']}] 新聞 {len(fresh)} 則，僅分析最新 {len(batch)} 則（其餘記為已見，不再推）")
        if not fresh:
            continue

        analysis = analyze_entry(entry, batch, api_key, model) if use_llm else None
        if analysis is not None:
            rows = [(it, analysis.get(i)) for i, it in enumerate(batch)]
            rows = [(it, a) for it, a in rows if should_send(it, a)]
            total_suppressed += len(batch) - len(rows)
            msg = build_message(entry, rows, True, now.date()) if rows else None
        else:  # 無 LLM 或解析失敗：摘要版；只列硬觸發關鍵字的標題並設上限，避免洗版
            hits = [it for it in batch if it.get("origin") == "Jin10" or HARD_TRIGGERS.search(it["title"])]
            rows = [(it, None) for it in hits[:MAX_DIGEST_ITEMS]]
            total_suppressed += len(batch) - len(rows)
            msg = build_message(entry, rows, False, now.date()) if rows else None

        sent_ok = True
        if msg:
            total_sent += len(rows)
            for part in split_message(msg):
                if args.dry_run or not can_send:
                    print("-" * 40 + f"\n{part}\n" + "-" * 40)
                else:
                    try:
                        send_telegram(token, chat_id, part)
                    except Exception as e:  # noqa: BLE001 - 不印 URL（含 token）
                        print(f"  [{entry['ticker']}] Telegram 傳送失敗：{type(e).__name__}，不標記已見，下輪重試")
                        sent_ok = False
                        break
        if sent_ok:  # 傳送失敗的標的不寫入已見，下一輪才會補送
            for it in fresh:
                new_state[it["key"]] = {"ts": now.isoformat(), "ticker": entry["ticker"], "title": it["title"][:120]}

    run_macro.jin10_listed = 0
    macro_sent = run_macro(cfg, args, jin10_items, state, claimed, new_state, now, token, chat_id, can_send) \
        if cfg.get("macro") and not args.ticker else 0
    digest_sent = flush_digest(state, now, token, chat_id, can_send, args.dry_run)
    print(f"\nJin10 彙總：原始 {len(jin10_items)} 則、比對到個股 {jin10_ticker_hits} 則、列入總經訊息 {run_macro.jin10_listed} 則")
    print(f"完成：新聞 {total_new} 則（待推播 {total_sent}，低價值靜默 {total_suppressed}，持股灌水文濾除 {noise_count[0]}；總經 {macro_sent} 則；入摘要佇列 {total_queued}，本輪摘要送出 {digest_sent} 則，佇列剩 {len(get_digest(state)['pending'])} 則）")
    if args.dry_run:
        return 0
    if not can_send and not os.environ.get("NEWS_ALLOW_UNSENT_STATE"):
        # 沒有 Telegram 憑證時不寫入已見，否則這批新聞之後再也不會被推播
        print("未設定 Telegram：不寫入狀態（本機測試去重可設 NEWS_ALLOW_UNSENT_STATE=1）。", file=sys.stderr)
        return 1 if os.environ.get("GITHUB_ACTIONS") else 0
    state.update(new_state)
    save_state(state_path, state)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只印出訊息，不推播、不寫入狀態")
    parser.add_argument("--no-llm", action="store_true", help="不呼叫 Claude，只送規則版摘要")
    parser.add_argument("--since-hours", type=int, default=36, help="回溯多少小時內的新聞（首輪補抓窗口）")
    parser.add_argument("--ticker", help="只處理 watchlist 內的單一標的")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="watchlist 路徑")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
