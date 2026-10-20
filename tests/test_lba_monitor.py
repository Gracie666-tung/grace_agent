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
    assert kw.rank(_job("Alternant Contrôleur de gestion", "budget"))[0]  # 法文標題字（標題命中一個就夠）
    assert kw.rank(_job("Assistant", "Pilotage de la chaîne d'approvisionnement, tableau de bord"))[0]  # 全文裡的法文字
    assert not kw.rank(_job("Aide boulanger", "pains et viennoiseries"))[0]  # 沒有任何正面字
    assert not kw.rank(_job("Community manager", "réseaux sociaux, contenu, supply chain"))[0]  # 標題負面、標題無正面
    assert kw.rank(_job("Alternant PMO - communication projet", "planning"))[0]  # 標題負面但標題也有正面字
    assert kw.rank(_job("Alternant achats", "On attend de bonnes capacités de communication et de paie"))[0]  # 全文的負面字不殺
    assert kw.rank(_job("Alternant achats", "x", dept="75"))[1] == kw.rank(_job("Alternant achats", "x"))[1] + 3  # 地點加權
    assert kw.rank(_job("Alternant achats", "x", dept="33"))[1] > kw.rank(_job("Alternant achats", "x", dept="69"))[1]
    assert kw.rank(_job("Analyste S&OP", "Excel, SQL"))[0]  # & 保留（標題命中）


# --- 離線端到端：保留／丟棄各路徑 ---------------------------------------------------

def test_fixture_end_to_end_paths_state_and_messages():
    d, sp = _tmp()
    with d:
        code, texts, out, _ = _fixture_run(sp)
        assert code == 0
        assert "匯出 12 筆：資料不全 1、超過 7 天 1、已過期 1、開始太早 0、匯出內重複 1、已見 0、關鍵字丟棄 2、法文 D 1" in out
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
        code, texts, out, _ = _live(sp, FakeNet(download=Resp(200, body=b'{"foo": 1}')))
        assert code == 1 and "匯出結構異常" in out and "最外層鍵=['foo']" in out and len(texts) == 1


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


# --- 審查後新增的回歸測試 -----------------------------------------------------------

def test_keyword_prefilter_title_or_two_distinct_body_words():
    kw = lba.Keywords(CFG)
    # 這三則在舊邏輯（全文只要一個正面字就保留）都會被放行
    assert not kw.rank(_job("Apprenti(e) boulanger H/F", "Fabrication du pain, gestion des stocks de farine, nettoyage du fournil."))[0]
    assert not kw.rank(_job("Aide-soignant en apprentissage", "Soins aux résidents, planification des tournées, transformation du projet d'établissement."))[0]
    assert not kw.rank(_job("Apprenti cuisinier", "Transformation des produits frais, méthode lean en cuisine, mise en place."))[0]
    assert kw.rank(_job("Alternance Supply Chain Analyst H/F", "Missions variées."))[0]  # 標題命中
    assert kw.rank(_job("Alternance Assistant(e) H/F", "Vous soutenez le contrôle de gestion et le tableau de bord mensuel."))[0]  # 內文兩個不同字
    assert not kw.rank(_job("Alternance Assistant(e) H/F", "contrôle de gestion, contrôle de gestion, contrôle de gestion"))[0]  # 同一個字重複不算
    assert kw.rank(_job("Alternant Contrôleur de gestion", "Budget."))[0]
    assert kw.rank(_job("Alternant Lean Manager", "Atelier."))[0] and kw.rank(_job("Chargé de transformation", "x"))[0]  # title_only 的字標題有才算
    assert not kw.rank(_job("Alternance Assistant(e) H/F", "lean et transformation, lean, transformation"))[0]


FRENCH_LBA_CASES = [
    ("Anglais courant ou bilingue", "A"),  # 舊邏輯：D（bilingue 單獨就觸發）
    ("Anglais langue maternelle ou bilingue", None),  # 只要求不是 D
    ("Bilingue anglais apprécié", None),
    ("Français : niveau C1 exigé.", "D"),  # 舊邏輯：N
    ("Maîtrise parfaite de la langue française.", "D"),  # 舊邏輯：N
    ("Excellente expression écrite et orale en français.", "D"),  # 舊邏輯：N
    ("Français niveau B2 minimum", "C"),  # 舊邏輯：N
    ("Français courant", "D"),
    ("French is a plus", "B"),
    ("Profil bilingue (français / anglais)", "D"),  # bilingue 綁在 français 旁邊才算
    ("English C1, French B1 minimum", "C"),  # 逗號隔開：C1 是英文的
    ("Anglais C1. Français B2 souhaité", "C"),  # C1 是英文的，B2 才是法文的
    ("Excellent written and spoken French required", "D"),
    ("<li>Anglais C1</li><li>Français niveau B1</li>", "C"),
]


