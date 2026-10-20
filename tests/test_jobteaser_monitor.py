#!/usr/bin/env python3
"""jobteaser_monitor 的離線測試：不連網、不呼叫 Claude、不送 Telegram。
HTML 都是依已驗證的網站結構自行捏造的最小範例（synthetic），不是真實 JobTeaser 頁面。

    python tests/test_jobteaser_monitor.py      # 或 python -m pytest tests/test_jobteaser_monitor.py -q
"""

import json
import os
import sys
import tempfile
import threading
from argparse import Namespace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import jobteaser_monitor as jm  # noqa: E402

NOW = datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)
FIXTURES = ROOT / "tests" / "fixtures" / "jobteaser"
CFG = jm.load_config(ROOT / "jobteaser_config.json")
FAKE_COOKIE = "_session_id=SECRETCOOKIEVALUE123; remember=ABCDEF999"
U1, U2, U3 = ("e0e91a2d-dc0d-4952-a737-b7dfe310d959", "11111111-2222-4333-8444-555555555555",
              "22222222-3333-4444-8555-666666666666")


def _args(**kw):
    d = dict(dry_run=False, max_pages=0, max_new=None, fixture_dir=None, no_llm=False, config=str(ROOT / "jobteaser_config.json"))
    d.update(kw)
    return Namespace(**d)


def _env(**kw):
    base = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "1", "JOBTEASER_COOKIE": "", "ANTHROPIC_API_KEY": ""}
    base.update(kw)
    return mock.patch.dict(os.environ, base)


def _resp(text="", status=200, url="https://kedgebs.jobteaser.com/en/job-offers"):
    return SimpleNamespace(status_code=status, url=url, text=text, raise_for_status=lambda: None)


def _run(args, sent, state_path, env=None, now=NOW):
    with _env(**(env or {})), mock.patch.dict(os.environ, {"JOBTEASER_STATE_PATH": str(state_path)}), \
            mock.patch.object(jm, "send_telegram", side_effect=lambda t, c, text: sent.append(text)), \
            mock.patch.object(jm.time, "sleep"):
        return jm.run(args, now=now)


# --- 列表連結 ---------------------------------------------------------------

LIST_HTML = f"""<html><body>
<a href="/en/job-offers/{U1}-mondelez-stage-janvier-2027?a=1">x</a><img src="/en/job-offers/{U1}-mondelez-stage-janvier-2027">
<a href="https://kedgebs.jobteaser.com/en/job-offers/{U2}-airbus-bizops">y</a>
<a href="/en/job-offers?page=2">next</a><a href="/en/job-offers/{U3}-third">z</a><a href="/en/job-offers/{U2}-airbus-bizops">dup</a>
</body></html>"""


def test_extract_links_dedupe_and_order():
    links = jm.extract_links(LIST_HTML)
    assert [u for u, _ in links] == [U1, U2, U3]  # 依出現順序、uuid 去重
    assert links[0][1] == f"/en/job-offers/{U1}-mondelez-stage-janvier-2027"  # 去掉查詢參數，補成站內路徑
    assert links[1][1] == f"/en/job-offers/{U2}-airbus-bizops"
    assert jm.extract_links("") == [] and jm.extract_links("<a href='/en/job-offers?page=2'>") == []


def test_collect_links_stops_when_page_has_nothing_new():
    pages = {1: LIST_HTML, 2: LIST_HTML, 3: LIST_HTML}
    src = SimpleNamespace(list_page=lambda n: pages[n])
    links, halted = jm.collect_links(src, 3)
    assert [u for u, _ in links] == [U1, U2, U3] and not halted


# --- 詳情 JSON-LD -----------------------------------------------------------

def _detail_html(ld):
    return f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head><body><h1>H1 Title</h1></body></html>'


