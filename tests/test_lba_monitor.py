#!/usr/bin/env python3
"""lba_monitor 的離線測試：不連網、不呼叫 Claude、不送 Telegram（requests 全部換成假的）。
tests/fixtures/lba/export.json 是依 swagger 欄位自行捏造的職缺（synthetic），不是真實 LBA 資料。

    python tests/test_lba_monitor.py      # 或 python -m pytest tests/test_lba_monitor.py -q
"""

import contextlib
import io
import json
import os
import re
import sys
import tempfile
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import lba_monitor as lba  # noqa: E402

NOW = datetime(2026, 10, 20, 7, 0, tzinfo=timezone.utc)
FIX = ROOT / "tests" / "fixtures" / "lba"
EXPORT = (FIX / "export.json").read_bytes()
CFG = lba.load_config(ROOT / "lba_config.json")
SENTINEL = "LBAKEY_SENTINEL_9f3a77c1d2e4"  # 假的 key；任何輸出裡出現這串就是洩漏
SIG = "X-Amz-Signature=PRESIGNSECRET123"
S3_URL = f"https://s3.rbx.io.cloud.ovh.net/bucket/export.json?{SIG}"
A, B, C, D, E_, F, G, H, I_, J = (f"a1b2c3d4e5f60718293a4b0{x}" for x in "123456789a")
FT = "france_travail:FT-889900"


def _args(**kw):
    d = dict(dry_run=False, max_new=None, fixture_dir=None, no_llm=False, config=str(ROOT / "lba_config.json"))
    d.update(kw)
    return Namespace(**d)


class Resp:
    def __init__(self, status=200, body=b"", json_body=None, headers=None):
        self.status_code, self.body, self.json_body, self.headers = status, body, json_body, headers or {}
        self.closed = False

    def json(self):
        if self.json_body is None:
            raise ValueError("no json")
        return self.json_body

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.body), 4096):
            yield self.body[i:i + 4096]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


class FakeNet:
    """取代 requests.get／post：記下每次呼叫（網址＋標頭），依網址回罐頭回應。"""

    def __init__(self, api=None, download=None):
        self.api = api or Resp(200, json_body={"url": S3_URL, "lastUpdate": "2026-10-20T01:00:00.000Z"})
        self.download = download or Resp(200, body=EXPORT)
        self.gets, self.posts = [], []

    def get(self, url, headers=None, **kw):
        self.gets.append((url, dict(headers or {}), kw))
        return self.api if url.startswith(lba.EXPORT_URL) else self.download

    def post(self, url, json=None, **kw):
        self.posts.append((url, json))
        return Resp(200)


def _run(args, state_path, env=None, now=NOW, net=None):
    """回傳 (exit code, 送出的 Telegram 文字, stdout+stderr, net)。Telegram 走真的 send_telegram，只換掉底層 requests.post。"""
    net = net or FakeNet()
    base = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "1", "LBA_API_KEY": "", "ANTHROPIC_API_KEY": "",
            "GITHUB_ACTIONS": "", "LBA_STATE_PATH": str(state_path)}
    base.update(env or {})
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, base), mock.patch.object(lba.requests, "get", net.get), \
            mock.patch.object(lba.requests, "post", net.post), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = lba.run(args, now=now)
    texts = [p["text"] for _, p in net.posts]
    return code, texts, out.getvalue() + err.getvalue(), net


def _fixture_run(sent_state, **kw):
    return _run(_args(fixture_dir=str(FIX), **kw), sent_state)


