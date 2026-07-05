#!/usr/bin/env python3
"""每日職缺推播：抓 104 公開搜尋 API，去重後把新職缺推到 Telegram。

用法：
    python src/daily_jobs.py --dry-run   # 只印出訊息，不推播、不更新去重紀錄
    python src/daily_jobs.py             # 正式執行（需要環境變數 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID）
"""

import argparse
import html
import json
import sys
import time
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
SEEN_PATH = ROOT / "data" / "seen_jobs.json"

API_URL = "https://www.104.com.tw/jobs/search/api/jobs"
# 104 前面有 Cloudflare，UA 太短（如 "Mozilla/5.0"）會被 403，必須用完整 UA
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.104.com.tw/jobs/search/",
    "Accept": "application/json",
}
TPE = ZoneInfo("Asia/Taipei")
TELEGRAM_MSG_LIMIT = 4096


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_seen() -> dict:
    if SEEN_PATH.exists():
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    return {}


def save_seen(seen: dict, retention_days: int) -> None:
    cutoff = (datetime.now(TPE) - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    kept = {no: d for no, d in seen.items() if d >= cutoff}
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(
        json.dumps(kept, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8"
    )


def fetch_search(search: dict) -> list[dict]:
    jobs = []
    for page in range(1, search.get("pages", 1) + 1):
        params = {
            "ro": 0,
            "keyword": search["keyword"],
            "order": 16,  # 依更新日期新→舊
            "asc": 0,
            "page": page,
            "mode": "s",
            "jobsource": "index_s",
            "pagesize": 20,
        }
        if search.get("area"):
            params["area"] = ",".join(search["area"])
        resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            break
        for j in data:
            jobs.append(
                {
                    "no": str(j["jobNo"]),
                    "name": j.get("jobName", ""),
                    "cust": j.get("custName", ""),
                    "area": j.get("jobAddrNoDesc", ""),
                    "date": j.get("appearDate", ""),
                    "url": j.get("link", {}).get("job", ""),
                    "desc": j.get("description", ""),
                }
            )
        time.sleep(1.5)  # 對站方友善，也避免被 Cloudflare 限流
    return jobs


def matches(job: dict, search: dict) -> bool:
    text = job["name"] + " " + job["desc"]
    if any(kw in job["name"] for kw in search.get("exclude", [])):
        return False
    if not all(kw in text for kw in search.get("must_all", [])):
        return False
    must_any = search.get("must_any", [])
    if must_any and not any(kw in text for kw in must_any):
        return False
    return True


def collect_new_jobs(cfg: dict, seen: dict) -> dict[str, list[dict]]:
    new_by_search: dict[str, list[dict]] = {}
    today = datetime.now(TPE).strftime("%Y-%m-%d")
    for search in cfg["searches"]:
        try:
            jobs = fetch_search(search)
        except Exception as e:  # 單一搜尋失敗不拖垮整次推播
            print(f"[warn] search '{search['name']}' failed: {e}", file=sys.stderr)
            continue
        fresh = []
        for job in jobs:
            if job["no"] in seen or not matches(job, search):
                continue
            seen[job["no"]] = today
            fresh.append(job)
        if fresh:
            fresh.sort(key=lambda x: x["date"], reverse=True)
            new_by_search[search["name"]] = fresh
    return new_by_search


def build_messages(new_by_search: dict, max_per: int) -> list[str]:
    today = datetime.now(TPE).strftime("%Y-%m-%d (%a)")
    if not new_by_search:
        return [f"📋 <b>{today}</b>\n今天沒有新職缺，明天見。"]
    blocks = [f"📋 <b>今日新職缺</b> {today}"]
    for name, jobs in new_by_search.items():
        lines = [f"\n<b>▍{html.escape(name)}</b>"]
        for i, job in enumerate(jobs[:max_per], 1):
            lines.append(
                f"{i}. <a href=\"{job['url']}\">{html.escape(job['name'])}</a>"
                f" — {html.escape(job['cust'])}（{html.escape(job['area'])}）"
            )
        if len(jobs) > max_per:
            lines.append(f"　…另有 {len(jobs) - max_per} 筆，見 104 搜尋")
        blocks.append("\n".join(lines))

    # Telegram 單則上限 4096 字，超過就拆多則
    messages, current = [], ""
    for block in blocks:
        if current and len(current) + len(block) + 1 > TELEGRAM_MSG_LIMIT - 100:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n{block}" if current else block
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只印出訊息，不推播、不更新去重紀錄")
    args = parser.parse_args()

    cfg = load_config()
    seen = load_seen()
    new_by_search = collect_new_jobs(cfg, seen)
    messages = build_messages(new_by_search, cfg.get("max_per_search", 8))

    if args.dry_run:
        for msg in messages:
            print("=" * 40)
            print(msg)
        total = sum(len(v) for v in new_by_search.values())
        print("=" * 40)
        print(f"[dry-run] 新職缺 {total} 筆，訊息 {len(messages)} 則，未推播、未寫入 seen_jobs.json")
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        sys.exit("缺少環境變數 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")

    for msg in messages:
        send_telegram(token, chat_id, msg)
        time.sleep(1)
    save_seen(seen, cfg.get("seen_retention_days", 90))
    total = sum(len(v) for v in new_by_search.values())
    print(f"sent {len(messages)} message(s), {total} new job(s)")


if __name__ == "__main__":
    main()
