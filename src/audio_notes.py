#!/usr/bin/env python3
"""音檔轉文字 + Claude 摘要／重點（TAKEAWAY & SUMMARY）。

用法：
    python src/audio_notes.py 錄音.m4a                    # 轉錄 + 摘要（課程模式）
    python src/audio_notes.py 會議.mp3 --type meeting     # 會議模式（決策 + 待辦）
    python src/audio_notes.py 錄音.m4a --model medium     # 換小模型換速度
    python src/audio_notes.py 錄音.m4a --no-llm           # 只轉錄，不呼叫 Claude
    python src/audio_notes.py 逐字稿.txt --from-text      # 已有逐字稿，只做摘要

轉錄在本機跑（faster-whisper，離線、免費），摘要走 Claude API。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "audio_config.json"

AUDIO_SUFFIXES = {
    ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus", ".wma",
    ".aiff", ".aif", ".caf", ".mp4", ".mov", ".m4v", ".mkv", ".webm",
}

DEFAULTS = {
    "whisper": {
        "model": "medium",
        "device": "cpu",
        "compute_type": "int8",
        "cpu_threads": 4,
        "language": "zh",
        "beam_size": 5,
        "vad_filter": True,
        "initial_prompt": "以下是繁體中文的錄音內容，可能夾雜英文專業術語。",
        "to_traditional": True,
        "opencc_config": "s2tw",
    },
    "claude": {
        "model": "claude-opus-5",
        "max_tokens": 16000,
        "effort": "high",
        "chunk_chars": 60000,
    },
    "output_dir": "data/audio_notes",
    "default_profile": "lecture",
}


# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------

def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


# --------------------------------------------------------------------------
# 轉錄
# --------------------------------------------------------------------------

def format_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def make_converter(enabled: bool, config: str = "s2tw"):
    """簡體轉繁體（台灣字形）。opencc 沒裝就原樣回傳。

    預設 s2tw 只做字形轉換。s2twp 會多做詞彙替換（軟件→軟體），
    但也會把「循環」誤轉成「迴圈」，對財金內容弊大於利。
    """
    if not enabled:
        return lambda text: text
    try:
        import opencc  # type: ignore
    except ImportError:
        print("提示：未安裝 opencc，輸出可能是簡體。pip install opencc-python-reimplemented", file=sys.stderr)
        return lambda text: text
    return opencc.OpenCC(config).convert


def _stdout_progress(message: str, **_extra) -> None:
    print(message, flush=True)


def transcribe(audio_path: Path, cfg: dict, model_override: str | None,
               language_override: str | None, progress=_stdout_progress) -> tuple[list[dict], dict]:
    from faster_whisper import WhisperModel

    w = cfg["whisper"]
    model_name = model_override or w["model"]
    language = language_override or w.get("language") or None
    if language in ("auto", ""):
        language = None

    progress(f"→ 載入模型 {model_name}（首次會下載，之後有快取）…")
    load_start = time.time()
    # cpu_threads 綁效能核心：M1 實測 4 執行緒比 8 快（跨到節能核心反而拖慢）。
    model = WhisperModel(model_name, device=w["device"], compute_type=w["compute_type"],
                         cpu_threads=int(w.get("cpu_threads", 4)))
    progress(f"  模型就緒（{time.time() - load_start:.1f}s）")

    progress(f"→ 轉錄 {audio_path.name} …")
    t0 = time.time()
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=w["beam_size"],
        vad_filter=w["vad_filter"],
        initial_prompt=w.get("initial_prompt") or None,
    )

    convert = make_converter(
        w.get("to_traditional", True) and (info.language or "").startswith("zh"),
        w.get("opencc_config", "s2tw"),
    )

    segments: list[dict] = []
    for seg in segments_iter:
        text = convert(seg.text.strip())
        if not text:
            continue
        segments.append({"start": seg.start, "end": seg.end, "text": text})
        if len(segments) == 1 or len(segments) % 5 == 0:
            done = float(seg.end)
            total = float(info.duration or 0)
            progress(f"  …已轉到 {format_ts(done)}",
                     ratio=(done / total if total else None))

    elapsed = time.time() - t0
    meta = {
        "language": info.language,
        "language_probability": round(float(info.language_probability or 0), 3),
        "duration_sec": float(info.duration or 0),
        "transcribe_sec": round(elapsed, 1),
        "whisper_model": model_name,
    }
    speed = meta["duration_sec"] / elapsed if elapsed else 0
    meta["realtime_factor"] = round(speed, 2)
    progress(f"  完成：音檔 {format_ts(meta['duration_sec'])}，耗時 {elapsed:.0f}s"
             f"（{speed:.1f}x 實時），{len(segments)} 段", ratio=1.0)
    return segments, meta


def segments_to_text(segments: list[dict], with_timestamps: bool = True) -> str:
    if with_timestamps:
        return "\n".join(f"[{format_ts(s['start'])}] {s['text']}" for s in segments)
    return "\n".join(s["text"] for s in segments)


# --------------------------------------------------------------------------
# 摘要（Claude）
# --------------------------------------------------------------------------

PROFILE_SPECS = {
    "lecture": {
        "label": "課程／講座錄音",
        "role": "你在幫一位念 IBMBA、財金背景的研究生整理課堂筆記。她要的是能直接拿去複習與寫報告的東西。",
        "sections": """## TL;DR
