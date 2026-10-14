#!/usr/bin/env python3
"""news_monitor 的離線測試：不連網、不呼叫 Claude、不送 Telegram。

    python tests/test_news_monitor.py
"""

import json
import os
import sys
import tempfile
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import news_monitor as nm  # noqa: E402

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

# 非 dry-run 的測試會歸檔：預設導到暫存目錄，絕不碰真的 data/news_archive。
os.environ["NEWS_ARCHIVE_DIR"] = tempfile.mkdtemp(prefix="news_archive_test_")

# 舊測試不該連網：run() 會呼叫 collect_jin10，預設換成空清單；Jin10 相關測試自行 patch。
_REAL_COLLECT_JIN10 = nm.collect_jin10
nm.collect_jin10 = lambda since_hours, now: []

# 依實際觀察到的 flash-api.jin10.com/get_flash_list 形狀，內容為虛構。
JIN10_JSON = {"status": 200, "message": "OK", "data": [
    {"id": "20261014200000000001", "time": "2026-10-14 19:30:00", "type": 0, "important": 1, "channel": [1, 2],
     "data": {"content": "【美国9月CPI高于预期】金十数据10月14日讯，美国9月CPI同比超预期，美联储加息风险上升。",
              "title": "", "pic": "", "source": "", "source_link": "", "exclusive_to": []}, "extras": {"ad": False}},
    {"id": "20261014200000000002", "time": "2026-10-14 19:20:00", "type": 0, "important": 0,
     "data": {"content": "特斯拉(TSLA.O)盘前大涨5%。", "title": "", "exclusive_to": []}},
    {"id": "20261014200000000003", "time": "2026-10-14 19:10:00", "type": 2, "important": 1,
     "data": {"content": "文章摘要。", "title": "某某事件", "link": "https://xnews.jin10.com/details/1"}},
    {"id": "20261014200000000004", "time": "2026-10-14 19:00:00", "type": 0, "important": 0,
     "data": {"content": "VIP 內容", "lock": True}},
    {"id": "20261014200000000005", "time": "bad", "type": 0, "important": 0, "data": {"content": ""}},
]}
MACRO_CFG = {"google_queries": ["US CPI inflation report"], "keywords_zh": ["美联储", "CPI", "非农", "美股"],
             "keywords_en": ["CPI", "Fed"], "exclude_zh": ["现报"]}

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>Arista beats estimates, raises guidance - Reuters</title>
<link>https://news.google.com/rss/articles/AAA?oc=5</link>
<pubDate>Wed, 14 Oct 2026 10:00:00 GMT</pubDate>
<description>&lt;a href="x"&gt;Arista beats estimates&lt;/a&gt;&amp;nbsp;&lt;font&gt;Reuters&lt;/font&gt;</description>
<source url="https://reuters.com">Reuters</source></item>
<item><title>Unrelated: Cisco ships a switch - CNBC</title>
<link>https://example.com/cisco</link>
<pubDate>Wed, 14 Oct 2026 09:00:00 GMT</pubDate><description>Cisco only</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>8-K - Current report</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/x-index.htm"/>
<summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2026-10-14</summary>
<updated>2026-10-14T08:00:00-04:00</updated></entry></feed>"""

ANET = {"ticker": "ANET", "name": "Arista Networks", "aliases": ["Arista"], "status": "candidate",
        "weight_pct": None, "thesis": "AI networking"}


HELD = dict(ANET, status="held", weight_pct=12, entry_price=204.5, stop_pct=15,
            next_events=[{"date": "2026-10-20", "label": "過去/較晚事件"}, {"date": "2026-11-03", "label": "ANET Q3 財報"},
                         {"date": "2026-10-01", "label": "已過期"}])


def _item(title="Arista beats", link="https://x.com/a", **kw):
    d = {"title": title, "link": link, "source": "Reuters", "published": NOW, "snippet": "", "origin": "Google News"}
    d.update(kw)
    return d


def test_parse_rss_and_atom():
    items = nm.parse_feed(RSS, "Google News")
    assert len(items) == 2
    assert items[0]["title"] == "Arista beats estimates, raises guidance - Reuters"
    assert items[0]["source"] == "Reuters"
    assert items[0]["published"] == datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
    assert "<" not in items[0]["snippet"]
    atom = nm.parse_feed(ATOM, "SEC EDGAR")
    assert atom[0]["link"] == "https://www.sec.gov/Archives/x-index.htm"
    assert atom[0]["published"] == datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def test_alias_relevance_filter():
    items = nm.parse_feed(RSS, "Google News")
    assert nm.is_relevant(items[0], ANET)
    assert not nm.is_relevant(items[1], ANET)
    assert not nm.is_relevant(_item("Aristarchus crater photographed"), ANET)  # 整詞比對
    assert nm.is_relevant(_item("ARISTA wins deal"), ANET)  # 不分大小寫
    assert nm.is_relevant(_item("8-K", trusted=True), ANET)  # SEC 官方來源不套別名過濾


def test_dedup_and_noise():
    a, dup = _item(), _item(link="https://x.com/a/")  # 尾端斜線視為同網址
    state = {}
    fresh = nm.select_new([a], ANET, state, set())
    assert len(fresh) == 1
    state[fresh[0]["key"]] = {"ts": NOW.isoformat()}
    assert nm.select_new([dup], ANET, state, set()) == []  # 跨輪去重
    claimed = set()
    first = nm.select_new([_item("Arista news X", "https://a/1")], ANET, {}, claimed)
    second = nm.select_new([_item("Arista news X", "https://b/2")], ANET, {}, claimed)  # 同標題不同網址
    assert len(first) == 1 and second == []
    spam = _item("Arista Networks, Inc. $ANET Stock Acquired by Foo Wealth LLC")
    assert nm.select_new([spam], ANET, {}, set()) == []


def test_prune_state():
    old = (NOW - timedelta(days=31)).isoformat()
    new = (NOW - timedelta(days=29)).isoformat()
    out = nm.prune_state({"old": {"ts": old}, "new": {"ts": new}, "bad": {"x": 1}}, NOW)
    assert list(out) == ["new"]


def test_split_message_limit_and_escape():
    entry = dict(ANET, name="A<b>&Co")
    rows = [(_item(f"Arista <script>alert({i})</script> & more " + "x" * 300, f"https://x.com/{i}?a=1&b=2"), None)
            for i in range(40)]
    msg = nm.build_message(entry, rows, analyzed=False)
    parts = nm.split_message(msg)
    assert len(parts) > 1 and all(len(p) <= nm.TG_LIMIT for p in parts)
    joined = "\n\n".join(parts)
    assert "<script>" not in joined and "&lt;script&gt;" in joined
    assert "A&lt;b&gt;&amp;Co" in joined
    assert "a=1&amp;b=2" in joined
    assert nm.split_message("短") == ["短"]
    assert all(len(p) <= nm.TG_LIMIT for p in nm.split_message("y" * 10000))  # 病態超長單段


def _write_cfg(tmp, end_date="2026-11-13", entry=None):
    cfg = {"end_date": end_date, "timezone": "Asia/Taipei", "entries": [entry or ANET]}
    p = Path(tmp) / "wl.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def _args(cfg, **kw):
    d = dict(dry_run=False, no_llm=False, since_hours=36, ticker=None, config=str(cfg))
    d.update(kw)
    return Namespace(**d)


def test_end_date_early_exit():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _write_cfg(tmp, end_date="2026-10-01")
        with mock.patch.object(nm, "collect_for_entry", side_effect=AssertionError("不該抓取")):
            assert nm.run(_args(cfg), now=NOW) == 0
    assert nm.is_past_end({"end_date": "2026-11-13"}, datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc)) is False
    assert nm.is_past_end({"end_date": "2026-11-13"}, datetime(2026, 11, 14, 17, 0, tzinfo=timezone.utc)) is True


def test_no_api_key_fallback_and_second_run_empty():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _write_cfg(tmp)
        env = {"NEWS_STATE_PATH": str(Path(tmp) / "s.json"), "NEWS_ALLOW_UNSENT_STATE": "1"}
        printed = []
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(nm, "collect_for_entry", return_value=nm.parse_feed(RSS, "Google News")), \
                mock.patch.object(nm, "call_claude", side_effect=AssertionError("無 key 不該呼叫 LLM")), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("NEWS_TELEGRAM_BOT_TOKEN", None)
            os.environ.pop("NEWS_TELEGRAM_CHAT_ID", None)
            assert nm.run(_args(cfg), now=NOW) == 0
            out1 = "\n".join(printed)
            assert "無分析" in out1 and "新聞 1 則" in out1
            printed.clear()
            assert nm.run(_args(cfg), now=NOW) == 0
            assert "新聞 0 則" in "\n".join(printed)  # 第二輪全是已見


def test_dry_run_writes_no_state():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _write_cfg(tmp)
        sp = Path(tmp) / "s.json"
        with mock.patch.dict(os.environ, {"NEWS_STATE_PATH": str(sp)}), \
                mock.patch.object(nm, "collect_for_entry", return_value=nm.parse_feed(RSS, "Google News")), \
                mock.patch("builtins.print"):
            nm.run(_args(cfg, dry_run=True, no_llm=True), now=NOW)
        assert not sp.exists()


LLM_REPLY = json.dumps({"items": [{
    "id": 1, "material": True, "value": "high", "summary_zh": "Arista 財報優於預期並調高財測",
    "direction": "up", "est_move": "+3% to +6% over 1-5 trading days",
    "priced_in": {"value": False, "why": "財測調升超出市場預期"}, "action": "加倉",
    "reasoning_zh": "AI 資料中心需求強，財測上修；但 Meta capex 仍是變數。", "confidence": "high",
    "watch_next": "10/21 Meta 財報",
}]})


def test_llm_stubbed_render_and_clamp():
    items = nm.parse_feed(RSS, "Google News")[:1]
    # 內文不可取得 -> 信心被強制壓到 low（即使模型回 high）
    parsed = nm.parse_analysis(LLM_REPLY, 1, body_ok=[False])
    assert parsed[0]["confidence"] == "low" and parsed[0]["priced_in"] is False
    parsed = nm.parse_analysis("```json\n" + LLM_REPLY + "\n```", 1, body_ok=[True])
    assert parsed[0]["confidence"] == "high"
    bad = json.dumps({"items": [{"id": 1, "material": "yes", "value": "HUGE", "action": "all-in", "direction": "?"}]})
    c = nm.parse_analysis(bad, 1, [True])[0]
    assert (c["value"], c["action"], c["direction"], c["material"]) == ("low", "觀察", "neutral", False)
    assert nm.should_send(items[0], parsed[0])  # material + high
    quiet = dict(parsed[0], material=False)
    assert nm.should_send(_item("Arista opens office"), quiet) is False
    assert nm.should_send(_item("Arista earnings preview"), quiet) is True  # 關鍵字硬觸發
    msg = nm.build_message(ANET, [(items[0], parsed[0])], analyzed=True)
    assert "預估僅供參考,非投資建議" in msg and "加倉" in msg and "估計值" in msg
    print("--- 範例 Telegram 訊息（LLM stub）---\n" + msg + "\n---")


def test_llm_pipeline_with_stub_http_and_parse_failure():
    items = nm.parse_feed(RSS, "Google News")[:1]
    with mock.patch.object(nm, "fetch_body", return_value=None), \
            mock.patch.object(nm, "call_claude", return_value=LLM_REPLY) as cc:
        res = nm.analyze_entry(ANET, items, "sk-test", "m")
        assert res[0]["action"] == "加倉"
        assert "不可取得" in cc.call_args[0][2]  # prompt 有告知內文不可取得
        assert "AI networking" in cc.call_args[0][2]  # 帶入 thesis
    with mock.patch.object(nm, "fetch_body", return_value="body " * 100), \
            mock.patch.object(nm, "call_claude", return_value="抱歉我無法用 JSON 回答"):
        assert nm.analyze_entry(ANET, items, "sk-test", "m") is None  # 不 crash，回 None
    with mock.patch.object(nm, "fetch_body", return_value=None), \
            mock.patch.object(nm, "call_claude", side_effect=RuntimeError("boom")):
        assert nm.analyze_entry(ANET, items, "sk-test", "m") is None


def test_llm_parse_failure_falls_back_to_digest_in_run():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _write_cfg(tmp, entry=HELD)
        printed = []
        env = {"NEWS_STATE_PATH": str(Path(tmp) / "s.json"), "ANTHROPIC_API_KEY": "sk-test"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(nm, "collect_for_entry", return_value=nm.parse_feed(RSS, "Google News")), \
                mock.patch.object(nm, "fetch_body", return_value=None), \
                mock.patch.object(nm, "call_claude", return_value="not json"), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            assert nm.run(_args(cfg, dry_run=True), now=NOW) == 0
        out = "\n".join(printed)
        assert "無法解析" in out and "判讀：" in out


def test_unsent_run_does_not_write_state_and_fails_in_actions():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _write_cfg(tmp)
        sp = Path(tmp) / "s.json"
        with mock.patch.dict(os.environ, {"NEWS_STATE_PATH": str(sp)}, clear=False), \
                mock.patch.object(nm, "collect_for_entry", return_value=nm.parse_feed(RSS, "Google News")), \
                mock.patch("builtins.print"):
            for k in ("ANTHROPIC_API_KEY", "NEWS_TELEGRAM_BOT_TOKEN", "NEWS_TELEGRAM_CHAT_ID", "NEWS_ALLOW_UNSENT_STATE", "GITHUB_ACTIONS"):
                os.environ.pop(k, None)
            assert nm.run(_args(cfg), now=NOW) == 0
            assert not sp.exists()
            os.environ["GITHUB_ACTIONS"] = "true"
            assert nm.run(_args(cfg), now=NOW) == 1
            assert not sp.exists()


def test_digest_only_hard_triggers_and_capped():
    items = [{"title": f"Arista routine note {i}", "link": f"https://x.example/{i}", "origin": "Google News", "source": "Reuters",
              "snippet": "Arista", "published": NOW, "key": f"k{i}"} for i in range(30)]
    items += [{"title": f"Arista raises guidance {i}", "link": f"https://y.example/{i}", "origin": "Google News", "source": "Reuters",
               "snippet": "Arista", "published": NOW, "key": f"g{i}"} for i in range(12)]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _write_cfg(tmp, entry=HELD)  # 候選股改走摘要佇列，上限測試要用即時（持有）標的
        printed = []
        env = {"NEWS_STATE_PATH": str(Path(tmp) / "s.json")}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(nm, "collect_for_entry", return_value=items), \
                mock.patch.object(nm, "select_new", side_effect=lambda it, e, st, cl: it), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            nm.run(_args(cfg, dry_run=True), now=NOW)
        out = "\n".join(printed)
        assert out.count("raises guidance") == nm.MAX_DIGEST_ITEMS
        assert "routine note" not in out


def test_fetch_body_skips_google_links():
    with mock.patch.object(nm.requests, "get", side_effect=AssertionError("不該請求 Google 連結")):
        assert nm.fetch_body({"link": "https://news.google.com/rss/articles/AAA", "origin": "Google News"}) is None


def test_rule_view_direction_and_types():
    v = nm.rule_view({"title": "Arista beats estimates, raises guidance", "snippet": ""})
    assert v["label"] == "📈 偏多" and "財測" in v["types"] and "beats" in v["words"]
    v = nm.rule_view({"title": "Tesla misses on earnings, shares tumble", "snippet": ""})
    assert v["label"] == "📉 偏空" and "財報" in v["types"]
    v = nm.rule_view({"title": "Palantir to hold annual meeting", "snippet": ""})
    assert v["label"] == "➖ 看不出方向"
    msg = nm.format_item_digest(1, {"title": "Arista beats estimates, raises guidance", "snippet": "",
                                    "link": "https://x.example/a", "source": "Reuters"})
    assert "判讀：📈 偏多" in msg and "無分析" not in msg


def test_escape_does_not_double_escape_entities():
    assert nm._e("AMD&#39;s <b>push</b>") == "AMD&#39;s &lt;b&gt;push&lt;/b&gt;"


def test_parse_jin10_fixture():
    items = nm.parse_jin10(JIN10_JSON)
    assert [i["link"] for i in items] == [
        "https://flash.jin10.com/detail/20261014200000000001",
        "https://flash.jin10.com/detail/20261014200000000002",
        "https://xnews.jin10.com/details/1"]  # lock、空內容被略過
    a = items[0]
    assert a["origin"] == "Jin10" and a["source"] == "Jin10" and "高于预期" in a["title"]  # 簡體原樣保留
    assert a["published"] == datetime(2026, 10, 14, 11, 30, tzinfo=timezone.utc)  # 北京時間 -> UTC
    assert items[2]["title"].startswith("某某事件：")
    assert nm.parse_jin10({"status": 200, "data": None}) == [] and nm.parse_jin10("x") == []
    long = nm.parse_jin10({"data": [{"id": "1", "time": "2026-10-14 10:00:00", "type": 0, "data": {"content": "字" * 500}}]})
    assert len(long[0]["title"]) == nm.JIN10_TITLE_MAX + 1


def test_alias_zh_matching():
    tsla = {"ticker": "TSLA", "aliases": ["Tesla"], "aliases_zh": ["特斯拉"]}
    mu = {"ticker": "MU", "aliases": ["Micron"], "aliases_zh": ["美光科技"]}
    net = {"ticker": "NET", "aliases": ["Cloudflare"], "aliases_zh": []}
    j = lambda t: {"title": t, "snippet": t}  # noqa: E731
    assert nm.is_relevant_jin10(j("特斯拉盘前大涨"), tsla)
    assert nm.is_relevant_jin10(j("现货(TSLA.O)上涨"), tsla)
    assert nm.is_relevant_jin10(j("美股Tesla财报"), tsla)  # 中文緊鄰英文別名仍可比對
    assert nm.is_relevant_jin10(j("美光科技发布财报"), mu)
    assert not nm.is_relevant_jin10(j("MU 是缩写，美光未提及"), mu)  # 短代號不裸比對
    assert nm.is_relevant_jin10(j("美光(MU.O)涨3%"), mu)
    assert not nm.is_relevant_jin10(j("the internet NET income rose"), net)
    assert not nm.is_relevant_jin10(j("特斯拉"), mu)


def test_macro_keyword_filter_and_hard_trigger():
    items = nm.parse_jin10(JIN10_JSON)
    cands = [i["title"][:6] for i in items if nm.macro_candidate_jin10(i, MACRO_CFG)]
    assert len(cands) == 1  # 只有 CPI 那則
    fx = {"title": "美元/日元现报150，美联储", "snippet": "美元/日元现报150，美联储"}
    assert not nm.macro_candidate_jin10(fx, MACRO_CFG)  # exclude_zh
    stock_only = {"title": "美股收盘涨", "snippet": "美股收盘涨", "origin": "Jin10", "published": NOW, "link": "u1"}
    cpi = dict(items[0], key=None)
    got = nm.select_macro([stock_only, cpi], [], MACRO_CFG, {}, set(), NOW, 48)
    assert [g["link"] for g in got] == [cpi["link"]]  # 美股候選但沒有硬觸發 -> 不推
    g = {"title": "Oil prices jump as Brent tops $90 - Reuters", "link": "https://g.example/1", "source": "Reuters",
         "published": NOW, "snippet": "", "origin": "Google News"}
    assert nm.select_macro([], [g], MACRO_CFG, {}, set(), NOW, 48)
    g2 = dict(g, title="Dividend yield stocks to buy", link="https://g.example/2")
    assert not nm.select_macro([], [g2], MACRO_CFG, {}, set(), NOW, 48)


def test_macro_view_direction():
    v = lambda t: nm.macro_view({"title": t, "snippet": ""})["label"]  # noqa: E731
    assert v("美国9月CPI高于预期，花旗预计美联储加息") == nm.MACRO_BEAR_LABEL
    assert v("美国CPI不及预期，市场押注降息") == nm.MACRO_BULL_LABEL
    assert v("美联储暂停加息，按兵不动") == "➖ 看不出"
    assert v("Hot September CPI sends rate-hike bets soaring") == nm.MACRO_BEAR_LABEL
    assert v("核心CPI环比上涨0.3%，通胀超预期") == nm.MACRO_BEAR_LABEL
    assert v("WTI原油大涨5%") == nm.MACRO_BEAR_LABEL  # 油價上漲對風險資產偏空
    assert v("10-year Treasury yields fall sharply") == nm.MACRO_BULL_LABEL
    assert v("非农就业数据超预期") == "➖ 看不出"  # 方向不明，不硬判
    assert v("Fed Chair to speak Friday") == "➖ 看不出"
    # 個股用的 rule_view 也吃簡體詞
    assert nm.rule_view({"title": "特斯拉大涨，利好", "snippet": ""})["label"] == "📈 偏多"
    assert nm.rule_view({"title": "某股暴跌", "snippet": ""})["label"] == "📉 偏空"


def test_macro_message_format_and_escape():
    rows = [{"title": f"美联储<b>加息</b> & CPI {i} " + "字" * 190, "link": f"https://x.example/a?x=1&y={i}",
             "source": "Jin10", "origin": "Jin10", "snippet": "", "published": NOW} for i in range(nm.MACRO_MAX_ITEMS)]
    msg = nm.build_macro_message(rows)
    assert msg.startswith("📊 <b>總經快訊</b>")
    assert "<b>加息</b>" not in msg and "&lt;b&gt;加息&lt;/b&gt; &amp; CPI" in msg and "&amp;y=1" in msg
    parts = nm.split_message(msg)
    assert all(len(p) <= nm.TG_LIMIT for p in parts)


def _run_macro_cfg(tmp, entries=None):
    cfg = Path(tmp) / "cfg.json"
    cfg.write_text(json.dumps({"entries": entries if entries is not None else [], "macro": MACRO_CFG}), encoding="utf-8")
    return cfg


def test_jin10_failure_does_not_break_run():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _run_macro_cfg(tmp, [HELD])  # 候選股摘要預設關閉、不印訊息；改用持股才看得到個股訊息
        printed = []
        with mock.patch.dict(os.environ, {"NEWS_STATE_PATH": str(Path(tmp) / "s.json")}), \
                mock.patch.object(nm, "collect_jin10", _REAL_COLLECT_JIN10), \
                mock.patch.object(nm, "fetch_jin10", side_effect=nm.requests.ConnectionError("blocked")), \
                mock.patch.object(nm, "collect_for_entry", return_value=[_item("Arista beats earnings estimates")]), \
                mock.patch.object(nm, "fetch_macro_google", return_value=[]), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            assert nm.run(_args(cfg, dry_run=True, no_llm=True), now=NOW) == 0
        out = "\n".join(printed)
        assert "Jin10 失敗（略過）：ConnectionError" in out and "Arista beats" in out
        assert "總經快訊" not in out  # 沒命中 -> 靜默


def test_run_jin10_ticker_and_macro_dedup_across_runs():
    flashes = nm.parse_jin10(JIN10_JSON)
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "s.json"
        tsla = {"ticker": "TSLA", "name": "Tesla", "aliases": ["Tesla"], "aliases_zh": ["特斯拉"], "status": "candidate"}
        cfg = _run_macro_cfg(tmp, [tsla])
        env = {"NEWS_STATE_PATH": str(sp), "NEWS_ALLOW_UNSENT_STATE": "1"}

        def once(dry=False):
            printed = []
            with mock.patch.dict(os.environ, env), mock.patch.object(nm, "collect_jin10", lambda h, n: list(flashes)), \
                    mock.patch.object(nm, "DIGEST_ENABLED", True), \
                    mock.patch.object(nm, "collect_for_entry", return_value=[]), \
                    mock.patch.object(nm, "fetch_macro_google", return_value=[]), \
                    mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                os.environ.pop("ANTHROPIC_API_KEY", None)
                nm.run(_args(cfg, dry_run=dry, no_llm=True), now=NOW)
            return "\n".join(printed)

        out = once(dry=True)
        assert "特斯拉(TSLA.O)盘前大涨5%" in out and "📊 <b>總經快訊</b> — 1 則" in out
        assert "🗞 <b>每小時摘要" in out and "• 📈 偏多" in out and "對風險資產偏空" in out  # 候選股改進摘要，行內只有判讀標籤
        assert not sp.exists()  # dry-run 不寫狀態
        once()  # 正式（無 Telegram，允許寫狀態）
        saved = json.loads(sp.read_text(encoding="utf-8"))
        assert {v["ticker"] for k, v in saved.items() if k != "_digest"} == {"TSLA", "MACRO"}
        out2 = once()
        assert "特斯拉(TSLA.O)" not in out2 and "總經快訊" not in out2  # 第二輪全已見
        # 同一則不會同時當個股與總經：CPI 那則只進總經，特斯拉那則只進個股
        assert out.count("盘前大涨5%") == 1


# ---------------------------------------------------------------------------
# 分級推播 / 每小時摘要
# ---------------------------------------------------------------------------

def _quiet_env(tmp):
    return {"NEWS_STATE_PATH": str(Path(tmp) / "s.json"), "NEWS_ALLOW_UNSENT_STATE": "1"}


def _clear_env():
    for k in ("ANTHROPIC_API_KEY", "NEWS_TELEGRAM_BOT_TOKEN", "NEWS_TELEGRAM_CHAT_ID", "NEWS_FORCE_DIGEST", "GITHUB_ACTIONS"):
        os.environ.pop(k, None)


def _qitem(i, title=None, **kw):
    d = {"key": f"k{i}", "title": title or f"Arista raises guidance {i}", "link": f"https://x.example/{i}",
         "source": "Reuters", "published": NOW - timedelta(minutes=i), "snippet": "", "origin": "Google News"}
    d.update(kw)
    return d


def test_tier_selection():
    assert nm.is_immediate(dict(ANET, status="held"))
    assert nm.is_immediate(dict(ANET, priority=True))
    assert not nm.is_immediate(ANET)
    assert not nm.is_immediate(dict(ANET, priority=False))


def test_candidate_queued_not_sent_and_dedups():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _write_cfg(tmp)
        sp = Path(tmp) / "s.json"
        printed = []
        env = _quiet_env(tmp)
        sent = []
        feed = [_qitem(1), _qitem(2, "Arista opens new office")]
        for _ in range(2):
            with mock.patch.dict(os.environ, env), mock.patch.object(nm, "collect_for_entry", return_value=feed), \
                    mock.patch.object(nm, "send_telegram", side_effect=lambda *a: sent.append(a)), \
                    mock.patch.object(nm, "DIGEST_INTERVAL_MIN", 60), mock.patch.object(nm, "DIGEST_ENABLED", True), \
                    mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                _clear_env()
                nm.run(_args(cfg, no_llm=True), now=NOW)  # 無 Telegram 憑證：只印出
                printed.append("=====")
            saved = json.loads(sp.read_text(encoding="utf-8"))
            if _ == 0:
                # 第一輪：last_sent 缺 -> 摘要到期，印出並清空；兩則都已標為已見
                assert "🗞 <b>每小時摘要" in "\n".join(printed) and "🔔" not in "\n".join(printed)
                assert saved["_digest"]["pending"] == [] and saved["_digest"]["last_sent"] == NOW.isoformat()
                assert len([k for k in saved if k != "_digest"]) == 2
                printed.clear()
        assert "每小時摘要" not in "\n".join(printed)  # 第二輪：全是已見，沒東西可排
        assert sent == []
    # 佇列內以 key 去重
    st = {}
    assert nm.queue_digest(st, "ANET", "A", _qitem(1), "x", NOW) and not nm.queue_digest(st, "ANET", "A", _qitem(1), "x", NOW)
    assert len(st["_digest"]["pending"]) == 1
    for i in range(300):
        nm.queue_digest(st, "ANET", "A", _qitem(100 + i), "x", NOW)
    pend = st["_digest"]["pending"]
    assert len(pend) == nm.DIGEST_MAX_PENDING and any(p["key"] == "k100" for p in pend)  # 保留最新（i 小=較新）
    assert not any(p["key"] == "k399" for p in pend)


def test_reserved_key_survives_prune_load_save():
    old = (NOW - timedelta(days=90)).isoformat()
    dg = {"last_sent": old, "pending": [{"key": "a", "ticker": "X", "title": "t", "link": "l", "source": "s", "label": "l", "ts": old}]}
    out = nm.prune_state({"_digest": dg, "old": {"ts": old}, "new": {"ts": NOW.isoformat()}}, NOW)
    assert out["_digest"] == dg and set(out) == {"_digest", "new"}  # 90 天前的佇列不被 30 天規則清掉
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x" / "s.json"
        nm.save_state(p, out)
        assert nm.load_state(p) == out
        p.write_text(json.dumps({"_digest": "壞掉", "k": {"ts": NOW.isoformat()}}), encoding="utf-8")
        assert "_digest" not in nm.load_state(p)
        assert nm.get_digest({"_digest": {"pending": "x"}}) == {"pending": [], "last_sent": None}
    assert nm.prune_state({"_digest": "bad"}, NOW) == {}  # 型別錯的保留鍵不 crash


def test_hourly_gating_and_clear_and_failure():
    def state_at(minutes_ago):
        st = {}
        nm.queue_digest(st, "ANET", "Arista", _qitem(1), "📈 偏多", NOW)
        if minutes_ago is not None:
            st["_digest"]["last_sent"] = (NOW - timedelta(minutes=minutes_ago)).isoformat()
        return st

    os.environ.pop("NEWS_FORCE_DIGEST", None)
    sent = []
    with mock.patch.object(nm, "send_telegram", side_effect=lambda *a: sent.append(a[2])), mock.patch("builtins.print"):
        st = state_at(59)
        assert nm.flush_digest(st, NOW, "t", "c", True, False) == 0 and sent == [] and len(st["_digest"]["pending"]) == 1
        st = state_at(60)
        assert nm.flush_digest(st, NOW, "t", "c", True, False) == 1 and len(sent) == 1
        assert st["_digest"]["pending"] == [] and st["_digest"]["last_sent"] == NOW.isoformat()  # 送出後清空
        st = state_at(None)  # 沒有 last_sent 且佇列非空 -> 送
        assert nm.flush_digest(st, NOW, "t", "c", True, False) == 1
        st = {}
        assert nm.flush_digest(st, NOW, "t", "c", True, False) == 0  # 空佇列不送
    old_last = (NOW - timedelta(minutes=90)).isoformat()
    st = state_at(90)
    with mock.patch.object(nm, "send_telegram", side_effect=RuntimeError("down")), mock.patch("builtins.print"):
        assert nm.flush_digest(st, NOW, "t", "c", True, False) == 0
    assert len(st["_digest"]["pending"]) == 1 and st["_digest"]["last_sent"] == old_last  # 失敗：佇列與 last_sent 不變


def test_digest_formatting_size_and_escape():
    pend = []
    for i in range(40):
        pend.append({"key": f"k{i}", "ticker": "ANET" if i % 2 else "MACRO", "name": "A<b>&Co",
                     "title": f"<script>x</script> & {i} " + "字" * 200, "link": f"https://x.example/a?x=1&y={i}",
                     "source": "Rou<ters>", "label": "📈 偏多", "ts": (NOW - timedelta(minutes=i)).isoformat()})
    msg = nm.build_digest_message(pend)
    assert msg.startswith("🗞 <b>每小時摘要（候選股與一般總經）— 40 則</b>")
    assert msg.count("• ") == nm.DIGEST_MAX_LISTED and "另有 25 則未列出" in msg
    assert "📊 <b>總經</b>" in msg and "<b>ANET A&lt;b&gt;&amp;Co</b>" in msg
    assert "<script>" not in msg and "&lt;script&gt;" in msg and "x=1&amp;y=0" in msg
    assert "字" * 111 not in msg and "…" in msg  # 標題截到 110 字
    assert msg.index("&amp; 0 ") < msg.index("&amp; 2 ")  # 新到舊
    assert all(len(p) <= nm.TG_LIMIT for p in nm.split_message(msg))
    assert "另有" not in nm.build_digest_message(pend[:3])


def test_cpi_immediate_fomc_to_digest():
    assert nm.is_immediate_macro({"title": "美国9月核心CPI同比上涨3.1%"})
    assert nm.is_immediate_macro({"title": "US consumer price index rises 0.3%"})
    assert nm.is_immediate_macro({"title": "Inflation report due Friday"})
    assert not nm.is_immediate_macro({"title": "FOMC holds rates; Powell speaks"})
    assert not nm.is_immediate_macro({"title": "非农就业超预期，美联储降息预期降温"})
    assert not nm.is_immediate_macro({"title": "Oil prices jump as Brent tops $90"})
    assert not nm.is_immediate_macro({"title": "PCE inflation steady"})  # 只認 CPI 類
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _run_macro_cfg(tmp, [])
        printed = []
        g = lambda t, n: {"title": t, "link": f"https://g.example/{n}", "source": "Reuters", "published": NOW,  # noqa: E731
                          "snippet": "", "origin": "Google News"}
        google = [g("US CPI hotter than expected - Reuters", 1), g("FOMC signals rate cut, Fed Powell says - Reuters", 2)]
        with mock.patch.dict(os.environ, _quiet_env(tmp)), mock.patch.object(nm, "fetch_macro_google", return_value=google), \
                mock.patch.object(nm, "DIGEST_ENABLED", True), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            _clear_env()
            assert nm.run(_args(cfg, no_llm=True), now=NOW) == 0
        out = "\n".join(printed)
        immediate, digest = out.split("🗞 <b>每小時摘要")
        assert "📊 <b>總經快訊</b> — 1 則" in immediate and "CPI hotter" in immediate and "FOMC" not in immediate
        assert "📊 <b>總經</b>" in digest and "FOMC signals" in digest and "CPI hotter" not in digest


def test_decision_card_full_and_partial():
    today = NOW.date()
    full = nm.decision_card(HELD, today)
    assert full.startswith("持有 12% · 成本 $204.50 · 停損 $173.82（−15%） · 下個事件")
    assert "下個事件 10/20 過去/較晚事件（6 天後）" in full  # 取 >= 今天的最早事件；已過期的略過
    held2 = dict(HELD, next_events=[{"date": "2026-11-03", "label": "ANET Q3 財報"}])
    assert "下個事件 11/03 ANET Q3 財報（20 天後）" in nm.decision_card(held2, today)
    partial = nm.decision_card(dict(HELD, weight_pct=None, entry_price=None, next_events=[]), today)
    assert partial == "停損 −15% · 無已排定事件" and "成本" not in partial and "$" not in partial
    only_entry = nm.decision_card(dict(HELD, stop_pct=None, weight_pct=None), today)
    assert "停損" not in only_entry and "成本 $204.50" in only_entry
    assert nm.decision_card({"status": "held"}, today) == "無已排定事件"
    msg = nm.build_message(held2, [(_item("Arista beats earnings"), None)], False, today)
    assert "🧭 持有 12% · 成本 $204.50" in msg.split("\n\n")[0]
    assert "🧭" not in nm.build_message(ANET, [(_item("Arista beats earnings"), None)], False, today)
    assert "🧭" in nm.build_message(dict(ANET, priority=True), [(_item("Arista beats earnings"), None)], False, today)


def test_held_entry_immediate_and_dry_run_writes_nothing_with_queue():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _write_cfg(tmp, entry=HELD)
        sp = Path(tmp) / "s.json"
        printed = []
        with mock.patch.dict(os.environ, {"NEWS_STATE_PATH": str(sp)}), \
                mock.patch.object(nm, "collect_for_entry", return_value=[_qitem(1)]), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            _clear_env()
            nm.run(_args(cfg, dry_run=True, no_llm=True), now=NOW)
        out = "\n".join(printed)
        assert "🔔" in out and "🧭" in out and "每小時摘要" not in out and not sp.exists()
        # 候選股 dry-run：印出摘要但不寫檔
        cfg2 = _write_cfg(tmp, entry=ANET)
        printed.clear()
        with mock.patch.dict(os.environ, {"NEWS_STATE_PATH": str(sp)}), mock.patch.object(nm, "DIGEST_ENABLED", True), \
                mock.patch.object(nm, "collect_for_entry", return_value=[_qitem(1)]), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            _clear_env()
            nm.run(_args(cfg2, dry_run=True, no_llm=True), now=NOW)
        out = "\n".join(printed)
        assert "🗞" in out and "🔔" not in out and not sp.exists()


# ---------------------------------------------------------------------------
# 歸檔 / 摘要關閉 / news_brief
# ---------------------------------------------------------------------------

import news_brief as nb  # noqa: E402


def _rec(i, ts=None, **kw):
    d = {"ts": (ts or NOW + timedelta(minutes=i)).isoformat(), "ticker": "ANET", "name": "Arista", "title": f"t{i}",
         "link": f"https://x.example/{i}", "source": "Reuters", "origin": "Google News", "label": "➖ 看不出方向",
         "types": [], "hard": False, "immediate": False, "key": f"key{i}"}
    d.update(kw)
    return d


def test_archive_write_dedup_sort_atomic_cap():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "arch"
        assert nm.write_archive([_rec(3), _rec(1), _rec(2)], d) == 3
        f = d / "2026-10-14.json"
        rows = json.loads(f.read_text(encoding="utf-8"))
        assert [r["key"] for r in rows] == ["key1", "key2", "key3"]  # 依 ts 排序
        assert set(rows[0]) == {"ts", "ticker", "name", "title", "link", "source", "origin", "label", "types", "hard", "immediate", "key"}
        assert nm.write_archive([_rec(2, title="改過"), _rec(4)], d) == 1  # 同 key 去重，先到者保留
        rows = json.loads(f.read_text(encoding="utf-8"))
        assert len(rows) == 4 and [r for r in rows if r["key"] == "key2"][0]["title"] == "t2"
        assert not list(d.glob("*.tmp"))  # 原子寫入：沒有殘留暫存檔
        # 原子性：寫入中途失敗時，原檔不變
        before = f.read_text(encoding="utf-8")
        with mock.patch.object(nm.os, "replace", side_effect=OSError("disk")), mock.patch("builtins.print"):
            assert nm.write_archive([_rec(9)], d) == 0
        assert f.read_text(encoding="utf-8") == before
        # 上限：丟最舊
        with mock.patch.object(nm, "ARCHIVE_MAX_PER_DAY", 5):
            nm.write_archive([_rec(i) for i in range(10, 20)], d)
        rows = json.loads(f.read_text(encoding="utf-8"))
        assert len(rows) == 5 and rows[0]["key"] == "key15" and rows[-1]["key"] == "key19"


def test_archive_corrupt_or_missing_file_recovers():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "2026-10-14.json").write_text("{壞掉", encoding="utf-8")
        assert nm.write_archive([_rec(1)], d) == 1
        assert [r["key"] for r in json.loads((d / "2026-10-14.json").read_text(encoding="utf-8"))] == ["key1"]
        (d / "2026-10-14.json").write_text(json.dumps({"not": "list"}), encoding="utf-8")
        assert nm.write_archive([_rec(2)], d) == 1
        (d / "2026-10-14.json").write_text(json.dumps([1, "x", {"no": "key"}, _rec(5)]), encoding="utf-8")
        assert nm.write_archive([_rec(6)], d) == 1
        assert [r["key"] for r in json.loads((d / "2026-10-14.json").read_text(encoding="utf-8"))] == ["key5", "key6"]
        assert nm.write_archive([], d / "none") == 0 and not (d / "none").exists()


def test_archive_per_day_file_naming_utc():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        late = datetime(2026, 10, 13, 23, 30, tzinfo=timezone.utc)
        taipei = datetime(2026, 10, 15, 1, 0, tzinfo=timezone(timedelta(hours=8)))  # = 10/14 17:00 UTC
        nm.write_archive([_rec(1, ts=late), _rec(2, ts=taipei), _rec(3)], d)
        assert sorted(p.name for p in d.glob("*.json")) == ["2026-10-13.json", "2026-10-14.json"]
        rec = nm.archive_record(_item("Arista beats earnings", published=None, key="k"), "ANET", "A", True, False, NOW)
        assert rec["ts"] == NOW.isoformat()  # 無發佈時間 -> 抓取時間


def _run_collect(tmp, entry, items, dry=False, jin10=None, google=None, macro=False, extra_env=None):
    cfg = _run_macro_cfg(tmp, [entry]) if macro else _write_cfg(tmp, entry=entry)
    sent, printed = [], []
    env = {"NEWS_STATE_PATH": str(Path(tmp) / "s.json"), "NEWS_ALLOW_UNSENT_STATE": "1",
           "NEWS_ARCHIVE_DIR": str(Path(tmp) / "arch")}
    env.update(extra_env or {})
    with mock.patch.dict(os.environ, env), mock.patch.object(nm, "collect_for_entry", return_value=items), \
            mock.patch.object(nm, "collect_jin10", lambda h, n: list(jin10 or [])), \
            mock.patch.object(nm, "fetch_macro_google", return_value=list(google or [])), \
            mock.patch.object(nm, "send_telegram", side_effect=lambda *a: sent.append(a[2])), \
            mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
        _clear_env()
        os.environ.update(extra_env or {})  # _clear_env 會清掉 Telegram 憑證，要在清完之後再補回
        nm.run(_args(cfg, dry_run=dry, no_llm=True), now=NOW)
    return "\n".join(printed), sent


def _read_arch(tmp, day="2026-10-14"):
    p = Path(tmp) / "arch" / f"{day}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def test_dry_run_archives_nothing_but_reports_count():
    with tempfile.TemporaryDirectory() as tmp:
        out, _ = _run_collect(tmp, ANET, [_qitem(1), _qitem(2, "Arista opens new office")], dry=True)
        assert not (Path(tmp) / "arch").exists() and "歸檔 2 則（本次不寫入）" in out


def test_digest_off_candidate_archived_not_queued_or_sent():
    assert nm.DIGEST_ENABLED is False
    with tempfile.TemporaryDirectory() as tmp:
        # 殘留的舊佇列：第一輪就清掉，且不會被送出
        Path(tmp, "s.json").write_text(json.dumps({"_digest": {"last_sent": None, "pending": [
            {"key": "old", "ticker": "X", "title": "t", "link": "l", "source": "s", "label": "l", "ts": NOW.isoformat()}]}}),
            encoding="utf-8")
        out, sent = _run_collect(tmp, ANET, [_qitem(1), _qitem(2, "Arista opens new office")])
        assert "🗞" not in out and "🔔" not in out and sent == []
        saved = json.loads(Path(tmp, "s.json").read_text(encoding="utf-8"))
        assert saved["_digest"]["pending"] == [] and len([k for k in saved if k != "_digest"]) == 2
        rows = _read_arch(tmp)
        assert {r["title"] for r in rows} == {"Arista raises guidance 1", "Arista opens new office"}
        hard = {r["title"]: r["hard"] for r in rows}
        assert hard["Arista raises guidance 1"] is True and hard["Arista opens new office"] is False
        assert all(r["immediate"] is False and r["ticker"] == "ANET" for r in rows)
        assert "歸檔新增 2 則" in out and "摘要已關閉" in out
        # 第二輪：已見，不再重複歸檔
        out2, _ = _run_collect(tmp, ANET, [_qitem(1), _qitem(2, "Arista opens new office")])
        assert "歸檔新增 0 則" in out2 and len(_read_arch(tmp)) == 2
        # NEWS_DIGEST=1 可重新開啟
        with tempfile.TemporaryDirectory() as tmp2:
            out3, _ = _run_collect(tmp2, ANET, [_qitem(1)], extra_env={"NEWS_DIGEST": "1"})
            assert "🗞 <b>每小時摘要" in out3


def test_held_entry_still_immediate_and_archived_with_flag():
    with tempfile.TemporaryDirectory() as tmp:
        out, sent = _run_collect(tmp, HELD, [_qitem(1), _qitem(2, "Arista opens new office")],
                                 extra_env={"NEWS_TELEGRAM_BOT_TOKEN": "t", "NEWS_TELEGRAM_CHAT_ID": "c"})
        assert len(sent) == 1 and "🔔" in sent[0] and "🧭" in sent[0] and "🗞" not in sent[0]
        rows = {r["title"]: r for r in _read_arch(tmp)}
        assert len(rows) == 2  # 持股的所有相關新聞都歸檔（含沒推的）
        assert rows["Arista raises guidance 1"]["immediate"] is True and rows["Arista raises guidance 1"]["hard"] is True
        assert rows["Arista opens new office"]["immediate"] is False


def test_cpi_still_immediate_other_macro_archived_only():
    g = lambda t, n: {"title": t, "link": f"https://g.example/{n}", "source": "Reuters", "published": NOW,  # noqa: E731
                      "snippet": "", "origin": "Google News"}
    google = [g("US CPI hotter than expected - Reuters", 1), g("FOMC signals rate cut, Fed Powell says - Reuters", 2),
              g("Local bakery wins award - Reuters", 3)]
    with tempfile.TemporaryDirectory() as tmp:
        out, sent = _run_collect(tmp, ANET, [], google=google, macro=True,
                                 extra_env={"NEWS_TELEGRAM_BOT_TOKEN": "t", "NEWS_TELEGRAM_CHAT_ID": "c"})
        assert len(sent) == 1 and "總經快訊" in sent[0] and "CPI hotter" in sent[0] and "FOMC" not in sent[0]
        rows = {r["title"]: r for r in _read_arch(tmp)}
        assert rows["US CPI hotter than expected - Reuters"]["immediate"] is True
        fomc = rows["FOMC signals rate cut, Fed Powell says - Reuters"]
        assert fomc["immediate"] is False and fomc["hard"] is True and fomc["ticker"] == "MACRO"
        bak = rows["Local bakery wins award - Reuters"]
        assert bak["hard"] is False and bak["immediate"] is False


def _fixture_repo(tmp):
    root = Path(tmp)
    (root / "data" / "news_archive").mkdir(parents=True)
    (root / "news_watchlist.json").write_text(json.dumps({"entries": [
        {"ticker": "ANET", "name": "Arista", "status": "held", "weight_pct": 12, "entry_price": 200, "stop_pct": 10,
         "next_events": [{"date": "2026-11-03", "label": "Q3 財報"}]},
        {"ticker": "CAND", "name": "Cand Co", "status": "candidate"},
        {"ticker": "PRIO", "name": "Prio", "status": "candidate", "priority": True}]}), encoding="utf-8")
    recs = [
        _rec(1, title="ANET routine note", hard=False),
        _rec(2, title="ANET raises guidance", hard=True, label="📈 偏多", immediate=True),
        _rec(3, ticker="CAND", name="Cand Co", title="Cand opens office", key="c1"),
        _rec(4, ticker="MACRO", name="", title="FOMC signals cut", key="m1", hard=True),
        _rec(5, ticker="PRIO", name="Prio", title="Prio wins contract " + "x" * 200, key="p1", hard=True),
        _rec(6, ticker="CAND", name="Cand Co", title="old news", key="c0", ts=NOW - timedelta(hours=30)),
    ]
    (root / "data" / "news_archive" / "2026-10-14.json").write_text(json.dumps(recs[:5]), encoding="utf-8")
    (root / "data" / "news_archive" / "2026-10-13.json").write_text(json.dumps(recs[5:]), encoding="utf-8")
    return root


def test_news_brief_grouping_and_ordering():
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ):
        os.environ.pop("NEWS_ARCHIVE_DIR", None)  # 本檔頂端把它導向暫存目錄；brief 要讀 fixture 的 data/news_archive
        root = _fixture_repo(tmp)
        out = nb.build_brief(root, since_hours=24, now=NOW + timedelta(hours=1))
        lines = out.splitlines()
        assert "共 5 則" in lines[0] and "old news" not in out  # 30 小時前那則（在前一天檔）被 since 濾掉
        idx = {t: out.index(f"== {t}") for t in ("ANET", "PRIO", "CAND", "MACRO")}
        assert idx["ANET"] < idx["PRIO"] < idx["CAND"] < idx["MACRO"]  # 持股、priority 先，其次候選，最後總經
        assert "持有 · 權重 12%" in out and "成本 200" in out and "停損 180.00" in out and "2026-11-03 Q3 財報" in out
        anet = out[idx["ANET"]:idx["PRIO"]].splitlines()
        items = [l for l in anet if l.startswith("[")]
        assert "raises guidance" in items[0] and "routine note" in items[1]  # 硬觸發先
        assert items[0].startswith("[12:02 UTC] 📈 偏多 | ANET raises guidance | https://x.example/2")
        prio_line = [l for l in out.splitlines() if "Prio wins" in l][0]
        assert len(prio_line.split(" | ")[1]) <= 130
        assert anet[0].startswith("== ANET Arista — 2 則（硬觸發 1，即時推播 1）")
        only = nb.build_brief(root, since_hours=24, now=NOW + timedelta(hours=1), ticker="CAND")
        assert "== CAND" in only and "== ANET" not in only
        k1 = nb.build_brief(root, since_hours=24, now=NOW + timedelta(hours=1), max_per_ticker=1)
        assert "ANET raises guidance" in k1 and "ANET routine note" not in k1 and "另 1 則" in k1
        by_date = nb.build_brief(root, date="2026-10-14")
        assert "共 5 則" in by_date and "old news" not in by_date  # --date 只取該 UTC 日期
        assert "沒有資料" in nb.build_brief(root, date="2026-01-01")


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
