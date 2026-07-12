#!/usr/bin/env python3
"""台股 AI 投資顧問：盤前/收盤後日報，含技術指標、賣出警示、新聞摘要、AI 判讀。

用法：
    python src/stock_advisor.py --mode pre --dry-run    # 盤前日報，只印出訊息
    python src/stock_advisor.py --mode close --dry-run  # 收盤後日報，只印出訊息
    python src/stock_advisor.py --mode pre               # 正式執行（需要環境變數
                                                           # TELEGRAM_BOT_TOKEN、
                                                           # TELEGRAM_CHAT_ID；
                                                           # ANTHROPIC_API_KEY 可選，
                                                           # 沒有就走規則版降級）
"""

import argparse
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "stock_config.json"
STATE_PATH = ROOT / "data" / "stock_state.json"

# 104 抓取那支程式已驗證過：Cloudflare / 一般反爬對「短 UA」會擋，這裡沿用同一個完整瀏覽器 UA。
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
RSS_HEADERS = {"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml"}

TPE = ZoneInfo("Asia/Taipei")
TELEGRAM_MSG_LIMIT = 4096
DISCLAIMER = "⚠️ 本報告為技術指標與 AI 判讀，非投資建議，決策請自行判斷"

SYSTEM_PROMPT = """你是台股投資顧問助理，會收到觀察清單與持股的技術指標 JSON、訊號旗標、
今日財經新聞標題列表，以及目前是盤前或收盤後（mode）。請用繁體中文輸出結構化短報告：

1. 每檔觀察清單股票：1-2 行技術面判讀，結尾標明傾向「偏多／中性／偏空」。
2. 每檔持股：給「續抱／減碼／出場」傾向與 1 行理由（若該股已有賣出警示，優先討論警示是否成立）。
3. 從新聞列表挑 3-5 則與台股/總體經濟最相關的，各寫一句話摘要。
4. 最後固定輸出一行：「⚠️ 本報告為技術指標與 AI 判讀，非投資建議，決策請自行判斷」

不要輸出多餘的開場白或收尾客套話，直接進入結構化內容。"""


# ---------------------------------------------------------------------------
# 設定 / 狀態
# ---------------------------------------------------------------------------

def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def build_name_map(cfg: dict) -> dict:
    return {w["code"]: w.get("name", w["code"]) for w in cfg.get("watchlist", [])}


def default_state(cfg: dict) -> dict:
    return {
        "holdings": {},
        "watchlist": [w["code"] for w in cfg.get("watchlist", [])],
        "telegram_offset": 0,
    }


def load_state(cfg: dict) -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state.setdefault("holdings", {})
        state.setdefault("watchlist", [w["code"] for w in cfg.get("watchlist", [])])
        state.setdefault("telegram_offset", 0)
        return state
    return default_state(cfg)


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Telegram 指令解析（純函式，方便單獨測試）
# ---------------------------------------------------------------------------

_CMD_BUY = re.compile(r"^買\s+(\S+)\s+(\S+)\s+(\S+)$")
_CMD_SELL = re.compile(r"^賣\s+(\S+)$")
_CMD_WATCH_ADD = re.compile(r"^觀察\s+(\S+)$")
_CMD_WATCH_DEL = re.compile(r"^移除\s+(\S+)$")


def parse_command(text: str) -> dict | None:
    """解析單則 Telegram 文字指令。無法辨識回傳 None（忽略）。"""
    text = (text or "").strip()

    m = _CMD_BUY.match(text)
    if m:
        code, price_s, shares_s = m.groups()
        try:
            price = float(price_s)
            shares = int(float(shares_s))
        except ValueError:
            return None
        return {"action": "buy", "code": code, "price": price, "shares": shares}

    m = _CMD_SELL.match(text)
    if m:
        return {"action": "sell", "code": m.group(1)}

    m = _CMD_WATCH_ADD.match(text)
    if m:
        return {"action": "watch_add", "code": m.group(1)}

    m = _CMD_WATCH_DEL.match(text)
    if m:
        return {"action": "watch_del", "code": m.group(1)}

    return None