三句以內講完這堂課在講什麼、結論是什麼。

## TAKEAWAYS
5–8 條。每條一行，是「可以帶走的判斷或結論」，不是流水帳。
重要的專有名詞保留英文原文。

## 概念架構
用巢狀清單畫出這堂課的知識骨架（主題 → 子概念 → 關鍵定義）。

## SUMMARY
依內容自然分段，每段開頭標時間戳 `[mm:ss]`，寫成連貫的段落敘述，不要條列流水帳。

## 可複習清單
5–10 個問題形式的自我檢核（「為什麼 X 會導致 Y？」），涵蓋這堂課的考點。

## 關鍵名詞
表格：名詞 | 這堂課裡的定義／用法。只收真的重要的。""",
    },
    "meeting": {
        "label": "會議／工作錄音",
        "role": "你在幫一位在銀行 Technology & Operations 實習的分析師整理會議紀錄。她要的是隔天能直接對照著做事的東西。",
        "sections": """## TL;DR
三句以內講完這場會的結論與下一步。

## TAKEAWAYS
5–8 條。每條是「這場會之後改變了什麼」——決定、共識、被推翻的假設。

## 決策
表格：決策事項 | 結論 | 理由／依據。沒有明確決策就寫「本次無明確決策」。

## 待辦事項
表格：待辦 | 負責人 | 期限 | 出處時間戳。
負責人或期限錄音裡沒講就填「未指派」／「未定」，**不要自行推測**。

## SUMMARY
依議題分段，每段開頭標時間戳 `[mm:ss]`，寫成連貫敘述。

## 風險與未解問題
條列還沒收斂、有分歧、或明顯需要再確認的事。

## 關鍵名詞
表格：名詞（含系統名、專案代號、縮寫） | 在這場會裡指什麼。""",
    },
}

COMMON_TAIL = """## 待確認（轉錄可能有誤）
語音轉文字可能聽錯的地方——人名、數字、系統名、英文縮寫——列出來並標時間戳，讓使用者回去核對。沒有就寫「無」。"""

SYSTEM_PROMPT = """你是一位擅長把口語內容轉成結構化筆記的分析師。

規則：
1. 只用繁體中文（台灣用語）；專業名詞保留英文原文（例如 Finance、KYC、remediation、cash flow），不要硬翻。
2. 只根據逐字稿內容寫。沒講的不要補、不要推測、不要用常識填空。逐字稿殘缺處就標「錄音不清」。
3. 逐字稿是語音辨識結果，會有錯字與斷句錯誤。你要讀懂意思，但把可疑處集中列在「待確認」。
4. 語氣精準、乾淨，不要客套與贅語。不要寫「以下是…」這種開場白。
5. 直接輸出 Markdown 內文，從第一個 `##` 標題開始，不要包在程式碼區塊裡。"""

CHUNK_PROMPT = """這是一段長錄音逐字稿的第 {idx}/{total} 段。

先做**中繼筆記**（之後會和其他段合併）：
- 這段的重點條列（保留時間戳）
- 出現的決策、待辦、數字、專有名詞（原文照抄關鍵句）
- 可疑的轉錄錯誤

不要寫 TL;DR，不要寫結論——那是合併階段的事。

逐字稿：
---
{text}
---"""

FINAL_PROMPT = """錄音類型：{label}
{role}

請依下列結構輸出筆記，標題與順序照抄，不要增減章節：

{sections}

{tail}

