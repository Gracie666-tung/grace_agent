#!/usr/bin/env python3
"""把音檔筆記送到 Notion 資料庫與 Telegram。

環境變數：
    NOTION_TOKEN               Notion integration token（ntn_ 開頭）
    NOTION_PARENT_PAGE         第一次用來自動建資料庫的父頁面（網址或 id）
    AUDIO_NOTION_DATABASE_ID   建好之後設這個，就不會重複建資料庫
    TELEGRAM_BOT_TOKEN         與職缺／台股工具共用同一組
    TELEGRAM_CHAT_ID

Notion 的 API 細節（session、限速重試、markdown→block）直接沿用 ig_curator，
不重複實作；這裡只加它沒有的表格轉換。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ig_curator import (  # noqa: E402
    NotionError,
    _inline,
    _rt,
    extract_notion_id,
    markdown_to_blocks,
    notion_request,
    notion_session,
)

TELEGRAM_MSG_LIMIT = 4096

PROP_TITLE = "標題"
PROP_KIND = "類型"
PROP_DATE = "錄音日期"
PROP_MINUTES = "長度(分)"
PROP_TLDR = "TL;DR"
PROP_MODEL = "轉錄模型"
PROP_SOURCE = "來源檔名"
PROP_NOTES = "我的筆記"  # 永遠留白，是 Grace 自己的欄位

KIND_LABELS = {"lecture": "課程／講座", "meeting": "會議／工作"}


# --------------------------------------------------------------------------
# Markdown → Notion（補上 ig_curator 沒有的表格支援）
# --------------------------------------------------------------------------

def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_divider_row(line: str) -> bool:
    return bool(re.fullmatch(r"\|?[\s:|-]+\|?", line.strip())) and "-" in line


def _table_block(rows: list[list[str]]) -> dict[str, Any]:
    width = max(len(r) for r in rows)
    children = []
    for row in rows:
        cells = [_inline(c) for c in row] + [[] for _ in range(width - len(row))]
        children.append({"type": "table_row", "table_row": {"cells": cells}})
    return {
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": False,
            "children": children,
        },
    }


def md_to_notion_blocks(markdown: str) -> list[dict[str, Any]]:
    """跟 ig_curator.markdown_to_blocks 一樣，但額外把 Markdown 表格轉成 Notion 表格。"""
    blocks: list[dict[str, Any]] = []
    plain: list[str] = []
    table: list[list[str]] = []

    def flush_plain() -> None:
        if plain:
            blocks.extend(markdown_to_blocks("\n".join(plain)))
            plain.clear()

    def flush_table() -> None:
        if table:
            blocks.append(_table_block(table))
            table.clear()

    for line in markdown.splitlines():
        stripped = line.strip()
        looks_like_row = stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2
        if looks_like_row:
            if _is_divider_row(stripped):
                continue  # |---|---| 分隔列不進 Notion
            flush_plain()
            table.append(_split_row(stripped))
        else:
            flush_table()
            plain.append(line)
    flush_table()
    flush_plain()
    return blocks


def transcript_blocks(transcript: str, max_children: int = 95) -> list[dict[str, Any]]:
    """逐字稿收在一個可摺疊的 toggle 裡，免得洗版。"""
    paragraphs: list[str] = []
    buf: list[str] = []
    size = 0
    for line in transcript.splitlines():
        if size + len(line) > 1800 and buf:
            paragraphs.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        paragraphs.append("\n".join(buf))

    truncated = len(paragraphs) > max_children
    if truncated:
        paragraphs = paragraphs[:max_children]

    children = [{"type": "paragraph", "paragraph": {"rich_text": _rt(p)}} for p in paragraphs]
    if truncated:
        children.append({
            "type": "paragraph",
            "paragraph": {"rich_text": _rt("（逐字稿過長，此處截斷；完整版見本機 .transcript.txt）")},
        })
    return [{"type": "toggle", "toggle": {"rich_text": _rt("逐字稿（點開）"), "children": children}}]


# --------------------------------------------------------------------------
# Notion
# --------------------------------------------------------------------------

def notion_configured() -> bool:
    return bool(os.environ.get("NOTION_TOKEN") and
                (os.environ.get("AUDIO_NOTION_DATABASE_ID") or os.environ.get("NOTION_PARENT_PAGE")))


def create_audio_database(session: requests.Session, parent_page_id: str, title: str) -> str:
    body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": title}}],
        "properties": {
            PROP_TITLE: {"title": {}},
            PROP_KIND: {"select": {"options": [{"name": v} for v in KIND_LABELS.values()]}},
            PROP_DATE: {"date": {}},
            PROP_MINUTES: {"number": {"format": "number"}},
            PROP_TLDR: {"rich_text": {}},
            PROP_MODEL: {"select": {}},
            PROP_SOURCE: {"rich_text": {}},
            PROP_NOTES: {"rich_text": {}},
        },
    }
    return notion_request(session, "POST", "/databases", body)["id"]


def _first_paragraph_after(markdown: str, heading: str) -> str:
    """抓某個 ## 標題底下的第一段文字，用來填 TL;DR 屬性。"""
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("## ") and heading in line:
            for follow in lines[i + 1:]:
                text = follow.strip()
                if text.startswith("## "):
                    break
                if text:
                    return re.sub(r"[*`]", "", text)[:1900]
    return ""


