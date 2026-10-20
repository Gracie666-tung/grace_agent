#!/usr/bin/env python3
"""KEDGE JobTeaser 通知信 → Telegram（Mac 本機）：從 Apple Mail（帳號名稱 "KEDGE"）讀 JobTeaser 每天寄的
「offres correspondant à vos critères」提醒信，解析信裡的職缺卡片（標題層級，沒有 JD 全文），
丟掉 CDI／CDD、依標題關鍵字把符合的排前面（✅），整理成 Telegram 摘要。
完全不碰 JobTeaser 網站：不抓網頁、不跟追蹤連結，連結只是原封不動放進訊息讓 Grace 自己點。

用法：
    python3 src/jobteaser_mail_digest.py --dry-run --fixture-dir tests/fixtures/jobteaser_mail   # 離線，不讀 Mail、不推播、不寫狀態
    python3 src/jobteaser_mail_digest.py --dry-run          # 讀真的 Mail，只印出（連結預設遮蔽，--show-urls 才顯示）
    python3 src/jobteaser_mail_digest.py                    # 正式：讀 Mail、推 Telegram、寫去重狀態
    --days N（只看最近 N 天的信，預設見 jobteaser_mail_config.json）、--no-sync（不先叫 Mail 收信）

Telegram token／chat id 讀 macOS Keychain（服務名 jobteaser-mail-telegram-token／jobteaser-mail-telegram-chat，
由 scripts/install_jobteaser_mail.sh 寫入）；環境變數 TELEGRAM_BOT_TOKEN／TELEGRAM_CHAT_ID 優先（測試用）。
去重狀態預設 ~/Library/Application Support/jobteaser-mail/seen.json（可用 --state-path 或環境變數
JOBTEASER_MAIL_STATE_PATH 覆蓋），只存 sha1 雜湊，不存連結。token 與追蹤連結絕不進 log／狀態檔。
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple
from datetime import datetime, timedelta
from email import message_from_bytes, policy
from html.parser import HTMLParser
from pathlib import Path

import requests

from jobteaser_monitor import _cap, _e, kw_regex, norm, send_telegram

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "jobteaser_mail_config.json"
KEYWORDS_PATH = ROOT / "jobteaser_config.json"
DEFAULT_STATE_PATH = Path.home() / "Library" / "Application Support" / "jobteaser-mail" / "seen.json"

KEYCHAIN_TOKEN = "jobteaser-mail-telegram-token"
KEYCHAIN_CHAT = "jobteaser-mail-telegram-chat"
SENDER_NEEDLE = "jobteaser"
SUBJECT_NEEDLES = ("offres correspondant", "vos crit")  # 主旨用字曾經微調過，放寬成其中任一；一律不分大小寫
BOUNDARY = "=====JTMAIL-MSG-BOUNDARY====="
TG_HARD_LIMIT = 4096
SEND_RETRY_DELAYS = (30, 60, 60)  # 網路暫時不通（例如睡眠醒來 Wi-Fi 還沒連上）時的重試間隔，秒
CONTENT_ERRORS = (400, 413)  # Telegram 拒收「這則內容」；401／403／404 是 token／chat 的問題，不屬於這一類

CONTRACT_LINE = re.compile(r"^(.+?)\s+-\s+(.+?)(?:\s+\((.+)\))?$")
KNOWN_CONTRACT = re.compile(
    r"^(?:stage|stagiaire|internship|intern|cdi|cdd|alternance|apprentissage|apprenticeship|"
    r"contrat d apprentissage|contrat de professionnalisation|professionnalisation|vie|v i e|"
    r"graduate|volontariat|interim|freelance)(?![a-z0-9])")
CONTRACT_KIND = (("stage", re.compile(r"^(?:stage|stagiaire|internship|intern)")),
                 ("alternance", re.compile(r"^(?:alternance|apprentissage|apprenticeship|contrat d apprentissage|"
                                           r"contrat de professionnalisation|professionnalisation)")),
                 ("vie", re.compile(r"^(?:vie|v i e|volontariat)")),
                 ("graduate", re.compile(r"^graduate")),
                 ("cdi", re.compile(r"^cdi")), ("cdd", re.compile(r"^cdd")))
CONTRACT_LABEL = {"stage": "Stage", "alternance": "Alternance", "vie": "VIE", "graduate": "Graduate"}

# osascript 讀信：把帳號名稱、天數當 argv 傳進去（不拼字串、不用 shell）。
# 每封信的原始 MIME（source）前面放一條分隔線，Python 這邊再切開。
APPLESCRIPT = """\
on run argv
  set acctName to item 1 of argv
  set nDays to (item 2 of argv) as integer
  set delim to linefeed & "{boundary}" & linefeed
  set cutoff to (current date) - (nDays * 86400)
  set out to ""
  with timeout of {timeout} seconds
    tell application "Mail"
      set acct to account acctName
      try
        set mb to mailbox "INBOX" of acct
      on error
        set mb to mailbox "Inbox" of acct
      end try
      set msgs to (messages of mb whose sender contains "{sender}" and (subject contains "{subject1}" or subject contains "{subject2}") and date received > cutoff)
      repeat with m in msgs
        set out to out & delim & (source of m)
      end repeat
    end tell
  end timeout
  return out
