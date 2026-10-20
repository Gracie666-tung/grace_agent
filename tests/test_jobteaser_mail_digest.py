#!/usr/bin/env python3
"""jobteaser_mail_digest 的離線測試：不讀 Apple Mail、不連網、不碰 Keychain（subprocess 與 requests 全部換成假的）。
tests/fixtures/jobteaser_mail/*.eml 與測試內組出來的信全是捏造資料（synthetic），沒有任何真實公司、人名、信箱或追蹤連結。

    python3 -m pytest tests/test_jobteaser_mail_digest.py -q
"""

import contextlib
import io
import json
import os
import plistlib
import stat
import re
import subprocess
import sys
import tempfile
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from html import escape
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import jobteaser_mail_digest as jm  # noqa: E402
import jobteaser_monitor  # noqa: E402
import requests  # noqa: E402

NOW = datetime(2026, 10, 20, 9, 30, tzinfo=timezone.utc)
FIX = ROOT / "tests" / "fixtures" / "jobteaser_mail"
CFG = jm.load_config(ROOT / "jobteaser_mail_config.json")
TOKEN = "TGTOKEN_SENTINEL_5b1e9c04d7a2"  # 假 token；任何輸出／狀態檔出現這串就是洩漏
URL_SENTINEL = "URLSENTINEL_c83f61aa90de"  # 假追蹤連結裡的記號；同上
SENDER = "Kedge BS <no-reply@notification.prod.jobteaser.net>"
SUBJ = "Il y a plus de 10 offres correspondant à vos critères"
PERM_ERR = b"execution error: Not authorized to send Apple events to Mail. (-1743)"


def _args(**kw):
    d = dict(dry_run=False, fixture_dir=str(FIX), days=None, no_sync=True, state_path=None, show_urls=False,
             config=str(ROOT / "jobteaser_mail_config.json"))
    d.update(kw)
    return Namespace(**d)


class Resp:
    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            # requests 的真實行為：訊息裡回顯含 token 的網址，且例外帶 response
            raise requests.HTTPError(
                f"{self.status_code} Error for url: https://api.telegram.org/bot{TOKEN}/sendMessage", response=self)


class FakeNet:
    """取代 requests.post：記下每次送出的 (網址, JSON)。
    fail_on：要失敗（HTTP 500）的第幾次呼叫（從 1 算）；errors：{第幾次: "conn"／"timeout"／HTTP 狀態碼}。"""

    def __init__(self, fail_on=(), errors=None):
        self.posts, self.fail_on, self.errors = [], set(fail_on), dict(errors or {})

    def post(self, url, json=None, **kw):
        self.posts.append((url, json))
        n = len(self.posts)
        err = self.errors.get(n)
        if err == "conn":
            raise requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage")
        if err == "timeout":
            raise requests.Timeout(f"Read timed out. url=/bot{TOKEN}/sendMessage")
        if isinstance(err, int):
            return Resp(err)
        return Resp(500 if n in self.fail_on else 200)

    @property
    def texts(self):
        return [p["text"] for _, p in self.posts]


def _run(args, state_path, env=None, now=NOW, net=None, sp=None, sleeps=None):
    """回傳 (exit code, FakeNet, stdout+stderr)。Telegram 走真的 send_telegram，只換掉底層 requests.post；
    subprocess.run 一律換掉（預設：任何呼叫都算錯誤），確保測試不會真的叫 osascript／security。"""
    net = net or FakeNet()

    def no_subprocess(cmd, **kw):
        raise AssertionError(f"測試不該呼叫外部程式：{cmd[0]}")

    base = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "12345", "JOBTEASER_MAIL_STATE_PATH": str(state_path)}
    base.update(env or {})
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, base), mock.patch.object(jobteaser_monitor.requests, "post", net.post), \
            mock.patch.object(jm.subprocess, "run", sp or no_subprocess), mock.patch.object(jm.time, "sleep", (sleeps.append if sleeps is not None else mock.Mock())), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = jm.run(args, now=now)
    return code, net, out.getvalue() + err.getvalue()


def _tmp():
    d = tempfile.TemporaryDirectory()
    return d, Path(d.name) / "seen.json"


