#!/usr/bin/env python3
"""KEDGE JobTeaser 職缺推播：每天抓學校 JobTeaser（需登入）的實習／alternance 新職缺，
先用關鍵字粗篩、法文要求規則分級，再由 Claude 翻譯成繁中並打 0-100 分，
>=70 即時用 Telegram 推播，60-69 合併成一則摘要，<60 只記為已見。

用法：
    python src/jobteaser_monitor.py --dry-run --max-pages 1      # 只印出，不推播、不寫狀態
    python src/jobteaser_monitor.py --dry-run --fixture-dir tests/fixtures/jobteaser   # 離線（Claude 也用假資料）
    python src/jobteaser_monitor.py                              # 正式執行，需要環境變數
                                                                 # JOBTEASER_COOKIE、TELEGRAM_BOT_TOKEN、
                                                                 # TELEGRAM_CHAT_ID；ANTHROPIC_API_KEY 可選，
                                                                 # 沒有就只送未翻譯的標題＋連結

可選環境變數：JOBTEASER_MODEL（覆蓋模型 id）、JOBTEASER_STATE_PATH（覆蓋去重狀態檔路徑，測試用）。
cookie 只放在請求標頭，絕不寫入狀態檔、log 或 Telegram 訊息。
"""

import argparse
import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "jobteaser_config.json"
STATE_PATH = ROOT / "data" / "seen_jobteaser.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 30
MIN_DELAY_S = 1.5  # 對學校網站客氣一點：設定檔寫得再小也不會低於這個值
TG_LIMIT = 4096
STATE_RETENTION_DAYS = 180
MAX_SCORE_TRIES = 3  # 評分失敗最多重試幾輪，之後視為已見，避免永遠卡在同一則
JD_MAX_CHARS = 6000
JOB_LINK = re.compile(r"job-offers/([0-9a-f-]{36})[^\"'\s<?#\\]*")
SIGN_IN = "/users/sign_in"
EXCLUDED_CONTRACT = re.compile(r"(?<![a-z])(?:excluded|cdi|v\.?i\.?e|permanent|graduate|volontariat)")
SIGN_IN_FORM = re.compile(r"""(?is)<input[^>]+type\s*=\s*["']?password|<form[^>]+action\s*=\s*["'][^"']*sign_in""")
MAX_REDIRECTS = 5
MAX_PAGES_CAP = 50  # 網站最多顯示 50 頁；--max-pages／設定檔再大也不會超過

# (欄位, 訊息標籤, 滿分)；總分 = 七項加總（由程式加，不信任模型自報的 total）
SUBSCORES = (("functional", "功能", 30), ("career", "方向", 20), ("industry", "產業", 15),
             ("skills", "技能", 10), ("language", "語言", 15), ("international", "國際", 5),
             ("location", "地點", 5))
FR_LABEL = {"A": "A 英文為主", "B": "B 法文加分", "C": "C 需 B1-B2／商務法文", "D": "D 需流利法文",
            "N": "N 未標明"}
CONTRACT_LABEL = {"internship": "實習", "alternance": "Alternance"}


class CookieExpired(Exception):
    """status 是觸發的 HTTP 狀態碼（導向登入頁時是 3xx／200）；403 可能只是被擋，不一定過期。"""

    def __init__(self, status: int | None = None):
        super().__init__(status)
        self.status = status


class RateLimited(Exception):
    pass


# ---------------------------------------------------------------------------
# 設定、狀態
# ---------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_state(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data = data if isinstance(data, dict) else {}
    seen = data.get("seen")
    return {"seen": seen if isinstance(seen, dict) else {}, "cookie_alert_sent": bool(data.get("cookie_alert_sent"))}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def prune_state(state: dict, now: datetime, days: int = STATE_RETENTION_DAYS) -> None:
    cutoff = (now - timedelta(days=days)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items()
                     if isinstance(v, dict) and str(v.get("first_seen", "")) >= cutoff}


def is_seen(rec: dict | None) -> bool:
    """score_failed 還沒用完重試次數就視為未見，下一輪再評一次。"""
    if not rec:
        return False
    return rec.get("status") != "score_failed" or rec.get("tries", 1) >= MAX_SCORE_TRIES


