#!/usr/bin/env python3
"""IG 收藏分類工具。

把 Instagram 官方資料匯出（Download your information）裡的「已儲存的內容」
讀進來，盡量補上公開貼文的文字內容，用 Claude 分成三大類 + 子標籤，
再同步到 Notion database，最後把整體分析寫成 Notion 頁面。

用法:
    python src/ig_curator.py --dry-run          # 不碰 Notion，只跑解析+分類，印出結果
    python src/ig_curator.py --no-enrich        # 跳過抓取貼文內容（快，但分類會很差）
    python src/ig_curator.py --limit 20         # 只處理前 20 筆，試跑用
    python src/ig_curator.py                    # 完整跑：解析 → 補內容 → 分類 → 同步 Notion

環境變數:
    ANTHROPIC_API_KEY   必要，分類與 summary 用
    NOTION_TOKEN        同步 Notion 時必要（ntn_ 開頭的 internal integration token）
    NOTION_PARENT_PAGE  同步 Notion 時必要，資料庫要建在哪一頁底下（page id）
    NOTION_DATABASE_ID  可選；第一次跑完會印出來，之後設起來就不會重建資料庫
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "ig_config.json"

# IG 對短 UA 會擋，用完整瀏覽器 UA（與 daily_jobs.py 同一個理由）
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

CONTENT_OK = "已抓到"
CONTENT_MISSING = "需人工補"


# --------------------------------------------------------------------------
# 設定與狀態
# --------------------------------------------------------------------------

def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    """已分類過的結果，用貼文網址當 key。避免重跑重複付費。"""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    return raw.get("posts", {})


def save_state(path: Path, posts: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "posts": posts,
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 1. 解析 IG 匯出檔
# --------------------------------------------------------------------------

def _walk_for_saved_items(node: Any) -> list[dict[str, Any]]:
    """在匯出的 JSON 裡找出「一筆收藏」長什麼樣的 dict。

    IG 匯出格式改過好幾版，頂層 key 和標籤字串（"Saved on"）都可能不一樣，
    帳號語言不是英文時標籤還會被在地化。所以不比對任何字串，
    改成找結構特徵：一個帶 string_map_data 的 dict，且裡面某個值有 href。
    """
    found: list[dict[str, Any]] = []

    if isinstance(node, dict):
        smd = node.get("string_map_data")
        if isinstance(smd, dict) and any(
            isinstance(v, dict) and v.get("href") for v in smd.values()
        ):
            found.append(node)
        else:
            for value in node.values():
                found.extend(_walk_for_saved_items(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_walk_for_saved_items(value))

    return found


_DATE_FORMATS = ("%b %d, %Y %I:%M %p", "%b %d, %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def _parse_saved_date(entry: dict[str, Any]) -> str | None:
    """收藏時間有兩種寫法：unix int 的 timestamp，或 value 裡的人類可讀日期。

    2025 之後的匯出都是前者，但 2023/2024 的檔案只有後者，所以兩種都吃。
    """
    ts = entry.get("timestamp")
    if isinstance(ts, int) and ts > 0:
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()

    raw = entry.get("value")
    if isinstance(raw, str) and raw.strip():
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(raw.strip(), fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _first_href_and_date(item: dict[str, Any]) -> tuple[str | None, str | None]:
    for entry in item.get("string_map_data", {}).values():
        if isinstance(entry, dict) and entry.get("href"):
            return entry["href"], _parse_saved_date(entry)
    return None, None


def _media_kind(url: str) -> str:
    if "/reel" in url:
        return "reel"
    if "/tv/" in url:
        return "igtv"
    return "貼文"


def _fix_mojibake(text: str) -> str:
    """IG 匯出的 JSON 常見的 latin-1 誤編碼中文，能救就救。"""
    if not text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


SETUP_HINT = (
    "請先到 IG App → 設定 → 帳號中心 → 你的資訊與權限 → 下載你的資訊，\n"
    "勾選「已儲存的內容」、格式選 JSON。收到檔案後解壓縮，\n"
    "把整個資料夾（或裡面的 saved_posts.json）放到設定檔指定的路徑。"
)


def resolve_export_path(path: Path) -> Path:
    """接受 saved_posts.json 本身，或解壓縮後的匯出資料夾。"""
    if path.is_file():
        return path
    if path.is_dir():
        matches = sorted(path.rglob("saved_posts.json"))
        if matches:
            return matches[0]
        raise FileNotFoundError(f"在 {path} 底下找不到 saved_posts.json\n\n{SETUP_HINT}")
    raise FileNotFoundError(f"找不到匯出檔：{path}\n\n{SETUP_HINT}")


def parse_collections(saved_posts_path: Path) -> dict[str, str]:
    """讀 saved_collections.json，回傳 {貼文網址: 收藏夾名稱}。

    格式是扁平的交錯陣列：一個帶 title 的標頭項，後面跟著它底下的成員（沒有
    title），直到下一個標頭。沒建過收藏夾的話這個檔案不存在，回空的就好。

    這是 Grace 自己分過的類，比模型猜的準，所以拿來當分類提示。
    """
    path = saved_posts_path.parent / "saved_collections.json"
    if not path.exists():
        return {}

    try:
        with path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}

    mapping: dict[str, str] = {}
    current = ""
    for item in raw.get("saved_saved_collections", []):
        if not isinstance(item, dict):
            continue
        title = _fix_mojibake(str(item.get("title") or "")).strip()
        if title:
            current = title
            continue
        url, _ = _first_href_and_date(item)
        if url and current:
            mapping[url] = current
    return mapping


def parse_export(path: Path) -> list[dict[str, Any]]:
    """讀 saved_posts.json，回傳 [{url, saved_at, author, collection, kind}]。"""
    saved_posts_path = resolve_export_path(path)

    with saved_posts_path.open(encoding="utf-8") as fh:
        raw = json.load(fh)

    collections = parse_collections(saved_posts_path)

    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in _walk_for_saved_items(raw):
        url, saved_at = _first_href_and_date(item)
        if not url or url in seen:
            continue
        seen.add(url)
        records.append(
            {
                "url": url,
                "saved_at": saved_at,
                "author": _fix_mojibake(str(item.get("title") or "")).strip(),
                "collection": collections.get(url, ""),
                "kind": _media_kind(url),
            }
        )

    records.sort(key=lambda r: r["saved_at"] or "", reverse=True)
    return records


# --------------------------------------------------------------------------
# 2. 補內容（盡力而為）
# --------------------------------------------------------------------------

# 先切出單一個 <meta> 標籤再抓屬性，才不會讓 content 的比對跨過標籤邊界
# （用一條含 .*? 的大 regex 會把前一個標籤的值連到後一個標籤上）
_META_TAG_RE = re.compile(r"<meta\s[^>]*>", re.IGNORECASE)
_OG_PROP_RE = re.compile(r'(?:property|name)\s*=\s*["\']og:([a-z:]+)["\']', re.IGNORECASE)
_CONTENT_RE = re.compile(r'content\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)


def _extract_og(page_html: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for tag in _META_TAG_RE.findall(page_html):
        prop = _OG_PROP_RE.search(tag)
        content = _CONTENT_RE.search(tag)
        if prop and content:
            tags.setdefault(prop.group(1).lower(), html.unescape(content.group(1)).strip())
    return tags


def enrich_post(session: requests.Session, url: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """抓公開貼文的 og: metadata。私人帳號、已刪除、或被擋登入牆時抓不到。"""
    timeout = cfg["enrich"]["timeout_sec"]
    retries = cfg["enrich"]["max_retries"]

    for attempt in range(retries + 1):
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code != 200:
                return {"content_status": CONTENT_MISSING, "fetch_note": f"HTTP {resp.status_code}"}

            og = _extract_og(resp.text)
            description = og.get("description", "")
            title = og.get("title", "")

            # 只有 IG 的通用標語、沒有實際內文的話，等於沒抓到
            if not description or "Instagram" == description.strip():
                return {"content_status": CONTENT_MISSING, "fetch_note": "頁面無 og:description（可能是私人帳號或登入牆）"}

            return {
                "og_title": title,
                "og_description": description,
                "og_image": og.get("image", ""),
                "content_status": CONTENT_OK,
                "fetch_note": "",
            }
        except requests.RequestException as exc:
            if attempt >= retries:
                return {"content_status": CONTENT_MISSING, "fetch_note": f"抓取失敗: {exc.__class__.__name__}"}
            time.sleep(2 * (attempt + 1))

    return {"content_status": CONTENT_MISSING, "fetch_note": "重試用盡"}


# --------------------------------------------------------------------------
# 3. 分類（Claude）
# --------------------------------------------------------------------------

def _classification_schema(categories: list[str], max_subtags: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "對應輸入清單的編號",
                        },
                        "title": {
                            "type": "string",
                            "description": "8-20 字的中文標題，描述這則貼文在講什麼",
                        },
                        "category": {
                            "type": "string",
                            "enum": categories,
                        },
                        "subtags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                f"最多 {max_subtags} 個具體的技術或主題標籤，例如 "
                                "RAG、LDA、Power BI、DCF 估值。用最常見的寫法，"
                                "英文專有名詞保留英文。資訊不足就給空陣列。"
                            ),
                        },
                        "summary": {
                            "type": "string",
                            "description": "一句話（40 字內）說明這則貼文的重點與對 Grace 的用處",
                        },
                    },
                    "required": ["index", "title", "category", "subtags", "summary"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def _build_batch_prompt(batch: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
    lines = []
    for i, post in enumerate(batch):
        parts = [f"[{i}] 網址: {post['url']}（{post.get('kind', '貼文')}）"]
        if post.get("author"):
            parts.append(f"    帳號: {post['author']}")
        if post.get("collection"):
            parts.append(f"    使用者自己放的收藏夾: {post['collection']}")
        if post.get("og_title"):
            parts.append(f"    標題: {post['og_title']}")
        if post.get("og_description"):
            desc = post["og_description"][:1200]
            parts.append(f"    內文: {desc}")
        if post.get("content_status") == CONTENT_MISSING:
            parts.append("    （抓不到內文，只能從帳號名稱和網址推測；沒把握就歸「其他」）")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


def classify_batch(client: Any, batch: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    hints = "\n".join(f"- {k}：{v}" for k, v in cfg["category_hints"].items())
    system = (
        "你在幫使用者整理她收藏的 Instagram 貼文，分成三大類加子標籤。\n\n"
        f"使用者背景：{cfg['owner_context']}\n\n"
        f"分類定義：\n{hints}\n\n"
        "判斷原則：\n"
        "- 一則貼文只給一個主類。同時沾到兩類時，選對她職涯敘事最有用的那一類。\n"
        "- 如果有標「使用者自己放的收藏夾」，那是她本人分的類，比你的推測可靠，"
        "除非明顯矛盾否則以它為準。\n"
        "- 資訊不足以判斷時就給「其他」，不要猜。錯誤分類比未分類更浪費她的時間。\n"
        "- 子標籤要具體到能看出她在囤什麼題材，不要用「資料」「科技」這種空泛詞。\n"
        "- 標題和摘要都用繁體中文，英文專有名詞保留英文。"
    )

    response = client.messages.create(
        model=cfg["model"],
        max_tokens=8000,
        system=system,
        output_config={
            "effort": "low",
            "format": {
                "type": "json_schema",
                "schema": _classification_schema(cfg["categories"], cfg["max_subtags"]),
            },
        },
        messages=[
            {
                "role": "user",
                "content": "請分類以下貼文：\n\n" + _build_batch_prompt(batch, cfg),
            }
        ],
    )

    text = next(b.text for b in response.content if b.type == "text")
    parsed = json.loads(text)

    out: list[dict[str, Any]] = []
    by_index = {r["index"]: r for r in parsed["results"]}
    for i, post in enumerate(batch):
        result = by_index.get(i)
        if result is None:
            # 模型漏了這筆，標成其他而不是整批失敗
            out.append(
                {
                    **post,
                    "title": post.get("og_title") or post["url"],
                    "category": "其他",
                    "subtags": [],
                    "summary": "分類時被模型漏掉，需人工確認",
                }
            )
            continue
        out.append(
            {
                **post,
                "title": result["title"],
                "category": result["category"],
                "subtags": result["subtags"][: cfg["max_subtags"]],
                "summary": result["summary"],
            }
        )
    return out


def build_summary(client: Any, posts: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
    """整體分析。這是判斷題，用高 effort。"""
    counts: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    for post in posts:
        counts[post["category"]] = counts.get(post["category"], 0) + 1
        for tag in post.get("subtags", []):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    top_tags = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:25]
    inventory = "\n".join(
        f"- [{p['category']}] {p['title']}｜{'、'.join(p.get('subtags', []))}｜{p['summary']}"
        for p in posts
    )

    system = (
        "你在幫使用者看她自己的收藏清單，寫一份誠實的分析。\n\n"
        f"使用者背景：{cfg['owner_context']}\n\n"
        "寫作要求：\n"
        "- 繁體中文，語氣直接、乾淨、結構化，不要客套與鼓勵語。\n"
        "- 給判斷，不要只列現象。看到偏食就說偏食，看到只收不做就說只收不做。\n"
        "- 用 Markdown，分成這四段：分佈與偏向 / 你在囤什麼題材 / 缺口 / 值得做成作品集的幾筆。\n"
        "- 最後一段要指名道姓點出 3-5 筆具體貼文，說明為什麼那幾筆能撐起一個專案，"
        "以及做出來能對應到哪種職缺。\n"
        "- 全文 400-600 字，不要湊字數。"
    )

    stats = (
        "分類統計：" + "、".join(f"{k} {v} 則" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        + f"（共 {len(posts)} 則）\n"
        + "熱門子標籤：" + "、".join(f"{t}×{c}" for t, c in top_tags)
    )

    response = client.messages.create(
        model=cfg["model"],
        max_tokens=8000,
        system=system,
        output_config={"effort": "high"},
        messages=[
            {
                "role": "user",
                "content": f"{stats}\n\n完整清單：\n{inventory}",
            }
        ],
    )
    return "".join(b.text for b in response.content if b.type == "text")


# --------------------------------------------------------------------------
# 4. Notion 同步
# --------------------------------------------------------------------------

# 釘住 2022-06-28：這是最後一個 database 直接掛 properties、page 的 parent 用
# database_id 的版本。2025-09-03 之後 database 底下多了 data source 層，建立頁面
# 與查詢都要先解析 data_source_id，多一次往返也多一個會壞的地方。Notion 的版本
# 政策明說舊版不會下架，所以釘著是安全的。
NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"
NOTION_DELAY = 0.35  # 官方限速約 3 req/s

PROP_TITLE = "標題"
PROP_CATEGORY = "分類"
PROP_SUBTAGS = "子標籤"
PROP_SUMMARY = "一句話摘要"
PROP_URL = "連結"
PROP_SAVED_AT = "收藏時間"
PROP_STATUS = "內容狀態"
PROP_NOTES = "我的筆記"

# 這兩欄是 Grace 的，程式只在建立時留白，之後永遠不碰
USER_OWNED = {PROP_NOTES}


class NotionError(RuntimeError):
    pass


def extract_notion_id(value: str) -> str:
    """接受純 id、帶連字號的 id、或整條 Notion 網址。

    id 一律在網址最後一段的結尾。不能直接用 regex 掃 32 碼十六進位，因為頁面
    標題裡的字母（Page 的 e、Cafe 的 e…）本身就是合法的十六進位字元，會讓比對
    起點偏掉一格。
    """
    value = value.strip().split("?")[0].split("#")[0].rstrip("/")
    segment = value.split("/")[-1].replace("-", "")
    if len(segment) < 32:
        raise NotionError(f"看不出 Notion id：{value!r}")
    raw = segment[-32:].lower()
    if not all(c in "0123456789abcdef" for c in raw):
        raise NotionError(f"看不出 Notion id：{value!r}")
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def notion_request(
    session: requests.Session, method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    resp = session.request(method, f"{NOTION_API}{path}", json=body, timeout=30)
    time.sleep(NOTION_DELAY)
    if resp.status_code == 429:
        wait = float(resp.headers.get("Retry-After", "3"))
        time.sleep(wait)
        resp = session.request(method, f"{NOTION_API}{path}", json=body, timeout=30)
        time.sleep(NOTION_DELAY)
    if resp.status_code >= 400:
        raise NotionError(f"{method} {path} → HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def notion_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
    )
    return session


def _rt(text: str) -> list[dict[str, Any]]:
    """純文字轉 rich_text，切在 2000 字上限內。"""
    text = text or ""
    if not text:
        return []
    return [{"type": "text", "text": {"content": text[i : i + 1900]}} for i in range(0, len(text), 1900)]


def create_database(session: requests.Session, parent_page_id: str, cfg: dict[str, Any]) -> str:
    body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": cfg["notion"]["database_title"]}}],
        "properties": {
            PROP_TITLE: {"title": {}},
            PROP_CATEGORY: {
                "select": {"options": [{"name": c} for c in cfg["categories"]]}
            },
            PROP_SUBTAGS: {"multi_select": {}},
            PROP_SUMMARY: {"rich_text": {}},
            PROP_URL: {"url": {}},
            PROP_SAVED_AT: {"date": {}},
            PROP_STATUS: {
                "select": {"options": [{"name": CONTENT_OK}, {"name": CONTENT_MISSING}]}
            },
            PROP_NOTES: {"rich_text": {}},
        },
    }
    return notion_request(session, "POST", "/databases", body)["id"]


def fetch_existing(session: requests.Session, database_id: str) -> dict[str, dict[str, Any]]:
    """一次把整個 database 撈下來，建 URL → page 的索引。

    比每筆各查一次少幾百個請求，也就不會撞到限速。
    """
    pages: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    while True:
        body: dict[str, Any] = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion_request(session, "POST", f"/databases/{database_id}/query", body)
        for page in data.get("results", []):
            url = (page.get("properties", {}).get(PROP_URL) or {}).get("url")
            if url:
                pages[url] = page
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages


def _props_for(post: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {
        PROP_TITLE: {"title": _rt(post.get("title") or post["url"])},
        PROP_CATEGORY: {"select": {"name": post["category"]}},
        PROP_SUBTAGS: {
            # Notion multi_select 不吃逗號
            "multi_select": [{"name": t.replace(",", " ")[:100]} for t in post.get("subtags", []) if t.strip()]
        },
        PROP_SUMMARY: {"rich_text": _rt(post.get("summary", ""))},
        PROP_URL: {"url": post["url"]},
        PROP_STATUS: {"select": {"name": post.get("content_status", CONTENT_MISSING)}},
    }
    if post.get("saved_at"):
        props[PROP_SAVED_AT] = {"date": {"start": post["saved_at"]}}
    return props


def _is_empty(prop: dict[str, Any] | None) -> bool:
    if not prop:
        return True
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in ("title", "rich_text", "multi_select"):
        return not value
    return value is None


def sync_posts(
    session: requests.Session, database_id: str, posts: list[dict[str, Any]]
) -> tuple[int, int, int]:
    """新增沒見過的貼文；已存在的只補空欄位，絕不覆寫 Grace 改過的東西。"""
    existing = fetch_existing(session, database_id)
    created = updated = skipped = 0

    for post in posts:
        page = existing.get(post["url"])
        desired = _props_for(post)

        if page is None:
            notion_request(
                session,
                "POST",
                "/pages",
                {"parent": {"database_id": database_id}, "properties": desired},
            )
            created += 1
            continue

        current = page.get("properties", {})
        patch = {
            name: value
            for name, value in desired.items()
            if name not in USER_OWNED and _is_empty(current.get(name))
        }
        if patch:
            notion_request(session, "PATCH", f"/pages/{page['id']}", {"properties": patch})
            updated += 1
        else:
            skipped += 1

    return created, updated, skipped


# --- Markdown → Notion blocks（只支援 summary 會用到的語法） ---

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _inline(text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    pos = 0
    for match in _BOLD_RE.finditer(text):
        if match.start() > pos:
            segments.append({"type": "text", "text": {"content": text[pos : match.start()][:1900]}})
        segments.append(
            {
                "type": "text",
                "text": {"content": match.group(1)[:1900]},
                "annotations": {"bold": True},
            }
        )
        pos = match.end()
    if pos < len(text):
        segments.append({"type": "text", "text": {"content": text[pos:][:1900]}})
    return segments or _rt(text)


def markdown_to_blocks(markdown: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            blocks.append({"type": "heading_3", "heading_3": {"rich_text": _inline(stripped[4:])}})
        elif stripped.startswith("## "):
            blocks.append({"type": "heading_2", "heading_2": {"rich_text": _inline(stripped[3:])}})
        elif stripped.startswith("# "):
            blocks.append({"type": "heading_2", "heading_2": {"rich_text": _inline(stripped[2:])}})
        elif stripped.startswith(("- ", "* ")):
            blocks.append(
                {
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": _inline(stripped[2:])},
                }
            )
        elif re.match(r"^\d+\.\s", stripped):
            blocks.append(
                {
                    "type": "numbered_list_item",
                    "numbered_list_item": {"rich_text": _inline(re.sub(r"^\d+\.\s", "", stripped))},
                }
            )
        else:
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": _inline(stripped)}})
    return blocks


def append_summary(
    session: requests.Session, parent_page_id: str, summary: str, cfg: dict[str, Any]
) -> None:
    """把這次的分析附在指定頁面後面，保留歷次紀錄。"""
    today = datetime.now().strftime("%Y-%m-%d")
    header = [
        {
            "type": "heading_1",
            "heading_1": {
                "rich_text": _rt(f"{cfg['notion']['summary_page_title']}（{today}）")
            },
        }
    ]
    blocks = header + markdown_to_blocks(summary) + [{"type": "divider", "divider": {}}]

    # 每次 append 上限 100 個 block
    for start in range(0, len(blocks), 90):
        notion_request(
            session,
            "PATCH",
            f"/blocks/{parent_page_id}/children",
            {"children": blocks[start : start + 90]},
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="把 IG 收藏分類並同步到 Notion")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true", help="不寫 Notion，只印結果")
    parser.add_argument("--no-enrich", action="store_true", help="跳過抓貼文內容")
    parser.add_argument("--limit", type=int, help="只處理前 N 筆")
    parser.add_argument("--reclassify", action="store_true", help="忽略快取，全部重新分類")
    args = parser.parse_args()

    cfg = load_config(args.config)
    export_path = REPO_ROOT / cfg["export_path"]
    state_path = REPO_ROOT / cfg["state_path"]

    records = parse_export(export_path)
    print(f"匯出檔讀到 {len(records)} 筆收藏")
    if args.limit:
        records = records[: args.limit]
        print(f"依 --limit 只處理前 {len(records)} 筆")

    state = {} if args.reclassify else load_state(state_path)
    todo = [r for r in records if r["url"] not in state]
    print(f"其中 {len(todo)} 筆需要處理（{len(records) - len(todo)} 筆用快取）")

    if todo and not args.no_enrich and cfg["enrich"]["enabled"]:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            }
        )
        for i, post in enumerate(todo, 1):
            post.update(enrich_post(session, post["url"], cfg))
            print(f"  補內容 {i}/{len(todo)} {post['content_status']}", end="\r", flush=True)
            time.sleep(cfg["enrich"]["delay_sec"])
        print()
        ok = sum(1 for p in todo if p.get("content_status") == CONTENT_OK)
        print(f"補到內容 {ok}/{len(todo)} 筆（抓不到的會標成「{CONTENT_MISSING}」）")
    else:
        for post in todo:
            post.setdefault("content_status", CONTENT_MISSING)
            post.setdefault("fetch_note", "略過補內容")

    if todo:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("錯誤：需要 ANTHROPIC_API_KEY 才能分類", file=sys.stderr)
            return 1
        import anthropic

        client = anthropic.Anthropic()
        size = cfg["classify_batch_size"]
        for start in range(0, len(todo), size):
            batch = todo[start : start + size]
            print(f"  分類 {start + 1}-{start + len(batch)}/{len(todo)}", end="\r", flush=True)
            for result in classify_batch(client, batch, cfg):
                state[result["url"]] = result
        print()
        save_state(state_path, state)
        print(f"分類完成，已寫入 {state_path.relative_to(REPO_ROOT)}")

    posts = [state[r["url"]] for r in records if r["url"] in state]
    if not posts:
        print("沒有可用資料，結束")
        return 0

    counts: dict[str, int] = {}
    for post in posts:
        counts[post["category"]] = counts.get(post["category"], 0) + 1
    print("\n分類結果：")
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {name}: {count}")

    if args.dry_run:
        print("\n--dry-run：不寫 Notion。前 5 筆預覽：")
        for post in posts[:5]:
            print(f"  [{post['category']}] {post['title']}")
            print(f"      {'、'.join(post.get('subtags', [])) or '(無子標籤)'}｜{post['summary']}")
        return 0

    token = os.environ.get("NOTION_TOKEN")
    parent_raw = os.environ.get("NOTION_PARENT_PAGE")
    if not token or not parent_raw:
        print(
            "錯誤：同步 Notion 需要 NOTION_TOKEN 與 NOTION_PARENT_PAGE。\n"
            "（只想看分類結果的話加 --dry-run）",
            file=sys.stderr,
        )
        return 1

    session = notion_session(token)
    parent_page_id = extract_notion_id(parent_raw)

    database_id = os.environ.get("NOTION_DATABASE_ID")
    if database_id:
        database_id = extract_notion_id(database_id)
    else:
        database_id = create_database(session, parent_page_id, cfg)
        print(f"\n已建立 Notion 資料庫。把這行加進環境變數，之後就不會重建：")
        print(f"  export NOTION_DATABASE_ID={database_id}")

    created, updated, skipped = sync_posts(session, database_id, posts)
    print(f"Notion 同步完成：新增 {created}、補欄位 {updated}、未變動 {skipped}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("缺 ANTHROPIC_API_KEY，略過 summary")
        return 0

    import anthropic

    print("產生整體分析…")
    summary = build_summary(anthropic.Anthropic(), posts, cfg)
    append_summary(session, parent_page_id, summary, cfg)
    print("分析已寫入 Notion 頁面")
    return 0


if __name__ == "__main__":
    sys.exit(main())