def test_french_class_lba_proximity_rules():
    wrong = []
    for text, want in FRENCH_LBA_CASES:
        got = lba.classify_french_lba(text)
        if (want and got != want) or (want is None and got == "D"):
            wrong.append((text, want, got))
    assert not wrong, wrong
    # jobteaser 既有的 21 個案例在 LBA 版也要同結果
    from test_jobteaser_monitor import FRENCH_CASES
    wrong = [(t, w, lba.classify_french_lba(t)) for t, w in FRENCH_CASES if lba.classify_french_lba(t) != w]
    assert not wrong, wrong
    # 描述的 HTML 區塊要變成句點，列表項目才不會黏成同一個子句
    job, _ = lba.parse_offer(_raw_with(desc="<ul><li>Anglais C1</li><li>Français niveau B1</li></ul>"), NOW, 7)
    assert lba.classify_french_lba(job["text"]) == "C"


def _raw_with(desc=None, **pub):
    raw = json.loads(json.dumps(json.loads(EXPORT)["jobs"][0]))
    if desc is not None:
        raw["offer"]["description"] = desc
    raw["offer"]["publication"].update(pub)
    return raw


def test_offer_status_filters_inactive_but_keeps_missing_or_active():
    for status, kept in (("Active", True), ("active", True), (None, True), ("", True), ("Filled", False), ("Cancelled", False), ("filled", False)):
        raw = _raw_with()
        if status is not None:
            raw["offer"]["status"] = status
        job, why = lba.parse_offer(raw, NOW, 7)
        assert (job is not None) == kept and (kept or why == "inactive"), status
    assert "status" not in json.loads(EXPORT)["jobs"][0]["offer"]  # fixture 預設沒有 status（缺欄位仍保留）


def test_date_only_expiration_counts_as_end_of_that_day():
    assert lba.parse_offer(_raw_with(expiration="2026-10-20"), NOW, 7)[0] is not None  # 07:00 UTC，當天還有效
    assert lba.parse_offer(_raw_with(expiration="2026-10-19"), NOW, 7) == (None, "expired")
    late = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
    assert lba.parse_offer(_raw_with(expiration="2026-10-20"), late, 7) == (None, "expired")
    assert lba.parse_offer(_raw_with(expiration="2026-10-20T06:00:00Z"), NOW, 7) == (None, "expired")  # 有時間的照舊


def test_userinfo_never_printed_for_download_url():
    d, sp = _tmp()
    with d:
        url = "https://dluser:USERINFOSECRET@dl.example.net:8443/bucket/x.json?sig=QUERYSECRET"
        net = FakeNet(api=Resp(200, json_body={"url": url, "lastUpdate": "x"}), download=Resp(500))
        code, _, out, _ = _live(sp, net)
        assert code == 1 and "[診斷] HTTP 500 dl.example.net:8443/bucket/x.json" in out and "USERINFOSECRET" not in out + str(net.posts)
        assert "QUERYSECRET" not in out and "dluser" not in out
        net = FakeNet(api=Resp(200, json_body={"url": url, "lastUpdate": "x"}))  # 成功路徑那行「下載主機」也一樣
        code, _, out, _ = _live(sp, net, dry_run=True)
        assert "下載主機 dl.example.net:8443（與 API 不同主機" in out and "USERINFOSECRET" not in out
    sp_ = lba.urlsplit("https://u:p@Host.Example.com/a?b=1")
    assert lba.host_label(sp_) == "host.example.com" and lba.diag_line(404, "https://u:p@h.io/a/b?x=1") == "[診斷] HTTP 404 h.io/a/b"


def _export_body(jobs, extra=None):
    return json.dumps({"jobs": jobs} | (extra or {})).encode()


def test_unrecognised_export_structure_fails_loudly_with_key_names_only():
    secret_value = "VALUESECRET_must_not_be_logged"
    # 真實 API 若把日期包成 {"$date": ...}、欄位改名，舊程式會「資料不全 3、候選 0」然後 exit 0
    jobs = [{"identifier": {"id": f"id{i}", "partner_job_id": secret_value}, "offer": {"title": secret_value, "publication": {"creation": {"$date": "2026-10-18T00:00:00Z"}}},
             "apply": {"url": "https://x.example/" + secret_value}, "newfield": [1], "workplace": {"name": secret_value}} for i in range(3)]
    d, sp = _tmp()
    with d:
        code, texts, out, net = _live(sp, FakeNet(download=Resp(200, body=_export_body(jobs, {"recruiters": []}))))
        assert code == 1 and not sp.exists()
        assert len(texts) == 1 and "欄位結構" in texts[0] and "[診斷]" in texts[0]
        diag = [ln for ln in out.splitlines() if ln.startswith("[診斷] 結構")]
        assert len(diag) == 1
        assert "最外層鍵=['jobs', 'recruiters']" in diag[0]
        assert "identifier{id,partner_job_id}" in diag[0] and "offer{title,publication}" in diag[0] and "newfield[]" in diag[0]
        assert "workplace{name}" in diag[0] and "apply{url}" in diag[0]
        for leak in (secret_value, "PRESIGNSECRET", SENTINEL, "2026-10-18"):
            assert leak not in out + "".join(texts), leak
        # dry-run：一樣 exit 1、印診斷，但不送 Telegram
        code, texts, out, net = _run(_args(no_llm=True, dry_run=True), sp, env={"LBA_API_KEY": SENTINEL}, net=FakeNet(download=Resp(200, body=_export_body(jobs))))
        assert code == 1 and net.posts == [] and "[診斷] 結構" in out
        # 空的、只有企業名單、最外層是陣列：同樣要報
        for body, want in ((b"[]", "最外層=陣列"), (b'[{"a": {"b": 1}}]', "a{b}"), (_export_body([], {"recruiters": [{"x": 1}]}), "最外層鍵=['jobs', 'recruiters']"),
                           (_export_body([{"identifier": {"partner_label": "recruteurs_lba", "id": "r1"}}] * 3), "identifier{partner_label,id}")):
            code, texts, out, _ = _live(sp, FakeNet(download=Resp(200, body=body)))
            assert code == 1 and len(texts) == 1 and want in out, (body, out)