# ---------------------------------------------------------------------------
# 文字工具與關鍵字
# ---------------------------------------------------------------------------

def strip_tags(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def norm(text: str) -> str:
    """小寫、去重音、標點轉空白（保留 & + #，讓 S&OP、FP&A 能比對）。"""
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9&+#]+", " ", s).strip()


def kw_regex(words: list[str], amp_as_space: bool = False) -> re.Pattern:
    """amp_as_space：網址 slug 會把 fp&a、s&op 變成「fp a」「s op」，所以另外收一份 & 換成空白的寫法。"""
    forms = {norm(w) for w in words if norm(w)}
    if amp_as_space:
        forms |= {f.replace("&", " ") for f in forms if "&" in f}
    alts = sorted((re.escape(f) for f in forms), key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(alts) + r")(?![a-z0-9])")


def slug_text(path: str) -> str:
    return re.sub(r"^.*?job-offers/[0-9a-f-]{36}-?", "", path).replace("-", " ")


def prefilter(slug: str, cfg: dict) -> tuple[bool, int]:
    """(保留?, 排序分)。負面字只在『沒有任何強正面字』時才丟；排序分 = 2×正面字 + 技能字。"""
    text = norm(slug)
    pos = set(kw_regex(cfg["keywords_positive"], amp_as_space=True).findall(text))
    neg = kw_regex(cfg["keywords_negative"], amp_as_space=True).search(text)
    skills = set(kw_regex(cfg["keywords_skills"], amp_as_space=True).findall(text))
    return (not neg or bool(pos)), 2 * len(pos) + len(skills)


# ---------------------------------------------------------------------------
# 法文要求分級（規則式，不靠 LLM）
# 注意：JD 本身用法文寫不等於要求法文，必須有明確等級用語才算 C/D。
# ---------------------------------------------------------------------------

_FR = r"(?:french|francais(?:e)?)"
_NEED = r"(?:required|mandatory|necessary|needed|expected|essential|exige|obligatoire|requis|necessaire|indispensable)"
# 明講「不需要法文」：有英文要求歸 A，沒有歸 B。先於其他規則拿掉，免得「no French required」被當成 D
FR_NONE = re.compile(
    rf"no {_FR}(?: language)?(?: skills?| knowledge| proficiency)?(?: is| are)? {_NEED}|"
    rf"{_FR}(?: language)?(?: skills?)? (?:is |are |est |sera )?(?:not |n est pas |non )(?:a )?(?:requirement|{_NEED})|"
    rf"without (?:any )?(?:knowledge of )?(?:the )?{_FR}|"
    rf"pas besoin (?:de |d |du )?(?:parler |connaitre |maitriser )?(?:le |la langue )?{_FR}|"
    rf"aucune? (?:connaissance|competence|maitrise)s? (?:du |en |de la langue )?(?:le )?{_FR}(?: n est| est)? {_NEED}|"
    rf"sans (?:connaissance (?:du |de la langue )?)?{_FR}")
FR_B = re.compile(
    rf"{_FR} (?:is |would be |est |serait )?(?:a |an |un |une )?(?:plus|asset|nice to have|appreciated|beneficial|advantage|atout|apprecie|"
    rf"preferred|desirable|desired|souhaite|bienvenu)|"
    rf"(?:knowledge|connaissance|notions?) (?:of |du |de )?(?:the )?{_FR}(?: is| est)? (?:a |un )?(?:plus|atout|apprecie)|notions? de francais")
FR_D = re.compile(
    rf"(?:fluent|native|perfect|excellent|full|complete) (?:in |command of |mastery of |level of )?(?:the )?{_FR}|"
    rf"{_FR} (?:is |est )?(?:fluent|native)|"
    rf"(?:fluent|proficient) (?:in )?(?:both )?(?:english and french|french and english)|"
    rf"(?:english and french|french and english) (?:are )?(?:required|mandatory)|"
    rf"{_FR}(?: language)?(?: and english)?(?: language)? skills? (?:are |is )?(?:mandatory|required|essential)|"
    rf"english and {_FR}(?: language)? skills? (?:are )?(?:mandatory|required|essential)|"
    rf"mother tongue french|french (?:as )?(?:a )?mother tongue|bilingual.{{0,25}}french|french.{{0,25}}bilingual|{_FR} (?:level )?(?:c1|c2)|(?:c1|c2) (?:level in|level|in) {_FR}|"
    rf"francais courant|courant en francais|francais et anglais courants?|anglais et francais courants?|"
    rf"(?:parle|parler) couramment (?:le )?francais|francais couramment|bilingue|langue maternelle|niveau natif|francais natif|"
    rf"(?:parfaite|excellente|totale) maitrise (?:du|de la langue) francais|excellent niveau (?:de|en) francais")
# 「strong French and English skills」這種沒有等級字的寫法判 C；一旦帶 required／mandatory／essential 就由上面的 FR_D 先接走
FR_C = re.compile(
    rf"{_FR} (?:level )?(?:b1|b2)|(?:b1|b2) (?:level )?(?:in |en )?{_FR}|"
    rf"(?:professional|working|business|intermediate|good|strong|solid|conversational)(?: working)?(?: level| proficiency)?(?: in| of)? {_FR}|"
    rf"(?:strong|good|solid) english and {_FR}(?: language)? skills?|"
    rf"{_FR} (?:professional|working proficiency|intermediate)|(?:good|strong) (?:command|knowledge) of (?:the )?{_FR}|"
    rf"francais (?:professionnel|des affaires)|bon niveau (?:de|en) francais|bonne maitrise du francais")
# 沒有等級修飾的「必須會法文」：排在 C 之後判斷，讓「bon niveau de français exigé」歸 C
FR_D_GENERIC = re.compile(
    rf"maitrise (?:du|de la langue) francais|{_FR} (?:is |est )?(?:mandatory|required|essential|a must|exige|obligatoire|indispensable)")
EN_A = re.compile(
    r"english (?:is )?(?:required|mandatory|essential|a must)|fluent (?:in )?english|english fluent|"
    r"working language(?: is)?(?: in)? english|english (?:is )?the working language|business language(?: is)? english|"
    r"international (?:environment|team|context)|english speaking|anglophone|"
    r"anglais (?:courant|exige|obligatoire|requis|professionnel)|courant en anglais|langue de travail (?:est )?l anglais|"
    r"environnement international|equipe internationale")


def classify_french(text: str) -> str:
    """A 英文為主／B 法文加分／C B1-B2 或商務法文／D 流利以上（不推）／N 未標明等級。"""
    t = norm(text)
    has_none = bool(FR_NONE.search(t))
    t = FR_NONE.sub(" ", t)  # 先拿掉「不需要法文」，否則 no French required 會被當成 French required
    has_b = bool(FR_B.search(t))
    t = FR_B.sub(" ", t)  # 再拿掉「加分」的句子，免得 french is a plus 被誤判
    if FR_D.search(t):
        return "D"
    if FR_C.search(t):
        return "C"
    if FR_D_GENERIC.search(t):
        return "D"
    if has_b:
        return "B"
    if has_none:
        return "A" if EN_A.search(t) else "B"
    return "A" if EN_A.search(t) else "N"


# ---------------------------------------------------------------------------
# 抓取：Live（帶 cookie）與 Fixture（離線）共用同一介面 list_page(n) / detail(path)
# ---------------------------------------------------------------------------

def extract_links(page_html: str) -> list[tuple[str, str]]:
    """列表頁 HTML -> [(uuid, path)]，依出現順序、以 uuid 去重。刻意不解析卡片 DOM。"""
    out, seen = [], set()
    for m in JOB_LINK.finditer(page_html or ""):
        uuid = m.group(1)
        if uuid not in seen:
            seen.add(uuid)
            out.append((uuid, "/en/" + m.group(0)))
    return out


def check_list_html(page_html: str, links: list) -> None:
    """只在第一頁用：沒有任何職缺連結而且長得像登入頁（密碼欄位或 sign_in 表單）才算過期。
    不看單純的 "sign_in" 字串（正常頁面的導覽列也可能有登出／登入連結）。"""
    if not links and SIGN_IN_FORM.search(page_html or ""):
        raise CookieExpired


def diag_line(resp) -> str:
    """被擋時留一行診斷到 log：只含狀態碼、路徑（不含 query）、Server／cf-ray 標頭與頁面 <title>；絕不含 cookie。
    任何屬性取不到都不能讓診斷本身出錯。"""
    try:
        def short(u: str) -> str:
            sp = urlsplit(u)
            return f"{sp.netloc}{sp.path}"
        hdr = getattr(resp, "headers", None) or {}
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", getattr(resp, "text", "") or "")
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:80] if m else "-"
        return (f"[診斷] HTTP {resp.status_code} {short(getattr(resp, 'url', '') or '')} → {short(hdr.get('Location') or '-')}"
                f"｜server={hdr.get('Server', '-')}｜cf-ray={'有' if hdr.get('CF-RAY') else '無'}｜title={title}")
    except Exception as e:  # noqa: BLE001
        return f"[診斷] 無法產生（{type(e).__name__}）"