def test_parse_detail_full_jsonld():
    ld = {"@type": "JobPosting", "title": "Supply Chain Intern", "datePosted": "2026-09-11T07:46:35.000Z",
          "description": "&lt;p&gt;Planification &amp;amp; S&amp;OP&lt;/p&gt;", "employmentType": ["INTERN"],
          "hiringOrganization": {"name": "Mondelez"},
          "jobLocation": [{"address": {"addressLocality": "Paris", "addressCountry": "FR"}}]}
    job = jm.parse_detail(_detail_html(ld), U1, f"/en/job-offers/{U1}-x", "https://kedgebs.jobteaser.com")
    assert (job["title"], job["company"], job["location"], job["employment_type"]) == ("Supply Chain Intern", "Mondelez", "Paris", "INTERN")
    assert job["date_posted"].startswith("2026-09-11") and "<" not in job["text"] and "S&OP" in job["text"]
    assert job["url"] == f"https://kedgebs.jobteaser.com/en/job-offers/{U1}-x"


def test_parse_detail_missing_fields_never_crashes():
    only_date = jm.parse_detail(_detail_html({"@type": "JobPosting", "datePosted": "2026-09-11"}), U1, f"/en/job-offers/{U1}-slug-here", "https://h")
    assert only_date["title"] == "H1 Title" and only_date["company"] == "" and only_date["location"] == ""
    assert only_date["date_posted"] == "2026-09-11"
    bare = jm.parse_detail("<html><body>no json here <b>Texte</b></body></html>", U1, f"/en/job-offers/{U1}-slug-here", "https://h")
    assert bare["title"] == "slug here" and "Texte" in bare["text"]  # h1 也沒有：退回網址 slug
    broken = jm.parse_detail('<script type="application/ld+json">{oops</script><h1>T</h1>', U1, f"/en/job-offers/{U1}-s", "https://h")
    assert broken["title"] == "T"
    weird = jm.parse_detail(_detail_html({"@type": "JobPosting", "title": None, "hiringOrganization": 5, "jobLocation": "Lyon"}), U1, f"/en/job-offers/{U1}-s", "https://h")
    assert weird["location"] == "Lyon" and weird["company"] == ""


# --- 法文要求分級 -----------------------------------------------------------

FRENCH_CASES = [
    ("Fluent English required. International environment.", "A"),
    ("Working language: English. Our team is English-speaking.", "A"),
    ("Anglais courant exigé, environnement international.", "A"),
    ("French is a plus.", "B"),
    ("Le français est un plus.", "B"),
    ("Fluent in English; French would be appreciated", "B"),
    ("French is not required for this role. Fluent English.", "A"),  # 明講不需要法文＋有英文要求 -> A
    ("Good level of French (B2).", "C"),
    ("English C1, French B1 minimum", "C"),
    ("Professional working proficiency in French and English", "C"),
    ("Bon niveau de français exigé", "C"),
    ("Fluent French required", "D"),
    ("Native French speaker", "D"),
    ("French C1 or above", "D"),
    ("Français courant exigé", "D"),
    ("Maîtrise du français indispensable", "D"),
    ("Candidat bilingue français-anglais", "D"),
    ("Langue maternelle française", "D"),
    ("Le français est exigé", "D"),
    ("Excellent command of French is mandatory", "D"),
    ("Fluent in English, French is a plus", "B"),  # 流利的是英文，不是法文
]


def test_french_classifier_phrases():
    wrong = [(t, want, jm.classify_french(t)) for t, want in FRENCH_CASES if jm.classify_french(t) != want]
    assert not wrong, wrong


def test_french_written_jd_without_level_is_not_d():
    jd = ("Vous rejoindrez l'équipe supply chain pour piloter les indicateurs de performance. Vos missions : "
          "suivre les stocks, produire des tableaux de bord. Profil : étudiant en école de commerce, rigoureux.")
    assert jm.classify_french(jd) == "N"
    assert jm.classify_french(jd + " Anglais courant, environnement international.") == "A"