def test_malformed_ratio_threshold_is_90_percent_of_non_recruiters():
    good = json.loads(EXPORT)["jobs"][0]
    bad = {"identifier": {"id": "bad"}, "offer": {"title": "x"}}
    recruiter = {"identifier": {"id": "r", "partner_label": "recruteurs_lba"}}
    d, sp = _tmp()
    with d:
        # 10 筆裡 8 筆壞（80%）：不到門檻，照常處理
        code, texts, out, _ = _live(sp, FakeNet(download=Resp(200, body=_export_body([good] + [dict(bad, identifier={"id": f"b{i}"}) for i in range(8)] + [good | {"identifier": {"id": "g2"}}]))))
        assert code == 0 and "資料不全 8" in out
        # 9 筆壞（90%）：報錯；企業名單不計入分母，也不算資料不全
        jobs = [good] + [dict(bad, identifier={"id": f"b{i}"}) for i in range(9)] + [recruiter] * 5
        code, texts, out, _ = _live(sp, FakeNet(download=Resp(200, body=_export_body(jobs))))
        assert code == 1 and "10 筆中 9" not in out and "15 筆中 9 筆讀不出必要欄位（企業名單 5 筆不計）" in out
        # 企業名單很多、職缺本身都正常：不報錯
        code, _, out, _ = _live(sp, FakeNet(download=Resp(200, body=_export_body([good] + [recruiter] * 50))))
        assert code == 0 and "企業名單" not in out.split("匯出結構異常")[0]


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
    code = "\n".join(ln for ln in wf.splitlines() if not ln.lstrip().startswith("#"))
    assert set(re.findall(r"secrets\.([A-Z_]+)", wf)) == {"LBA_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY"}
    assert set(re.findall(r"github\.event\.inputs\.([a-z_]+)", wf)) == {"max_new", "dry_run"}
    assert "workflow_dispatch" in code and re.search(r'cron: "0 7 \* \* \*"', code)  # 每天一次；排程觸發沒有 inputs，所以不是 dry-run
    assert re.search(r"dry_run:\s*\n(?:\s+.*\n)*?\s+type: boolean\n(?:\s+.*\n)*?\s+default: true", wf)
    assert "--dry-run" in wf and "[skip ci]" in wf and "src/lba_monitor.py" in wf and "data/seen_lba.json" in wf
    persist = wf[wf.index("Persist seen offers"):]
    assert "if: always() && github.event.inputs.dry_run != 'true'" in persist  # dry-run 不 commit
    assert persist.index("git commit") < persist.index("git pull --rebase") < persist.index("git push")
    assert re.search(r"for \w+ in 1 2 3; do", persist) and "exit 1" in persist  # push 最多試 3 次


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()


def test_min_start_date_drops_early_starts_but_keeps_unknown():
    jobs = json.loads((FIX / "export.json").read_text(encoding="utf-8"))["jobs"]
    base = next(j for j in jobs if j["offer"]["title"] == "Alternant Supply Chain Analyst (H/F)")
    min_start = lba.parse_ts("2026-12-01")

    def with_start(v):
        j = json.loads(json.dumps(base))
        j["contract"]["start"] = v
        return j
    assert lba.parse_offer(with_start("2026-09-01T00:00:00.000Z"), NOW, 7, min_start) == (None, "early")
    assert lba.parse_offer(with_start("2026-11-30T23:59:59.000Z"), NOW, 7, min_start) == (None, "early")
    assert lba.parse_offer(with_start("2026-12-01T00:00:00.000Z"), NOW, 7, min_start)[0] is not None  # 邊界：剛好 12/01 保留
    assert lba.parse_offer(with_start("2027-01-04T00:00:00.000Z"), NOW, 7, min_start)[0] is not None
    assert lba.parse_offer(with_start(None), NOW, 7, min_start)[0] is not None  # 沒寫開始日：保留
    assert lba.parse_offer(with_start("2026-09-01T00:00:00.000Z"), NOW, 7)[0] is not None  # 沒設 min_start：不過濾