class LiveSource:
    def __init__(self, cookie: str, cfg: dict):
        self.cookie, self.cfg = cookie, cfg
        self.delay = max(MIN_DELAY_S, float(cfg.get("request_delay_s", MIN_DELAY_S)))
        self.requests_made = 0

    def _get(self, url: str) -> str:
        """requests 跟著轉址時會丟掉手動設的 Cookie 標頭，所以自己跟：只跟同主機的轉址並每一跳重送 cookie；
        轉去登入頁或別的主機一律視為 cookie 失效，且絕不把 cookie 送去別的主機。"""
        origin = urlsplit(self.cfg["base_url"])[:2]
        for _ in range(MAX_REDIRECTS + 1):
            if self.requests_made:
                time.sleep(self.delay)
            self.requests_made += 1
            resp = requests.get(url, headers={"User-Agent": UA, "Cookie": self.cookie, "Accept": "text/html"},
                                timeout=TIMEOUT, allow_redirects=False)
            if resp.status_code == 429:
                raise RateLimited
            if resp.status_code in (401, 403) or SIGN_IN in resp.url:
                print(diag_line(resp))
                raise CookieExpired(resp.status_code)
            if resp.status_code in (301, 302, 303, 307, 308):
                target = urljoin(url, resp.headers.get("Location") or "")
                if SIGN_IN in target or urlsplit(target)[:2] != origin:
                    print(diag_line(resp))
                    raise CookieExpired(resp.status_code)
                url = target
                continue
            resp.raise_for_status()
            return resp.text
        raise RuntimeError("too many redirects")

    def list_page(self, n: int) -> str:
        params = [(k, v) for k, vals in self.cfg["list_params"].items()
                  for v in (vals if isinstance(vals, list) else [vals])] + [("page", n)]
        return self._get(f"{self.cfg['base_url']}{self.cfg['list_path']}?{urlencode(params)}")

    def detail(self, path: str) -> str:
        return self._get(self.cfg["base_url"] + path)


