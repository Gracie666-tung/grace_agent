#!/usr/bin/env python3
"""全球原物料價格監控：抓 Yahoo Finance 期貨/ETF 日線（主資料源），Yahoo 失敗時改走
FRED 公開 CSV 端點（備援，不需 API key），算變動率、移動平均乖離率、波動度、z-score
異常判定、品項間相關係數矩陣，做 baseline 成本預測，Claude AI 判讀，推播 Telegram，
並輸出 GitHub Pages dashboard 用的 data.json。

用法：
    python src/commodity_monitor.py --dry-run                    # 只印報告，不推播、不寫歷史
    python src/commodity_monitor.py --dry-run --write-dashboard  # 同上，另外輸出
                                                                    docs/commodity/data.json
                                                                    方便本機預覽 dashboard
    python src/commodity_monitor.py                              # 正式執行（需要環境變數
                                                                    COMMODITY_TELEGRAM_BOT_TOKEN、
                                                                    COMMODITY_TELEGRAM_CHAT_ID；
                                                                    ANTHROPIC_API_KEY 可選，
                                                                    沒有就走規則版降級）
"""

import argparse
import html
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "commodity_config.json"
HISTORY_PATH = ROOT / "data" / "commodity_history.json"
DASHBOARD_DATA_PATH = ROOT / "docs" / "commodity" / "data.json"

# 沿用 stock_advisor.py / daily_jobs.py 已驗證過的完整瀏覽器 UA（短 UA 容易被反爬擋掉）。
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
YAHOO_HEADERS = {"User-Agent": UA, "Accept": "application/json", "Referer": "https://finance.yahoo.com/"}
# FRED 反而跟 Yahoo 相反：帶完整瀏覽器 UA 會被它的 CDN 判定異常，直接 HTTP/2 連線重置
# （實測 curl/requests 用完整 Chrome UA 打會在 <0.1 秒內連線失敗，換回預設 UA 就正常
# 200，2026-07-20 實測確認）。所以 FRED 請求刻意不帶自訂 UA，用 requests 預設值。
FRED_HEADERS: dict = {}
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
# Yahoo backoff：429 目前實測是短時間限流，非永久封鎖，但不保證幾秒內就解除 —— 拉長等待。
YAHOO_BACKOFF_SECONDS = [5, 15, 45]

TPE = ZoneInfo("Asia/Taipei")
TELEGRAM_MSG_LIMIT = 4096
DISCLAIMER_FORECAST = "⚠️ 成本預測為 naive／移動平均／線性趨勢外推等統計 baseline，非預測保證，僅供採購參考"
DISCLAIMER = "⚠️ 本報告為統計指標與 AI 判讀，非投資或採購建議，決策請自行判斷"

SYSTEM_PROMPT = """你是全球原物料採購分析助理，會收到今日各原物料/期貨的技術指標 JSON
（變動率、移動平均乖離率、波動度、z-score異常旗標）、品項間相關係數矩陣、以及成本預測
baseline。部分品項的 metrics 會標示 freq="monthly"（來自 FRED 月頻數列）或
data_source="fred"／"history"（該品項今日改用備援資料源或沿用舊資料，非 Yahoo 當日
即時價）——這些品項沒有日/週變動率、波動度、z-score 是正常現象，不是資料缺失，判讀時
請據實反映，不要假裝有日頻資訊。若收到 cross_source_check，代表 Yahoo 與 FRED
同時有資料可比對，數字差異是預期現象（期貨 vs 現貨價差），僅供資料品質參考。
請用繁體中文輸出結構化短報告，恰好三段：

1. 本日重點：2-4 行摘要今日整體原物料價格走勢與最值得注意的品項。
2. 異常波動警示：針對 z-score 判定為異常波動的品項逐一說明可能成因（不要編造具體新聞事件，
   只根據數字合理推論），若無異常品項則明講「今日無品項觸發異常波動門檻」。
3. 對採購的意涵：給 1-3 點對採購/成本規劃的具體含意（例如是否該提前鎖價、觀望、或留意
   替代品項），語氣像分析師而非行銷。

不要輸出多餘的開場白或收尾客套話，直接進入三段內容。最後不要加額外免責聲明，程式會自行附加。"""


# ---------------------------------------------------------------------------
# 設定 / 歷史資料存取
# ---------------------------------------------------------------------------

def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_history() -> dict:
    """回傳 {code: {date_iso: close}}。"""
    if HISTORY_PATH.exists():
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    return {}


def save_history(history: dict) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Yahoo Finance 抓取
# ---------------------------------------------------------------------------

