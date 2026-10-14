#!/usr/bin/env python3
"""讀 data/news_archive/*.json，印出給每日分析師（Claude）看的精簡純文字新聞簡報。不連網。

用法：
    python src/news_brief.py --since-hours 24            # 過去 24 小時（預設）
    python src/news_brief.py --date 2026-10-14           # 某個 UTC 日期的整天
    python src/news_brief.py --ticker ANET --max-per-ticker 30
    python src/news_brief.py --repo /path/to/repo        # 預設為此腳本所在 repo 根目錄
    （環境變數 NEWS_ARCHIVE_DIR 可改歸檔目錄，測試/示範用）

輸出依代號分組：持有/priority 先，其次其他標的，最後 MACRO；組內硬觸發先、再依時間新到舊。
每組附 news_watchlist.json 的背景（狀態、權重、成本、停損、下個事件）。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TITLE_MAX = 130


def _load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _parse_ts(s) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def load_records(root: Path, days: list[str]) -> list[dict]:
    out = []
    for day in days:
        base = Path(os.environ["NEWS_ARCHIVE_DIR"]) if os.environ.get("NEWS_ARCHIVE_DIR") else root / "data" / "news_archive"
        data = _load_json(base / f"{day}.json", [])
        if isinstance(data, list):
            for r in data:
                if isinstance(r, dict) and _parse_ts(r.get("ts")):
                    out.append(dict(r, _dt=_parse_ts(r["ts"])))
    return out


def _clip(s: str, n: int = TITLE_MAX) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def watch_context(entry: dict | None) -> str:
    if not entry:
        return ""
    parts = []
    w, ep, sp = entry.get("weight_pct"), entry.get("entry_price"), entry.get("stop_pct")
    status = {"held": "持有", "candidate": "候選"}.get(entry.get("status"), entry.get("status") or "")
    if entry.get("priority"):
        status += "/priority"
    if status:
        parts.append(status)
    if w is not None:
        parts.append(f"權重 {w:g}%")
    if ep is not None:
        parts.append(f"成本 {ep:g}")
        if sp is not None:
            parts.append(f"停損 {ep * (1 - sp / 100):.2f}（−{sp:g}%）")
    elif sp is not None:
        parts.append(f"停損 −{sp:g}%")
    evs = sorted((e for e in entry.get("next_events") or [] if isinstance(e, dict) and e.get("date")),
                 key=lambda e: str(e["date"]))
    if evs:
        parts.append(f"下個事件 {evs[0]['date']} {evs[0].get('label') or ''}".rstrip())
    return " · ".join(parts)


def build_brief(root: Path, since_hours: float = 24, date: str | None = None, ticker: str | None = None,
                max_per_ticker: int = 12, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if date:
        recs = [r for r in load_records(root, [date]) if r["_dt"].strftime("%Y-%m-%d") == date]
        scope = f"UTC 日期 {date}"
    else:
        cutoff = now - timedelta(hours=since_hours)
        days = sorted({(cutoff + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((now.date() - cutoff.date()).days + 1)})
        recs = [r for r in load_records(root, days) if cutoff <= r["_dt"] <= now + timedelta(minutes=5)]
        scope = f"過去 {since_hours:g} 小時"
    if ticker:
        recs = [r for r in recs if str(r.get("ticker", "")).upper() == ticker.upper()]
    if not recs:
        return f"新聞簡報（{scope}）：沒有資料。"

    wl = _load_json(root / "news_watchlist.json", {})
    entries = {e["ticker"]: e for e in (wl.get("entries") or []) if isinstance(e, dict) and e.get("ticker")}
    order = list(entries)
    groups: dict[str, list[dict]] = {}
    for r in recs:
        groups.setdefault(str(r.get("ticker") or "?"), []).append(r)

    def rank(t: str):
        e = entries.get(t)
        if t == "MACRO":
            return (3, 0)
        if e and (e.get("status") == "held" or e.get("priority")):
            return (0, order.index(t))
        return (1, order.index(t)) if e else (2, 0)

    lo, hi = min(r["_dt"] for r in recs), max(r["_dt"] for r in recs)
    multi_day = lo.date() != hi.date()
    fmt = "%Y-%m-%d %H:%M"
    lines = [f"新聞簡報（{scope}）｜共 {len(recs)} 則｜{len(groups)} 個標的｜"
             f"{lo.strftime(fmt)} ~ {hi.strftime(fmt)} UTC｜硬觸發 {sum(1 for r in recs if r.get('hard'))} 則",
             "計數：" + "、".join(f"{t} {len(groups[t])}" for t in sorted(groups, key=rank)), ""]
    for t in sorted(groups, key=rank):
        rows = sorted(groups[t], key=lambda r: r["_dt"], reverse=True)
        rows.sort(key=lambda r: not r.get("hard"))  # 穩定排序：硬觸發先、組內仍新到舊
        name = next((r.get("name") for r in rows if r.get("name")), "") or (entries.get(t) or {}).get("name", "")
        lines.append(f"== {t}{(' ' + name) if name else ''} — {len(rows)} 則（硬觸發 {sum(1 for r in rows if r.get('hard'))}，"
                     f"即時推播 {sum(1 for r in rows if r.get('immediate'))}）")
        ctx = watch_context(entries.get(t))
        if ctx:
            lines.append(f"   背景：{ctx}")
        for r in rows[:max_per_ticker]:
            stamp = r["_dt"].strftime("%m-%d %H:%M" if multi_day else "%H:%M")
            lines.append(f"[{stamp} UTC] {r.get('label') or ''} | {_clip(r.get('title', ''))} | {r.get('link', '')}")
        if len(rows) > max_per_ticker:
            lines.append(f"   …另 {len(rows) - max_per_ticker} 則未列出（調高 --max-per-ticker 可看全部）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since-hours", type=float, default=24)
    ap.add_argument("--date", help="UTC 日期 YYYY-MM-DD（優先於 --since-hours）")
    ap.add_argument("--ticker")
    ap.add_argument("--max-per-ticker", type=int, default=12)
    ap.add_argument("--repo", default=str(ROOT), help="repo 路徑（含 data/news_archive 與 news_watchlist.json）")
    a = ap.parse_args()
    if a.date:
        try:
            datetime.strptime(a.date, "%Y-%m-%d")
        except ValueError:
            print("--date 格式應為 YYYY-MM-DD", file=sys.stderr)
            sys.exit(2)
    sys.stdout.write(build_brief(Path(a.repo), a.since_hours, a.date, a.ticker, a.max_per_ticker))


if __name__ == "__main__":
    main()