class FixtureSource:
    """離線模式：list_<n>.html 與 <uuid>.html；缺檔的列表頁視為空頁（分頁到此結束）。"""

    def __init__(self, folder: Path):
        self.folder = folder

    def list_page(self, n: int) -> str:
        p = self.folder / f"list_{n}.html"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def detail(self, path: str) -> str:
        return (self.folder / f"{JOB_LINK.search(path).group(1)}.html").read_text(encoding="utf-8")


def collect_links(source, max_pages: int) -> tuple[list[tuple[str, str]], bool]:
    """依序抓列表頁；某頁沒有任何本輪新連結就提早停。回傳 (連結, 是否被 429 中斷)。
    限制：整頁都和前面重複就停，若網站依相關度排序而頁與頁之間有重疊，後面頁的新職缺可能被漏掉。
    空的第 2 頁以後只是分頁結束，已收集的連結照留；只有第 1 頁才檢查是不是被導向登入頁。"""
    links: list[tuple[str, str]] = []
    known: set[str] = set()
    for n in range(1, max_pages + 1):
        try:
            page = source.list_page(n)
        except RateLimited:
            return links, True
        except CookieExpired:
            raise
        except Exception as e:  # noqa: BLE001 - 不印 URL／標頭，避免洩漏
            print(f"  列表第 {n} 頁失敗（{type(e).__name__}），以已抓到的為準")
            break
        found = extract_links(page)
        if n == 1:
            check_list_html(page, found)
        fresh = [(u, p) for u, p in found if u not in known]
        print(f"  列表第 {n} 頁：{len(found)} 則連結，新 {len(fresh)}")
        if not fresh:
            break
        known.update(u for u, _ in fresh)
        links += fresh
    return links, False