def _state(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _fixture_jobs():
    raws = jm.read_fixtures(FIX)
    col = jm.collect_jobs(raws)
    return col.jobs, col.subjects


def _eml(subject=SUBJ, sender=SENDER, html="", when=NOW):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"] = sender, "Test <t@example.invalid>", subject, format_datetime(when)
    m.set_content("texte")
    m.add_alternative(html, subtype="html", cte="quoted-printable")
    return m.as_bytes()


def _html_card(url, *nodes):
    spans = "".join(f"<span>{escape(n)}</span>" for n in nodes[:-1])
    return f'<a href="{escape(url)}">{spans}<table><tr><td>\n {escape(nodes[-1])}</td></tr></table></a>'


def _long_url(tag, n=1500):
    """恰好 n 字元的假追蹤連結：一個很長的 upn 記號加幾個 & 參數（真實連結也是這個形狀）。"""
    head, tail = f"https://link.jobteaser.com/ls/click?upn=FAKE{tag}", "&ref=FAKE&lang=fr&utm=FAKE"
    return head + "X" * (n - len(head) - len(tail)) + tail


def _job(i, title=None, url=None, priority=False, kind="stage"):
    j = {"company": f"Fakeco{i}", "title": title or f"Fake Title {i}", "contract": "Stage", "city": "Paris",
         "country": "France", "url": url or _long_url(i), "kind": kind, "priority": priority}
    j["hash"] = jm.job_hash(j)
    return j


# --- 解析：卡片形狀 -----------------------------------------------------------

def test_parse_cards_exact():
    raw = (FIX / "alert_2026-10-20.eml").read_bytes()
    msg, cards = jm.parse_message(raw)
    assert msg is not None and len(cards) == 10  # logo、Modifier、Voir plus、取消訂閱、隱私政策都不是卡片
    shape = [(c["company"], c["title"], c["contract"], c["city"], c["country"]) for c in cards]
    assert shape == [
        ("Orbitex Consulting", "Analyste performance opérationnelle & excellence industrielle (H/F)", "Stage", "Paris", "France"),
        ("Helvara", "Concepteur outillages prototypes F/H", "CDI", "Bourges", "France"),
        ("Quantiva France", "Junior Data Officer F/H", "CDD", "Puteaux", "France"),
        ("Banque Lumière Capital", "Assistant chargé d'Affaires Clients -", "Stage", "Nantes", "France"),  # 公司名被斷成兩行
        ("Banque Lumière Capital", "Assistant chargé d'Affaires Clients -", "Stage", "Nantes", "France"),  # 重複卡片
        ("Kestrel Foods", "Acheteur junior packaging H/F", "Alternance", "Val-de-Reuil", "France"),
        ("", "Stage - Fictive Treasury Analyst - H/F", "Stage", "Paris", "France"),  # 沒有公司
        ("Aldersgate Partners", "Business Analyst Intern - Supply Chain H/F", "Stage", "Puteaux", "France"),
        ("Nordvolt Systèmes d'Information (NVSI)", "Stage - Chef de projet SI F/H", "Stage", "Le Mans", "France"),
        ("Nordvolt Systèmes d'Information (NVSI)", "Stage - Consultant juridique F/H", "Stage", "Le Mans", "France"),
    ]
    assert all(c["url"].startswith("https://link.jobteaser.com/ls/click?upn=FAKE-") and "&amp;" not in c["url"]
               for c in cards)  # HTML 實體已還原成真正的 &


def test_card_from_lines_shapes():
    last = "Stage - Paris (France)"
    c = jm.card_from_lines(["EFAKO", "Management Consultants", "Some Title (H/F)", last], "https://x/")
    assert (c["company"], c["title"]) == ("EFAKO Management Consultants", "Some Title (H/F)")  # 三行：最後一行是標題
    c = jm.card_from_lines(["Stage", "- Fictive Quant - H/F", last], "https://x/")
    assert (c["company"], c["title"]) == ("", "Stage - Fictive Quant - H/F")  # 標題斷成兩行、沒有公司
    c = jm.card_from_lines(["Solo Title", "CDI - Lyon"], "https://x/")
    assert (c["company"], c["title"], c["contract"], c["city"], c["country"]) == ("", "Solo Title", "CDI", "Lyon", "")
    assert jm.card_from_lines(["Modifier"], "https://x/") is None  # 只有一行
    assert jm.card_from_lines(["Voir plus", "d'offres"], "https://x/") is None  # 最後一行不是『合約 - 城市』
    assert jm.card_from_lines(["Corp", "Title", "Weird - Paris (France)"], "https://x/") is None  # 不是已知合約字
    assert jm.card_from_lines([], "https://x/") is None


def test_non_alert_mails_ignored():
    msgs = {p.name: jm.parse_message(p.read_bytes()) for p in sorted(FIX.glob("*.eml"))}
    assert msgs["events_2026-10-20.eml"] == (None, [])  # 同為 JobTeaser 但主旨不是 offres correspondant
    assert msgs["other_sender.eml"] == (None, [])  # 主旨對但寄件者不是 jobteaser
    jobs, subjects = _fixture_jobs()
    assert len(subjects) == 2 and all("offres correspondant" in s for s in subjects)
    assert not any("autre expéditeur" in j["title"] or "Webinaire" in j["title"] for j in jobs)


def test_dedupe_within_email_and_across_days():
    jobs, _ = _fixture_jobs()
    titles = [j["title"] for j in jobs]
    assert len(jobs) == 11  # 今天 10 張 -1 張信內重複；昨天 4 張 -2 張與今天相同（URL 不同）
    assert titles.count("Assistant chargé d'Affaires Clients -") == 1
    assert titles.count("Business Analyst Intern - Supply Chain H/F") == 1
    assert len({j["hash"] for j in jobs}) == 11
    # 同一職缺出現在兩封信：留下較新那封的卡片（URL 記號 A* 而非 B*）
    kept = next(j for j in jobs if j["title"].startswith("Business Analyst Intern"))
    assert "upn=FAKE-A7" in kept["url"]


def test_job_hash_ignores_url_and_case():
    a = {"company": "Fakeco", "title": "Intern Analyst", "city": "Paris", "url": "https://a/1"}
    b = {"company": "FAKECO ", "title": "intern  analyst", "city": "paris", "url": "https://b/2"}
    assert jm.job_hash(a) == jm.job_hash(b)
    assert jm.job_hash(a) != jm.job_hash({**a, "city": "Lyon"})
    assert re.fullmatch(r"[0-9a-f]{40}", jm.job_hash(a))


# --- 篩選與排序 ---------------------------------------------------------------

def test_select_drops_cdi_cdd_and_sorts_priority_first():
    jobs, _ = _fixture_jobs()
    keep, st = jm.select_jobs(jobs, {}, CFG)
    assert st == {"cards": 11, "dropped_contract": 2, "already_seen": 0}
    titles = [j["title"] for j in keep]
    assert "Concepteur outillages prototypes F/H" not in titles and "Junior Data Officer F/H" not in titles  # CDI／CDD
    assert any(j["kind"] == "vie" for j in keep) and any(j["kind"] == "alternance" for j in keep)  # 其他合約都留
    flags = [j["priority"] for j in keep]
    assert flags == sorted(flags, reverse=True)  # ✅ 全在前面
    assert flags.count(True) == 6 and len(keep) == 9
    # 沒有關鍵字的職缺不會被丟掉，且維持信裡的原順序（今天的信在前）
    rest = [j["title"] for j in keep if not j["priority"]]
    assert rest == ["Assistant chargé d'Affaires Clients -", "Stage - Consultant juridique F/H", "VIE Planning Officer H/F"]
    prio = [j["title"] for j in keep if j["priority"]]
    assert prio[0].startswith("Analyste performance") and prio[-1] == "Stage - Fictive Pricing Analyst - H/F"


def test_generic_title_words_are_not_priority():
    assert not {"project", "projet", "operations", "data"} & set(CFG["extra_title_keywords"])
    pos = jm.kw_regex(CFG["keywords_positive"])
    for t in ("Project Assistant H/F", "Assistant Opérations", "Data Entry Intern", "Chargé de projet communication"):
        assert not pos.search(jm.norm(t)), t
    for t in ("Buyer Intern", "Acheteur junior", "Treasury Analyst", "Chef de projet SI"):
        assert pos.search(jm.norm(t)), t
    prio = sum(bool(pos.search(jm.norm(c["title"]))) for c in jm.parse_message((FIX / "alert_2026-10-20.eml").read_bytes())[1])
    assert prio == 5  # 10 張合成卡片裡有 5 張 ✅


def test_select_skips_seen():
    jobs, _ = _fixture_jobs()
    seen = {jobs[0]["hash"]: "2026-10-19"}
    keep, st = jm.select_jobs(jobs, seen, CFG)
    assert st["already_seen"] == 1 and jobs[0]["hash"] not in {j["hash"] for j in keep}


# --- 訊息格式 -----------------------------------------------------------------

def test_format_entry_escaping_and_shape():
    j = _job(1, title='R&D "Intern" <b>x</b>', url="https://link.jobteaser.com/ls/click?a=1&b=2&c=\"3\"", priority=True)
    j["company"] = "A&B <Corp>"
    ent = jm.format_entry(j)
    assert ent.startswith("✅ <b>R&amp;D &quot;Intern&quot; &lt;b&gt;x&lt;/b&gt;</b> — A&amp;B &lt;Corp&gt; · Paris · Stage\n")
    assert ent.endswith('<a href="https://link.jobteaser.com/ls/click?a=1&amp;b=2&amp;c=&quot;3&quot;">查看職缺</a>')
    j["company"], j["priority"], j["kind"], j["city"] = "", False, "cdx", ""
    j["contract"] = "Freelance"
    assert jm.format_entry(j).startswith("▫️ <b>") and "— Freelance\n" in jm.format_entry(j)  # 沒公司沒城市：只剩合約


def test_chunks_under_3500_with_long_urls():
    jobs = [_job(i, priority=(i % 2 == 0)) for i in range(10)]
    assert all(len(j["url"]) == 1500 for j in jobs)
    msgs, hidden = jm.build_messages(jobs, 3500, 99)
    assert hidden == 0 and len(msgs) == 5  # 約 2 則一封
    for text, hashes in msgs:
        assert len(text) <= 3500 and len(hashes) == 2
        assert "僅標題層級" in text and "JobTeaser 提醒信" in text  # 標頭說明
        assert text.count("&amp;") >= 6 and re.search(r"&(?!amp;|quot;|lt;|gt;|#39;)", text) is None  # 連結的 & 都跳脫了
    assert [h for _, hs in msgs for h in hs] == [j["hash"] for j in jobs]  # 順序與完整


def test_message_cap_and_overflow_note():
    jobs = [_job(i) for i in range(10)]
    msgs, hidden = jm.build_messages(jobs, 3500, 2)
    assert len(msgs) == 2 and hidden == 6
    assert "另有 6 則因單次上限未顯示" in msgs[-1][0] and all(len(t) <= 3500 for t, _ in msgs)
    # 註記放不下時，最後一則會把最後一個職缺也順延，而不是超過上限
    jobs = [_job(i, url=_long_url(i, 1560)) for i in range(4)]
    msgs, hidden = jm.build_messages(jobs, 3500, 1)
    assert len(msgs) == 1 and len(msgs[0][0]) <= 3500 and hidden == 4 - len(msgs[0][1]) and hidden >= 2


def test_single_oversized_url_drops_link_instead_of_failing():
    msgs, _ = jm.build_messages([_job(1, url=_long_url(1, 5000))], 3500, 6)
    assert len(msgs) == 1 and len(msgs[0][0]) <= 3500 and "連結過長" in msgs[0][0] and "href" not in msgs[0][0]


# --- 完整流程：狀態、Telegram -------------------------------------------------

def test_full_run_sends_then_dedupes_across_runs():
    d, sp = _tmp()
    with d:
        code, net, out = _run(_args(), sp)
        assert code == 0 and len(net.posts) == 1
        url, payload = net.posts[0]
        assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"  # token 只在這個 HTTP 呼叫的網址裡
        assert payload["parse_mode"] == "HTML" and payload["chat_id"] == "12345"
        assert payload["text"].count("<a href=") == 9 and "Junior Data Officer" not in payload["text"]
        st = _state(sp)
        assert len(st["seen"]) == 9 and all(re.fullmatch(r"[0-9a-f]{40}", k) for k in st["seen"])
        assert set(st["seen"].values()) == {"2026-10-20"}
        raw = sp.read_text(encoding="utf-8")
        assert "http" not in raw and "link.jobteaser" not in raw  # 狀態檔只有雜湊
        code, net2, out2 = _run(_args(), sp)  # 同一批信再跑一次：沒有新職缺，什麼都不送
        assert code == 0 and net2.posts == [] and "沒有新職缺" in out2 and "已推過 9" in out2


def test_new_offer_next_day_only_sends_the_new_one():
    d, sp = _tmp()
    with d:
        fx = Path(d.name) / "fx"
        fx.mkdir()
        (fx / "a.eml").write_bytes(_eml(html=_html_card(_long_url(1), "Fakeco1", "Intern Analyst", "Stage - Paris (France)")))
        assert _run(_args(fixture_dir=str(fx)), sp)[1].texts[0].count("<a href=") == 1
        both = _html_card(_long_url(7), "Fakeco1", "Intern Analyst", "Stage - Paris (France)") + \
            _html_card(_long_url(8), "Fakeco2", "Other Intern", "Alternance - Lyon (France)")
        (fx / "b.eml").write_bytes(_eml(html=both, when=NOW + timedelta(days=1)))  # 同一職缺，不同（個人化）連結
        code, net, _ = _run(_args(fixture_dir=str(fx)), sp, now=NOW + timedelta(days=1))
        assert code == 0 and len(net.posts) == 1
        assert "Other Intern" in net.texts[0] and "Intern Analyst" not in net.texts[0]


def test_state_retention_prunes_old_hashes():
    d, sp = _tmp()
    with d:
        sp.write_text(json.dumps({"seen": {"a" * 40: "2026-05-01", "b" * 40: "2026-10-01"}, "last_alert_date": ""}))
        code, _, _ = _run(_args(), sp)
        seen = _state(sp)["seen"]
        assert code == 0 and "a" * 40 not in seen and "b" * 40 in seen  # 90 天前的丟掉


def test_telegram_failure_does_not_write_state_and_exits_1():
    d, sp = _tmp()
    with d:
        code, net, out = _run(_args(), sp, net=FakeNet(fail_on={1, 2, 3, 4}))
        assert code == 1 and not sp.exists() and len(net.posts) == 4  # 5xx 會退避重試 3 次才放棄
        assert "Telegram 傳送失敗：HTTPError（HTTP 500）" in out
        assert TOKEN not in out  # 例外訊息裡有含 token 的網址，只能印型別


def test_partial_send_marks_only_delivered_chunks():
    d, sp = _tmp()
    with d:
        fx = Path(d.name) / "fx"
        fx.mkdir()
        cards = "".join(_html_card(_long_url(i), f"Fakeco{i}", f"Intern {i}", "Stage - Paris (France)") for i in range(4))
        (fx / "a.eml").write_bytes(_eml(html=cards))
        code, net, _ = _run(_args(fixture_dir=str(fx)), sp, net=FakeNet(fail_on={2, 3, 4, 5}))
        assert code == 1 and len(net.posts) == 5
        assert len(_state(sp)["seen"]) == 2  # 第一則（2 個職缺）送成功才記；失敗的下次重送
        code, net, _ = _run(_args(fixture_dir=str(fx)), sp)
        assert code == 0 and len(net.posts) == 1 and net.texts[0].count("<a href=") == 2


def test_cap_leaves_overflow_unseen_for_next_run():
    d, sp = _tmp()
    with d:
        fx = Path(d.name) / "fx"
        fx.mkdir()
        cards = "".join(_html_card(_long_url(i), f"Fakeco{i}", f"Intern {i}", "Stage - Paris (France)") for i in range(10))
        (fx / "a.eml").write_bytes(_eml(html=cards))
        cfg = Path(d.name) / "cfg.json"
        cfg.write_text(json.dumps({**json.loads((ROOT / "jobteaser_mail_config.json").read_text()), "max_messages": 2}))
        code, net, _ = _run(_args(fixture_dir=str(fx), config=str(cfg)), sp)
        assert code == 0 and len(net.posts) == 2 and "因單次上限未顯示" in net.texts[-1]
        assert len(_state(sp)["seen"]) == 4
        code, net, _ = _run(_args(fixture_dir=str(fx), config=str(cfg)), sp)
        assert len(net.posts) == 2 and len(_state(sp)["seen"]) == 8


def test_dry_run_writes_nothing_sends_nothing_and_masks_urls():
    d, sp = _tmp()
    with d:
        code, net, out = _run(_args(dry_run=True), sp, env={"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})
        assert code == 0 and net.posts == [] and not sp.exists()
        assert not list(sp.parent.glob("*"))  # 臨時資料夾裡什麼檔案都沒多
        assert "查看職缺" in out and "link.jobteaser.com" not in out and "連結已隱藏" in out
        code, _, out = _run(_args(dry_run=True, show_urls=True), sp)
        assert "link.jobteaser.com/ls/click?upn=FAKE-" in out and not sp.exists()


def test_no_secret_or_url_leaks_in_output_or_state():
    d, sp = _tmp()
    with d:
        fx = Path(d.name) / "fx"
        fx.mkdir()
        card = _html_card(f"https://link.jobteaser.com/ls/click?upn={URL_SENTINEL}&x=1", "Fakeco", "Intern Analyst",
                          "Stage - Paris (France)")
        (fx / "a.eml").write_bytes(_eml(html=card))
        for kw, net in (({}, FakeNet()), ({}, FakeNet(fail_on={1, 2, 3, 4})), ({"dry_run": True}, FakeNet())):
            code, net, out = _run(_args(fixture_dir=str(fx), **kw), sp, net=net)
            assert TOKEN not in out and URL_SENTINEL not in out
            if sp.exists():
                assert TOKEN not in sp.read_text(encoding="utf-8") and URL_SENTINEL not in sp.read_text(encoding="utf-8")
                sp.unlink()
        # 成功那次，連結確實有送進 Telegram（那是給 Grace 點的），只是不在 log／狀態裡
        code, net, out = _run(_args(fixture_dir=str(fx)), sp)
        assert URL_SENTINEL in net.texts[0] and URL_SENTINEL not in out and URL_SENTINEL not in sp.read_text(encoding="utf-8")


def test_missing_credentials_exit_1_without_reading_mail():
    d, sp = _tmp()
    with d:
        def sec(cmd, **kw):
            assert cmd[0] == "security"
            return subprocess.CompletedProcess(cmd, 44, b"", b"The specified item could not be found")
        code, net, out = _run(_args(), sp, env={"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, sp=sec)
        assert code == 1 and net.posts == [] and "找不到 Telegram 憑證" in out


def test_credentials_keychain_and_env_precedence():
    calls = []

    def sec(cmd, **kw):
        calls.append(cmd)
        assert cmd[:3] == ["security", "find-generic-password", "-s"] and cmd[-1] == "-w"
        val = {"jobteaser-mail-telegram-token": b"KC_TOKEN\n", "jobteaser-mail-telegram-chat": b"-100777\n"}[cmd[3]]
        return subprocess.CompletedProcess(cmd, 0, val, b"")

    with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}), \
            mock.patch.object(jm.subprocess, "run", sec):
        assert jm.get_credentials() == ("KC_TOKEN", "-100777") and len(calls) == 2
    calls.clear()
    with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "ENV_T", "TELEGRAM_CHAT_ID": "42"}), \
            mock.patch.object(jm.subprocess, "run", sec):
        assert jm.get_credentials() == ("ENV_T", "42") and calls == []  # 環境變數優先，不碰 Keychain


# --- osascript 路徑 -----------------------------------------------------------

def _mail_stdout(*files):
    return "".join(f"\n{jm.BOUNDARY}\n" + (FIX / f).read_text(encoding="utf-8") for f in files).encode("utf-8")


def test_read_mail_via_osascript_no_shell_and_argv():
    seen = []

    def osa(cmd, **kw):
        seen.append((cmd, kw))
        if "check for new mail" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return subprocess.CompletedProcess(
            cmd, 0, _mail_stdout("alert_2026-10-20.eml", "alert_2026-10-19.eml", "events_2026-10-20.eml"), b"")

    d, sp = _tmp()
    with d:
        code, net, out = _run(_args(fixture_dir=None, no_sync=False, days=5), sp, sp=osa)
        assert code == 0 and len(net.posts) == 1
        assert len(seen) == 2  # 先叫 Mail 收信（sleep 已被換掉），再讀信
        for cmd, kw in seen:
            assert cmd[0] == "osascript" and not kw.get("shell") and isinstance(cmd, list)
        read_cmd, read_kw = seen[1]
        assert read_cmd[-2:] == ["KEDGE", "5"]  # 帳號名稱與天數走 argv
        script = "\n".join(read_cmd[i + 1] for i, a in enumerate(read_cmd) if a == "-e")
        assert 'account acctName' in script and "source of m" in script and 'sender contains "jobteaser"' in script
        assert 'subject contains "offres correspondant"' in script and '"INBOX"' in script and '"Inbox"' in script
        assert read_kw["timeout"] > 0
        assert len(net.texts[0].split("<a href=")) - 1 == 9
        assert "讀到 3 封信，其中 JobTeaser 通知信 2 封" in out
        # --no-sync：不叫 Mail 收信
        seen.clear()
        sp.unlink()
        _run(_args(fixture_dir=None, no_sync=True), sp, sp=osa)
        assert len(seen) == 1 and "check for new mail" not in " ".join(seen[0][0])


def test_osascript_permission_error_alerts_once_per_day_and_exits_1():
    def osa(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, b"", PERM_ERR)

    d, sp = _tmp()
    with d:
        code, net, out = _run(_args(fixture_dir=None), sp, sp=osa)
        assert code == 1 and len(net.posts) == 1
        assert "自動化" in net.texts[0] and "隱私權與安全性" in net.texts[0]
        assert "permission" in out and "-1743" in out and "Not authorized" not in out  # 一行診斷，不轉貼 osascript 原文
        assert _state(sp)["last_alert_date"] == "2026-10-20" and _state(sp)["seen"] == {}
        code, net2, out2 = _run(_args(fixture_dir=None), sp, sp=osa)  # 同一天再失敗：exit 1 但不再推
        assert code == 1 and net2.posts == [] and "今天已提醒過" in out2
        code, net3, _ = _run(_args(fixture_dir=None), sp, sp=osa, now=NOW + timedelta(days=1))  # 隔天又可提醒一次
        assert code == 1 and len(net3.posts) == 1


def test_osascript_timeout_and_other_failures():
    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 270)

    def notfound(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, b"", b'execution error: Mail got an error: Can\'t get account "KEDGE". (-1728)')

    def garbage(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, b"", b"something weird with a secret")

    for fn, kind, text in ((timeout, "timeout", "逾時"), (notfound, "notfound", "找不到帳號"), (garbage, "other", "讀取 Mail 失敗")):
        d, sp = _tmp()
        with d:
            code, net, out = _run(_args(fixture_dir=None), sp, sp=fn)
            assert code == 1 and len(net.posts) == 1 and text in net.texts[0] and kind in out
            assert "something weird" not in out and TOKEN not in out