FRENCH_NEGATED_CASES = [  # 明講不需要法文：有英文要求 A，沒有 B（絕不能是 D）
    ("No French required", "B"),
    ("French is not required", "B"),
    ("French is not needed", "B"),
    ("Without French", "B"),
    ("Français non requis", "B"),
    ("Pas besoin de parler français", "B"),
    ("Aucune connaissance du français requise", "B"),
    ("English is the working language. No French required.", "A"),
    ("Fluent English; French is not needed", "A"),
    ("Anglais courant. Aucune connaissance du français requise.", "A"),
    ("French required", "D"),
    ("Français exigé", "D"),
]
FRENCH_FLUENT_CASES = [
    ("Français et anglais courants", "D"),
    ("Anglais et français courants", "D"),
    ("Français courant", "D"),
    ("French language skills are mandatory", "D"),
    ("Strong French and English skills are required", "D"),
    ("Strong French and English skills", "C"),  # 沒有 fluent／native／mandatory／required 就是 C
    ("Anglais courant, français souhaité", "B"),
    ("English required, French preferred", "B"),
]


def test_french_negation_is_not_class_d():
    wrong = [(t, want, jm.classify_french(t)) for t, want in FRENCH_NEGATED_CASES if jm.classify_french(t) != want]
    assert not wrong, wrong


def test_french_fluent_phrasings_and_souhaite():
    wrong = [(t, want, jm.classify_french(t)) for t, want in FRENCH_FLUENT_CASES if jm.classify_french(t) != want]
    assert not wrong, wrong


# --- 粗篩 -------------------------------------------------------------------

def _pf(slug):
    return jm.prefilter(slug, CFG)


def test_prefilter_rules():
    assert _pf("stage business analyst paris")[0]
    assert _pf("supply chain planning intern")[0]
    assert not _pf("community manager stage")[0]
    assert _pf("project coordinator social media tools rollout")[0]  # 有強正面字，負面字不單獨殺
    assert not _pf("stage assistant relations publiques")[0] and not _pf("marketing communication intern")[0]
    assert _pf("fp a excel sql power bi intern")[1] > _pf("generic intern")[1]
    assert _pf("supply chain demand planning")[1] > _pf("supply chain")[1]  # 命中越多排越前


def test_prefilter_matches_ampersand_keywords_in_slugs():
    assert _pf("stage fp a analyst")[0] and _pf("stage fp a analyst")[1] > _pf("stage analyst")[1]  # fp&a
    assert _pf("s op planner intern")[1] > 0 and _pf("stage s&op planner")[1] > 0  # s&op 兩種寫法
    assert not _pf("stage pr tax assistant")[0]  # 負面字仍照常擋


def test_prefilter_accents_and_french_terms():
    assert _pf("stage controle de gestion industriel")[0]
    assert not _pf("stage developpeur full stack")[0]
    assert jm.slug_text(f"/en/job-offers/{U1}-mondelez-stage-x") == "mondelez stage x"


# --- 分數 -> 動作 ------------------------------------------------------------

def test_score_routing():
    assert [jm.route(s, CFG) for s in (80, 72, 65, 59)] == ["push", "push", "digest", "low"]
    assert [jm.route(s, CFG) for s in (70, 69, 60)] == ["push", "digest", "digest"]


def _score_json(**kw):
    d = dict(functional=30, career=20, industry=15, skills=10, language=15, international=5, location=5,
             reason_zh="r", title_zh="t", summary_zh="s", start_date="2027-01", contract="internship")
    d.update(kw)
    return json.dumps(d)


def test_parse_score_validates_and_clamps():
    r = jm.parse_score("```json\n" + _score_json(functional=99, skills=-3, location="4.6") + "\n```")
    assert r["sub"]["functional"] == 30 and r["sub"]["skills"] == 0 and r["sub"]["location"] == 5
    assert r["total"] == 30 + 20 + 15 + 0 + 15 + 5 + 5
    assert jm.parse_score("blah " + _score_json() + " blah")["total"] == 100  # 前後夾雜文字
    assert jm.parse_score(_score_json(contract="excluded"))["total"] == 59  # CDI/VIE 不推
    assert jm.parse_score("not json") is None and jm.parse_score("") is None and jm.parse_score("[1]") is None
    assert jm.parse_score(json.dumps({"functional": 3})) is None  # 缺分項