# ---------------------------------------------------------------------------
# 職缺詳情（JSON-LD JobPosting，缺欄位一律容忍）
# ---------------------------------------------------------------------------

def _s(v) -> str:
    """JSON-LD 值可能是字串、物件（取 name）或清單；統一轉成字串。"""
    if isinstance(v, str):
        return strip_tags(v)
    if isinstance(v, dict):
        return _s(v.get("name") or v.get("addressLocality") or "")
    if isinstance(v, list):
        return ", ".join(x for x in (_s(i) for i in v) if x)
    return ""


def _jobposting(page_html: str) -> dict:
    for m in re.finditer(r'(?is)<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page_html):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for node in nodes:
            if isinstance(node, dict) and "JobPosting" in str(node.get("@type")):
                return node
    return {}


def parse_detail(page_html: str, uuid: str, path: str, base_url: str) -> dict:
    jp = _jobposting(page_html)
    h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", page_html)
    desc = jp.get("description") if isinstance(jp.get("description"), str) else ""
    if "&lt;" in desc:
        desc = html.unescape(desc)
    text = strip_tags(desc)
    if not text:  # 沒有 JSON-LD 內文：退回 meta description＋整頁文字
        meta = re.search(r'(?is)<meta[^>]+name="description"[^>]+content="([^"]*)"', page_html)
        text = strip_tags(f"{meta.group(1) if meta else ''} {page_html}")
    loc = jp.get("jobLocation")
    loc = loc[0] if isinstance(loc, list) and loc else loc
    addr = loc.get("address") if isinstance(loc, dict) else None
    return {
        "uuid": uuid, "path": path, "url": base_url + path,
        "title": _s(jp.get("title")) or (strip_tags(h1.group(1)) if h1 else "") or slug_text(path),
        "company": _s(jp.get("hiringOrganization")),
        "location": _s(addr if isinstance(addr, dict) and addr.get("addressLocality") else loc),
        "employment_type": _s(jp.get("employmentType")),
        "date_posted": _s(jp.get("datePosted")),
        "text": text[:JD_MAX_CHARS],
    }


# ---------------------------------------------------------------------------
# LLM 評分
# ---------------------------------------------------------------------------

def build_system(cfg: dict) -> str:
    return ("You score internship / alternance job postings for one candidate. Candidate profile:\n"
            + "\n".join(cfg["persona"]) + "\n\n" + "\n".join(cfg["rubric"]) +
            "\n\nReturn ONLY one JSON object, no markdown fences, no other text.")


def build_prompt(job: dict) -> str:
    schema = ('{"functional":0-30,"career":0-20,"industry":0-15,"skills":0-10,"language":0-15,'
              '"international":0-5,"location":0-5,"reason_zh":"<=60字繁體中文","title_zh":"繁體中文職稱",'
              '"summary_zh":"2-3行繁體中文摘要，用換行分隔","start_date":"YYYY-MM 或空字串",'
              '"contract":"internship|alternance|excluded|unknown"}')
    meta = {k: job.get(k) or "" for k in ("title", "company", "location", "employment_type", "date_posted")}
    return (f"職缺資料：{json.dumps(meta, ensure_ascii=False)}\n"
            f"規則式法文要求分級（A/B/C/N，D 已被濾掉）：{job['fr']}\n\n"
            f"JD 全文：\n{job['text']}\n\n只回傳符合此結構的 JSON：\n{schema}")


def call_claude(api_key: str, model: str, system: str, prompt: str) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": model, "max_tokens": 1500, "system": system,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    resp.raise_for_status()
    return "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")


def fixture_llm(folder: Path):
    """離線縫隙：<fixture-dir>/llm_stub.json 把 uuid 對到固定回應（dict 或原樣字串，後者可模擬壞 JSON）。"""
    stub = json.loads((folder / "llm_stub.json").read_text(encoding="utf-8"))

    def llm(system: str, prompt: str, uuid: str) -> str:
        v = stub.get(uuid, {})
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return llm


def _clip(v, n: int) -> str:
    s = str(v).strip() if v is not None else ""
    return "" if s.lower() == "null" else s[:n]


