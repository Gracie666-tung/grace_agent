#!/usr/bin/env python3
"""La Bonne Alternance（官方 API）alternance 職缺推播：每天下載一次「全部職缺匯出檔」，在本機篩出
最近 N 天新發布、未過期的職缺，用關鍵字（含法文）粗篩、規則式判定法文要求（D 不推）、
有 ANTHROPIC_API_KEY 就由 Claude 打 0-100 分（≥70 即時推、60-69 合併摘要、<60 只記錄），否則只送一則未評分摘要。

用法：
    python src/lba_monitor.py --dry-run --fixture-dir tests/fixtures/lba   # 離線，不推播、不寫狀態
    python src/lba_monitor.py --dry-run                                     # 連真的 API（需 LBA_API_KEY），只印出
    python src/lba_monitor.py                                               # 正式：LBA_API_KEY、TELEGRAM_BOT_TOKEN、
                                                                            # TELEGRAM_CHAT_ID；ANTHROPIC_API_KEY 可選

可選環境變數：LBA_MODEL（覆蓋模型 id）、LBA_STATE_PATH（覆蓋去重狀態檔路徑，測試用）。
API key 只放在對 api.apprentissage.beta.gouv.fr 的 Authorization 標頭；絕不寫入狀態檔、log 或 Telegram，
也絕不送去匯出檔的下載連結（那是預簽名連結，不需要也不該帶 key）。

API 依據：https://api.apprentissage.beta.gouv.fr/api/swagger.json（2026-10-20 讀過）：
  GET /api/job/v1/export，Authorization: Bearer <key>，回 {url, lastUpdate}；url 兩分鐘內有效，
  內容為 JSON，每日 03:00（巴黎）更新，限速每分鐘 2 次。
"""

import argparse
import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from jobteaser_monitor import (EN_A, FR_B, FR_C, FR_D, FR_D_GENERIC, FR_NONE, JD_MAX_CHARS, _cap, _e, _link, build_system,
                               call_claude, fixture_llm, format_digest_entry, format_job, is_seen, kw_regex, norm, pack,
                               prune_state, route, save_state, score_job, send_telegram, strip_tags)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "lba_config.json"
STATE_PATH = ROOT / "data" / "seen_lba.json"

API_HOST = "api.apprentissage.beta.gouv.fr"
EXPORT_URL = f"https://{API_HOST}/api/job/v1/export"
UA = "grace-lba-monitor/1.0 (+https://github.com/Gracie666-tung/grace_agent; personal job alert, 1 export call/day)"
TIMEOUT = 30
DOWNLOAD_TIMEOUT = (10, 60)  # (連線, 兩個資料塊之間)
RATE_LIMIT_STATUSES = (419, 429)  # swagger 的 429 說明寫 429，錯誤 schema 寫 419，兩個都當限流
KEY_FORMAT = re.compile(r"^[\x21-\x7e]+$")  # 可印出 ASCII、無空白；不合的 key 不送出（requests 的錯誤訊息會回顯標頭值）
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BLOCK_END = re.compile(r"(?i)</(?:p|li|div|h\d|ul|ol|tr)>|<br\s*/?>")  # strip_tags 會把換行壓成空白，先換成句點才分得出子句
POSTAL = re.compile(r"\b(\d{5})\s+([^\d,][^,]*)$")
LIST_KEYS = ("jobs", "offers", "items", "data", "results")  # 匯出檔結構未驗證：這幾個鍵底下的陣列都當職缺清單


class AuthError(Exception):
    """401／403，或 key 沒設／格式不對（status=None）。"""

    def __init__(self, status: int | None = None):
        super().__init__(status)
        self.status = status


class RateLimited(Exception):
    pass


class ExportError(Exception):
    """訊息只放固定文字與狀態碼，不放 URL／標頭。"""


# ---------------------------------------------------------------------------
# 設定、狀態
# ---------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    """lba_config.json 的 inherit_from 指向的檔（關鍵字、persona、rubric）當底，自己的值覆蓋。"""
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    parent = cfg.pop("inherit_from", None)
    if parent:
        with open(Path(path).parent / parent, encoding="utf-8") as f:
            base = json.load(f)
        cfg = base | cfg
    return cfg