def test_excluded_contract_variants_are_capped_at_59():
    base = dict(functional=30, career=20, industry=15, skills=10, language=15, international=5, location=5)
    for c in ("excluded", "CDI", "cdi (permanent)", "VIE", "V.I.E.", "Graduate Programme", "graduate", "alternance-excluded",
              "permanent", "Volontariat International"):
        res = jm.parse_score(json.dumps(base | {"contract": c}))
        assert res["total"] == 59 and res["contract"] == "excluded", c
    for c in ("internship", "alternance", "unknown", "", None, "postgraduate-internship"):
        assert jm.parse_score(json.dumps(base | {"contract": c}))["total"] == 100, c


def test_score_job_retries_once_then_gives_up():
    job = {"uuid": U1, "title": "T", "fr": "A", "text": "x"}
    calls = []
    assert jm.score_job(job, "sys", lambda s, p, u: calls.append(1) or "garbage") is None and len(calls) == 2
    seq = iter(["garbage", _score_json()])
    assert jm.score_job(job, "sys", lambda s, p, u: next(seq))["total"] == 100
    def boom(s, p, u):
        raise RuntimeError("x")
    assert jm.score_job(job, "sys", boom) is None


def test_persona_embedded_in_system_prompt():
    system = jm.build_system(CFG)
    assert all(line in system for line in CFG["persona"]) and len(CFG["persona"]) == 24
    assert "Language feasibility is critical." in system and "100 points" in system


# --- Telegram 訊息 ------------------------------------------------------------

def test_telegram_truncation_and_digest_packing():
    job = {"title": "T" * 3000 + " & <b>", "company": "C" * 500, "location": "L" * 500, "employment_type": "x",
           "fr": "N", "url": "https://kedgebs.jobteaser.com/en/job-offers/" + U1}
    res = {"sub": {k: 1 for k, _, _ in jm.SUBSCORES}, "total": 7, "contract": "internship", "reason_zh": "好" * 900,
           "title_zh": "題" * 900, "summary_zh": "摘要&" * 900, "start_date": "2027-01"}
    msg = jm.format_job(job, res)
    assert len(msg) <= jm.TG_LIMIT and msg.splitlines()[-1].startswith("<a href=") and msg.endswith("</a>")
    assert msg.count("<b>") == msg.count("</b>")
    entries = [jm.format_digest_entry(job, res) for _ in range(30)]
    chunks = jm.pack("HEAD", entries)
    assert len(chunks) > 1 and all(len(c) <= jm.TG_LIMIT and c.startswith("HEAD") for c in chunks)
    assert sum(c.count("<a href=") for c in chunks) == 30


# --- 整合：fixture、cookie、429 ----------------------------------------------

def test_fixture_end_to_end_state_and_messages():
    with tempfile.TemporaryDirectory() as d:
        sp, sent = Path(d) / "s.json", []
        assert _run(_args(fixture_dir=str(FIXTURES)), sent, sp) == 0
        state = json.loads(sp.read_text())
        assert sorted(v["status"] for v in state["seen"].values()) == ["digest", "filtered_french", "low", "prefiltered", "pushed", "pushed", "score_failed"]
        assert len(sent) == 3 and sum("🎯" in m for m in sent) == 2 and sum("候選" in m for m in sent) == 1
        assert state["seen"]["a0000001-0000-4000-8000-000000000001"]["score"] == 94
        # 第二輪：只剩 score_failed 會重試（仍壞），其餘不重推
        sent.clear()
        _run(_args(fixture_dir=str(FIXTURES)), sent, sp)
        assert sent == [] and json.loads(sp.read_text())["seen"]["a0000007-0000-4000-8000-000000000007"]["tries"] == 2