def parse_score(text: str) -> dict | None:
    """解析並夾限 LLM 回傳；缺任何分項或不是 JSON 回傳 None。總分由程式加總。"""
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        try:
            data = json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    sub = {}
    for key, _, top in SUBSCORES:
        try:
            sub[key] = max(0, min(top, round(float(data[key]))))
        except (KeyError, TypeError, ValueError):
            return None
    contract = _clip(data.get("contract"), 20).lower()
    total = sum(sub.values())
    if EXCLUDED_CONTRACT.search(contract):  # CDI／Graduate／VIE：不推；模型常不照列舉值回，所以認各種寫法
        contract = "excluded"
        total = min(total, 59)
    return {"sub": sub, "total": total, "contract": contract, "reason_zh": _clip(data.get("reason_zh"), 120),
            "title_zh": _clip(data.get("title_zh"), 120), "summary_zh": _clip(data.get("summary_zh"), 400),
            "start_date": _clip(data.get("start_date"), 30)}


def score_job(job: dict, system: str, llm) -> dict | None:
    """呼叫一次，壞 JSON 或呼叫失敗再試一次；仍失敗回傳 None（呼叫端標 score_failed）。"""
    prompt = build_prompt(job)
    for _ in range(2):
        try:
            res = parse_score(llm(system, prompt, job["uuid"]))
        except Exception as e:  # noqa: BLE001 - 不印 response body／標頭
            print(f"  LLM 呼叫失敗（{type(e).__name__}）")
            continue
        if res:
            return res
    return None


def route(total: int, cfg: dict) -> str:
    if total >= cfg["push_threshold"]:
        return "push"
    return "digest" if total >= cfg["digest_min"] else "low"


# ---------------------------------------------------------------------------
# 訊息組裝與 Telegram
# ---------------------------------------------------------------------------

def _e(s) -> str:
    return html.escape(str(s), quote=True).replace("&#x27;", "&#39;")