def fetch_chart(code: str, range_: str, retries: int = 4) -> dict:
    """打 Yahoo Finance chart API，含重試 backoff（這個端點對高頻請求會回 429，
    實測是暫時性限流，但解除時間不保證只有幾秒，backoff 拉長到 5/15/45 秒）。
    404（symbol 不存在/已下市）不重試，直接視為失敗，讓呼叫端記錄並略過（或改走 FRED 備援）。"""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{code}",
                params={"range": range_, "interval": "1d"},
                headers=YAHOO_HEADERS,
                timeout=20,
            )
        except requests.RequestException as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(YAHOO_BACKOFF_SECONDS[min(attempt, len(YAHOO_BACKOFF_SECONDS) - 1)])
            continue

        if resp.status_code == 404:
            raise RuntimeError(f"{code} 查無資料（可能代碼錯誤或已下市）")

        if resp.status_code == 429 or resp.status_code >= 500:
            last_err = RuntimeError(f"HTTP {resp.status_code}")
            if attempt < retries - 1:
                time.sleep(YAHOO_BACKOFF_SECONDS[min(attempt, len(YAHOO_BACKOFF_SECONDS) - 1)])
            continue

        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001 — 非預期格式也走重試路徑
            last_err = e
            if attempt < retries - 1:
                time.sleep(YAHOO_BACKOFF_SECONDS[min(attempt, len(YAHOO_BACKOFF_SECONDS) - 1)])
            continue

        result = (data.get("chart") or {}).get("result")
        if not result:
            err = (data.get("chart") or {}).get("error") or {}
            raise RuntimeError(f"{code} 回傳無資料：{err.get('description', '未知錯誤')}")
        return result[0]

    raise RuntimeError(f"{code} 抓取失敗（重試 {retries} 次）：{last_err}")


def parse_chart(result: dict) -> list[tuple[str, float]]:
    """把 chart API 的原始結果轉成 [(date_iso, close), ...]，依日期遞增排序，
    丟掉 close 為 null 的 bar（Yahoo 偶爾會有休市但仍給時間戳的殘缺資料）。
    日期用該商品所屬交易所的當地時區判斷「交易日」，不是台北時區 —— 所以美股期貨的
    「今天」在台北時間看，其實是台北的前一個日曆天，這是刻意的（跟著交易所行事曆走）。"""
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    tz_name = (result.get("meta") or {}).get("exchangeTimezoneName") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc

    points = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        date_str = datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
        points.append((date_str, float(close)))
    dedup = dict(points)  # 同一天若出現多筆（時區換算邊界），保留最後一筆
    return sorted(dedup.items())


def update_history_yahoo(history: dict, code: str, cfg: dict) -> list[tuple[str, float]]:
    """回傳合併後、依日期遞增排序的 (date, close) 序列（僅記憶體內，落不落地由呼叫端決定）。
    首次執行（history 沒有這個 code 或是空的）回補約 2 年（涵蓋 spec 要求的至少 400 天）；
    之後已有歷史只需抓近期補新資料，減少 API 負擔。"""
    range_ = "2y" if not history.get(code) else "5d"
    result = fetch_chart(code, range_)
    fetched = parse_chart(result)

    series = dict(history.get(code, {}))
    for date_str, close in fetched:
        series[date_str] = close
    history[code] = series

    return sorted(series.items())


# ---------------------------------------------------------------------------
# FRED 抓取（備援資料源＋交叉驗證，不需 API key、無限流疑慮）
# ---------------------------------------------------------------------------

def fetch_fred(series_id: str) -> list[tuple[str, float]]:
    """打 FRED 公開 CSV 端點，回傳 [(date_iso, value), ...] 依日期遞增排序。
    缺值列（假日/未公布）用 "." 表示，直接跳過。CSV 沒有分頁/range 參數，每次都是整個數列，
    FRED 沒有已知的限流問題所以不用像 Yahoo 那樣分 2y/5d 省請求。"""
    resp = requests.get(FRED_CSV_URL, params={"id": series_id}, headers=FRED_HEADERS, timeout=20)
    resp.raise_for_status()
    lines = resp.text.strip().splitlines()
    if len(lines) < 2:
        raise RuntimeError(f"FRED {series_id} 回傳空白或格式不符")

    points: list[tuple[str, float]] = []
    for line in lines[1:]:
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        date_str, value = parts[0], parts[1]
        if value in (".", ""):
            continue
        try:
            points.append((date_str, float(value)))
        except ValueError:
            continue
    return sorted(dict(points).items())


def update_history_fred(history: dict, code: str, series_id: str, cfg: dict) -> list[tuple[str, float]]:
    """FRED 序列存在獨立的 history key（f"{code}::fred"），不跟 Yahoo 序列（history[code]）
    混在一起 —— 兩者尺度/單位常常不同（例如銅：FRED 是 USD/公噸月均價，Yahoo 期貨是
    USD/lb 日收盤），混進同一個時間序列會讓漲跌幅/波動度計算出現假的巨幅跳動。"""
    key = f"{code}::fred"
    fetched = fetch_fred(series_id)
    series = dict(history.get(key, {}))
    for date_str, value in fetched:
        series[date_str] = value
    history[key] = series
    return sorted(series.items())


