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


def _write_cfg(tmp, end_date="2026-11-13"):
    cfg = {"end_date": end_date, "timezone": "Asia/Taipei", "entries": [ANET]}
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
        cfg = _write_cfg(tmp)
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
        cfg = _write_cfg(tmp)
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


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