def apply_command(cmd: dict, state: dict) -> str:
    """套用已解析的指令到 state（原地修改），回傳給使用者的一行確認訊息。"""
    code = cmd["code"].strip()
    action = cmd["action"]

    if action == "buy":
        state["holdings"][code] = {
            "avg_price": cmd["price"],
            "shares": cmd["shares"],
            "buy_date": datetime.now(TPE).strftime("%Y-%m-%d"),
            "peak_close": cmd["price"],
        }
        return f"✅ 已登記買入 {code}（均價 {cmd['price']}，{cmd['shares']} 股）"

    if action == "sell":
        if code in state["holdings"]:
            del state["holdings"][code]
            return f"✅ 已平倉 {code}"
        return f"⚠️ 找不到持股 {code}，略過賣出指令"

    if action == "watch_add":
        if code not in state["watchlist"]:
            state["watchlist"].append(code)
        return f"✅ 已加入觀察 {code}"

    if action == "watch_del":
        if code in state["watchlist"]:
            state["watchlist"].remove(code)
            return f"✅ 已移除觀察 {code}"
        return f"⚠️ 觀察清單沒有 {code}，略過"

    return ""


def fetch_telegram_commands(token: str, chat_id: str, offset: int) -> tuple[list[dict], int]:
    resp = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"offset": offset, "timeout": 0},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    commands = []
    next_offset = offset
    for upd in data.get("result", []):
        next_offset = max(next_offset, upd["update_id"] + 1)
        msg = upd.get("message") or upd.get("channel_post") or {}
        if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
            continue
        cmd = parse_command(msg.get("text", ""))
        if cmd:
            commands.append(cmd)
    return commands, next_offset


# ---------------------------------------------------------------------------
# 行情與技術指標
# ---------------------------------------------------------------------------

def fetch_history(code: str, days: int) -> pd.DataFrame:
    ticker = yf.Ticker(f"{code}.TW")
    hist = ticker.history(period=f"{max(days, 60) + 20}d", interval="1d")
    if hist.empty:
        raise RuntimeError(f"{code} 無歷史資料")
    # yfinance 有時會回傳「今天」尚未完整成交的 bar（Volume=0，Close 沿用前一筆），
    # 只砍尾端這種不完整資料，不動中間可能存在的合法零成交日。
    while len(hist) and hist["Volume"].iloc[-1] == 0:
        hist = hist.iloc[:-1]
    if len(hist) < 20:
        raise RuntimeError(f"{code} 有效資料筆數不足（{len(hist)} 筆）")
    return hist