def cross_check_sources(
    yahoo_series: list[tuple[str, float]], fred_series: list[tuple[str, float]]
) -> dict | None:
    """取 Yahoo 與 FRED 都有報價的最近共同交易日，比較收盤價差異百分比。
    兩者本來就會有價差（Yahoo 是期貨、FRED 多為現貨），這不是套利訊號，是拿來偵測
    「資料抓錯／單位換算錯／過期快取」這類異常的品質檢查。"""
    y = dict(yahoo_series)
    f = dict(fred_series)
    common = sorted(set(y) & set(f))
    if not common:
        return None
    d = common[-1]
    yv, fv = y[d], f[d]
    if yv == 0:
        return None
    return {
        "date": d,
        "yahoo_close": round(yv, 4),
        "fred_close": round(fv, 4),
        "diff_pct": round((yv - fv) / yv * 100, 2),
    }


# ---------------------------------------------------------------------------
# 資料源 dispatcher：Yahoo 為主，失敗才 fallback FRED
# ---------------------------------------------------------------------------

def fetch_symbol(
    history: dict, symbol_cfg: dict, cfg: dict, source_mode: str = "auto"
) -> tuple[list[tuple[str, float]], str, str]:
    """決定並抓取單一標的的資料序列，回傳 (series, source, freq)。
    source_mode（CLI --source）："auto"＝預設，先試 Yahoo，失敗且有 FRED 對照才 fallback；
    "yahoo"＝只試 Yahoo，不 fallback（測試用）；"fred"＝完全跳過 Yahoo、只走 FRED
    （測試/驗證用，避免對目前被限流中的 Yahoo 端點增加請求）。
    全部來源都失敗會拋錯，由呼叫端決定要不要 fallback 到舊歷史資料。"""
    code = symbol_cfg["code"]
    fred_id = symbol_cfg.get("fred")
    fred_freq = symbol_cfg.get("fred_freq")

    if source_mode != "fred":
        try:
            series = update_history_yahoo(history, code, cfg)
            if len(series) < 20:
                raise RuntimeError(f"{code} yahoo 有效資料筆數不足（{len(series)} 筆）")
            return series, "yahoo", "daily"
        except Exception as e:
            if source_mode == "yahoo" or not fred_id:
                raise
            print(f"[warn] {code} yahoo 失敗，改走 FRED（{fred_id}）：{e}", file=sys.stderr)

    if not fred_id:
        raise RuntimeError(f"{code} 無 FRED 對照可用，且已指定跳過 Yahoo")

    series = update_history_fred(history, code, fred_id, cfg)
    min_points = 3 if fred_freq == "monthly" else 20
    if len(series) < min_points:
        raise RuntimeError(f"{code} FRED（{fred_id}）有效資料筆數不足（{len(series)} 筆）")
    return series, "fred", (fred_freq or "daily")


# ---------------------------------------------------------------------------
# 分析層
# ---------------------------------------------------------------------------

def log_returns(closes: list[float]) -> list[float]:
    """log(close[i]/close[i-1])，兩邊都要 > 0 才計算 —— 原本只檢查分母，FRED 的
    DCOILWTICO 全歷史數列含 2020-04-20 WTI 期貨史上首次收盤負值（-36.98），分子為負
    會讓 math.log 丟 ValueError（math domain error），這裡直接跳過那一筆日報酬
    （沒有意義的極端事件，跳過比讓整支程式炸掉更誠實）。"""
    return [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]


def _round2(v: float | None) -> float | None:
    return round(v, 2) if v is not None else None


def pct_change(closes: list[float], back: int) -> float | None:
    """back 是「交易日數」不是日曆天數 —— 週=5、月=21、年=252 是金融資料常見近似。"""
    if len(closes) <= back:
        return None
    prev = closes[-1 - back]
    if prev == 0:
        return None
    return (closes[-1] - prev) / prev * 100


def moving_average(closes: list[float], window: int) -> float | None:
    if len(closes) < window:
        return None
    return statistics.fmean(closes[-window:])