{source_label}：
---
{text}
---"""


def call_claude(client, cfg: dict, system: str, user: str) -> str:
    kwargs = dict(
        model=cfg["model"],
        max_tokens=cfg["max_tokens"],
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    effort = cfg.get("effort")
    if effort:
        kwargs["output_config"] = {"effort": effort}
    with client.messages.stream(**kwargs) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise RuntimeError("Claude 拒絕處理這段內容（stop_reason=refusal）。")
    return "".join(b.text for b in message.content if b.type == "text").strip()


def split_chunks(text: str, limit: int) -> list[str]:
    lines = text.split("\n")
    chunks, buf, size = [], [], 0
    for line in lines:
        if size + len(line) > limit and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def summarize(transcript: str, profile: str, cfg: dict, progress=_stdout_progress) -> str:
    import anthropic

    client = anthropic.Anthropic()
    spec = PROFILE_SPECS[profile]
    limit = cfg["chunk_chars"]

    source_label = "逐字稿"
    text = transcript

    if len(transcript) > limit:
        chunks = split_chunks(transcript, limit)
        progress(f"→ 逐字稿 {len(transcript):,} 字，分 {len(chunks)} 段先做中繼筆記…")
        notes = []
        for i, chunk in enumerate(chunks, 1):
            progress(f"  中繼筆記 {i}/{len(chunks)} …", ratio=i / (len(chunks) + 1))
            notes.append(call_claude(
                client, cfg, SYSTEM_PROMPT,
                CHUNK_PROMPT.format(idx=i, total=len(chunks), text=chunk),
            ))
        text = "\n\n".join(f"### 第 {i} 段中繼筆記\n{n}" for i, n in enumerate(notes, 1))
        source_label = "各段中繼筆記（依時間順序）"

    progress("→ 產生 TAKEAWAY / SUMMARY …")
    return call_claude(client, cfg, SYSTEM_PROMPT, FINAL_PROMPT.format(
        label=spec["label"], role=spec["role"], sections=spec["sections"],
        tail=COMMON_TAIL, source_label=source_label, text=text,
    ))


# --------------------------------------------------------------------------
# 輸出
# --------------------------------------------------------------------------

def slugify(name: str) -> str:
    slug = re.sub(r"[^\w一-鿿-]+", "_", name).strip("_")
    return slug[:60] or "audio"


def build_note(title: str, meta: dict, notes: str, transcript: str) -> str:
    head = [f"# {title}", ""]
    info = []
    if meta.get("duration_sec"):
        info.append(f"長度 {format_ts(meta['duration_sec'])}")
    if meta.get("whisper_model"):
        info.append(f"轉錄 {meta['whisper_model']}")
    if meta.get("language"):
        info.append(f"語言 {meta['language']}")
    info.append(f"產生於 {datetime.now():%Y-%m-%d %H:%M}")
    head.extend(["> " + "｜".join(info), "", ""])
    body = notes if notes else "_（未產生摘要）_"
    return "\n".join(head) + body + "\n\n---\n\n## 逐字稿\n\n" + transcript + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser(
        description="音檔轉文字 + Claude 摘要／重點",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", help="音檔路徑（或 --from-text 時的逐字稿 .txt）")
    parser.add_argument("--type", choices=sorted(PROFILE_SPECS),
                        default=cfg["default_profile"], help="筆記模式（預設 %(default)s）")
    parser.add_argument("--model", help=f"whisper 模型（預設 {cfg['whisper']['model']}；"
                                        "tiny/base/small/medium/large-v3）")
    parser.add_argument("--lang", help="語言碼，如 zh / en / auto（預設 "
                                       f"{cfg['whisper']['language']}）")
    parser.add_argument("--out", help=f"輸出資料夾（預設 {cfg['output_dir']}）")
    parser.add_argument("--no-llm", action="store_true", help="只轉錄，不呼叫 Claude")
    parser.add_argument("--from-text", action="store_true", help="來源已是逐字稿文字檔")
    args = parser.parse_args()

    source = Path(args.source).expanduser()
    if not source.exists():
        print(f"找不到檔案：{source}", file=sys.stderr)
        return 1
    if not args.from_text and source.suffix.lower() not in AUDIO_SUFFIXES:
        print(f"不像音檔（{source.suffix}）。若這是逐字稿請加 --from-text。", file=sys.stderr)
        return 1

    out_dir = Path(args.out).expanduser() if args.out else ROOT / cfg["output_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = slugify(source.stem)

    if args.from_text:
        transcript = source.read_text(encoding="utf-8").strip()
        meta = {"whisper_model": None, "language": None, "duration_sec": 0}
        segments = []
    else:
        segments, meta = transcribe(source, cfg, args.model, args.lang)
        if not segments:
            print("轉錄結果是空的——確認音檔有聲音、或換 --lang。", file=sys.stderr)
            return 1
        transcript = segments_to_text(segments)
        (out_dir / f"{stem}.transcript.txt").write_text(transcript + "\n", encoding="utf-8")
        (out_dir / f"{stem}.segments.json").write_text(
            json.dumps({"meta": meta, "segments": segments}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    notes = ""
    if args.no_llm:
        print("→ 已略過 Claude（--no-llm）", flush=True)
    elif not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("→ 未設 ANTHROPIC_API_KEY，跳過摘要。逐字稿已存檔，設好 key 後可用："
              f"\n  python src/audio_notes.py {out_dir / f'{stem}.transcript.txt'} "
              f"--from-text --type {args.type}", file=sys.stderr)
    else:
        try:
            notes = summarize(transcript, args.type, cfg["claude"])
        except Exception as exc:  # noqa: BLE001 — 摘要失敗不該弄丟逐字稿
            print(f"摘要失敗：{exc}\n逐字稿已保留，可稍後用 --from-text 重跑。", file=sys.stderr)

    title = source.stem
    note_path = out_dir / f"{stem}.md"
    note_path.write_text(build_note(title, meta, notes, transcript), encoding="utf-8")
    print(f"\n✅ 筆記：{note_path}")
    if segments:
        print(f"   逐字稿：{out_dir / f'{stem}.transcript.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