def load_state(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data = data if isinstance(data, dict) else {}
    seen = data.get("seen")
    return {"seen": seen if isinstance(seen, dict) else {}, "key_alert_sent": bool(data.get("key_alert_sent"))}


# ---------------------------------------------------------------------------
# 匯出檔：下載與串流解析
# ---------------------------------------------------------------------------

def host_label(sp) -> str:
    """主機（非預設埠才加埠）。刻意不用 netloc：https://user:SECRET@host 的 netloc 帶著帳密。"""
    try:
        port = sp.port
    except ValueError:
        port = None
    host = sp.hostname or ""
    return f"{host}:{port}" if port and port != 443 else host


def diag_line(status: int, url: str, extra: str = "") -> str:
    """非 200 時留一行診斷：只有狀態碼與 host+path（不含 query，預簽名連結的簽章在 query；也不含 userinfo），絕不含標頭或 key。"""
    sp = urlsplit(url)
    return f"[診斷] HTTP {status} {host_label(sp)}{sp.path}{extra}"


def fetch_export(api_key: str, dest, max_bytes: int) -> dict:
    """呼叫一次 export API，再把下載連結的內容串流寫進 dest；回傳 {"lastUpdate": ...}。
    key 只送給 API_HOST；下載連結是預簽名連結，不帶任何 Authorization，不跟轉址。"""
    if not KEY_FORMAT.match(api_key):
        raise AuthError(None)
    try:
        resp = requests.get(EXPORT_URL, headers={"Authorization": f"Bearer {api_key}", "User-Agent": UA,
                                                 "Accept": "application/json"},
                            timeout=TIMEOUT, allow_redirects=False)
    except Exception as e:  # noqa: BLE001 - requests 的例外訊息可能回顯標頭，只印型別
        raise ExportError(f"呼叫匯出 API 失敗（{type(e).__name__}）") from None
    status = resp.status_code
    if status != 200:
        retry = (resp.headers or {}).get("retry-after")
        print(diag_line(status, EXPORT_URL, f"｜retry-after={retry}" if retry else ""))
        if status in (401, 403):
            raise AuthError(status)
        if status in RATE_LIMIT_STATUSES:
            raise RateLimited
        raise ExportError(f"匯出 API 回 HTTP {status}")
    try:
        info = resp.json()
        url, last_update = info["url"], info.get("lastUpdate")
    except Exception:  # noqa: BLE001
        raise ExportError("匯出 API 回應不是預期的 {url, lastUpdate} JSON") from None
    sp = urlsplit(url) if isinstance(url, str) else None
    if not sp or sp.scheme != "https" or not sp.hostname:
        raise ExportError("匯出下載連結不是 https，不下載")
    print(f"匯出檔 lastUpdate={last_update}；下載主機 {host_label(sp)}"
          f"{'（與 API 不同主機，不帶 key）' if host_label(sp) != API_HOST else ''}")
    try:
        dl = requests.get(url, headers={"User-Agent": UA}, stream=True, timeout=DOWNLOAD_TIMEOUT, allow_redirects=False)
    except Exception as e:  # noqa: BLE001
        raise ExportError(f"下載匯出檔失敗（{type(e).__name__}）") from None
    try:
        if dl.status_code != 200:
            print(diag_line(dl.status_code, url))
            if dl.status_code in RATE_LIMIT_STATUSES:
                raise RateLimited
            raise ExportError(f"下載匯出檔回 HTTP {dl.status_code}")
        total = 0
        try:
            for chunk in dl.iter_content(chunk_size=1 << 20):
                total += len(chunk)
                if total > max_bytes:
                    raise ExportError(f"匯出檔超過上限 {max_bytes // (1 << 20)} MB，中止（可調 max_export_mb）")
                dest.write(chunk)
        except ExportError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ExportError(f"下載匯出檔中斷（{type(e).__name__}）") from None
    finally:
        dl.close()
    print(f"匯出檔 {total / (1 << 20):.1f} MB")
    return {"lastUpdate": last_update}


class _JsonStream:
    """對檔案物件做增量 JSON 解析（只用標準庫），記憶體只放一個緩衝區＋一筆職缺。"""

    DEC = json.JSONDecoder()
    CHUNK = 1 << 20

    def __init__(self, fp):
        self.fp, self.buf, self.pos, self.eof = fp, "", 0, False

    def _fill(self) -> bool:
        data = self.fp.read(self.CHUNK)
        if not data:
            self.eof = True
            return False
        self.buf, self.pos = self.buf[self.pos:] + data, 0
        return True

    def peek(self) -> str:
        while True:
            while self.pos < len(self.buf) and self.buf[self.pos] in " \t\r\n":
                self.pos += 1
            if self.pos < len(self.buf):
                return self.buf[self.pos]
            if not self._fill():
                return ""

    def skip(self) -> None:
        self.pos += 1

    def value(self):
        if not self.peek():
            raise ValueError("unexpected end of JSON")
        while True:
            try:
                obj, end = self.DEC.raw_decode(self.buf, self.pos)
            except json.JSONDecodeError:
                if not self._fill():
                    raise
                continue
            if end == len(self.buf) and not self.eof and self._fill():  # 緊貼緩衝區尾端的數字可能被截斷
                continue
            self.pos = end
            return obj

    def array(self):
        self.skip()  # '['
        if self.peek() == "]":
            self.skip()
            return
        while True:
            yield self.value()
            c = self.peek()
            self.skip()
            if c == "]":
                return
            if c != ",":
                raise ValueError("malformed JSON array")


def iter_offers(fp):
    """匯出檔 -> 逐筆職缺 dict。結構未驗證，所以兩種都吃：最外層是陣列，或是物件且職缺在 LIST_KEYS 其中一個鍵的陣列
    （例如搜尋 API 的 {jobs, recruiters, warnings}；recruiters 等其他鍵逐筆讀過就丟）。"""
    s = _JsonStream(fp)
    c = s.peek()
    if c == "[":
        yield from s.array()
        return
    if c != "{":
        raise ValueError("export is neither a JSON array nor object")
    s.skip()
    if s.peek() == "}":
        return
    while True:
        key = s.value()
        if s.peek() != ":":
            raise ValueError("malformed JSON object")
        s.skip()
        if s.peek() == "[":
            for item in s.array():
                if key in LIST_KEYS:
                    yield item
        else:
            s.value()
        c = s.peek()
        s.skip()
        if c == "}":
            return
        if c != ",":
            raise ValueError("malformed JSON object")


# ---------------------------------------------------------------------------
# 單筆職缺：解析、時間與地點
# ---------------------------------------------------------------------------

def parse_ts(v) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _g(d, *path):
    for p in path:
        d = d.get(p) if isinstance(d, dict) else None
    return d


def is_recruiter(raw) -> bool:
    return isinstance(raw, dict) and _g(raw, "identifier", "partner_label") == "recruteurs_lba"


def _names(v) -> str:
    """只取欄位名稱（最多兩層），不含任何值。"""
    def clean(k) -> str:
        return re.sub(r"[^\w.$@:-]", "?", str(k))[:40]
    if not isinstance(v, dict):
        return type(v).__name__
    parts = []
    for k, x in list(v.items())[:40]:
        sub = f"{{{','.join(clean(c) for c in list(x)[:25])}}}" if isinstance(x, dict) else "[]" if isinstance(x, list) else ""
        parts.append(clean(k) + sub)
    return ", ".join(parts)


def describe_export(fp) -> str:
    """匯出檔結構摘要，給 Actions log 用：最外層鍵名＋第一筆職缺的鍵名（兩層）。只有名稱，沒有值。"""
    try:
        s = _JsonStream(fp)
        c = s.peek()
        if c == "[":
            return f"最外層=陣列；第一筆：{_names(next(s.array(), None))}"[:1500]
        if c != "{":
            return f"最外層既不是陣列也不是物件（{c!r}）"
        s.skip()
        keys, first = [], {}
        while s.peek() != "}":
            key = s.value()
            if s.peek() != ":":
                raise ValueError("malformed JSON object")
            s.skip()
            keys.append(re.sub(r"[^\w.$@:-]", "?", str(key))[:40])
            if s.peek() == "[":
                for item in s.array():
                    first.setdefault(key, item)
            else:
                s.value()
            if s.peek() == ",":
                s.skip()
        pick = next((first[k] for k in LIST_KEYS if k in first), next(iter(first.values()), None))
        return f"最外層鍵={keys}；第一筆：{_names(pick) if pick is not None else '（沒有陣列）'}"[:1500]
    except (ValueError, StopIteration) as e:
        return f"無法解析（{type(e).__name__}）"


def offer_id(raw: dict) -> str:
    """identifier.id 在 France Travail 來源是 null（swagger），退而用 partner_label:partner_job_id。"""
    ident = raw.get("identifier") if isinstance(raw.get("identifier"), dict) else {}
    if ident.get("id"):
        return str(ident["id"])
    if ident.get("partner_job_id"):
        return f"{ident.get('partner_label') or 'partner'}:{ident['partner_job_id']}"
    return ""


def city_dept(address: str) -> tuple[str, str]:
    """'12 rue X 75008 PARIS' -> ('PARIS', '75')；抓不到就 ('', '')。"""
    m = POSTAL.search((address or "").strip())
    return (re.sub(r"\s+", " ", m.group(2)).strip().title(), m.group(1)[:2]) if m else ("", "")


def parse_offer(raw, now: datetime, recent_days: int) -> tuple[dict | None, str]:
    """回傳 (job, 原因)：job 為 None 時原因是 malformed／old／expired／inactive。先做便宜的檢查再做文字處理。"""
    if not isinstance(raw, dict):
        return None, "malformed"
    oid, title = offer_id(raw), _g(raw, "offer", "title")
    url = _g(raw, "apply", "url")
    if not oid or not isinstance(title, str) or not title.strip() or not (isinstance(url, str) and url.startswith("https://")):
        return None, "malformed"
    created = parse_ts(_g(raw, "offer", "publication", "creation"))
    if created is None:
        return None, "malformed"
    if created < now - timedelta(days=recent_days):
        return None, "old"
    status = _g(raw, "offer", "status")  # swagger enum：Active／Filled／Cancelled；沒給就當作有效
    if isinstance(status, str) and status.strip() and status.strip().lower() != "active":
        return None, "inactive"
    exp_raw = _g(raw, "offer", "publication", "expiration")
    expires = parse_ts(exp_raw)
    if expires is not None and DATE_ONLY.match(exp_raw.strip()):
        expires += timedelta(days=1)  # 只有日期：當天整天有效，到隔天 00:00 UTC 才算過期
    if expires is not None and expires <= now:
        return None, "expired"
    address = _g(raw, "workplace", "location", "address")
    address = address if isinstance(address, str) else ""
    city, dept = city_dept(address)
    start = parse_ts(_g(raw, "contract", "start"))
    ctypes = _g(raw, "contract", "type")
    ctypes = ", ".join(str(t) for t in ctypes) if isinstance(ctypes, list) else str(ctypes or "")
    desc = _g(raw, "offer", "description")
    company = next((c for c in (_g(raw, "workplace", "name"), _g(raw, "workplace", "brand"),
                                _g(raw, "workplace", "legal_name")) if isinstance(c, str) and c.strip()), "")
    return {
        "uuid": oid, "url": url, "title": strip_tags(title), "company": strip_tags(company),
        "location": city or _cap(address, 60), "dept": dept,
        "employment_type": f"alternance（{ctypes or '未標明'}）" + (f"；預計 {start:%Y-%m} 開始" if start else ""),
        "start": f"{start:%Y-%m}" if start else "", "date_posted": created.date().isoformat(),
        "created": created, "text": strip_tags(BLOCK_END.sub(". ", desc) if isinstance(desc, str) else "")[:JD_MAX_CHARS],
    }, ""


# ---------------------------------------------------------------------------
# 關鍵字粗篩（法文描述，所以對 標題＋全文 比對；負面字只看標題，避免「溝通能力」之類的客套話誤殺）
# ---------------------------------------------------------------------------

class Keywords:
    def __init__(self, cfg: dict):
        self.pos = kw_regex(cfg["keywords_positive"] + cfg.get("keywords_positive_extra", []))
        self.title_only = kw_regex(cfg.get("keywords_title_only", [])) if cfg.get("keywords_title_only") else None
        self.neg = kw_regex(cfg["keywords_negative"])
        self.skills = kw_regex(cfg["keywords_skills"])
        self.priority_depts = set(cfg.get("priority_departments", []))

    def rank(self, job: dict) -> tuple[bool, int]:
        """(保留?, 排序分)。保留 = 標題有正面字，或內文有 >=2 個不同的正面字；
        而且不是『標題有負面字而標題沒有任何正面字』。title_only 的字（transformation、lean 這類在食品／醫療 JD 也常見）只算標題。"""
        title, text = norm(job["title"]), norm(job["text"])
        pos_title = set(self.pos.findall(title)) | (set(self.title_only.findall(title)) if self.title_only else set())
        pos_body = set(self.pos.findall(text))
        keep = (bool(pos_title) or len(pos_body) >= 2) and not (self.neg.search(title) and not pos_title)
        score = 2 * len(pos_body | pos_title) + 3 * len(pos_title) + len(set(self.skills.findall(title + " " + text)))
        return keep, score + (3 if job["dept"] in self.priority_depts else 0)


# ---------------------------------------------------------------------------
# 法文要求分級（LBA 專用）：沿用 jobteaser 的正規式，但改成「逐子句」判斷，並把語言程度字綁在 français／French 附近。
# jobteaser_monitor.classify_french 整段文字一起比，英文 JD 的「Anglais courant ou bilingue」會被當成 D。
# ---------------------------------------------------------------------------

CLAUSE_SPLIT = re.compile(r"[,;.!?\n•|]+")
FR_WORD = re.compile(r"(?<![a-z])(?:francais(?:e|es)?|french|francophone)(?![a-z])")
STANDALONE_LEVEL = re.compile(r"(?<![a-z])(?:bilingue|langue maternelle|niveau natif)(?![a-z])")
# 等級字直接黏在 français／French 後面（中間只允許「niveau／level／de niveau」）
LBA_D = re.compile(
    r"(?<![a-z])(?:francais(?:e)?|french) (?:de niveau |niveau |level )?(?:c1|c2)(?![a-z0-9])|"
    r"(?<![a-z0-9])(?:niveau |level )(?:c1|c2) (?:en |in |de )?(?:francais|french)|"
    r"maitrise (?:(?:parfaite|totale|complete|absolue|excellente|irreprochable) )?(?:du|de la|de la langue) (?:langue )?francais|"
    r"(?:excellente?|parfaite?|irreprochable) (?:[a-z]+ ){1,6}?(?:en|du|de la langue|de) (?:langue )?francais|"
    r"excellent (?:[a-z]+ ){1,4}?french(?![a-z])")
LBA_C = re.compile(
    r"(?<![a-z])(?:francais(?:e)?|french) (?:de niveau |niveau |level )?(?:b1|b2)(?![a-z0-9])|"
    r"(?<![a-z0-9])(?:niveau |level )(?:b1|b2) (?:en |in |de )?(?:francais|french)")


def _near_french(t: str, m: re.Match, span: int = 5) -> bool:
    before, after = " ".join(t[:m.start()].split()[-span:]), " ".join(t[m.end():].split()[:span])
    return bool(FR_WORD.search(before) or FR_WORD.search(after))


def classify_french_lba(text: str) -> str:
    """A 英文為主／B 法文加分／C B1-B2 或商務法文／D 流利以上（不推）／N 未標明。逐子句（逗號、句點、換行分隔）判斷：
    bilingue／langue maternelle／niveau natif 沒有 français／French 在 5 個字以內就忽略（那是在講別的語言）。"""
    flags = {"D": False, "C": False, "B": False, "none": False}
    for clause in CLAUSE_SPLIT.split(text or ""):
        t = norm(clause)
        if not t:
            continue
        t = STANDALONE_LEVEL.sub(lambda m: m.group(0) if _near_french(t, m) else " ", t)
        flags["none"] |= bool(FR_NONE.search(t))
        t = FR_NONE.sub(" ", t)
        flags["B"] |= bool(FR_B.search(t))
        t = FR_B.sub(" ", t)
        if FR_D.search(t) or LBA_D.search(t):
            flags["D"] = True
        elif FR_C.search(t) or LBA_C.search(t):
            flags["C"] = True
        elif FR_D_GENERIC.search(t):
            flags["D"] = True
    if flags["D"]:
        return "D"
    if flags["C"]:
        return "C"
    if flags["B"]:
        return "B"
    english = bool(EN_A.search(norm(text)))
    if flags["none"]:
        return "A" if english else "B"
    return "A" if english else "N"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def fit_entries(head: str, ents: list[str], max_msgs: int) -> int:
    """最多放得進 max_msgs 則訊息的 entry 數。"""
    k = len(ents)
    while k and len(pack(head, ents[:k])) > max_msgs:
        k -= 1
    return k


def run(args, now: datetime | None = None) -> int:
    cfg = load_config(Path(args.config))
    fixture = Path(args.fixture_dir) if args.fixture_dir else None
    if now is None and fixture and (fixture / "meta.json").exists():  # 離線 fixture 的日期是固定的，所以固定「現在」
        now = parse_ts(json.loads((fixture / "meta.json").read_text(encoding="utf-8")).get("now"))
    now = now or datetime.now(timezone.utc)
    api_key = os.environ.get("LBA_API_KEY", "").strip()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not args.dry_run and not (token and chat_id):
        print("未設定 TELEGRAM_BOT_TOKEN／TELEGRAM_CHAT_ID：不抓取、不寫狀態（可先用 --dry-run）。", file=sys.stderr)
        return 1 if os.environ.get("GITHUB_ACTIONS") else 0

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    model = os.environ.get("LBA_MODEL") or cfg.get("model") or "claude-sonnet-5-5"
    system = build_system(cfg)
    if fixture and not args.no_llm:
        llm = fixture_llm(fixture)
    elif anthropic_key and not args.no_llm:
        def llm(sys_prompt, prompt, oid):
            return call_claude(anthropic_key, model, sys_prompt, prompt)
    else:
        llm = None
    state_path = Path(os.environ.get("LBA_STATE_PATH") or STATE_PATH)
    state = load_state(state_path)
    prune_state(state, now, int(cfg.get("state_retention_days", 30)))
    seen = state["seen"]
    print(f"模式：{'LLM 評分' if llm else '無評分（只送一則摘要）'}；{'dry-run' if args.dry_run else 'Telegram 推播'}"
          f"{'；離線 fixture' if fixture else ''}")

    sent = {"n": 0}  # 本輪已送出的內容訊息數（警示不算）

    def emit(text: str, content: bool = True) -> bool:
        if args.dry_run:
            print("-" * 40 + f"\n{text}\n" + "-" * 40)
        else:
            try:
                send_telegram(token, chat_id, text)
            except Exception as e:  # noqa: BLE001 - 不印 URL（含 token）
                print(f"  Telegram 傳送失敗：{type(e).__name__}，不標記已見，下輪重試")
                return False
        sent["n"] += content
        return True

    def persist() -> None:
        if not args.dry_run:
            save_state(state_path, state)

    def record(job: dict, status: str, score=None) -> None:
        old = seen.get(job["uuid"], {})
        rec = {"score": score, "first_seen": old.get("first_seen", now.isoformat()), "title": _cap(job["title"], 120),
               "status": status}
        if status == "score_failed":
            rec["tries"] = old.get("tries", 0) + 1
        seen[job["uuid"]] = rec
        persist()

    def key_problem(status: int | None) -> int:
        if state["key_alert_sent"]:
            print("LBA API key 無效／未設定／無權限（已提醒過，不重複推播）。")
        else:
            why = ("未設定或格式不對" if status is None else f"被拒絕 (HTTP {status})")
            msg = (f"⚠️ La Bonne Alternance：LBA_API_KEY {why}。請到 api.apprentissage.beta.gouv.fr/compte/profil 確認 key，"
                   f"必要時更新 GitHub secret LBA_API_KEY。\nLBA_API_KEY missing/invalid/rejected; update the secret.")
            if emit(msg, content=False):
                state["key_alert_sent"] = True
                persist()
        return 0

    # --- 取得匯出檔 -------------------------------------------------------
    with tempfile.TemporaryFile() as tmp:
        if fixture:
            with open(fixture / "export.json", "rb") as f:
                shutil.copyfileobj(f, tmp)
        else:
            if not api_key:
                return key_problem(None)
            try:
                fetch_export(api_key, tmp, int(cfg.get("max_export_mb", 400)) * (1 << 20))
            except AuthError as e:
                return key_problem(e.status)
            except RateLimited:
                emit("⚠️ La Bonne Alternance 回 429（限流），本輪已停止請求，下次排程再試。\n"
                     "LBA API rate-limited (429); run stopped.", content=False)
                return 0
            except ExportError as e:
                print(f"匯出失敗：{e}")
                return 1
        if state["key_alert_sent"]:
            state["key_alert_sent"] = False  # key 恢復正常，下次失效才會再提醒
            persist()

        # --- 串流篩選：年齡／過期 -> 重複 -> 已見 -> 關鍵字 -> 法文 ----------------
        kw = Keywords(cfg)
        recent = int(cfg.get("recent_days", 7))
        counts = dict.fromkeys(("total", "recruiter", "malformed", "inactive", "old", "expired", "dup", "seen", "keyword",
                                "french_d"), 0)
        ids: set[str] = set()
        kept: list[tuple[int, datetime, dict]] = []
        tmp.seek(0)
        export_text = io.TextIOWrapper(tmp, encoding="utf-8")  # 留著參照：被回收時會連 tmp 一起關掉
        try:
            for raw in iter_offers(export_text):
                counts["total"] += 1
                if is_recruiter(raw):  # 沒有職缺的企業名單（candidature spontanée 用），不是職缺也不算資料不全
                    counts["recruiter"] += 1
                    continue
                job, why = parse_offer(raw, now, recent)
                if job is None:
                    counts[why] += 1
                    continue
                if job["uuid"] in ids:
                    counts["dup"] += 1
                    continue
                ids.add(job["uuid"])
                if is_seen(seen.get(job["uuid"])):
                    counts["seen"] += 1
                    continue
                keep, rank = kw.rank(job)
                if not keep:
                    counts["keyword"] += 1
                    continue
                job["fr"] = classify_french_lba(job["title"] + ". " + job["text"])
                if job["fr"] == "D":  # 規則式、不花錢，不寫進狀態（狀態只放處理過的）
                    counts["french_d"] += 1
                    continue
                kept.append((rank, job["created"], job))
        except ValueError as e:
            print(f"匯出檔解析失敗（{type(e).__name__}），本輪不處理、不寫狀態。")
            return 1
        eligible = counts["total"] - counts["recruiter"]
        if eligible == 0 or counts["malformed"] >= 0.9 * eligible:  # 欄位名稱／格式和預期差太多：靜默吞掉比報錯更糟
            export_text.seek(0)
            print(f"匯出結構異常：{counts['total']} 筆中 {counts['malformed']} 筆讀不出必要欄位"
                  f"（企業名單 {counts['recruiter']} 筆不計），本輪不處理、不寫狀態。")
            print(f"[診斷] 結構（只列欄位名稱）：{describe_export(export_text)}")
            emit("⚠️ La Bonne Alternance：匯出檔的欄位結構和程式預期不同，本輪沒有處理任何職缺。"
                 "請看 GitHub Actions log 裡的 [診斷] 結構 那一行。\n"
                 "LBA export structure differs from what the parser expects; nothing processed.", content=False)
            return 1
    kept.sort(key=lambda t: (-t[0], -t[1].timestamp()))  # 排序分高者先；同分較新者先
    max_new = args.max_new if args.max_new is not None else int(cfg.get("max_new_per_run", 30))
    cands = [j for _, _, j in kept[:max_new]]
    print(f"匯出 {counts['total']} 筆：資料不全 {counts['malformed']}、超過 {recent} 天 {counts['old']}、已過期 {counts['expired']}、"
          f"匯出內重複 {counts['dup']}、已見 {counts['seen']}、關鍵字丟棄 {counts['keyword']}、法文 D {counts['french_d']}；"
          f"候選 {len(kept)}、本輪處理 {len(cands)}（其餘留待下輪）")

    # --- 評分與推播 ---------------------------------------------------------
    msg_cap = int(cfg.get("max_messages_per_run", 10))
    push_cap = max(0, msg_cap - 2)  # 至少留 2 則給摘要
    stats = {"push": 0, "digest": 0, "low": 0, "score_failed": 0, "unscored": 0, "deferred": 0}
    digest: list[tuple[dict, dict]] = []
    for job in (cands if llm else []):
        res = score_job(job, system, llm)
        if res is None:
            record(job, "score_failed")
            stats["score_failed"] += 1
            continue
        res["contract"] = "excluded" if res["contract"] == "excluded" else "alternance"
        res["start_date"] = res["start_date"] or job["start"]
        action = route(res["total"], cfg)  # CDI／VIE 之類 parse_score 已把總分壓到 59 以下
        if action == "push":
            if sent["n"] >= push_cap:
                stats["deferred"] += 1  # 訊息數已到上限：不記為已見，明天再推
            elif emit(format_job(job, res)):
                record(job, "pushed", res["total"])
                stats["push"] += 1
        elif action == "digest":
            digest.append((job, res))
        else:
            record(job, "low", res["total"])
            stats["low"] += 1

    if digest:
        head = f"📋 <b>La Bonne Alternance 候選（{cfg['digest_min']}–{cfg['push_threshold'] - 1} 分）{len(digest)} 則</b>"
        ents = [format_digest_entry(j, r) for j, r in digest]
        k = fit_entries(head, ents, msg_cap - sent["n"])
        if k and all(emit(m) for m in pack(head, ents[:k])):
            for j, r in digest[:k]:
                record(j, "digest", r["total"])
            stats["digest"] = k
        stats["deferred"] += len(digest) - k
    if not llm and cands:
        head = (f"⚠️ <b>La Bonne Alternance 新職缺 {len(cands)} 則（未啟用 AI 評分／翻譯，僅依關鍵字粗篩，"
                f"依 JD 判定法文要求）</b>")
        ents = [f"{_e(_cap(j['title'], 100))} — {_e(_cap(j['company'], 40) or '—')}, {_e(_cap(j['location'], 40) or '—')}"
                f" · 法文 {j['fr']}\n{_link(j)}" for j in cands]
        k = fit_entries(head, ents, msg_cap - sent["n"])
        if k and all(emit(m) for m in pack(head, ents[:k])):
            for j in cands[:k]:
                record(j, "pushed_unscored")
            stats["unscored"] = k
        stats["deferred"] += len(cands) - k
    print(f"\n完成：{stats}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只印出訊息，不推播、不寫入狀態")
    parser.add_argument("--max-new", type=int, default=None, help="本輪最多評分／列入摘要幾則（預設用設定檔）")
    parser.add_argument("--fixture-dir", help="離線模式：從此資料夾讀 export.json、meta.json、llm_stub.json")
    parser.add_argument("--no-llm", action="store_true", help="不呼叫 Claude，只送一則未評分摘要")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="設定檔路徑")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
