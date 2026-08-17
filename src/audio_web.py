#!/usr/bin/env python3
"""音檔轉文字網頁版：上傳錄音 → 本機轉錄 → Claude 摘要 → Notion / Telegram。

啟動：
    python src/audio_web.py                 # 電腦與同 Wi-Fi 的手機都能連
    python src/audio_web.py --local-only    # 只綁 localhost
    python src/audio_web.py --port 9000

轉錄跑在同一台機器上（faster-whisper，CPU），所以是單一工作執行緒依序處理；
上傳多個檔案會排隊，不會互相搶 CPU。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import sys
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audio_deliver as deliver  # noqa: E402
import audio_notes as an  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
UI_PATH = ROOT / "web" / "audio_notes.html"
UPLOAD_DIR = ROOT / "data" / "audio_uploads"
JOBS_PATH = ROOT / "data" / "audio_notes" / "jobs.json"

CFG = an.load_config()
OUT_DIR = ROOT / CFG["output_dir"]

MODELS = ["tiny", "base", "small", "medium", "large-v3"]

app = FastAPI(title="音檔筆記")
_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_work: "queue.Queue[str]" = queue.Queue()


# --------------------------------------------------------------------------
# 任務狀態
# --------------------------------------------------------------------------

def _save_jobs() -> None:
    JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        snapshot = list(_jobs.values())[-50:]
    JOBS_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_jobs() -> None:
    if not JOBS_PATH.exists():
        return
    try:
        for job in json.loads(JOBS_PATH.read_text(encoding="utf-8")):
            if job.get("stage") in ("queued", "running"):
                job["stage"] = "error"
                job["error"] = "伺服器重啟，這個任務沒跑完"
            _jobs[job["id"]] = job
    except (json.JSONDecodeError, KeyError):
        pass  # 壞掉的紀錄檔不值得讓服務起不來


def _update(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job.update(fields)


def _log(job_id: str, message: str, ratio: float | None = None) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["log"].append({"t": datetime.now().strftime("%H:%M:%S"), "msg": message})
        job["log"] = job["log"][-200:]
        if ratio is not None:
            job["ratio"] = max(0.0, min(1.0, float(ratio)))


def _progress_for(job_id: str):
    def progress(message: str, ratio: float | None = None, **_extra) -> None:
        _log(job_id, message, ratio)
    return progress


# --------------------------------------------------------------------------
# 工作執行緒
# --------------------------------------------------------------------------

def _run_job(job_id: str) -> None:
    with _lock:
        job = dict(_jobs[job_id])
    progress = _progress_for(job_id)
    audio_path = Path(job["upload_path"])
    stem = an.slugify(Path(job["filename"]).stem)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    _update(job_id, stage="running", step="transcribe", started_at=datetime.now().isoformat(timespec="seconds"))

    segments, meta = an.transcribe(audio_path, CFG, job["model"], job["lang"], progress=progress)
    if not segments:
        raise RuntimeError("轉錄結果是空的——確認音檔有聲音，或改語言設定。")
    meta["source_name"] = job["filename"]
    transcript = an.segments_to_text(segments)
    (OUT_DIR / f"{stem}.transcript.txt").write_text(transcript + "\n", encoding="utf-8")
    (OUT_DIR / f"{stem}.segments.json").write_text(
        json.dumps({"meta": meta, "segments": segments}, ensure_ascii=False, indent=2), encoding="utf-8")
    _update(job_id, meta=meta, transcript=transcript, ratio=None)

    notes = ""
    if job["summarize"]:
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            progress("→ 未設 ANTHROPIC_API_KEY，跳過摘要（逐字稿已存檔）")
        else:
            _update(job_id, step="summarize")
            notes = an.summarize(transcript, job["profile"], CFG["claude"], progress=progress)

    title = Path(job["filename"]).stem
    note_md = an.build_note(title, meta, notes, transcript)
    note_path = OUT_DIR / f"{stem}.md"
    note_path.write_text(note_md, encoding="utf-8")
    _update(job_id, note_markdown=note_md, note_path=str(note_path), stem=stem)

    notion_url = ""
    if job["to_notion"]:
        _update(job_id, step="notion")
        progress("→ 送到 Notion …")
        try:
            result = deliver.deliver_notion(note_md, transcript, meta, title, job["profile"],
                                            CFG, progress=lambda m: _log(job_id, m))
            notion_url = result["url"]
            _update(job_id, notion_url=notion_url)
        except Exception as exc:  # noqa: BLE001 — 投遞失敗不該弄丟筆記
            progress(f"⚠️ Notion 失敗：{exc}")
            _update(job_id, notion_error=str(exc))

    if job["to_telegram"]:
        _update(job_id, step="telegram")
        progress("→ 推到 Telegram …")
        try:
            deliver.deliver_telegram(note_md, note_path, title, meta, notion_url,
                                     progress=lambda m: _log(job_id, m))
            _update(job_id, telegram_ok=True)
        except Exception as exc:  # noqa: BLE001
            progress(f"⚠️ Telegram 失敗：{exc}")
            _update(job_id, telegram_error=str(exc))

    progress("✅ 完成", ratio=1.0)
    _update(job_id, stage="done", step="done",
            finished_at=datetime.now().isoformat(timespec="seconds"))


def _worker() -> None:
    while True:
        job_id = _work.get()
        try:
            _run_job(job_id)
        except Exception as exc:  # noqa: BLE001 — 一個任務炸掉不能拖垮整個服務
            _log(job_id, f"❌ 失敗：{exc}")
            _update(job_id, stage="error", step="error", error=str(exc),
                    traceback=traceback.format_exc()[-2000:],
                    finished_at=datetime.now().isoformat(timespec="seconds"))
        finally:
            _save_jobs()
            _work.task_done()


# --------------------------------------------------------------------------
# 存取控制（選用）
# --------------------------------------------------------------------------

def _check_auth(request: Request) -> None:
    expected = os.environ.get("AUDIO_WEB_TOKEN", "")
    if not expected:
        return
    given = request.headers.get("x-auth-token") or request.query_params.get("token", "")
    if given != expected:
        raise HTTPException(status_code=401, detail="需要正確的 token")


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(UI_PATH.read_text(encoding="utf-8"))


@app.get("/api/status")
def status(request: Request) -> JSONResponse:
    _check_auth(request)
    return JSONResponse({
        "models": MODELS,
        "default_model": CFG["whisper"]["model"],
        "default_profile": CFG["default_profile"],
        "default_lang": CFG["whisper"].get("language", "zh"),
        "profiles": {k: v["label"] for k, v in an.PROFILE_SPECS.items()},
        "claude_ready": bool(os.environ.get("ANTHROPIC_API_KEY") or
                             os.environ.get("ANTHROPIC_AUTH_TOKEN")),
        "notion_ready": deliver.notion_configured(),
        "telegram_ready": deliver.telegram_configured(),
        "output_dir": str(OUT_DIR),
    })


@app.get("/api/jobs")
def list_jobs(request: Request) -> JSONResponse:
    _check_auth(request)
    with _lock:
        jobs = [_public(j) for j in list(_jobs.values())[-30:]]
    jobs.reverse()
    return JSONResponse(jobs)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> JSONResponse:
    _check_auth(request)
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="沒有這個任務")
        return JSONResponse(_public(job, full=True))


def _public(job: dict, full: bool = False) -> dict:
    keep = ["id", "filename", "profile", "model", "lang", "stage", "step", "ratio",
            "created_at", "started_at", "finished_at", "error", "notion_url",
            "notion_error", "telegram_ok", "telegram_error", "meta"]
    out = {k: job.get(k) for k in keep}
    out["queued_ahead"] = _work.qsize() if job.get("stage") == "queued" else 0
    if full:
        out["log"] = job.get("log", [])
        out["note_markdown"] = job.get("note_markdown", "")
        out["transcript"] = job.get("transcript", "")
    else:
        out["last_log"] = (job.get("log") or [{}])[-1].get("msg", "")
    return out


@app.post("/api/jobs")
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    profile: str = Form("lecture"),
    model: str = Form(""),
    lang: str = Form(""),
    summarize: str = Form("1"),
    to_notion: str = Form("0"),
    to_telegram: str = Form("0"),
) -> JSONResponse:
    _check_auth(request)
    filename = Path(file.filename or "recording").name
    suffix = Path(filename).suffix.lower()
    if suffix not in an.AUDIO_SUFFIXES:
        raise HTTPException(status_code=400,
                            detail=f"不支援的副檔名 {suffix or '（無）'}；"
                                   f"可用：{', '.join(sorted(an.AUDIO_SUFFIXES))}")
    if profile not in an.PROFILE_SPECS:
        raise HTTPException(status_code=400, detail="不認得的筆記模式")
    if model and model not in MODELS:
        raise HTTPException(status_code=400, detail="不認得的轉錄模型")

    job_id = uuid.uuid4().hex[:12]
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_path = UPLOAD_DIR / f"{an.slugify(Path(filename).stem)}_{job_id}{suffix}"
    size = 0
    with upload_path.open("wb") as out:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            out.write(chunk)
    if size == 0:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="檔案是空的")

    job = {
        "id": job_id,
        "filename": filename,
        "upload_path": str(upload_path),
        "size": size,
        "profile": profile,
        "model": model or None,
        "lang": lang or None,
        "summarize": summarize == "1",
        "to_notion": to_notion == "1",
        "to_telegram": to_telegram == "1",
        "stage": "queued",
        "step": "queued",
        "ratio": None,
        "log": [{"t": datetime.now().strftime("%H:%M:%S"), "msg": "已排入佇列"}],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _lock:
        _jobs[job_id] = job
    _work.put(job_id)
    _save_jobs()
    return JSONResponse({"id": job_id})


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, request: Request, kind: str = "md") -> FileResponse:
    _check_auth(request)
    with _lock:
        job = _jobs.get(job_id)
    if not job or not job.get("stem"):
        raise HTTPException(status_code=404, detail="還沒有可下載的檔案")
    names = {"md": f"{job['stem']}.md", "txt": f"{job['stem']}.transcript.txt",
             "json": f"{job['stem']}.segments.json"}
    if kind not in names:
        raise HTTPException(status_code=400, detail="kind 只能是 md / txt / json")
    path = OUT_DIR / names[kind]
    if not path.exists():
        raise HTTPException(status_code=404, detail="檔案不存在")
    return FileResponse(path, filename=names[kind], media_type="application/octet-stream")


# --------------------------------------------------------------------------
# 啟動
# --------------------------------------------------------------------------

def lan_ip() -> str:
    """找出區網 IP，讓手機知道要連哪裡。連不出去也不影響服務。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="音檔轉文字網頁版")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--local-only", action="store_true", help="只綁 localhost，手機連不到")
    args = parser.parse_args()

    host = "127.0.0.1" if args.local_only else "0.0.0.0"
    _load_jobs()
    threading.Thread(target=_worker, daemon=True, name="audio-worker").start()

    token = os.environ.get("AUDIO_WEB_TOKEN", "")
    suffix = f"/?token={token}" if token else "/"
    print("\n  音檔筆記已啟動")
    print(f"  電腦： http://localhost:{args.port}{suffix}")
    if not args.local_only:
        print(f"  手機： http://{lan_ip()}:{args.port}{suffix}   （需同一個 Wi-Fi）")
    if not token:
        print("  ⚠️  沒設 AUDIO_WEB_TOKEN：同網段的人都能上傳與讀取筆記")
    print()

    import uvicorn
    uvicorn.run(app, host=host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