def compute_indicators(hist: pd.DataFrame) -> dict:
    close, high, low, volume = hist["Close"], hist["High"], hist["Low"], hist["Volume"]

    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi14 = (100 - (100 / (1 + rs))).fillna(50)

    low9 = low.rolling(9).min()
    high9 = high.rolling(9).max()
    rsv = ((close - low9) / (high9 - low9).replace(0, float("nan")) * 100).fillna(50)
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()  # KD(9,3,3) 近似：2/3,1/3 遞迴等同 alpha=1/3 的 ewm
    d = k.ewm(alpha=1 / 3, adjust=False).mean()

    vol_ma20 = volume.rolling(20).mean()
    high20 = high.rolling(20).max()
    low20 = low.rolling(20).min()

    last_close = float(close.iloc[-1])
    ma5_l, ma20_l, ma60_l = ma5.iloc[-1], ma20.iloc[-1], ma60.iloc[-1]
    ma5_p, ma20_p = ma5.iloc[-2], ma20.iloc[-2]

    signals = []
    if pd.notna(ma5_p) and pd.notna(ma20_p) and pd.notna(ma5_l) and pd.notna(ma20_l):
        if ma5_p <= ma20_p and ma5_l > ma20_l:
            signals.append("MA5黃金交叉MA20")
        elif ma5_p >= ma20_p and ma5_l < ma20_l:
            signals.append("MA5死亡交叉MA20")

    rsi_last = float(rsi14.iloc[-1])
    if rsi_last > 70:
        signals.append(f"RSI過熱({rsi_last:.1f}>70)")
    elif rsi_last < 30:
        signals.append(f"RSI超賣({rsi_last:.1f}<30)")

    if pd.notna(ma20_l):
        signals.append("站上MA20" if last_close > ma20_l else "跌破MA20")
    if pd.notna(ma60_l) and last_close < ma60_l:
        signals.append("跌破MA60")

    vol_ma20_l = vol_ma20.iloc[-1]
    high20_l, low20_l = high20.iloc[-1], low20.iloc[-1]

    return {
        "close": round(last_close, 2),
        "ma5": round(float(ma5_l), 2) if pd.notna(ma5_l) else None,
        "ma20": round(float(ma20_l), 2) if pd.notna(ma20_l) else None,
        "ma60": round(float(ma60_l), 2) if pd.notna(ma60_l) else None,
        "rsi14": round(rsi_last, 1),
        "k": round(float(k.iloc[-1]), 1),
        "d": round(float(d.iloc[-1]), 1),
        "volume": int(volume.iloc[-1]),
        "vol_ratio": (
            round(float(volume.iloc[-1] / vol_ma20_l), 2) if pd.notna(vol_ma20_l) and vol_ma20_l else None
        ),
        "dist_high20_pct": (
            round(float((last_close - high20_l) / high20_l * 100), 2) if pd.notna(high20_l) else None
        ),
        "dist_low20_pct": (
            round(float((last_close - low20_l) / low20_l * 100), 2) if pd.notna(low20_l) else None
        ),
        "signals": signals,
    }


def analyze_ticker(code: str, cfg: dict) -> dict:
    hist = fetch_history(code, cfg.get("history_days", 180))
    return compute_indicators(hist)


def build_sell_alerts(holdings: dict, market: dict, cfg: dict) -> dict[str, list[str]]:
    stop_loss_pct = cfg.get("stop_loss_pct", -8)
    trailing_stop_pct = cfg.get("trailing_stop_pct", 10)
    alerts: dict[str, list[str]] = {}

    for code, h in holdings.items():
        ind = market.get(code)
        if not ind:
            continue
        last_close = ind["close"]
        peak = max(h.get("peak_close", h["avg_price"]), last_close)
        h["peak_close"] = peak  # 更新峰值；是否落盤看呼叫端是否 save_state

        return_pct = (last_close - h["avg_price"]) / h["avg_price"] * 100
        drawdown_pct = (last_close - peak) / peak * 100

        reasons = []
        if return_pct <= stop_loss_pct:
            reasons.append(f"停損：報酬率 {return_pct:.1f}% ≤ {stop_loss_pct}%")
        if drawdown_pct <= -trailing_stop_pct:
            reasons.append(f"移動停利：自高點 {peak:.2f} 回落 {drawdown_pct:.1f}% ≥ {trailing_stop_pct}%")
        if "MA5死亡交叉MA20" in ind["signals"]:
            reasons.append("MA5死亡交叉MA20")
        if "跌破MA60" in ind["signals"]:
            reasons.append("跌破MA60")
        if reasons:
            alerts[code] = reasons
    return alerts


# ---------------------------------------------------------------------------
# 新聞
# ---------------------------------------------------------------------------