def test_dry_run_writes_nothing_and_sends_nothing():
    with tempfile.TemporaryDirectory() as d:
        sp, sent = Path(d) / "s.json", []
        assert _run(_args(fixture_dir=str(FIXTURES), dry_run=True), sent, sp, env={"TELEGRAM_BOT_TOKEN": ""}) == 0
        assert sent == [] and not sp.exists()


def test_max_new_cap_leaves_leftovers_unseen():
    with tempfile.TemporaryDirectory() as d:
        sp, sent = Path(d) / "s.json", []
        _run(_args(fixture_dir=str(FIXTURES), max_new=2), sent, sp)
        seen = json.loads(sp.read_text())["seen"]
        processed = [v for v in seen.values() if v["status"] != "prefiltered"]
        assert len(processed) == 2 and len(seen) == 3  # 6 則通過粗篩只處理 2 則，其餘不記為已見


def test_no_cookie_exits_cleanly_sends_nothing():
    with tempfile.TemporaryDirectory() as d:
        sp, sent = Path(d) / "s.json", []
        with mock.patch.object(jm.requests, "get", side_effect=AssertionError("不該連網")):
            assert _run(_args(), sent, sp) == 0
        assert sent == [] and not sp.exists()


def test_cookie_expired_redirect_alerts_once_and_sets_flag():
    with tempfile.TemporaryDirectory() as d:
        sp, sent = Path(d) / "s.json", []
        redirected = _resp("<form action='/en/users/sign_in'>", url="https://kedgebs.jobteaser.com/en/users/sign_in?redirect_after_authentication=x")
        with mock.patch.object(jm.requests, "get", return_value=redirected):
            assert _run(_args(), sent, sp, env={"JOBTEASER_COOKIE": FAKE_COOKIE}) == 0
            assert len(sent) == 1 and "JOBTEASER_COOKIE" in sent[0]
            assert json.loads(sp.read_text())["cookie_alert_sent"] is True
            assert _run(_args(), sent, sp, env={"JOBTEASER_COOKIE": FAKE_COOKIE}) == 0
            assert len(sent) == 1  # 第二輪不重複提醒


def test_cookie_expired_variants_and_flag_reset():
    for resp in (_resp(status=403), _resp(status=401), _resp("<input type='password'> sign_in")):
        with tempfile.TemporaryDirectory() as d:
            sent = []
            with mock.patch.object(jm.requests, "get", return_value=resp):
                assert _run(_args(), sent, Path(d) / "s.json", env={"JOBTEASER_COOKIE": FAKE_COOKIE}) == 0
            assert len(sent) == 1
    with tempfile.TemporaryDirectory() as d:  # cookie 換新、列表正常後旗標歸零
        sp, sent = Path(d) / "s.json", []
        sp.write_text(json.dumps({"seen": {}, "cookie_alert_sent": True}))
        with mock.patch.object(jm.requests, "get", return_value=_resp("<html></html>")):
            _run(_args(), sent, sp, env={"JOBTEASER_COOKIE": FAKE_COOKIE})
        assert json.loads(sp.read_text())["cookie_alert_sent"] is False and sent == []


def test_rate_limit_429_stops_without_hammering():
    with tempfile.TemporaryDirectory() as d:
        sp, sent = Path(d) / "s.json", []
        with mock.patch.object(jm.requests, "get", return_value=_resp(status=429)) as g:
            assert _run(_args(), sent, sp, env={"JOBTEASER_COOKIE": FAKE_COOKIE}) == 0
        assert g.call_count == 1 and len(sent) == 1 and "429" in sent[0]