end run
"""
SYNC_SCRIPT = 'tell application "Mail" to check for new mail'


class MailError(Exception):
    """kind：permission／notfound／timeout／other；code：AppleScript 錯誤碼（可能為 None）。訊息只放固定字串。"""

    def __init__(self, kind: str, code: int | None = None):
        super().__init__(kind, code)
        self.kind, self.code = kind, code


# ---------------------------------------------------------------------------
# 設定與狀態
# ---------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    """jobteaser_config.json 的中英法關鍵字當底，jobteaser_mail_config.json 的值覆蓋；extra_title_keywords 併進正面字。"""
    with open(KEYWORDS_PATH, encoding="utf-8") as f:
        base = json.load(f)
    with open(path, encoding="utf-8") as f:
        cfg = base | json.load(f)
    cfg["keywords_positive"] = list(cfg["keywords_positive"]) + list(cfg.get("extra_title_keywords", []))
    return cfg


def load_state(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    data = data if isinstance(data, dict) else {}
    seen = data.get("seen")
    seen = {k: v for k, v in seen.items() if isinstance(v, str)} if isinstance(seen, dict) else {}
    return {"seen": seen, "last_alert_date": str(data.get("last_alert_date") or "")}


def save_state(path: Path, state: dict) -> None:
    """原子寫入：同資料夾寫暫存檔再 os.replace，中途被殺掉也不會留下半個 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def prune_state(state: dict, today: datetime, days: int) -> None:
    cutoff = (today - timedelta(days=days)).date().isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}


def job_hash(job: dict) -> str:
    """公司｜標題｜城市的 sha1。不用 URL：追蹤連結因人而異、天天不同。"""
    key = "|".join(norm(job.get(k, "")) for k in ("company", "title", "city"))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 解析：信 → 職缺卡片
# ---------------------------------------------------------------------------