def fetch_news(cfg: dict) -> list[dict] | None:
    """回傳新聞列表；來源全掛回傳 None（呼叫端據此顯示「新聞源異常」）。"""
    lookback_hours = cfg.get("news_lookback_hours", 24)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    items: list[dict] = []
    any_ok = False
    for url in cfg.get("rss_feeds", []):
        try:
            resp = requests.get(url, headers=RSS_HEADERS, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            any_ok = True
            for it in root.findall(".//item"):
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                pub_raw = it.findtext("pubDate")
                pub_dt = None
                if pub_raw:
                    try:
                        pub_dt = parsedate_to_datetime(pub_raw)
                        if pub_dt.tzinfo is None:
                            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    except (TypeError, ValueError):
                        pub_dt = None
                if pub_dt and pub_dt < cutoff:
                    continue
                if title:
                    items.append({
                        "title": title,
                        "link": link,
                        "pub": pub_dt.isoformat() if pub_dt else "",
                    })
        except Exception as e:  # 單一來源失敗不拖垮整份日報
            print(f"[warn] rss '{url}' failed: {e}", file=sys.stderr)
            continue

    if not any_ok:
        return None
    items.sort(key=lambda x: x["pub"], reverse=True)
    return items[: cfg.get("news_fetch_max", 20)]


# ---------------------------------------------------------------------------
# Claude API 判讀（含降級路徑）
# ---------------------------------------------------------------------------

def get_ai_report(cfg: dict, mode: str, market: dict, holdings: dict,
                   sell_alerts: dict, news: list[dict] | None) -> tuple[str | None, bool]:
    """回傳 (ai_text, ai_used)。任何失敗都吞下並回傳 (None, False) — 絕不讓 API 問題整個 crash。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, False
    try:
        import anthropic

        payload = {
            "mode": mode,
            "watchlist_indicators": market,
            "holdings": holdings,
            "sell_alerts": sell_alerts,
            "news": [{"title": n["title"], "link": n["link"]} for n in (news or [])][
                : cfg.get("news_pick_for_ai", 15)
            ],
        }
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


# ---------------------------------------------------------------------------
# 日報組裝
# ---------------------------------------------------------------------------

def build_report_blocks(mode: str, command_lines: list[str], market: dict, failed_codes: list[str],
                         holdings: dict, sell_alerts: dict, news: list[dict] | None,
                         ai_text: str | None, ai_used: bool, name_map: dict) -> list[str]:
    today = datetime.now(TPE).strftime("%Y-%m-%d (%a)")
    mode_label = "盤前" if mode == "pre" else "收盤後"
    close_label = "昨收" if mode == "pre" else "今日收盤"
    blocks = [f"📈 <b>台股投資顧問｜{mode_label}日報</b> {today}"]

    if command_lines:
        lines = ["<b>▍指令回報</b>"] + [f"・{html.escape(c)}" for c in command_lines]
        blocks.append("\n".join(lines))

    if holdings:
        lines = [f"<b>▍持股（{close_label}）</b>"]
        for code, h in sorted(holdings.items()):
            name = name_map.get(code, code)
            ind = market.get(code)
            if not ind:
                lines.append(f"・{name}（{code}）：行情資料取得失敗")
                continue
            ret = (ind["close"] - h["avg_price"]) / h["avg_price"] * 100
            lines.append(
                f"・{name}（{code}）{ind['close']}，均價 {h['avg_price']}，"
                f"報酬 {ret:+.1f}%，{h['shares']} 股"
            )
            if ind["signals"]:
                lines.append(f"　訊號：{'、'.join(ind['signals'])}")
        blocks.append("\n".join(lines))

    if sell_alerts:
        lines = ["<b>▍⚠️ 賣出警示</b>"]
        for code, reasons in sell_alerts.items():
            name = name_map.get(code, code)
            lines.append(f"・{name}（{code}）：{'；'.join(reasons)}")
        blocks.append("\n".join(lines))

    watch_codes = sorted(c for c in market if c not in holdings)
    watch_failed = sorted(c for c in failed_codes if c not in holdings)
    if watch_codes or watch_failed:
        lines = [f"<b>▍觀察清單（{close_label}）</b>"]
        for code in watch_codes:
            name = name_map.get(code, code)
            ind = market[code]
            lines.append(f"・{name}（{code}）{ind['close']}，MA20 {ind['ma20']}，RSI {ind['rsi14']}")
            if ind["signals"]:
                lines.append(f"　訊號：{'、'.join(ind['signals'])}")
        for code in watch_failed:
            lines.append(f"・{name_map.get(code, code)}（{code}）：行情資料取得失敗")
        blocks.append("\n".join(lines))

    if news is None:
        blocks.append("<b>▍今日新聞</b>\n今日新聞源異常，暫無法取得新聞摘要。")
    elif news:
        lines = ["<b>▍今日新聞</b>"]
        for n in news[:20]:
            lines.append(f"・<a href=\"{html.escape(n['link'], quote=True)}\">{html.escape(n['title'])}</a>")
        blocks.append("\n".join(lines))
    else:
        blocks.append("<b>▍今日新聞</b>\n近 24 小時無相關新聞。")

    if ai_used and ai_text:
        blocks.append("<b>▍AI 判讀</b>\n" + html.escape(ai_text))
    else:
        blocks.append("<b>▍AI 判讀</b>\nAI 判讀未啟用（缺 ANTHROPIC_API_KEY 或呼叫失敗），以上為規則版指標與訊號。")

    blocks.append(DISCLAIMER)
    return blocks


def chunk_blocks(blocks: list[str], limit: int) -> list[str]:
    """比照 daily_jobs.py 的切段邏輯：單則訊息超過 Telegram 上限就拆多則。"""
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
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["pre", "close"], required=True, help="pre=盤前, close=收盤後")
    parser.add_argument("--dry-run", action="store_true", help="只印出訊息，不推播、不處理 Telegram 指令、不寫入狀態")
    args = parser.parse_args()

    cfg = load_config()
    state = load_state(cfg)
    name_map = build_name_map(cfg)

    command_lines: list[str] = []
    if not args.dry_run:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if token and chat_id:
            try:
                commands, new_offset = fetch_telegram_commands(token, chat_id, state.get("telegram_offset", 0))
                state["telegram_offset"] = new_offset
                for cmd in commands:
                    command_lines.append(apply_command(cmd, state))
            except Exception as e:  # 指令處理失敗不影響本次日報產出
                print(f"[warn] telegram getUpdates failed: {e}", file=sys.stderr)

    tickers = sorted(set(state["watchlist"]) | set(state["holdings"].keys()))
    market: dict[str, dict] = {}
    failed_codes: list[str] = []
    for code in tickers:
        try:
            market[code] = analyze_ticker(code, cfg)
        except Exception as e:
            print(f"[warn] analyze '{code}' failed: {e}", file=sys.stderr)
            failed_codes.append(code)

    sell_alerts = build_sell_alerts(state["holdings"], market, cfg)
    news = fetch_news(cfg)
    ai_text, ai_used = get_ai_report(cfg, args.mode, market, state["holdings"], sell_alerts, news)

    blocks = build_report_blocks(
        args.mode, command_lines, market, failed_codes, state["holdings"],
        sell_alerts, news, ai_text, ai_used, name_map,
    )
    messages = chunk_blocks(blocks, cfg.get("telegram_msg_limit", TELEGRAM_MSG_LIMIT))

    if args.dry_run:
        for msg in messages:
            print("=" * 40)
            print(msg)
        print("=" * 40)
        print(
            f"[dry-run] 觀察/持股 {len(tickers)} 檔（失敗 {len(failed_codes)}）、"
            f"賣出警示 {len(sell_alerts)} 檔、AI 判讀 {'啟用' if ai_used else '未啟用'}、"
            f"訊息 {len(messages)} 則，未推播、未寫入 stock_state.json"
        )
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        sys.exit("缺少環境變數 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")

    for msg in messages:
        send_telegram(token, chat_id, msg)
        time.sleep(1)

    save_state(state)
    print(f"sent {len(messages)} message(s), {len(tickers)} ticker(s), {len(sell_alerts)} sell alert(s)")


if __name__ == "__main__":
    main()