def test_osascript_failure_dry_run_sends_nothing():
    d, sp = _tmp()
    with d:
        code, net, out = _run(_args(fixture_dir=None, dry_run=True),
                              sp, sp=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", PERM_ERR))
        assert code == 1 and net.posts == [] and not sp.exists() and "不送 Telegram 警示" in out


def test_sync_failure_is_not_fatal():
    def osa(cmd, **kw):
        if "check for new mail" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 1, b"", PERM_ERR)
        return subprocess.CompletedProcess(cmd, 0, _mail_stdout("alert_2026-10-19.eml"), b"")

    d, sp = _tmp()
    with d:
        code, net, out = _run(_args(fixture_dir=None, no_sync=False), sp, sp=osa)
        assert code == 0 and len(net.posts) == 1 and "叫 Mail 收信失敗" in out


def test_alert_mail_without_any_cards_reports_layout_change():
    d, sp = _tmp()
    with d:
        fx = Path(d.name) / "fx"
        fx.mkdir()
        (fx / "a.eml").write_bytes(_eml(html="<html><body><p>nouvelle mise en page</p></body></html>"))
        code, net, out = _run(_args(fixture_dir=str(fx)), sp)
        assert code == 1 and len(net.posts) == 1 and "版面" in net.texts[0] and "layout" in out
        assert _run(_args(fixture_dir=str(fx)), sp)[:2][0] == 1  # 同一天不重複推
        assert len(_run(_args(fixture_dir=str(fx)), sp)[1].posts) == 0
        empty = Path(d.name) / "empty"
        empty.mkdir()
        code, net, out = _run(_args(fixture_dir=str(empty)), sp)
        assert code == 0 and net.posts == [] and "沒有新職缺" in out  # 一封通知信都沒有＝正常（今天沒寄）