class _Anchors(HTMLParser):
    """收集每個 <a> 的 href 與裡面的文字節點（以標籤邊界切開、空白壓成單一空格）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cur: dict | None = None
        self.anchors: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.cur = {"href": dict(attrs).get("href") or "", "lines": []}

    def handle_endtag(self, tag):
        if tag == "a" and self.cur is not None:
            self.anchors.append(self.cur)
            self.cur = None

    def handle_data(self, data):
        if self.cur is not None:
            text = re.sub(r"\s+", " ", data.replace("\xa0", " ")).strip()
            if text:
                self.cur["lines"].append(text)


def contract_kind(contract: str) -> str:
    c = norm(contract)
    for kind, rx in CONTRACT_KIND:
        if rx.search(c):
            return kind
    return c


def card_from_lines(lines: list[str], url: str) -> dict | None:
    """['公司…', '標題', 'Contract - City (Country)'] → 卡片；不像職缺卡片回 None。
    最後一行必須是『已知合約字 - 城市』且前面至少還有一行。前面只有一行＝沒有公司；兩行＝公司＋標題；
    三行以上＝最後一行是標題、其餘都是公司名稱的斷行。兩行但第二行以 '-' 開頭＝標題被斷成兩行（沒有公司）。"""
    if len(lines) < 2:
        return None
    m = CONTRACT_LINE.match(lines[-1])
    if not m or not KNOWN_CONTRACT.match(norm(m.group(1))):
        return None
    pre = lines[:-1]
    if len(pre) == 1:
        company, title = "", pre[0]
    elif len(pre) == 2 and pre[1].startswith("-") and KNOWN_CONTRACT.match(norm(pre[0])):
        company, title = "", f"{pre[0]} {pre[1]}"
    else:
        company, title = " ".join(pre[:-1]), pre[-1]
    return {"company": company, "title": title, "contract": m.group(1).strip(), "city": m.group(2).strip(),
            "country": (m.group(3) or "").strip(), "url": url}


def parse_cards(html_text: str) -> list[dict]:
    p = _Anchors()
    p.feed(html_text or "")
    p.close()
    out = []
    for a in p.anchors:
        if not a["href"].lower().startswith(("http://", "https://")):
            continue
        card = card_from_lines(a["lines"], a["href"])
        if card:
            out.append(card)
    return out


def is_alert(msg) -> bool:
    sender = str(msg.get("From", "")).casefold()
    subject = str(msg.get("Subject", "")).casefold()
    return SENDER_NEEDLE in sender and any(n in subject for n in SUBJECT_NEEDLES)


def parse_message(raw: bytes):
    """原始 MIME → (msg 或 None, 卡片清單)；不是 JobTeaser 通知信就是 (None, [])。"""
    msg = message_from_bytes(raw, policy=policy.default)
    if not is_alert(msg):
        return None, []
    body = msg.get_body(preferencelist=("html",))
    if body is None:
        return msg, []
    return msg, parse_cards(body.get_content())


def msg_sort_key(msg) -> float:
    try:
        return msg["Date"].datetime.timestamp()
    except Exception:  # noqa: BLE001 - 日期缺或壞掉就當最舊
        return 0.0


# ---------------------------------------------------------------------------
# 讀信：Apple Mail（osascript）或 fixture
# ---------------------------------------------------------------------------

def classify_osascript_error(stderr: str) -> MailError:
    """只留錯誤類別與錯誤碼；osascript 的原始訊息不往外傳（可能含信件資訊）。"""
    codes = re.findall(r"\((-?\d+)\)", stderr or "")
    code = int(codes[-1]) if codes else None
    if "Not authorized" in (stderr or "") or code == -1743:
        return MailError("permission", -1743)
    if code in (-1728, -1719):
        return MailError("notfound", code)
    if code == -1712:
        return MailError("timeout", code)
    return MailError("other", code)


def _osascript(script: str, args: list[str], timeout: int) -> str:
    cmd = ["osascript"]
    for line in script.splitlines():
        cmd += ["-e", line]
    cmd += args
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MailError("timeout") from None
    except OSError:
        raise MailError("other") from None
    if res.returncode != 0:
        raise classify_osascript_error(res.stderr.decode("utf-8", "replace"))
    return res.stdout.decode("utf-8", "replace")


def sync_mail(wait_s: float) -> None:
    """請 Mail 收信再等一下；失敗不致命（讀信那一步會再報同樣的權限問題）。"""
    try:
        _osascript(SYNC_SCRIPT, [], 60)
    except MailError as e:
        print(f"  [略過] 叫 Mail 收信失敗（{e.kind}），改用 Mail 目前已有的信")
        return
    time.sleep(wait_s)


def read_mail(cfg: dict, days: int) -> list[bytes]:
    script = APPLESCRIPT.format(boundary=BOUNDARY, sender=SENDER_NEEDLE, subject1=SUBJECT_NEEDLES[0], subject2=SUBJECT_NEEDLES[1],
                                timeout=int(cfg["osascript_timeout_s"]))
    out = _osascript(script, [cfg["mail_account"], str(int(days))], int(cfg["osascript_timeout_s"]) + 30)
    parts = [p.strip("\r\n") for p in out.split(BOUNDARY)[1:]]
    return [p.encode("utf-8") for p in parts if p.strip()]


def read_fixtures(folder: Path) -> list[bytes]:
    return [p.read_bytes() for p in sorted(folder.glob("*.eml"))]


# ---------------------------------------------------------------------------
# 篩選與訊息組裝
# ---------------------------------------------------------------------------

class Collected(NamedTuple):
    jobs: list          # 去重後的卡片（新信優先）
    subjects: list      # 成功解析的通知信主旨（新到舊）
    newest_cards: int   # 最新一封通知信解析出幾張卡片（沒有通知信時為 -1）
    errors: int         # 解析時丟出例外而被略過的信件數


def collect_jobs(raws: list[bytes]) -> Collected:
    """所有通知信的卡片，新信優先；信內與跨信都以 job_hash 去重。單封信解析失敗（未知字集等）只略過那一封並計數，
    不往外丟例外（例外文字可能含信件內容，一律不印）。"""
    parsed, errors = [], 0
    for r in raws:
        try:
            parsed.append(parse_message(r))
        except Exception:  # noqa: BLE001
            errors += 1
    msgs = sorted(((m, c) for m, c in parsed if m is not None), key=lambda mc: -msg_sort_key(mc[0]))
    jobs, hashes = [], set()
    for _, cards in msgs:
        for card in cards:
            card["hash"] = job_hash(card)
            if card["hash"] not in hashes:
                hashes.add(card["hash"])
                jobs.append(card)
    return Collected(jobs, [str(m["Subject"]) for m, _ in msgs], len(msgs[0][1]) if msgs else -1, errors)


def select_jobs(jobs: list[dict], seen: dict, cfg: dict) -> tuple[list[dict], dict]:
    """丟掉 CDI／CDD 與已推過的；標題符合正面字的標 priority 並排前面，其餘維持原順序。"""
    drop = {norm(c) for c in cfg.get("drop_contracts", [])}
    pos = kw_regex(cfg["keywords_positive"])
    stats = {"cards": len(jobs), "dropped_contract": 0, "already_seen": 0}
    keep = []
    for job in jobs:
        kind = contract_kind(job["contract"])
        if kind in drop:
            stats["dropped_contract"] += 1
        elif job["hash"] in seen:
            stats["already_seen"] += 1
        else:
            job["kind"] = kind
            job["priority"] = bool(pos.search(norm(job["title"])))
            keep.append(job)
    keep.sort(key=lambda j: not j["priority"])  # stable：同級維持信裡的順序
    return keep, stats


def format_entry(job: dict, with_link: bool | None = True) -> str:
    """with_link：True＝連結；False＝「連結過長」註記；None＝完全不放連結行（Telegram 拒收時的退路）。"""
    meta = [x for x in (_cap(job["company"], 60), _cap(job["city"], 40),
                        CONTRACT_LABEL.get(job["kind"]) or _cap(job["contract"], 20)) if x]
    line = f"{'✅' if job['priority'] else '▫️'} <b>{_e(_cap(job['title'], 160))}</b>"
    if meta:
        line += " — " + " · ".join(_e(x) for x in meta)
    if with_link is None:
        return line
    link = f'<a href="{_e(job["url"])}">查看職缺</a>' if with_link else "（連結過長，略）"
    return f"{line}\n{link}"


def header(part: int, total: int, n_jobs: int) -> str:
    return (f"📬 <b>JobTeaser 通知信新職缺</b>（{n_jobs} 則，{part}/{total}）\n"
            "僅標題層級（沒有 JD 全文），來自 JobTeaser 提醒信；✅＝標題符合你的關鍵字，▫️＝其他。")


def build_messages(jobs: list[dict], limit: int, max_messages: int) -> tuple[list[tuple[str, list[str]]], int]:
    """回傳 ([(訊息文字, 該訊息內職缺的 hash 清單)], 因上限而沒放進去的職缺數)。每則訊息 <= limit 字元。"""
    reserve = len(header(99, 99, 999)) + 2
    chunks: list[list[tuple[str, str]]] = []
    cur, cur_len = [], 0
    for job in jobs:
        ent = format_entry(job)
        if reserve + len(ent) > min(limit, TG_HARD_LIMIT):
            ent = format_entry(job, with_link=False)  # 單一連結就塞不進一則訊息：寧可沒連結也不要送不出去
        if cur and reserve + cur_len + len(ent) + 2 > limit:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append((job["hash"], ent))
        cur_len += len(ent) + 2
    if cur:
        chunks.append(cur)
    shown, hidden = chunks[:max_messages], [x for c in chunks[max_messages:] for x in c]
    out = []
    for i, chunk in enumerate(shown, 1):
        last = i == len(shown)

        def compose(items):
            text = header(i, len(shown), len(jobs)) + "\n\n" + "\n\n".join(e for _, e in items)
            if last and hidden:
                text += f"\n\n另有 {len(hidden)} 則因單次上限未顯示，下次執行補送。"
            return text
        text = compose(chunk)
        while last and len(text) > limit and len(chunk) > 1:  # 超出上限的註記放不下：把最後一則也順延
            hidden.insert(0, chunk.pop())
            text = compose(chunk)
        out.append((text, [h for h, _ in chunk]))
    return out, len(hidden)


def mask_urls(text: str) -> str:
    return re.sub(r'href="([^"]*)"', lambda m: f'href="(連結已隱藏，{len(m.group(1))} 字元)"', text)


# ---------------------------------------------------------------------------
# Telegram 憑證與警示
# ---------------------------------------------------------------------------

def keychain_get(service: str) -> str:
    try:
        res = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                             capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return res.stdout.decode("utf-8", "replace").strip() if res.returncode == 0 else ""


def get_credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or keychain_get(KEYCHAIN_TOKEN)
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or keychain_get(KEYCHAIN_CHAT)
    return token, chat


ALERT_TEXT = {
    "permission": ("沒有權限控制 Mail。請到「系統設定 > 隱私權與安全性 > 自動化」，允許執行本工具的程式"
                   "（Terminal 或 python3）控制「郵件」，然後重跑一次。"),
    "notfound": "在 Mail 找不到帳號或收件匣。請確認學校信箱在「郵件」裡的帳號名稱（預設 KEDGE，可改 jobteaser_mail_config.json）。",
    "timeout": "讀取 Mail 逾時。請打開「郵件」App 讓它完成同步後再試。",
    "layout": "找到 JobTeaser 通知信但解析不到任何職缺卡片，可能是信件版面改了。請把這封信的版面回報給維護者。",
    "parse": "所有 JobTeaser 通知信都解析失敗（例如未知的編碼）。請在 Terminal 執行 python3 src/jobteaser_mail_digest.py --dry-run 看診斷。",
    "other": "讀取 Mail 失敗。請在 Terminal 執行 python3 src/jobteaser_mail_digest.py --dry-run 看診斷。",
}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def run(args, now: datetime | None = None) -> int:
    now = now or datetime.now().astimezone()
    cfg = load_config(Path(args.config))
    days = args.days or int(cfg["days"])
    fixture = Path(args.fixture_dir) if args.fixture_dir else None
    state_path = Path(args.state_path or os.environ.get("JOBTEASER_MAIL_STATE_PATH") or DEFAULT_STATE_PATH)
    state = load_state(state_path)
    prune_state(state, now, int(cfg["state_retention_days"]))
    today = now.date().isoformat()
    token = chat_id = ""
    if not args.dry_run:
        token, chat_id = get_credentials()
        if not (token and chat_id):
            print("[錯誤] 找不到 Telegram 憑證（Keychain 服務 jobteaser-mail-telegram-token／-chat，"
                  "或環境變數 TELEGRAM_BOT_TOKEN／TELEGRAM_CHAT_ID）。請先跑 scripts/install_jobteaser_mail.sh。",
                  file=sys.stderr)
            return 1
    where = "離線 fixture" if fixture else "Apple Mail 帳號 %s，最近 %d 天" % (cfg["mail_account"], days)
    print(f"模式：{'dry-run（不推播、不寫狀態）' if args.dry_run else 'Telegram 推播'}；{where}")

    def send(text: str) -> str:
        """回傳 "ok"／"content"（Telegram 回 400／413，這則內容被拒）／"fail"（其他）。
        連線錯誤、逾時、HTTP 429 與 5xx 依 SEND_RETRY_DELAYS 退避重試；例外訊息會回顯含 token 的網址，只印型別與狀態碼。"""
        for attempt in range(len(SEND_RETRY_DELAYS) + 1):
            try:
                send_telegram(token, chat_id, text)
                return "ok"
            except Exception as e:  # noqa: BLE001
                status = getattr(getattr(e, "response", None), "status_code", None)
                print(f"  Telegram 傳送失敗：{type(e).__name__}" + (f"（HTTP {status}）" if status else ""))
                if status in CONTENT_ERRORS:
                    return "content"
                transient = (isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))
                             or status == 429 or (status or 0) >= 500)
                if not transient or attempt == len(SEND_RETRY_DELAYS):
                    return "fail"
                print(f"  {SEND_RETRY_DELAYS[attempt]} 秒後重試（{attempt + 1}/{len(SEND_RETRY_DELAYS)}）")
                time.sleep(SEND_RETRY_DELAYS[attempt])
        return "fail"

    def alert_once(text: str) -> None:
        """每天最多一則 Telegram 提醒（旗標存在狀態檔）。dry-run 不送。"""
        if args.dry_run:
            print("  （dry-run：不送 Telegram 警示）")
        elif state["last_alert_date"] == today:
            print("  今天已提醒過，不重複推播。")
        elif send(f"⚠️ JobTeaser 通知信工具：{_e(text)}") == "ok":
            state["last_alert_date"] = today
            save_state(state_path, state)

    def fail(kind: str, code: int | None = None, detail: str = "") -> int:
        """一行診斷（只有類別與錯誤碼）＋每天最多一則 Telegram 提醒；回傳 1。"""
        text = ALERT_TEXT[kind] + detail
        print(f"[錯誤] {'讀取 Apple Mail 失敗' if kind in ('permission', 'notfound', 'timeout', 'other') else '解析失敗'}："
              f"{kind}{f'，錯誤碼 {code}' if code is not None else ''}。{text}", file=sys.stderr)
        alert_once(text)
        return 1

    try:
        if fixture:
            raws = read_fixtures(fixture)
        else:
            if not args.no_sync:
                sync_mail(float(cfg["sync_wait_s"]))
            raws = read_mail(cfg, days)
    except MailError as e:
        return fail(e.kind, e.code)

    col = collect_jobs(raws)
    jobs, subjects = col.jobs, col.subjects
    print(f"讀到 {len(raws)} 封信，其中 JobTeaser 通知信 {len(subjects)} 封" + ("：" + "｜".join(subjects) if subjects else "")
          + (f"；{col.errors} 封解析失敗已略過" if col.errors else ""))
    rc = 0
    if col.errors and not subjects:
        return fail("parse", detail=f"（{col.errors} 封解析失敗）")
    if subjects and col.newest_cards == 0:  # 只看最新那封：舊信有卡片不代表版面沒改
        rc = fail("layout")
    fresh, st = select_jobs(jobs, state["seen"], cfg)
    print(f"職缺卡片（去重後）{st['cards']} 張：CDI／CDD 略過 {st['dropped_contract']}、已推過 {st['already_seen']}、"
          f"新職缺 {len(fresh)}（✅ {sum(j['priority'] for j in fresh)}）")
    if not fresh:
        print("沒有新職缺，不推播。")
        return rc

    by_hash = {j["hash"]: j for j in fresh}
    messages, hidden = build_messages(fresh, int(cfg["message_limit"]), int(cfg["max_messages"]))
    skipped = 0
    for i, (text, hashes) in enumerate(messages, 1):
        if args.dry_run:
            print("-" * 40 + f"\n{text if args.show_urls else mask_urls(text)}\n" + "-" * 40)
            continue
        res = send(text)
        if res == "content":  # 這則被 Telegram 拒收：去掉連結再試一次，免得一則壞訊息永遠卡住後面所有的
            plain = (header(i, len(messages), len(fresh)) + "\n\n"
                     + "\n\n".join(format_entry(by_hash[h], with_link=None) for h in hashes)
                     + "\n\n（Telegram 拒收含連結的版本，連結略去；請到「郵件」開原信點選。）")
            res = send(plain)
            if res == "ok":
                print(f"  第 {i}/{len(messages)} 則改用無連結版送出。")
            elif res == "content":
                skipped += len(hashes)
                print(f"  第 {i}/{len(messages)} 則連無連結版也被拒收：略過 {len(hashes)} 個職缺並標記已見。")
        if res == "fail":
            print(f"  第 {i}/{len(messages)} 則失敗：這則的職缺不標記已見，下次重試。")
            return 1
        for h in hashes:  # 每則送成功（或確定無法送）就記，避免後面失敗時前面已送的下次又重送
            state["seen"][h] = today
        save_state(state_path, state)
    if skipped:
        print(f"[警告] 共 {skipped} 個職缺因 Telegram 拒收（HTTP 4xx）而略過。")
        alert_once(f"有 {skipped} 個職缺因 Telegram 拒收訊息（HTTP 4xx）而略過、已標記為已見。請到「郵件」查看原信。")
        return 1
    print(f"完成：{'dry-run 印出' if args.dry_run else '已推播'} {len(messages)} 則訊息"
          + (f"，{hidden} 則留待下次" if hidden else ""))
    return rc


def main() -> None:
    ap = argparse.ArgumentParser(description="JobTeaser 通知信（Apple Mail）→ Telegram 摘要")
    ap.add_argument("--dry-run", action="store_true", help="只印出，不推播、不寫狀態")
    ap.add_argument("--fixture-dir", help="讀這個資料夾的 *.eml，不連 Mail（離線測試）")
    ap.add_argument("--days", type=int, help="只看最近 N 天的信（覆蓋設定檔）")
    ap.add_argument("--no-sync", action="store_true", help="不先叫 Mail 收信")
    ap.add_argument("--state-path", help="去重狀態檔路徑（預設 ~/Library/Application Support/jobteaser-mail/seen.json）")
    ap.add_argument("--show-urls", action="store_true", help="dry-run 也顯示追蹤連結（預設遮蔽）")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
