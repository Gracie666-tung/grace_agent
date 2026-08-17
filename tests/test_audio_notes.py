#!/usr/bin/env python3
"""audio_notes 的離線測試：不呼叫 Claude、不跑 whisper。

    python tests/test_audio_notes.py
"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


# --- 假的 anthropic 模組，攔截所有送出的 prompt -------------------------------

class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeMessage:
    stop_reason = "end_turn"

    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _FakeMessages:
    def __init__(self, calls):
        self.calls = calls

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeStream(_FakeMessage(f"## 假回應 {len(self.calls)}"))


class _FakeClient:
    def __init__(self, calls):
        self.messages = _FakeMessages(calls)


def install_fake_anthropic():
    calls = []
    module = types.ModuleType("anthropic")
    module.Anthropic = lambda *a, **kw: _FakeClient(calls)
    sys.modules["anthropic"] = module
    return calls


CALLS = install_fake_anthropic()
import audio_notes as an  # noqa: E402


def check(condition, label):
    if not condition:
        raise AssertionError(label)
    print(f"  ok  {label}")


def test_format_ts():
    check(an.format_ts(0) == "00:00", "format_ts(0) == 00:00")
    check(an.format_ts(75) == "01:15", "format_ts(75) == 01:15")
    check(an.format_ts(3725) == "1:02:05", "format_ts(3725) == 1:02:05")


def test_split_chunks():
    text = "\n".join(f"line {i} " + "字" * 50 for i in range(100))
    chunks = an.split_chunks(text, 1000)
    check(len(chunks) > 1, "長文會被切成多段")
    check("\n".join(chunks) == text, "切完再接回來完全等於原文（沒漏行）")
    check(all(len(c) <= 1200 for c in chunks), "每段大致不超過上限")


def test_config_defaults():
    cfg = an.load_config()
    for key in ("whisper", "claude", "output_dir", "default_profile"):
        check(key in cfg, f"設定含 {key}")
    check(cfg["default_profile"] in an.PROFILE_SPECS, "default_profile 是有效的模式")
    check(cfg["claude"]["model"] == "claude-opus-5", "Claude 模型為 claude-opus-5")


def test_summarize_short_single_call():
    CALLS.clear()
    cfg = {"model": "claude-opus-5", "max_tokens": 100, "effort": "high", "chunk_chars": 10000}
    out = an.summarize("[00:00] 短短的逐字稿。", "lecture", cfg)
    check(len(CALLS) == 1, "短逐字稿只呼叫 Claude 一次")
    prompt = CALLS[0]["messages"][0]["content"]
    check("## 可複習清單" in prompt, "lecture 模式帶入可複習清單章節")
    check("## 待確認（轉錄可能有誤）" in prompt, "帶入待確認章節")
    check(CALLS[0]["output_config"] == {"effort": "high"}, "有帶 effort 設定")
    check(out.startswith("## "), "回傳的是 Markdown 內文")


def test_summarize_meeting_profile():
    CALLS.clear()
    cfg = {"model": "claude-opus-5", "max_tokens": 100, "effort": None, "chunk_chars": 10000}
    an.summarize("[00:00] 會議逐字稿。", "meeting", cfg)
    prompt = CALLS[0]["messages"][0]["content"]
    check("## 待辦事項" in prompt, "meeting 模式帶入待辦事項章節")
    check("## 決策" in prompt, "meeting 模式帶入決策章節")
    check("可複習清單" not in prompt, "meeting 模式不會混進 lecture 章節")
    check("output_config" not in CALLS[0], "effort 為 None 時不送 output_config")


def test_summarize_long_map_reduce():
    CALLS.clear()
    cfg = {"model": "claude-opus-5", "max_tokens": 100, "effort": "high", "chunk_chars": 500}
    long_text = "\n".join(f"[{i:02d}:00] " + "逐" * 80 for i in range(30))
    an.summarize(long_text, "lecture", cfg)
    check(len(CALLS) >= 3, f"長逐字稿走分段流程（實際 {len(CALLS)} 次呼叫）")
    check("中繼筆記" in CALLS[0]["messages"][0]["content"], "第一次呼叫是中繼筆記")
    final = CALLS[-1]["messages"][0]["content"]
    check("各段中繼筆記" in final, "最後一次呼叫是合併中繼筆記")
    check("## TL;DR" in final, "合併階段才要求 TL;DR")


def test_build_note():
    note = an.build_note(
        "週會", {"duration_sec": 3725, "whisper_model": "large-v3", "language": "zh"},
        "## TL;DR\n測試", "[00:00] 逐字稿內容",
    )
    check(note.startswith("# 週會"), "標題正確")
    check("1:02:05" in note, "有音檔長度")
    check("large-v3" in note, "有註明轉錄模型")
    check("## 逐字稿" in note and "[00:00] 逐字稿內容" in note, "附完整逐字稿")


def test_slugify():
    check(an.slugify("財管 第三週!!") == "財管_第三週", "中文檔名保留、空白與符號換底線")
    check(an.slugify("!!!") == "audio", "全是符號時有 fallback")


MEETING_MD = """## 待辦事項