def test_forbidden_403_alert_does_not_assert_expiry_but_redirect_does():
    for resp, blocked in ((_resp(status=403), True), (_resp(status=401), False),
                          (_resp("x", url="https://kedgebs.jobteaser.com/en/users/sign_in"), False)):
        with tempfile.TemporaryDirectory() as d:
            sent = []
            with mock.patch.object(jm.requests, "get", return_value=resp):
                _run(_args(), sent, Path(d) / "s.json", env={"JOBTEASER_COOKIE": FAKE_COOKIE})
            assert len(sent) == 1
            assert ("Cloudflare" in sent[0] and "HTTP 403" in sent[0] and "可能過期" in sent[0]) == blocked
            assert ("cookie 已過期" in sent[0]) == (not blocked)


def test_empty_later_list_page_keeps_links_and_never_raises():
    nav = "<nav><a href='/en/users/sign_in'>Sign in</a></nav>"  # 一般頁面的導覽列也可能有 sign_in 連結
    pages = {1: nav + LIST_HTML, 2: nav + "<p>No more results</p>", 3: LIST_HTML}
    links, halted = jm.collect_links(SimpleNamespace(list_page=lambda n: pages[n]), 3)
    assert [u for u, _ in links] == [U1, U2, U3] and not halted  # 第 2 頁空 -> 分頁結束、已收集的保留
    pages = {1: LIST_HTML, 2: nav + "<input type='password'>", 3: LIST_HTML}  # 第 2 頁起不再檢查登入頁
    links, _ = jm.collect_links(SimpleNamespace(list_page=lambda n: pages[n]), 3)
    assert [u for u, _ in links] == [U1, U2, U3]


def test_empty_first_page_is_cookie_expired_only_with_signin_form():
    for html_, expired in (("<form action='/en/users/sign_in'><input name=x></form>", True),
                           ('<input type="password" name="p">', True),
                           ("<input type='password'>", True),
                           ("<nav><a href='/en/users/sign_in'>Sign in</a></nav><p>0 results</p>", False)):
        try:
            jm.collect_links(SimpleNamespace(list_page=lambda n, h=html_: h), 2)
            raised = False
        except jm.CookieExpired:
            raised = True
        assert raised == expired, html_


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hits.append((self.path, self.headers.get("Cookie")))
        loc = self.server.redirects.get(self.path.split("?")[0])
        if loc:
            self.send_response(302)
            self.send_header("Location", loc)
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"final page")

    def log_message(self, *a):
        pass


def _server(redirects=None):
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    srv.hits, srv.redirects = [], redirects or {}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _live(srv):
    return jm.LiveSource(FAKE_COOKIE, {"base_url": f"http://127.0.0.1:{srv.server_port}"})


def test_same_host_redirect_chain_resends_cookie_on_every_hop():
    srv = _server({"/a": "/b", "/b": "/c"})
    try:
        with mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1"}), mock.patch.object(jm.time, "sleep"):
            assert _live(srv).detail("/a") == "final page"
    finally:
        srv.shutdown()
    assert srv.hits == [("/a", FAKE_COOKIE), ("/b", FAKE_COOKIE), ("/c", FAKE_COOKIE)]


def test_redirect_to_other_host_never_carries_cookie():
    other = _server()
    srv = _server({"/a": f"http://127.0.0.1:{other.server_port}/steal"})  # 不同 port = 不同 origin
    try:
        with mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1"}), mock.patch.object(jm.time, "sleep"):
            try:
                _live(srv).detail("/a")
                raised = False
            except jm.CookieExpired:
                raised = True
    finally:
        srv.shutdown()
        other.shutdown()
    assert raised and other.hits == []  # 對方主機連一個請求都沒收到


def test_redirect_to_sign_in_is_cookie_expired_and_loops_are_bounded():
    srv = _server({"/a": "/en/users/sign_in?redirect_after_authentication=x", "/loop": "/loop"})
    try:
        with mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1"}), mock.patch.object(jm.time, "sleep"):
            for path, exc in (("/a", jm.CookieExpired), ("/loop", RuntimeError)):
                try:
                    _live(srv).detail(path)
                    assert False, path
                except exc:
                    pass
    finally:
        srv.shutdown()
    assert [h for h in srv.hits if h[0].startswith("/en/users")] == []  # 不會去請求登入頁
    assert len([h for h in srv.hits if h[0] == "/loop"]) == jm.MAX_REDIRECTS + 1