def _state(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _tmp():
    d = tempfile.TemporaryDirectory()
    return d, Path(d.name) / "seen.json"


# --- 單筆職缺解析：保留／過舊／過期／不完整 -----------------------------------

def _raw(**over):
    raw = json.loads(EXPORT)["jobs"][0]
    for k, v in over.items():
        raw[k] = v
    return raw


def test_parse_offer_keep_old_expired_malformed():
    jobs = {o["identifier"]["id"] or o["identifier"]["partner_job_id"]: o for o in json.loads(EXPORT)["jobs"]}
    job, why = lba.parse_offer(jobs[A], NOW, 7)
    assert job and why == "" and job["uuid"] == A and job["location"] == "Paris" and job["dept"] == "75"
    assert job["start"] == "2027-01" and "Apprentissage" in job["employment_type"] and "<" not in job["text"]
    assert lba.parse_offer(jobs[H], NOW, 7) == (None, "old")
    assert lba.parse_offer(jobs[G], NOW, 7) == (None, "expired")
    assert lba.parse_offer(jobs[J], NOW, 7)[0] is not None  # expiration=None：沒有過期日就當作還有效
    assert lba.parse_offer({"identifier": {"id": I_}, "offer": {"title": ""}}, NOW, 7)[1] == "malformed"
    assert lba.parse_offer("junk", NOW, 7)[1] == "malformed" and lba.parse_offer({}, NOW, 7)[1] == "malformed"
    no_date = json.loads(json.dumps(jobs[A]))
    no_date["offer"]["publication"]["creation"] = None
    assert lba.parse_offer(no_date, NOW, 7)[1] == "malformed"  # 沒有建立日就無法判斷新舊
    plain = json.loads(json.dumps(jobs[A]))
    plain["apply"]["url"] = "javascript:alert(1)"
    assert lba.parse_offer(plain, NOW, 7)[1] == "malformed"  # 連結一定要 https
    ft, _ = lba.parse_offer(jobs["FT-889900"], NOW, 7)
    assert ft["uuid"] == FT and ft["dept"] == "92" and ft["location"] == "Boulogne-Billancourt"  # id 為 null：用 label:partner_job_id
    assert "Professionnalisation" in ft["employment_type"]


def test_city_dept():
    assert lba.city_dept("12 Rue X 75008 PARIS") == ("Paris", "75")
    assert lba.city_dept("5 Cours Y, 33000 Bordeaux") == ("Bordeaux", "33")
    assert lba.city_dept("pas d'adresse") == ("", "")


# --- 串流 JSON：兩種外層結構、小緩衝區、壞檔 ----------------------------------

def test_iter_offers_shapes_and_tiny_chunks():
    doc = {"warnings": [], "recruiters": [{"a": 1}, {"b": [1, 2]}], "n": 12345, "jobs": [{"x": 1}, {"y": "é"}, 7]}
    text = json.dumps(doc, ensure_ascii=False, indent=1)
    for chunk in (1, 3, 17, 1 << 20):
        with mock.patch.object(lba._JsonStream, "CHUNK", chunk):
            assert list(lba.iter_offers(io.StringIO(text))) == [{"x": 1}, {"y": "é"}, 7]
            assert list(lba.iter_offers(io.StringIO(json.dumps([{"x": 1}, 12345])))) == [{"x": 1}, 12345]  # 緊貼尾端的數字不能被截斷
    assert list(lba.iter_offers(io.StringIO("[]"))) == [] and list(lba.iter_offers(io.StringIO("{}"))) == []
    for bad in ('{"jobs": [{"x": 1}', '[{"x": 1} {"y": 2}]', "not json", '{"jobs" [1]}', ""):
        try:
            list(lba.iter_offers(io.StringIO(bad)))
        except ValueError:
            continue
        raise AssertionError(f"should fail: {bad!r}")


# --- 關鍵字粗篩 ----------------------------------------------------------------

def _job(title, text, dept=""):
    return {"title": title, "text": text, "dept": dept}


def test_keywords_french_and_negative_context():
    kw = lba.Keywords(CFG)
    assert kw.rank(_job("Alternant Contrôleur de gestion", "budget"))[0]  # 法文標題字
    assert kw.rank(_job("Assistant", "Pilotage de la chaîne d'approvisionnement, tableau de bord"))[0]  # 全文裡的法文字
    assert not kw.rank(_job("Aide boulanger", "pains et viennoiseries"))[0]  # 沒有任何正面字
    assert not kw.rank(_job("Community manager", "réseaux sociaux, contenu, supply chain"))[0]  # 標題負面、標題無正面
    assert kw.rank(_job("Alternant PMO - communication projet", "planning"))[0]  # 標題負面但標題也有正面字
    assert kw.rank(_job("Alternant achats", "On attend de bonnes capacités de communication et de paie"))[0]  # 全文的負面字不殺
    assert kw.rank(_job("Alternant achats", "x", dept="75"))[1] == kw.rank(_job("Alternant achats", "x"))[1] + 3  # 地點加權
    assert kw.rank(_job("Alternant achats", "x", dept="33"))[1] > kw.rank(_job("Alternant achats", "x", dept="69"))[1]
    assert kw.rank(_job("Analyste", "S&OP, Excel, SQL"))[0]  # & 保留


# --- 離線端到端：保留／丟棄各路徑 ---------------------------------------------------

def test_fixture_end_to_end_paths_state_and_messages():
    d, sp = _tmp()
    with d:
        code, texts, out, _ = _fixture_run(sp)
        assert code == 0
        assert "匯出 12 筆：資料不全 1、超過 7 天 1、已過期 1、匯出內重複 1、已見 0、關鍵字丟棄 2、法文 D 1" in out
        allmsg = "\n".join(texts)
        assert "Supply Chain Analyst" in allmsg and "PMO" in allmsg and "管理控制" in allmsg
        for dropped in ("Community manager", "boulanger", "Acheteur junior", "doublon", "Chef de projet industriel"):
            assert dropped not in allmsg
        assert allmsg.count("法文要求") == 2 and "法文 C" in allmsg  # 即時推播顯示等級；摘要也顯示
        assert "S&amp;OP" in allmsg and "<" not in re.sub(r"</?(?:b|a)[^>]*>", "", allmsg)  # 內文經過跳脫
        st = _state(sp)
        by = {k: v["status"] for k, v in st["seen"].items()}
        assert by == {A: "pushed", FT: "pushed", B: "digest", C: "low", J: "score_failed"}, by
        for gone in (D, E_, F, G, H):  # 被丟掉的不進狀態（狀態只放處理過的）
            assert gone not in st["seen"]
        assert st["key_alert_sent"] is False and st["seen"][A]["score"] == 95
        # 第二輪：已見的不再推；score_failed 還有重試次數，會再評一次（stub 仍壞，tries 累加）
        code, texts2, out2, _ = _fixture_run(sp)
        assert texts2 == [] and "已見 4" in out2
        assert _state(sp)["seen"][J]["tries"] == 2
        _fixture_run(sp)
        _fixture_run(sp)
        assert _state(sp)["seen"][J]["tries"] == 3  # 到上限就視為已見，不再重試
        assert "已見 5" in _fixture_run(sp)[2]


def test_french_class_d_never_pushed_or_recorded():
    d, sp = _tmp()
    with d:
        _, texts, out, _ = _fixture_run(sp)
        assert "Acheteur" not in "\n".join(texts) and F not in _state(sp)["seen"] and "法文 D 1" in out


def test_dry_run_writes_nothing_and_sends_nothing():
    d, sp = _tmp()
    with d:
        code, texts, out, net = _run(_args(fixture_dir=str(FIX), dry_run=True), sp, env={"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})
        assert code == 0 and texts == [] and net.posts == [] and not sp.exists() and "Supply Chain Analyst" in out


def test_no_llm_sends_one_unscored_digest_with_city_and_french_class():
    d, sp = _tmp()
    with d:
        code, texts, _, _ = _fixture_run(sp, no_llm=True)
        assert code == 0 and len(texts) == 1
        t = texts[0]
        assert "未啟用 AI 評分" in t and "Paris" in t and "Bordeaux" in t and "法文 B" in t and "法文 C" in t
        assert "Community manager" not in t and "Acheteur" not in t
        assert {v["status"] for v in _state(sp)["seen"].values()} == {"pushed_unscored"}
        assert len(_state(sp)["seen"]) == 5


def test_max_new_and_message_cap_defer_without_marking_seen():
    d, sp = _tmp()
    with d:
        _, texts, out, _ = _fixture_run(sp, max_new=1)
        assert "本輪處理 1（其餘留待下輪）" in out and len(_state(sp)["seen"]) == 1  # 只處理排序最高那一則
    d2, sp2 = _tmp()
    with d2:
        small = Path(d2.name) / "cfg.json"
        small.write_text(json.dumps(CFG | {"max_messages_per_run": 3}), encoding="utf-8")  # 即時推播上限 = 3 - 2 = 1
        _, texts, out, _ = _run(_args(fixture_dir=str(FIX), config=str(small)), sp2)
        st = _state(sp2)["seen"]
        assert sum(v["status"] == "pushed" for v in st.values()) == 1 and len(texts) <= 3
        assert "'deferred': 1" in out and len(st) == 4  # 被擠掉的那則沒進狀態，明天再推


def test_telegram_failure_does_not_mark_seen():
    d, sp = _tmp()
    with d:
        net = FakeNet()
        net.post = lambda url, json=None, **kw: (_ for _ in ()).throw(RuntimeError(url))  # 例外訊息含 token 網址
        code, texts, out, _ = _run(_args(fixture_dir=str(FIX)), sp, net=net)
        assert code == 0 and "傳送失敗：RuntimeError" in out and "bot" + "tok" not in out
        assert {v["status"] for v in _state(sp)["seen"].values()} <= {"low", "score_failed"}


# --- 真實網路路徑（假 requests）----------------------------------------------------

def _live(sp, net=None, key=SENTINEL, **kw):
    return _run(_args(no_llm=True, **kw), sp, env={"LBA_API_KEY": key}, net=net)


def test_live_flow_sends_key_to_api_host_only_and_not_to_presigned_url():
    d, sp = _tmp()
    with d:
        net = FakeNet()
        code, texts, out, _ = _live(sp, net)
        assert code == 0 and len(texts) == 1 and len(net.gets) == 2  # 一輪只呼叫 export 一次
        (api_url, api_hdr, api_kw), (dl_url, dl_hdr, dl_kw) = net.gets
        assert api_url == "https://api.apprentissage.beta.gouv.fr/api/job/v1/export"
        assert api_hdr["Authorization"] == f"Bearer {SENTINEL}" and "grace-lba-monitor" in api_hdr["User-Agent"]
        assert dl_url == S3_URL and not any(k.lower() in ("authorization", "cookie", "x-api-key") for k in dl_hdr)
        assert SENTINEL not in json.dumps(dl_hdr) and SENTINEL not in dl_url
        assert api_kw["allow_redirects"] is False and dl_kw["allow_redirects"] is False and dl_kw["stream"] is True
        assert net.download.closed


def test_download_on_another_or_insecure_host_never_gets_key():
    d, sp = _tmp()
    with d:
        for url, expect_download in (("https://evil.example.net/x.json?sig=1", True), (f"https://{lba.API_HOST}/signed.json?sig=1", True),
                                     ("http://s3.example.com/x.json", False), ("file:///etc/passwd", False)):
            net = FakeNet(api=Resp(200, json_body={"url": url, "lastUpdate": "x"}))
            code, _, out, _ = _live(sp, net, dry_run=True)
            assert len(net.gets) == (2 if expect_download else 1), url
            if expect_download:
                assert not any(k.lower() == "authorization" for k in net.gets[1][1]), url  # 連同主機也不帶：預簽名連結不需要
            else:
                assert code == 1 and "不是 https" in out
            assert SENTINEL not in out


def test_missing_and_invalid_key_alert_once_then_recover():
    d, sp = _tmp()
    with d:
        net = FakeNet(api=Resp(401, json_body={"message": f"bad key {SENTINEL}"}))
        code, texts, out, _ = _live(sp, net)
        assert code == 0 and len(texts) == 1 and "LBA_API_KEY" in texts[0] and "HTTP 401" in texts[0]
        assert _state(sp) == {"seen": {}, "key_alert_sent": True}
        assert "[診斷] HTTP 401 api.apprentissage.beta.gouv.fr/api/job/v1/export" in out
        code, texts2, out2, _ = _live(sp, FakeNet(api=Resp(403)))  # 第二次：不再推播
        assert code == 0 and texts2 == [] and "已提醒過" in out2 and _state(sp)["key_alert_sent"] is True
        code, texts3, _, _ = _live(sp, FakeNet())  # key 修好：照常運作並重設旗標
        assert code == 0 and len(texts3) == 1 and _state(sp)["key_alert_sent"] is False
        code, texts4, _, _ = _live(sp, FakeNet(api=Resp(403)))  # 之後再壞會再提醒一次
        assert len(texts4) == 1 and "HTTP 403" in texts4[0]


def test_unset_or_malformed_key_alerts_without_any_request():
    for key in ("", "has space inside", "新字元", "line\nbreak"):
        d, sp = _tmp()
        with d:
            net = FakeNet()
            code, texts, out, _ = _live(sp, net, key=key)
            assert code == 0 and len(texts) == 1 and net.gets == [] and "未設定或格式不對" in texts[0]
            assert key.strip() == "" or key not in out + texts[0]


def test_rate_limit_stops_alerts_and_exits_zero():
    for status in (429, 419):
        d, sp = _tmp()
        with d:
            net = FakeNet(api=Resp(status, headers={"retry-after": "60"}))
            code, texts, out, _ = _live(sp, net)
            assert code == 0 and len(texts) == 1 and "429" in texts[0] and len(net.gets) == 1  # 不重試
            assert f"HTTP {status}" in out and "retry-after=60" in out and not sp.exists()
    d, sp = _tmp()
    with d:  # 下載那一步被限流也一樣
        code, texts, _, net = _live(sp, FakeNet(download=Resp(429)))
        assert code == 0 and len(texts) == 1 and "429" in texts[0]


def test_server_error_exits_nonzero_with_one_diagnostic_line_and_no_query_string():
    d, sp = _tmp()
    with d:
        code, texts, out, _ = _live(sp, FakeNet(api=Resp(503)))
        assert code == 1 and texts == [] and "[診斷] HTTP 503 api.apprentissage.beta.gouv.fr/api/job/v1/export" in out
        code, texts, out, _ = _live(sp, FakeNet(download=Resp(500)))
        assert code == 1 and "[診斷] HTTP 500 s3.rbx.io.cloud.ovh.net/bucket/export.json" in out
        assert "PRESIGNSECRET" not in out and "X-Amz" not in out and not sp.exists()
        code, _, out, _ = _live(sp, FakeNet(download=Resp(200, body=b'{"jobs": [{"x"')))
        assert code == 1 and "解析失敗" in out and not sp.exists()  # 壞檔：不處理、不寫狀態
        code, _, out, _ = _live(sp, FakeNet(download=Resp(200, body=b'{"foo": 1}')))
        assert code == 1 and "沒有解析出任何職缺" in out


def test_export_size_cap():
    d, sp = _tmp()
    with d:
        small = Path(d.name) / "cfg.json"
        small.write_text(json.dumps(CFG | {"max_export_mb": 0}), encoding="utf-8")
        code, texts, out, _ = _live(sp, FakeNet(), config=str(small))
        assert code == 1 and texts == [] and "超過上限" in out and not sp.exists()


def test_key_never_appears_anywhere():
    """用哨兵 key 跑成功與各種失敗路徑，檢查 stdout／stderr／狀態檔／Telegram payload／所有送往非 API 主機的請求。"""
    cases = [FakeNet(), FakeNet(api=Resp(401)), FakeNet(api=Resp(403)), FakeNet(api=Resp(429)), FakeNet(api=Resp(500)),
             FakeNet(download=Resp(500)), FakeNet(download=Resp(200, body=b"{broken"))]
    for net in cases:
        d, sp = _tmp()
        with d:
            code, texts, out, _ = _live(sp, net)
            blob = out + json.dumps(net.posts, ensure_ascii=False) + (sp.read_text(encoding="utf-8") if sp.exists() else "")
            assert SENTINEL not in blob, (net.api.status_code, net.download.status_code)
            for url, hdr, _ in net.gets:
                if not url.startswith(lba.EXPORT_URL):
                    assert SENTINEL not in url + json.dumps(hdr)
    d, sp = _tmp()
    with d:  # 連例外也不洩漏：requests 丟出的例外訊息帶著標頭值
        def boom(url, headers=None, **kw):
            raise RuntimeError(f"Invalid header value {headers}")
        net = FakeNet()
        net.get = boom
        code, texts, out, _ = _live(sp, net)
        assert code == 1 and SENTINEL not in out and "RuntimeError" in out


# --- 設定、狀態、workflow -------------------------------------------------------

def test_config_inherits_jobteaser_keywords_and_scoring_text():
    for k in ("keywords_positive", "keywords_negative", "keywords_skills", "persona", "rubric"):
        assert CFG[k], k
    assert CFG["push_threshold"] == 70 and CFG["digest_min"] == 60 and CFG["recent_days"] == 7
    assert {"75", "77", "78", "91", "92", "93", "94", "95", "33"} == set(CFG["priority_departments"])
    assert "inherit_from" not in CFG


def test_state_default_and_committed_file():
    with tempfile.TemporaryDirectory() as d:
        assert lba.load_state(Path(d) / "missing.json") == {"seen": {}, "key_alert_sent": False}
        (Path(d) / "bad.json").write_text("{oops", encoding="utf-8")
        assert lba.load_state(Path(d) / "bad.json") == {"seen": {}, "key_alert_sent": False}
    committed = json.loads((ROOT / "data" / "seen_lba.json").read_text())
    assert isinstance(committed.get("seen"), dict) and isinstance(committed.get("key_alert_sent"), bool)


def test_workflow_references_only_known_secrets_and_inputs():
    wf = (ROOT / ".github" / "workflows" / "lba-monitor.yml").read_text(encoding="utf-8")
    assert set(re.findall(r"secrets\.([A-Z_]+)", wf)) == {"LBA_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY"}
    assert set(re.findall(r"github\.event\.inputs\.([a-z_]+)", wf)) == {"max_new"}
    assert 'cron: "0 7 * * *"' in wf and "workflow_dispatch" in wf and "if: always()" in wf and "[skip ci]" in wf
    assert wf.index("git commit") < wf.index("git pull --rebase") < wf.index("git push")
    assert "src/lba_monitor.py" in wf and "data/seen_lba.json" in wf


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