| 待辦 | 負責人 | 期限 |
|---|---|---|
| 補 KYC 文件 | Grace | 8/20 |
| 確認 **API** 權限 | 未指派 | 未定 |

## 風險
- 一項風險
"""


def _cells(table_block):
    return [[("".join(t["text"]["content"] for t in cell) if cell else "")
             for cell in row["table_row"]["cells"]]
            for row in table_block["table"]["children"]]


def test_notion_table_conversion():
    import audio_deliver as ad
    blocks = ad.md_to_notion_blocks(MEETING_MD)
    kinds = [b["type"] for b in blocks]
    check(kinds == ["heading_2", "table", "heading_2", "bulleted_list_item"],
          f"Markdown 依序轉成 {kinds}")
    rows = _cells(blocks[1])
    check(rows[0] == ["待辦", "負責人", "期限"], "表頭正確")
    check(rows[1] == ["補 KYC 文件", "Grace", "8/20"], "第一列資料正確")
    check(rows[2][0] == "確認 API 權限", "粗體標記不會留在儲存格文字裡")
    check(blocks[1]["table"]["table_width"] == 3, "欄寬正確")
    check(len(rows) == 3, "分隔列 |---| 沒被當成資料列")


def test_notion_transcript_toggle():
    import audio_deliver as ad
    blocks = ad.transcript_blocks("\n".join(f"[{i:02d}:00] 內容" for i in range(500)))
    check(blocks[0]["type"] == "toggle", "逐字稿收在 toggle 裡")
    children = blocks[0]["toggle"]["children"]
    check(len(children) <= 96, f"子區塊數量在 Notion 上限內（{len(children)}）")


def test_notion_tldr_extraction():
    import audio_deliver as ad
    md = "## TL;DR\n這場會決定了 A 與 B。\n\n## TAKEAWAYS\n- x"
    check(ad._first_paragraph_after(md, "TL;DR") == "這場會決定了 A 與 B。", "抓得到 TL;DR")
    check(ad._first_paragraph_after(md, "沒有這章節") == "", "找不到章節時回空字串")


def test_telegram_message_prep():
    import audio_deliver as ad
    md = "## TL;DR\n重點\n\n## 逐字稿\n\n[00:00] 很長的逐字稿"
    check("逐字稿" not in ad._strip_transcript(md), "推播訊息不含逐字稿")
    parts = ad._chunk("行\n" * 5000, 4046)
    check(all(len(p) <= 4046 for p in parts), "每則訊息都在 Telegram 4096 上限內")
    check("".join(p.replace("\n", "") for p in parts) == "行" * 5000, "切割不會漏字")


def test_web_module_contract():
    import audio_web
    check(audio_web.UI_PATH.exists(), f"網頁檔存在：{audio_web.UI_PATH.name}")
    check("large-v3" in audio_web.MODELS and "small" in audio_web.MODELS, "模型清單完整")
    routes = {r.path for r in audio_web.app.routes}
    for path in ("/", "/api/status", "/api/jobs", "/api/jobs/{job_id}",
                 "/api/jobs/{job_id}/download"):
        check(path in routes, f"路由存在：{path}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n全部 {len(tests)} 組通過。")
    print("注意：這些測試都不碰網路——不驗證 Claude 回應品質、whisper 轉錄準確度，")
    print("也不驗證 Notion／Telegram 真的收得到（只驗證送出去的資料結構正確）。")


if __name__ == "__main__":
    main()