def _cap(s, n: int) -> str:
    s = re.sub(r"[ \t]+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _link(job: dict, label: str = "查看職缺") -> str:
    return f'<a href="{_e(job["url"])}">{_e(label)}</a>'


def fit_lines(lines: list[str], limit: int = TG_LIMIT) -> str:
    """每行自帶完整標籤；太長就從中間挑最長的行刪掉，保留首行（標題）與末行（連結）。"""
    lines = list(lines)
    while len("\n".join(lines)) > limit and len(lines) > 2:
        del lines[max(range(1, len(lines) - 1), key=lambda i: len(lines[i]))]
    return "\n".join(lines)[:limit]


def format_job(job: dict, res: dict) -> str:
    sub = " · ".join(f"{label} {res['sub'][k]}/{top}" for k, label, top in SUBSCORES)
    contract = CONTRACT_LABEL.get(res["contract"]) or _cap(job["employment_type"], 30) or "—"
    lines = [
        f"🎯 <b>{res['total']}</b> · <b>{_e(_cap(res['title_zh'] or job['title'], 120))}</b>",
        f"原文：{_e(_cap(job['title'], 120))}",
        f"🏢 {_e(_cap(job['company'], 60) or '—')} · 📍 {_e(_cap(job['location'], 60) or '—')} · {_e(contract)}",
        f"🗓 開始：{_e(res['start_date'] or '未標明')} · 🇫🇷 法文要求：{FR_LABEL[job['fr']]}",
        f"分項：{sub}",
        _e(_cap(res["summary_zh"], 400).replace("\\n", "\n")) if res["summary_zh"] else "",
        f"💡 {_e(_cap(res['reason_zh'], 120))}" if res["reason_zh"] else "",
        _link(job),
    ]
    return fit_lines([ln for ln in lines if ln])


def pack(head: str, entries: list[str], limit: int = TG_LIMIT) -> list[str]:
    """把多則 entry 依序塞進訊息，每則訊息 <= limit；entry 本身都已限長，不會被腰斬。"""
    msgs, cur = [], head
    for ent in entries:
        if len(cur) + len(ent) + 2 > limit:
            msgs.append(cur)
            cur = head
        cur += "\n\n" + ent
    msgs.append(cur)
    return msgs


def format_digest_entry(job: dict, res: dict) -> str:
    return (f"<b>{res['total']}</b> · {_e(_cap(res['title_zh'] or job['title'], 100))} — "
            f"{_e(_cap(job['company'], 40) or '—')}, {_e(_cap(job['location'], 40) or '—')} · 法文 {job['fr']}\n"
            f"{_e(_cap(res['reason_zh'], 100))}\n{_link(job)}")


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

def run(args, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    cfg = load_config(Path(args.config))
    fixture = Path(args.fixture_dir) if args.fixture_dir else None
    cookie = os.environ.get("JOBTEASER_COOKIE", "").strip()
    if not fixture and not cookie:
        print("未設定 JOBTEASER_COOKIE：不抓取、不推播。請把 cookie 放進環境變數／GitHub secret。"
              "（JOBTEASER_COOKIE is not set; nothing fetched, nothing sent.）")
        return 0
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not args.dry_run and not (token and chat_id):
        print("未設定 TELEGRAM_BOT_TOKEN／TELEGRAM_CHAT_ID：不抓取、不寫狀態（可先用 --dry-run）。", file=sys.stderr)
        return 1 if os.environ.get("GITHUB_ACTIONS") else 0

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    model = os.environ.get("JOBTEASER_MODEL") or cfg.get("model") or "claude-sonnet-5-5"
    system = build_system(cfg)
    if fixture and not args.no_llm:
        llm = fixture_llm(fixture)
    elif api_key and not args.no_llm:
        def llm(sys_prompt, prompt, uuid):
            return call_claude(api_key, model, sys_prompt, prompt)
    else:
        llm = None
    source = FixtureSource(fixture) if fixture else LiveSource(cookie, cfg)
    state_path = Path(os.environ.get("JOBTEASER_STATE_PATH") or STATE_PATH)
    state = load_state(state_path)
    prune_state(state, now)
    seen = state["seen"]
    print(f"模式：{'LLM 評分' if llm else '無評分（只送標題＋連結）'}；{'dry-run' if args.dry_run else 'Telegram 推播'}"
          f"{'；離線 fixture' if fixture else ''}")

    def emit(text: str) -> bool:
        if args.dry_run:
            print("-" * 40 + f"\n{text}\n" + "-" * 40)
            return True
        try:
            send_telegram(token, chat_id, text)
            return True
        except Exception as e:  # noqa: BLE001 - 不印 URL（含 token）
            print(f"  Telegram 傳送失敗：{type(e).__name__}，不標記已見，下輪重試")
            return False

    def persist() -> None:
        if not args.dry_run:
            save_state(state_path, state)

    def record(job: dict, status: str, score=None, **extra) -> None:
        rec = {"score": score, "first_seen": now.isoformat(), "title": _cap(job["title"], 120), "status": status}
        old = seen.get(job["uuid"], {})
        rec["first_seen"] = old.get("first_seen", rec["first_seen"])
        if status == "score_failed":
            rec["tries"] = old.get("tries", 0) + 1
        seen[job["uuid"]] = rec | extra
        persist()

    def cookie_expired(status: int | None = None) -> int:
        if state["cookie_alert_sent"]:
            print("JobTeaser cookie 已過期或被擋（已提醒過，不重複推播）。")
        else:
            if status == 403:  # 403 不一定是過期，也可能是 Cloudflare／IP 封鎖，不要斷言過期
                msg = ("⚠️ KEDGE JobTeaser：cookie 可能過期，或被網站/Cloudflare 擋下 (HTTP 403)。"
                       "請先確認 cookie，必要時更新 GitHub secret JOBTEASER_COOKIE。\n"
                       "JobTeaser: cookie may have expired, or the request was blocked (HTTP 403).")
            else:
                msg = ("⚠️ KEDGE JobTeaser cookie 已過期，請更新 GitHub secret JOBTEASER_COOKIE。\n"
                       "JobTeaser cookie expired, please update the JOBTEASER_COOKIE secret.")
            if emit(msg):
                state["cookie_alert_sent"] = True
                persist()
        return 0

    try:
        links, halted = collect_links(source, min(args.max_pages or cfg["max_pages"], MAX_PAGES_CAP))
    except CookieExpired as e:
        return cookie_expired(e.status)
    if state["cookie_alert_sent"]:
        state["cookie_alert_sent"] = False  # cookie 恢復正常，下次過期才會再提醒
        persist()

    new = [(u, p) for u, p in links if not is_seen(seen.get(u))]
    kept, dropped = [], 0
    for uuid, path in new:
        keep, rank = prefilter(slug_text(path), cfg)
        if keep:
            kept.append((rank, uuid, path))
            continue
        dropped += 1  # 明顯不相關：記為已見但不花詳情請求／LLM
        record({"uuid": uuid, "title": slug_text(path)}, "prefiltered")
    kept.sort(key=lambda t: -t[0])  # sorted 穩定：同分維持列表順序
    max_new = args.max_new if args.max_new is not None else cfg["max_new_per_run"]
    cands = kept[:max_new]
    print(f"連結 {len(links)}、未見 {len(new)}、粗篩丟棄 {dropped}、本輪處理 {len(cands)}"
          f"（其餘 {len(kept) - len(cands)} 則留待下輪）")

    stats = {"push": 0, "digest": 0, "low": 0, "filtered_french": 0, "score_failed": 0, "unscored": 0}
    digest: list[tuple[dict, dict]] = []
    plain: list[dict] = []
    for _, uuid, path in cands:
        try:
            job = parse_detail(source.detail(path), uuid, path, cfg["base_url"])
        except RateLimited:
            halted = True
            break
        except CookieExpired as e:
            return cookie_expired(e.status)
        except Exception as e:  # noqa: BLE001
            print(f"  詳情失敗，跳過（{type(e).__name__}）：{slug_text(path)[:60]}")
            continue
        job["fr"] = classify_french(job["text"])
        if job["fr"] == "D":
            record(job, "filtered_french")
            stats["filtered_french"] += 1
            continue
        if llm is None:
            if kw_regex(cfg["keywords_positive"]).search(norm(job["title"] + " " + job["text"])):
                plain.append(job)
            else:
                record(job, "prefiltered")
            continue
        res = score_job(job, system, llm)
        if res is None:
            record(job, "score_failed")
            stats["score_failed"] += 1
            continue
        action = route(res["total"], cfg)
        if action == "push":
            if emit(format_job(job, res)):
                record(job, "pushed", res["total"])
                stats["push"] += 1
        elif action == "digest":
            digest.append((job, res))
        else:
            record(job, "low", res["total"])
            stats["low"] += 1

    if digest:
        head = f"📋 <b>JobTeaser 候選（{cfg['digest_min']}–{cfg['push_threshold'] - 1} 分）{len(digest)} 則</b>"
        if all(emit(m) for m in pack(head, [format_digest_entry(j, r) for j, r in digest])):
            for j, r in digest:
                record(j, "digest", r["total"])
            stats["digest"] = len(digest)
    if plain:
        head = (f"⚠️ <b>JobTeaser 新職缺 {len(plain)} 則（未啟用 AI 評分／翻譯，僅依關鍵字粗篩，"
                f"依 JD 判定法文要求）</b>")
        ents = [f"{_e(_cap(j['title'], 100))} — {_e(_cap(j['company'], 40) or '—')} · 法文 {j['fr']}\n{_link(j)}"
                for j in plain]
        if all(emit(m) for m in pack(head, ents)):
            for j in plain:
                record(j, "pushed_unscored")
            stats["unscored"] = len(plain)
    if halted:
        emit("⚠️ JobTeaser 回 429（限流），本輪已停止請求，下次排程再試。\nJobTeaser rate-limited (429); run stopped.")
    print(f"\n完成：{stats}" + ("；因 429 提前結束" if halted else ""))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只印出訊息，不推播、不寫入狀態")
    parser.add_argument("--max-pages", type=int, default=0, help="最多抓幾頁列表（預設用設定檔）")
    parser.add_argument("--max-new", type=int, default=None, help="本輪最多抓詳情＋評分幾則（預設用設定檔）")
    parser.add_argument("--fixture-dir", help="離線模式：從此資料夾讀 list_<n>.html、<uuid>.html、llm_stub.json")
    parser.add_argument("--no-llm", action="store_true", help="不呼叫 Claude，只送標題＋連結")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="設定檔路徑")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