def test_max_pages_is_hard_capped():
    for requested in (0, 999):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(jm, "collect_links", return_value=([], False)) as cl:
            _run(_args(max_pages=requested, fixture_dir=str(FIXTURES)), [], Path(d) / "s.json")
        assert cl.call_args[0][1] == (jm.MAX_PAGES_CAP if requested == 999 else CFG["max_pages"])
    assert jm.MAX_PAGES_CAP == 50


def test_cookie_never_in_state_or_payloads():
    page = f'<a href="/en/job-offers/{U1}-business-analyst-intern">x</a>'
    ld = {"@type": "JobPosting", "title": "Business Analyst", "description": "English required.", "hiringOrganization": {"name": "Acme"}}
    seen_headers = []

    def fake_get(url, headers=None, **kw):
        seen_headers.append(headers)
        return _resp(page if "page=" in url else _detail_html(ld))

    with tempfile.TemporaryDirectory() as d:
        sp, sent = Path(d) / "s.json", []
        with mock.patch.object(jm.requests, "get", side_effect=fake_get), \
                mock.patch.object(jm, "call_claude", return_value=_score_json()):
            _run(_args(max_pages=1), sent, sp, env={"JOBTEASER_COOKIE": FAKE_COOKIE, "ANTHROPIC_API_KEY": "k"})
        assert sent and any(h.get("Cookie") == FAKE_COOKIE for h in seen_headers)  # 請求標頭確實帶了 cookie
        blob = sp.read_text() + "\n".join(sent)
        assert "SECRETCOOKIEVALUE123" not in blob and "ABCDEF999" not in blob
        assert json.loads(sp.read_text())["seen"][U1]["status"] == "pushed"


def test_live_list_url_has_all_filter_params():
    urls = []
    src = jm.LiveSource(FAKE_COOKIE, CFG)
    with mock.patch.object(jm.requests, "get", side_effect=lambda u, **k: urls.append(u) or _resp("x")), mock.patch.object(jm.time, "sleep") as sl:
        src.list_page(3)
        src.detail("/en/job-offers/abc")
    assert "contract=internship" in urls[0] and "contract=alternating" in urls[0] and "page=3" in urls[0] and "lat=46.711046" in urls[0]
    assert urls[1] == "https://kedgebs.jobteaser.com/en/job-offers/abc"
    sl.assert_called_once_with(1.5)  # 第二個請求前才等待


def test_no_api_key_degrades_to_untranslated_digest():
    with tempfile.TemporaryDirectory() as d:
        sp, sent = Path(d) / "s.json", []
        _run(_args(fixture_dir=str(FIXTURES), no_llm=True), sent, sp)
        assert len(sent) == 1 and "未啟用 AI 評分" in sent[0] and "法文 D" not in sent[0]
        st = json.loads(sp.read_text())["seen"]
        assert "pushed_unscored" in {v["status"] for v in st.values()} and "filtered_french" in {v["status"] for v in st.values()}


def test_state_is_json_and_cookie_flag_default():
    # 不讀 repo 裡的 seen_jobteaser.json：機器人跑起來會把 cookie_alert_sent 寫成 true，測試會跟著線上狀態變動。
    # 這裡只驗「檔案存在且是合法的 state JSON」＋「沒有狀態檔時的預設值」。
    committed = json.loads((ROOT / "data" / "seen_jobteaser.json").read_text())
    assert isinstance(committed.get("seen"), dict) and isinstance(committed.get("cookie_alert_sent"), bool)
    with tempfile.TemporaryDirectory() as d:
        assert jm.load_state(Path(d) / "missing.json") == {"seen": {}, "cookie_alert_sent": False}


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