def compute_metrics(closes: list[float], cfg: dict) -> dict:
    last_close = closes[-1]
    ma_windows = cfg.get("ma_windows", [20, 60])

    mas: dict[str, float | None] = {}
    deviations: dict[str, float | None] = {}
    for w in ma_windows:
        raw = moving_average(closes, w)
        mas[f"ma{w}"] = round(raw, 4) if raw is not None else None
        deviations[f"dev_ma{w}_pct"] = (
            round((last_close - raw) / raw * 100, 2) if raw else None
        )

    vol_window = cfg.get("vol_window", 30)
    rets_all = log_returns(closes)
    vol_rets = rets_all[-vol_window:]
    annualized_vol_pct = (
        round(statistics.stdev(vol_rets) * (252 ** 0.5) * 100, 2) if len(vol_rets) >= 5 else None
    )

    zscore_window = cfg.get("zscore_window", 250)
    z_rets = rets_all[-zscore_window:]
    zscore = None
    if len(z_rets) >= 20:  # 樣本太少的 z-score 不穩定，至少要 20 筆日報酬才計算
        mu = statistics.fmean(z_rets)
        sigma = statistics.stdev(z_rets)
        if sigma > 0:
            zscore = round((z_rets[-1] - mu) / sigma, 2)

    threshold = cfg.get("zscore_alert_threshold", 2.0)
    anomaly = zscore is not None and abs(zscore) >= threshold

    return {
        "close": round(last_close, 4),
        "change_1d_pct": _round2(pct_change(closes, 1)),
        "change_1w_pct": _round2(pct_change(closes, 5)),
        "change_1m_pct": _round2(pct_change(closes, 21)),
        "change_1y_pct": _round2(pct_change(closes, 252)),
        **mas,
        **deviations,
        "volatility_30d_annualized_pct": annualized_vol_pct,
        "zscore_return": zscore,
        "anomaly": anomaly,
        "data_points": len(closes),
        "freq": "daily",
    }


def compute_metrics_monthly(closes: list[float], cfg: dict) -> dict:
    """月頻資料（目前是 FRED 的銅/鋁月均價數列）不可套用日頻指標公式 —— 30 日年化波動度、
    250 日報酬 z-score、日/週變動率的「back」都是用交易日數當作近似，樣本間隔其實是一個月
    的資料套進這些公式算出來的數字沒有統計意義，只會誤導判讀。只給月變動率（back=1，即
    月對月）與年變動率（back=12，即年對年），其餘欄位一律 null，並用 freq 標記讓下游
    （報告/dashboard/AI）知道要用不同方式呈現。"""
    last_close = closes[-1]
    return {
        "close": round(last_close, 4),
        "change_1d_pct": None,
        "change_1w_pct": None,
        "change_1m_pct": _round2(pct_change(closes, 1)),
        "change_1y_pct": _round2(pct_change(closes, 12)),
        "ma20": None,
        "ma60": None,
        "dev_ma20_pct": None,
        "dev_ma60_pct": None,
        "volatility_30d_annualized_pct": None,
        "zscore_return": None,
        "anomaly": False,
        "data_points": len(closes),
        "freq": "monthly",
    }


def compute_correlation_matrix(
    all_series: dict[str, list[tuple[str, float]]], window: int
) -> dict[str, dict[str, float | None]]:
    """兩兩品項用「共同交易日」的日對數報酬計算 Pearson 相關係數，取最近 window 筆共同日。
    不同交易所行事曆不同（例如台股 ETF 跟 COMEX 期貨休市日不一樣），直接對齊各自最近 N 筆
    會造成時間錯位，所以先取日期交集、再切最近 window 筆。共同交易日不足 10 筆就不計算，
    回傳 None，避免用太少樣本算出沒意義的假相關。"""
    returns_by_code: dict[str, dict[str, float]] = {}
    for code, series in all_series.items():
        dates = [d for d, _ in series]
        closes = [c for _, c in series]
        rets = log_returns(closes)
        ret_dates = dates[1:]
        returns_by_code[code] = dict(zip(ret_dates, rets))

    codes = sorted(all_series.keys())
    matrix: dict[str, dict[str, float | None]] = {c: {} for c in codes}
    for i, a in enumerate(codes):
        for b in codes[i:]:
            if a == b:
                matrix[a][b] = 1.0
                continue
            common_dates = sorted(set(returns_by_code[a]) & set(returns_by_code[b]))
            tail = common_dates[-window:]
            if len(tail) < 10:
                matrix[a][b] = None
                matrix[b][a] = None
                continue
            xa = [returns_by_code[a][d] for d in tail]
            xb = [returns_by_code[b][d] for d in tail]
            try:
                corr = round(statistics.correlation(xa, xb), 3)
            except statistics.StatisticsError:
                corr = None
            matrix[a][b] = corr
            matrix[b][a] = corr
    return matrix