def deliver_notion(note_markdown: str, transcript: str, meta: dict,
                   title: str, profile: str, cfg: dict, progress=print) -> dict:
    """在 Notion 資料庫建一頁。回傳 {url, database_id}。"""
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise NotionError("缺少 NOTION_TOKEN")

    session = notion_session(token)
    database_id = os.environ.get("AUDIO_NOTION_DATABASE_ID")
    created_db = False
    if database_id:
        database_id = extract_notion_id(database_id)
    else:
        parent = os.environ.get("NOTION_PARENT_PAGE")
        if not parent:
            raise NotionError("缺少 AUDIO_NOTION_DATABASE_ID，也沒有 NOTION_PARENT_PAGE 可以建資料庫")
        progress("  Notion：第一次使用，建立資料庫…")
        database_id = create_audio_database(
            session, extract_notion_id(parent),
            cfg.get("notion", {}).get("database_title", "錄音筆記"))
        created_db = True

    minutes = round(float(meta.get("duration_sec") or 0) / 60, 1) or None
    props: dict[str, Any] = {
        PROP_TITLE: {"title": [{"type": "text", "text": {"content": title[:1900]}}]},
        PROP_KIND: {"select": {"name": KIND_LABELS.get(profile, profile)}},
        PROP_DATE: {"date": {"start": datetime.now().strftime("%Y-%m-%d")}},
        PROP_SOURCE: {"rich_text": _rt(meta.get("source_name", ""))},
    }
    if minutes:
        props[PROP_MINUTES] = {"number": minutes}
    tldr = _first_paragraph_after(note_markdown, "TL;DR")
    if tldr:
        props[PROP_TLDR] = {"rich_text": _rt(tldr)}
    if meta.get("whisper_model"):
        props[PROP_MODEL] = {"select": {"name": str(meta["whisper_model"])}}

    body_blocks = md_to_notion_blocks(note_markdown)
    if transcript:
        body_blocks += [{"type": "divider", "divider": {}}] + transcript_blocks(transcript)

    page = notion_request(session, "POST", "/pages", {
        "parent": {"database_id": database_id},
        "properties": props,
        "children": body_blocks[:90],
    })
    for start in range(90, len(body_blocks), 90):
        notion_request(session, "PATCH", f"/blocks/{page['id']}/children",
                       {"children": body_blocks[start:start + 90]})

    result = {"url": page.get("url", ""), "database_id": database_id, "created_db": created_db}
    if created_db:
        progress(f"  Notion：資料庫已建立，請設 AUDIO_NOTION_DATABASE_ID={database_id}")
    progress(f"  Notion：已建立頁面 {result['url']}")
    return result


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def telegram_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _strip_transcript(markdown: str) -> str:
    """推播訊息只要摘要，不要整份逐字稿。"""
    marker = "\n## 逐字稿"
    return markdown.split(marker)[0].rstrip()


def _chunk(text: str, limit: int) -> list[str]:
    parts, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            if buf:
                parts.append(buf)
            buf = line[:limit]
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        parts.append(buf)
    return parts


def deliver_telegram(note_markdown: str, note_path: Path | None, title: str,
                     meta: dict, notion_url: str = "", progress=print) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        raise RuntimeError("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")

    header = f"🎧 {title}"
    if meta.get("duration_sec"):
        mins = int(float(meta["duration_sec"]) // 60)
        secs = int(float(meta["duration_sec"]) % 60)
        header += f"（{mins:02d}:{secs:02d}）"
    body = _strip_transcript(note_markdown)
    if notion_url:
        body += f"\n\nNotion：{notion_url}"

    for i, part in enumerate(_chunk(f"{header}\n\n{body}", TELEGRAM_MSG_LIMIT - 50)):
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": part, "disable_web_page_preview": True},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Telegram sendMessage 失敗：HTTP {resp.status_code} {resp.text[:300]}")
        progress(f"  Telegram：已送出第 {i + 1} 則訊息")

    if note_path and note_path.exists():
        with note_path.open("rb") as fh:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": chat_id},
                files={"document": (note_path.name, fh, "text/markdown")},
                timeout=120,
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Telegram sendDocument 失敗：HTTP {resp.status_code} {resp.text[:300]}")
        progress(f"  Telegram：已附上 {note_path.name}")