def test_classify_osascript_error():
    c = jm.classify_osascript_error
    assert (c("... Not authorized to send Apple events ... (-1743)").kind, c("x (-1743)").code) == ("permission", -1743)
    assert c("Can't get account \"X\". (-1728)").kind == "notfound"
    assert c("AppleEvent timed out. (-1712)").kind == "timeout"
    assert (c("boom").kind, c("boom").code) == ("other", None)


# --- 安裝腳本 -----------------------------------------------------------------

def test_install_script_plist_and_shell_syntax():
    for name in ("install_jobteaser_mail.sh", "uninstall_jobteaser_mail.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)]).returncode == 0
    res = subprocess.run(["bash", str(ROOT / "scripts" / "install_jobteaser_mail.sh"), "--print-plist"],
                         capture_output=True, text=True, timeout=30, env={**os.environ, "PYTHON_PATH": "/usr/bin/python3"})
    assert res.returncode == 0, res.stderr
    plist = plistlib.loads(res.stdout.encode("utf-8"))
    assert plist["Label"] == "com.grace.jobteaser-mail"
    assert plist["ProgramArguments"] == ["/usr/bin/python3", str(ROOT / "src" / "jobteaser_mail_digest.py")]
    assert plist["StartCalendarInterval"] == {"Hour": 9, "Minute": 30} and "RunAtLoad" not in plist
    assert plist["StandardOutPath"].endswith("Library/Logs/jobteaser-mail.log")


# --- 新增：重試、4xx 退路、主旨、版面、原子寫入、解析例外 -----------------------

def _cards_fixture(d, n, extra_html="", when=NOW, name="a.eml", subject=SUBJ):
    fx = Path(d.name) / "fx"
    fx.mkdir(exist_ok=True)
    cards = "".join(_html_card(_long_url(i), f"Fakeco{i}", f"Intern {i}", "Stage - Paris (France)") for i in range(n))
    (fx / name).write_bytes(_eml(subject=subject, html=cards + extra_html, when=when))
    return fx


def test_telegram_retries_on_connection_error_and_timeout_with_backoff():
    d, sp = _tmp()
    with d:
        fx = _cards_fixture(d, 1)
        sleeps = []
        code, net, out = _run(_args(fixture_dir=str(fx)), sp, net=FakeNet(errors={1: "conn", 2: "timeout"}), sleeps=sleeps)
        assert code == 0 and len(net.posts) == 3 and sleeps == [30, 60]  # 第三次成功
        assert len(_state(sp)["seen"]) == 1 and TOKEN not in out and "ConnectionError" in out and "Timeout" in out
        sp.unlink()
        sleeps.clear()
        code, net, out = _run(_args(fixture_dir=str(fx)), sp, net=FakeNet(errors={1: "conn", 2: "conn", 3: "conn", 4: "conn"}),
                              sleeps=sleeps)
        assert code == 1 and len(net.posts) == 4 and sleeps == [30, 60, 60]  # 用完重試次數：不寫狀態
        assert not sp.exists() and TOKEN not in out


def test_persistent_4xx_retries_without_link_then_continues():
    d, sp = _tmp()
    with d:
        fx = _cards_fixture(d, 3)  # 1500 字元連結：2 則訊息（2 + 1 個職缺）
        code, net, out = _run(_args(fixture_dir=str(fx)), sp, net=FakeNet(errors={1: 400}))
        assert code == 0 and len(net.posts) == 3
        plain = net.texts[1]
        assert "href" not in plain and "link.jobteaser" not in plain and plain.count("<b>") == 3  # 標題＋2 個職缺的標題行
        assert "Telegram 拒收含連結的版本" in plain and "href" in net.texts[2]  # 後面的訊息照常送
        assert len(_state(sp)["seen"]) == 3 and TOKEN not in out and "link.jobteaser" not in out


def test_unsendable_message_is_marked_seen_counted_and_alerted_once():
    d, sp = _tmp()
    with d:
        fx = _cards_fixture(d, 3)
        code, net, out = _run(_args(fixture_dir=str(fx)), sp, net=FakeNet(errors={1: 400, 2: 400}))
        assert code == 1 and len(net.posts) == 4  # 帶連結、無連結（都 400）、第二則、警示
        st = _state(sp)
        assert len(st["seen"]) == 3 and st["last_alert_date"] == "2026-10-20"  # 不再卡住：兩個壞的標已見
        assert "略過 2 個職缺" in out and "有 2 個職缺因 Telegram 拒收" in net.texts[-1]
        assert TOKEN not in out and "link.jobteaser" not in out and all(TOKEN not in t for t in net.texts)
        # 同一天再有壞訊息：仍計數、exit 1，但不重複推警示
        fx2 = _cards_fixture(d, 1, name="b.eml", when=NOW + timedelta(hours=1))
        (fx / "a.eml").unlink()
        (fx / "b.eml").write_bytes(_eml(html=_html_card(_long_url(9), "Fakeco9", "Intern 9", "Stage - Paris (France)")))
        code, net, out = _run(_args(fixture_dir=str(fx)), sp, net=FakeNet(errors={1: 400, 2: 400}))
        assert code == 1 and len(net.posts) == 2 and "今天已提醒過" in out


def test_auth_4xx_stops_without_marking_anything_seen():
    d, sp = _tmp()
    with d:
        fx = _cards_fixture(d, 3)
        code, net, out = _run(_args(fixture_dir=str(fx)), sp, net=FakeNet(errors={1: 401}))
        assert code == 1 and len(net.posts) == 1 and not sp.exists()  # token 壞了不是「這則內容」的問題：不能把職缺吃掉


def test_looser_subject_match_case_insensitive():
    ok = ["Il y a plus de 10 offres correspondant à vos critères", "VOS CRITÈRES : 5 nouvelles offres",
          "Nouvelles OFFRES CORRESPONDANT à votre profil", "Mise à jour de vos critères de recherche"]
    bad = ["Vos évènements « employabilité » à suivre cette semaine", "Bienvenue sur JobTeaser"]
    page = _html_card(_long_url(1), "Fakeco", "Intern Analyst", "Stage - Paris (France)")
    for subj in ok:
        assert jm.parse_message(_eml(subject=subj, html=page))[0] is not None, subj
    for subj in bad:
        assert jm.parse_message(_eml(subject=subj, html=page)) == (None, []), subj
    script = jm.APPLESCRIPT.format(boundary=jm.BOUNDARY, sender="jobteaser", subject1=jm.SUBJECT_NEEDLES[0],
                                   subject2=jm.SUBJECT_NEEDLES[1], timeout=10)
    assert 'subject contains "offres correspondant" or subject contains "vos crit"' in script


def test_layout_check_uses_newest_alert_only():
    d, sp = _tmp()
    with d:
        fx = Path(d.name) / "fx"
        fx.mkdir()
        old = _eml(html=_html_card(_long_url(1), "Fakeco1", "Intern 1", "Stage - Paris (France)"), when=NOW - timedelta(days=1))
        new = _eml(html="<html><body><p>nouvelle mise en page</p></body></html>", when=NOW)
        (fx / "old.eml").write_bytes(old)
        (fx / "new.eml").write_bytes(new)
        code, net, out = _run(_args(fixture_dir=str(fx)), sp)
        assert code == 1 and "layout" in out  # 最新的那封沒有卡片：舊信有卡片也要警示
        assert len(net.posts) == 2 and "版面" in net.texts[0] and "Intern 1" in net.texts[1]  # 警示＋舊信的職缺照送
        sp.unlink()
        (fx / "old.eml").write_bytes(_eml(html="<p>vide</p>", when=NOW - timedelta(days=1)))  # 舊信沒卡片、新信有：正常
        (fx / "new.eml").write_bytes(_eml(html=_html_card(_long_url(2), "Fakeco2", "Intern 2", "Stage - Paris (France)"), when=NOW))
        code, net, out = _run(_args(fixture_dir=str(fx)), sp)
        assert code == 0 and "layout" not in out and len(net.posts) == 1


def test_save_state_is_atomic():
    d, sp = _tmp()
    with d:
        jm.save_state(sp, {"seen": {"a" * 40: "2026-10-20"}, "last_alert_date": ""})
        before = sp.read_text(encoding="utf-8")
        with mock.patch.object(jm.os, "replace", side_effect=OSError("disk full")):
            try:
                jm.save_state(sp, {"seen": {}, "last_alert_date": "x"})
                raise AssertionError("應該丟出 OSError")
            except OSError:
                pass
        assert sp.read_text(encoding="utf-8") == before  # 原檔沒被動到
        assert [p.name for p in sp.parent.iterdir()] == [sp.name]  # 暫存檔也清掉了
        jm.save_state(sp, {"seen": {}, "last_alert_date": "x"})
        assert _state(sp)["last_alert_date"] == "x" and [p.name for p in sp.parent.iterdir()] == [sp.name]


BAD_MIME = (b"From: Kedge BS <no-reply@notification.prod.jobteaser.net>\r\nSubject: " + SUBJ.encode("ascii", "ignore")
            + b" offres correspondant\r\nMIME-Version: 1.0\r\nContent-Type: text/html; charset=bogus-9\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\nPGE+PC9hPg==\r\n")


def test_one_unparsable_email_is_skipped_and_counted():
    d, sp = _tmp()
    with d:
        fx = _cards_fixture(d, 1)
        (fx / "bad.eml").write_bytes(BAD_MIME)
        col = jm.collect_jobs([(fx / "a.eml").read_bytes(), BAD_MIME])  # 真的壞 MIME：不能丟例外
        assert len(col.jobs) == 1 and col.errors in (0, 1)
        real = jm.parse_message

        def flaky(raw):
            if b"bogus-9" in raw:
                raise LookupError("unknown encoding: bogus-9 https://link.jobteaser.com/ls/click?upn=" + URL_SENTINEL)
            return real(raw)
        with mock.patch.object(jm, "parse_message", flaky):
            code, net, out = _run(_args(fixture_dir=str(fx)), sp)
        assert code == 0 and len(net.posts) == 1 and "1 封解析失敗已略過" in out
        assert URL_SENTINEL not in out and "Traceback" not in out


def test_all_alert_emails_unparsable_alerts_with_count():
    d, sp = _tmp()
    with d:
        fx = Path(d.name) / "fx"
        fx.mkdir()
        (fx / "a.eml").write_bytes(BAD_MIME)
        (fx / "b.eml").write_bytes(BAD_MIME)
        def always(raw):
            raise LookupError("unknown encoding https://link.jobteaser.com/ls/click?upn=" + URL_SENTINEL)
        with mock.patch.object(jm, "parse_message", always):
            code, net, out = _run(_args(fixture_dir=str(fx)), sp)
        assert code == 1 and len(net.posts) == 1 and "（2 封解析失敗）" in net.texts[0] and "parse" in out
        assert URL_SENTINEL not in out and "Traceback" not in out and TOKEN not in out
        assert _state(sp)["last_alert_date"] == "2026-10-20"


# --- 安裝腳本：找 python、重裝流程 ---------------------------------------------

INSTALL = ROOT / "scripts" / "install_jobteaser_mail.sh"


def _fake_python(folder, name, mode):
    """mode：old＝版本檢查失敗；norequests＝版本過、import requests 失敗；ok＝轉給目前這個 python。"""
    path = Path(folder) / name
    real = sys.executable
    if mode == "old":
        body = "exit 1"
    elif mode == "norequests":
        body = f'case "$*" in *"import requests"*) exit 1;; esac\nexec "{real}" "$@"'
    else:
        body = f'exec "{real}" "$@"'
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _install_dry(candidates):
    env = {**os.environ, "DRY_RUN": "1", "JOBTEASER_PYTHON_CANDIDATES": " ".join(candidates)}
    return subprocess.run(["bash", str(INSTALL)], capture_output=True, text=True, timeout=60, env=env, stdin=subprocess.DEVNULL)


def test_install_find_python_checks_version_and_requests():
    with tempfile.TemporaryDirectory() as d:
        old = _fake_python(d, "py_old", "old")
        nor = _fake_python(d, "py_norequests", "norequests")
        ok = _fake_python(d, "py_ok", "ok")
        res = _install_dry([old, nor, ok])  # 前兩個不合格，退而用第三個
        assert res.returncode == 0, res.stderr
        assert f"Python：{os.path.realpath(sys.executable)}" in res.stdout
        res = _install_dry([old])
        assert res.returncode == 1 and "3.10" in res.stderr and "brew install python" in res.stderr
        res = _install_dry([old, nor])
        assert res.returncode == 1 and "pip install requests" in res.stderr and "步驟 1/3" not in res.stdout
        res = _install_dry([str(Path(d) / "does_not_exist")])
        assert res.returncode == 1 and "找不到" in res.stderr


def test_install_waits_for_bootout_before_bootstrap_in_dry_run():
    with tempfile.TemporaryDirectory() as d:
        res = _install_dry([_fake_python(d, "py_ok", "ok")])
        lines = [ln for ln in res.stdout.splitlines() if ln.startswith("[DRY_RUN] launchctl")]
        assert res.returncode == 0 and "bootout" in lines[0] and "bootstrap" in lines[-1]


def test_install_reinstall_waits_for_bootout_and_retries_bootstrap_with_stubs():
    """用假的 launchctl／security 放在 PATH 最前面跑完整安裝流程（plist／log 路徑指到暫存資料夾）：不碰真的 launchd 與 Keychain。"""
    with tempfile.TemporaryDirectory() as d:
        stubs = Path(d) / "bin"
        stubs.mkdir()
        log = Path(d) / "calls.log"
        counter = Path(d) / "print_count"
        (stubs / "launchctl").write_text(f"""#!/bin/sh
echo "launchctl $*" >> "{log}"
case "$1" in
  print)  n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{counter}"
          [ "$n" -le 3 ] && exit 0 || exit 113 ;;      # 前 3 次還找得到舊 job，之後消失
  bootstrap) [ -e "{Path(d) / 'boot1'}" ] && exit 0; touch "{Path(d) / 'boot1'}"; echo "Bootstrap failed: 5: Input/output error" >&2; exit 5 ;;
esac
exit 0
""")
        (stubs / "security").write_text(f'#!/bin/sh\necho "security $*" >> "{log}"\nexit 0\n')
        (stubs / "sleep").write_text("#!/bin/sh\nexit 0\n")
        for f in stubs.iterdir():
            f.chmod(0o755)
        py = _fake_python(d, "py_ok", "ok")
        env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "JOBTEASER_PYTHON_CANDIDATES": py,
               "PLIST_PATH": str(Path(d) / "x.plist"), "LOG_PATH": str(Path(d) / "x.log")}
        res = subprocess.run(["bash", str(INSTALL)], input="99887766\n", capture_output=True, text=True, timeout=60, env=env)
        assert res.returncode == 0, res.stdout + res.stderr
        calls = log.read_text().splitlines()
        kinds = [c.split()[1] for c in calls if c.startswith("launchctl")]
        assert kinds[0] == "bootout" and kinds.count("print") == 4 and kinds[1:5] == ["print"] * 4  # 輪詢到消失才往下
        assert kinds[5:] == ["bootstrap", "bootstrap"]  # 第一次失敗，重試成功
        assert "bootstrap 第一次失敗" in res.stderr and (Path(d) / "x.plist").exists()
        assert any(c.startswith("security add-generic-password -U -s jobteaser-mail-telegram-token") and c.endswith(" -w") for c in calls)
        # 兩次 bootstrap 都失敗時要明確失敗而不是默默結束
        (Path(d) / "boot1").unlink()
        (stubs / "launchctl").write_text(f"#!/bin/sh\ncase \"$1\" in bootstrap) exit 5;; print) exit 113;; esac\nexit 0\n")
        res = subprocess.run(["bash", str(INSTALL)], input="1\n", capture_output=True, text=True, timeout=60, env=env)
        assert res.returncode == 1 and "載入排程失敗" in res.stderr


def test_uninstall_dry_run_does_not_claim_deletion():
    res = subprocess.run(["bash", str(ROOT / "scripts" / "uninstall_jobteaser_mail.sh"), "--purge"], capture_output=True,
                         text=True, timeout=30, stdin=subprocess.DEVNULL,
                         env={**os.environ, "DRY_RUN": "1", "JOBTEASER_MAIL_STATE_DIR": "/nonexistent/jtmail-state"})
    assert res.returncode == 0 and "已刪除" not in res.stdout and "（dry-run）會刪除" in res.stdout