def compute_forecast(closes: list[float], cfg: dict) -> dict:
    """三種 baseline：naive（今天收盤原樣外推）、移動平均、線性趨勢外推。
    線性趨勢：對最近 trend_window_days 筆收盤做最小平方法迴歸，外推 horizon 個交易日；
    區間用該迴歸窗口內「殘差」的樣本標準差（±1 個標準差），不是信賴區間，只是
    「歷史配適誤差的大小」，report 裡會明講這不是預測保證。"""
    trend_window = cfg.get("trend_window_days", 60)
    horizon = cfg.get("forecast_horizon_days", 30)
    last_close = closes[-1]

    ma_baseline = moving_average(closes, min(trend_window, len(closes)))

    window = closes[-trend_window:]
    trend_point = trend_low = trend_high = residual_std = None
    if len(window) >= 10:
        xs = list(range(len(window)))
        reg = statistics.linear_regression(xs, window)
        residuals = [window[i] - (reg.slope * i + reg.intercept) for i in xs]
        residual_std = statistics.stdev(residuals) if len(residuals) >= 2 else 0.0
        future_x = len(window) - 1 + horizon
        trend_point = reg.slope * future_x + reg.intercept
        trend_low = trend_point - residual_std
        trend_high = trend_point + residual_std

    return {
        "naive": round(last_close, 4),
        "ma_baseline": round(ma_baseline, 4) if ma_baseline is not None else None,
        "trend_window_days": len(window),
        "horizon_days": horizon,
        "trend_forecast": round(trend_point, 4) if trend_point is not None else None,
        "trend_low": round(trend_low, 4) if trend_low is not None else None,
        "trend_high": round(trend_high, 4) if trend_high is not None else None,
        "residual_std": round(residual_std, 4) if residual_std is not None else None,
    }


# ---------------------------------------------------------------------------
# Claude API 判讀（含降級路徑）
# ---------------------------------------------------------------------------

def build_ai_payload(metrics_by_code: dict, name_map: dict, correlation: dict,
                      forecast_by_code: dict, failed: list[str], source_by_code: dict,
                      cross_check_by_code: dict) -> dict:
    return {
        "date": datetime.now(TPE).strftime("%Y-%m-%d"),
        "metrics": {
            name_map.get(c, c): {**m, "data_source": source_by_code.get(c, "yahoo")}
            for c, m in metrics_by_code.items()
        },
        "correlation_60d": {
            name_map.get(a, a): {name_map.get(b, b): v for b, v in row.items()}
            for a, row in correlation.items()
        },
        "forecast": {name_map.get(c, c): f for c, f in forecast_by_code.items()},
        "failed_symbols": [name_map.get(c, c) for c in failed],
        "cross_source_check": {
            name_map.get(c, c): cc for c, cc in cross_check_by_code.items()
        },
    }


def get_ai_report(cfg: dict, payload: dict) -> tuple[str | None, bool]:
    """回傳 (ai_text, ai_used)。任何失敗都吞下並回傳 (None, False) — 絕不讓 API 問題整個 crash。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, False
    try:
        import anthropic

        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=cfg.get("model", "claude-opus-4-8"),
            max_tokens=4000,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
        text = "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        if not text:
            return None, False
        return text, True
    except Exception as e:  # noqa: BLE001 — 任何 SDK/網路錯誤都走降級路徑
        print(f"[warn] claude api failed: {e}", file=sys.stderr)
        return None, False


def rule_based_summary(metrics_by_code: dict, name_map: dict, threshold: float) -> str:
    lines = []
    movers = sorted(
        ((c, m) for c, m in metrics_by_code.items() if m.get("change_1d_pct") is not None),
        key=lambda kv: abs(kv[1]["change_1d_pct"]),
        reverse=True,
    )
    if movers:
        code, m = movers[0]
        lines.append(f"今日波動最大：{name_map.get(code, code)} {m['change_1d_pct']:+.2f}%。")
    anomalies = [c for c, m in metrics_by_code.items() if m.get("anomaly")]
    if anomalies:
        names = "、".join(name_map.get(c, c) for c in anomalies)
        lines.append(f"觸發異常波動門檻（|z-score|≥{threshold}）：{names}。")
    else:
        lines.append(f"今日無品項觸發異常波動門檻（|z-score|≥{threshold}）。")
    return "\n".join(lines) if lines else "今日無足夠資料可摘要。"


# ---------------------------------------------------------------------------
# 日報組裝
# ---------------------------------------------------------------------------

def _fmt_pct(v: float | None) -> str:
    return f"{v:+.2f}%" if v is not None else "N/A"


def _format_corr_table(correlation: dict, name_map: dict) -> str:
    codes = sorted(correlation.keys())
    short = {c: (name_map.get(c, c)[:4] or c) for c in codes}
    header = "        " + " ".join(f"{short[c]:>6}" for c in codes)
    rows = [header]
    for a in codes:
        cells = []
        for b in codes:
            v = correlation[a][b]
            cells.append(f"{v:>6.2f}" if v is not None else "   N/A")
        rows.append(f"{short[a]:<8}" + " ".join(cells))
    return "\n".join(rows)


_SOURCE_TAG = {"yahoo": "", "fred": "［FRED］", "history": "［沿用歷史］"}


def build_report_blocks(metrics_by_code: dict, name_map: dict, cfg: dict, correlation: dict,
                         forecast_by_code: dict, failed: list[str],
                         ai_text: str | None, ai_used: bool, source_by_code: dict,
                         last_updated_by_code: dict, cross_check_by_code: dict) -> list[str]:
    today = datetime.now(TPE).strftime("%Y-%m-%d (%a)")
    blocks = [f"📦 <b>全球原物料價格監控｜每日報告</b> {today}"]

    codes = sorted(metrics_by_code.keys())
    if codes:
        lines = ["<b>▍今日行情與變動率</b>"]
        for code in codes:
            m = metrics_by_code[code]
            name = name_map.get(code, code)
            source = source_by_code.get(code, "yahoo")
            src_tag = _SOURCE_TAG.get(source, "")
            freq = m.get("freq", "daily")
            stale_note = (
                f"　（今日未更新，最後更新 {last_updated_by_code.get(code, '?')}）"
                if source == "history" else ""
            )

            if freq == "monthly":
                lines.append(
                    f"・{name}{src_tag}［月頻］ {m['close']}　月{_fmt_pct(m['change_1m_pct'])} "
                    f"年{_fmt_pct(m['change_1y_pct'])}{stale_note}"
                )
                continue

            flag = " ⚠️異常波動" if m.get("anomaly") else ""
            lines.append(
                f"・{name}{src_tag} {m['close']}　日{_fmt_pct(m['change_1d_pct'])} "
                f"週{_fmt_pct(m['change_1w_pct'])} 月{_fmt_pct(m['change_1m_pct'])} "
                f"年{_fmt_pct(m['change_1y_pct'])}{flag}{stale_note}"
            )
            extra = []
            if m.get("dev_ma20_pct") is not None:
                extra.append(f"乖離MA20 {m['dev_ma20_pct']:+.1f}%")
            if m.get("volatility_30d_annualized_pct") is not None:
                extra.append(f"30日年化波動 {m['volatility_30d_annualized_pct']:.1f}%")
            if m.get("zscore_return") is not None:
                extra.append(f"報酬z-score {m['zscore_return']:+.2f}")
            if extra:
                lines.append("　" + "、".join(extra))
        blocks.append("\n".join(lines))

    if failed:
        names = "、".join(name_map.get(c, c) for c in failed)
        blocks.append(f"<b>▍資料取得失敗</b>\n・{names}：本輪略過，其餘品項照常分析")

    if cross_check_by_code:
        threshold = cfg.get("cross_check_threshold_pct", 2.0)
        lines = ["<b>▍資料源交叉驗證（Yahoo vs FRED）</b>"]
        for code, cc in cross_check_by_code.items():
            name = name_map.get(code, code)
            note = " ⚠️差異偏大" if abs(cc["diff_pct"]) > threshold else ""
            lines.append(
                f"・{name}（{cc['date']}）：Yahoo {cc['yahoo_close']} vs FRED {cc['fred_close']}"
                f"　差異 {cc['diff_pct']:+.2f}%{note}"
            )
        lines.append("（Yahoo 為期貨、FRED 多為現貨，本有價差屬預期現象；此比對用途是偵測資料異常，非套利訊號）")
        blocks.append("\n".join(lines))

    if forecast_by_code:
        horizon = cfg.get("forecast_horizon_days", 30)
        trend_w = cfg.get("trend_window_days", 60)
        lines = [f"<b>▍成本預測 baseline（外推 {horizon} 交易日）</b>"]
        for code in codes:
            f = forecast_by_code.get(code)
            if not f or f.get("trend_forecast") is None:
                continue
            name = name_map.get(code, code)
            lines.append(
                f"・{name}：naive {f['naive']}／MA{trend_w} {f['ma_baseline']}／"
                f"趨勢外推 {f['trend_forecast']}（{f['trend_low']}~{f['trend_high']}）"
            )
        lines.append(DISCLAIMER_FORECAST)
        blocks.append("\n".join(lines))

    if correlation:
        lines = [
            f"<b>▍{cfg.get('corr_window', 60)}日相關係數矩陣</b>",
            "<pre>" + html.escape(_format_corr_table(correlation, name_map)) + "</pre>",
        ]
        blocks.append("\n".join(lines))

    if ai_used and ai_text:
        blocks.append("<b>▍AI 判讀</b>\n" + html.escape(ai_text))
    else:
        threshold = cfg.get("zscore_alert_threshold", 2.0)
        fallback = rule_based_summary(metrics_by_code, name_map, threshold)
        blocks.append(
            "<b>▍AI 判讀</b>\nAI 判讀未啟用（缺 ANTHROPIC_API_KEY 或呼叫失敗），以下為規則版摘要：\n"
            + html.escape(fallback)
        )

    blocks.append(DISCLAIMER)
    return blocks


def chunk_blocks(blocks: list[str], limit: int) -> list[str]:
    """比照 stock_advisor.py 的切段邏輯：單則訊息超過 Telegram 上限就拆多則。"""
    messages, current = [], ""
    for block in blocks:
        if current and len(current) + len(block) + 1 > limit - 100:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        messages.append(current)
    return messages


def send_telegram(token: str, chat_id: str, text: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Dashboard data.json
# ---------------------------------------------------------------------------

def build_dashboard_data(all_series: dict, metrics_by_code: dict, name_map: dict, category_map: dict,
                          correlation: dict, forecast_by_code: dict, failed: list[str], cfg: dict,
                          source_by_code: dict, last_updated_by_code: dict,
                          cross_check_by_code: dict, ai_report: dict | None = None) -> dict:
    # dashboard 的 sparkline 只需近一年走勢；完整歷史（原油/天然氣可達上萬筆）留在
    # data/commodity_history.json，不塞進 data.json，避免 Actions 每日 commit 灌爆 git 歷史。
    spark_points = cfg.get("dashboard_history_points", 260)
    symbols = []
    for code, series in all_series.items():
        symbols.append({
            "code": code,
            "name": name_map.get(code, code),
            "category": category_map.get(code, ""),
            "history": [{"date": d, "close": c} for d, c in series[-spark_points:]],
            "metrics": metrics_by_code.get(code),
            "forecast": forecast_by_code.get(code),
            "source": source_by_code.get(code, "yahoo"),
            "last_updated": last_updated_by_code.get(code),
        })
    return {
        "generated_at": datetime.now(TPE).isoformat(),
        "config": {
            "corr_window": cfg.get("corr_window", 60),
            "vol_window": cfg.get("vol_window", 30),
            "zscore_window": cfg.get("zscore_window", 250),
            "zscore_alert_threshold": cfg.get("zscore_alert_threshold", 2.0),
            "forecast_horizon_days": cfg.get("forecast_horizon_days", 30),
            "trend_window_days": cfg.get("trend_window_days", 60),
            "cross_check_threshold_pct": cfg.get("cross_check_threshold_pct", 2.0),
        },
        "symbols": symbols,
        "correlation": correlation,
        "failed_symbols": failed,
        "cross_check": cross_check_by_code,
        "ai_report": ai_report,
    }


def save_dashboard_data(data: dict) -> None:
    DASHBOARD_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_DATA_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只印出訊息，不推播、不寫入歷史資料")
    parser.add_argument(
        "--write-dashboard", action="store_true",
        help="輸出 docs/commodity/data.json；dry-run 也可搭配使用，方便本機預覽 dashboard 而不推播/不寫歷史",
    )
    parser.add_argument(
        "--source", choices=["auto", "yahoo", "fred"], default="auto",
        help="測試/除錯用：強制指定資料源。auto＝預設（Yahoo 為主、失敗才 fallback FRED）；"
             "yahoo＝只試 Yahoo 不 fallback；fred＝完全跳過 Yahoo、只走 FRED（用來驗證 FRED 路徑，"
             "避免對被限流中的 Yahoo 端點增加請求）",
    )
    args = parser.parse_args()

    cfg = load_config()
    symbols_cfg = cfg.get("symbols", [])
    name_map = {s["code"]: s.get("name", s["code"]) for s in symbols_cfg}
    category_map = {s["code"]: s.get("category", "") for s in symbols_cfg}

    history = load_history()
    all_series: dict[str, list[tuple[str, float]]] = {}
    source_by_code: dict[str, str] = {}
    freq_by_code: dict[str, str] = {}
    last_updated_by_code: dict[str, str] = {}
    cross_check_by_code: dict[str, dict] = {}
    failed: list[str] = []

    for i, s in enumerate(symbols_cfg):
        code = s["code"]
        try:
            series, source_used, freq_used = fetch_symbol(history, s, cfg, args.source)
            all_series[code] = series
            source_by_code[code] = source_used
            freq_by_code[code] = freq_used
            print(
                f"[info] {code} 抓取成功（來源：{source_used}／{freq_used}），共 {len(series)} 筆歷史"
                f"（最新 {series[-1][0]} 收盤 {series[-1][1]}）"
            )
        except Exception as e:
            print(f"[warn] fetch '{code}' failed on all sources: {e}", file=sys.stderr)
            # 絕不用空殼覆蓋好資料：全部來源都失敗時，若舊歷史資料還在，照常用它分析，
            # 只是標記「今日未更新」，而不是把這個品項整個丟掉。
            fallback_series = None
            fallback_freq = "daily"
            if history.get(code):
                fallback_series = sorted(history[code].items())
                fallback_freq = "daily"
            elif history.get(f"{code}::fred"):
                fallback_series = sorted(history[f"{code}::fred"].items())
                fallback_freq = s.get("fred_freq") or "daily"
            if fallback_series:
                all_series[code] = fallback_series
                source_by_code[code] = "history"
                freq_by_code[code] = fallback_freq
                last_updated_by_code[code] = fallback_series[-1][0]
                print(f"[warn] {code} 改用既有歷史資料（最後更新 {fallback_series[-1][0]}），今日未更新")
            else:
                failed.append(code)

        # 交叉驗證：這輪 Yahoo 成功、且該品項有 FRED 日頻對照時，額外拉 FRED 做比對
        # （FRED 沒有限流疑慮，不受 5 次 Yahoo 請求上限限制）。任何失敗都不影響主資料。
        if source_by_code.get(code) == "yahoo" and s.get("fred_freq") == "daily" and s.get("fred"):
            try:
                fred_series = update_history_fred(history, code, s["fred"], cfg)
                cc = cross_check_sources(all_series[code], fred_series)
                if cc:
                    cross_check_by_code[code] = cc
            except Exception as e:
                print(f"[warn] {code} 交叉驗證抓取 FRED 失敗（不影響主資料）：{e}", file=sys.stderr)

        if i < len(symbols_cfg) - 1:
            time.sleep(2.5)  # 避免連續打 Yahoo 觸發暫時性限流（429）

    if not all_series:
        print(
            "[error] 全部品項的資料源（Yahoo／FRED／既有歷史）都無法取得資料，"
            "拒絕寫入 dashboard data.json（不覆蓋任何既有好資料），本輪中止。",
            file=sys.stderr,
        )
        sys.exit(1)

    metrics_by_code = {}
    forecast_by_code = {}
    for code, series in all_series.items():
        closes = [c for _, c in series]
        freq = freq_by_code.get(code, "daily")
        if freq == "monthly":
            metrics_by_code[code] = compute_metrics_monthly(closes, cfg)
            # 月頻資料的 trend_window_days/horizon_days 是「交易日」單位，套在月資料上
            # 語意不通（會變成拿 N 個月當 N 個交易日回歸），寧可不出這個 baseline。
        else:
            metrics_by_code[code] = compute_metrics(closes, cfg)
            forecast_by_code[code] = compute_forecast(closes, cfg)

    daily_series = {c: s for c, s in all_series.items() if freq_by_code.get(c, "daily") != "monthly"}
    correlation = (
        compute_correlation_matrix(daily_series, cfg.get("corr_window", 60)) if len(daily_series) >= 2 else {}
    )

    payload = build_ai_payload(
        metrics_by_code, name_map, correlation, forecast_by_code, failed, source_by_code, cross_check_by_code
    )
    ai_text, ai_used = get_ai_report(cfg, payload)

    blocks = build_report_blocks(
        metrics_by_code, name_map, cfg, correlation, forecast_by_code, failed, ai_text, ai_used,
        source_by_code, last_updated_by_code, cross_check_by_code,
    )
    messages = chunk_blocks(blocks, cfg.get("telegram_msg_limit", TELEGRAM_MSG_LIMIT))

    should_write_dashboard = args.write_dashboard or not args.dry_run
    if should_write_dashboard:
        # dashboard 也顯示判讀：有 API key 走 AI 版，否則放規則版摘要（與 Telegram 報告一致）
        if ai_used and ai_text:
            ai_report = {"text": ai_text, "source": "ai"}
        else:
            threshold = cfg.get("zscore_alert_threshold", 2.0)
            ai_report = {"text": rule_based_summary(metrics_by_code, name_map, threshold), "source": "rule"}
        dash = build_dashboard_data(
            all_series, metrics_by_code, name_map, category_map, correlation, forecast_by_code, failed, cfg,
            source_by_code, last_updated_by_code, cross_check_by_code, ai_report,
        )
        save_dashboard_data(dash)
        print(f"[info] dashboard data 已寫入 {DASHBOARD_DATA_PATH}")

    if args.dry_run:
        for msg in messages:
            print("=" * 40)
            print(msg)
        print("=" * 40)
        source_counts = {}
        for c in all_series:
            source_counts[source_by_code.get(c, "yahoo")] = source_counts.get(source_by_code.get(c, "yahoo"), 0) + 1
        print(
            f"[dry-run] 品項 {len(symbols_cfg)} 檔（成功 {len(all_series)}、失敗 {len(failed)}，"
            f"來源分布 {source_counts}）、AI 判讀 {'啟用' if ai_used else '未啟用'}、"
            f"訊息 {len(messages)} 則，未推播、未寫入 commodity_history.json"
        )
        return

    token = os.environ.get("COMMODITY_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("COMMODITY_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        sys.exit("缺少環境變數 COMMODITY_TELEGRAM_BOT_TOKEN 或 COMMODITY_TELEGRAM_CHAT_ID")

    for msg in messages:
        send_telegram(token, chat_id, msg)
        time.sleep(1)

    save_history(history)
    print(f"sent {len(messages)} message(s), {len(all_series)} symbol(s) ok, {len(failed)} failed")


if __name__ == "__main__":
    main()
